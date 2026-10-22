#!/usr/bin/env python3
"""
train_offline.py — Offline Neural Network Training & Model Bootstrapping for MASA

1. Loads data from a CSV file (e.g. sweep_training_data.csv) or directly from MongoDB.
2. Derives 4 features:
     - total_msg:          Normalised to [0, 300] using max_total_msg
     - pct_delivered_norm: Delivery % normalised to [0, 1]
     - latency_ratio:      P90 latency normalised to [0, 1] (capped at 2.0s)
     - undelivered_norm:   Undelivered msg/s normalised to [0, 1] using max_undelivered
3. Computes label-adjusted training targets:
     - label=1 (SLA breached)   -> desired_replicas = replicas + 1
     - label=2 (SLA satisfied)  -> desired_replicas = replicas
     - label=3 (Overprovisioned)-> desired_replicas = max(1, replicas - 1)
4. Augments with synthetic edge-case samples (0 load -> 1 replica, extreme spikes -> 10 replicas).
5. Trains a Neural Network model with early stopping.
6. Exports `best_replica_predictor_model.h5` and `scaler.json` for MASA Planning & Reflection agents.
"""

import os
import sys
import json
import logging
import numpy as np
import pandas as pd
from sklearn.model_selection import train_test_split
from tensorflow.keras.models import Sequential
from tensorflow.keras.layers import Dense, Dropout, BatchNormalization, Input
from tensorflow.keras.optimizers import Adam
from tensorflow.keras.callbacks import EarlyStopping, ModelCheckpoint

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger("masa-offline-training")

LATENCY_CAP = 2.0  # seconds
FEATURES = ['total_msg', 'pct_delivered_norm', 'latency_ratio', 'undelivered_norm']


def load_dataset(csv_path: str = None, mongo_uri: str = None) -> pd.DataFrame:
    """Load dataset from CSV file or MongoDB."""
    if csv_path and os.path.exists(csv_path):
        logger.info(f"Loading data from CSV: {csv_path}")
        df = pd.read_csv(csv_path)
        return df

    if mongo_uri or os.getenv("MONGO_URI"):
        from pymongo import MongoClient
        uri = mongo_uri or os.getenv("MONGO_URI")
        logger.info("Connecting to MongoDB to load records...")
        client = MongoClient(uri)
        db = client['mongo_scaling']
        collection = db['microservices']
        data = list(collection.find())
        if not data:
            raise ValueError("No records found in MongoDB collection 'microservices'.")
        df = pd.DataFrame(data)
        logger.info(f"Loaded {len(df)} records from MongoDB.")
        return df

    raise ValueError("Must provide a valid --csv path or MongoDB connection details.")


def generate_synthetic_samples(num_samples: int = 500) -> pd.DataFrame:
    """
    Generates synthetic edge-case data points across the full 1-10 replica spectrum.
    Assumes approx ~450 msg/s capacity per Mosquitto pod under SLA.
    """
    logger.info(f"Generating {num_samples} synthetic edge-case samples to anchor model bounds...")
    rows = []
    
    # Pod capacity approx 450 msg/s
    for r in range(1, 11):
        capacity = r * 450.0
        # Normal operation (label=2)
        for _ in range(num_samples // 30):
            throughput = np.random.uniform(max(10.0, capacity - 350.0), capacity)
            p90 = np.random.uniform(0.05, 0.45)
            pct = np.random.uniform(99.0, 100.0)
            undelivered = 0.0
            rows.append({
                'replicas': r,
                'm_delivered': throughput,
                'm_undelivered': undelivered,
                'm_percentage__delivered': pct,
                'm_quantile_90': p90,
                'label': 2,
            })
            
        # Overloaded operation (label=1) -> requires r + 1
        for _ in range(num_samples // 30):
            throughput = capacity + np.random.uniform(100.0, 500.0)
            p90 = np.random.uniform(0.6, 2.0)
            pct = np.random.uniform(80.0, 95.0)
            undelivered = throughput * (1.0 - pct / 100.0)
            rows.append({
                'replicas': r,
                'm_delivered': throughput - undelivered,
                'm_undelivered': undelivered,
                'm_percentage__delivered': pct,
                'm_quantile_90': p90,
                'label': 1,
            })
            
        # Idle / over-provisioned operation (label=3) -> requires max(1, r - 1)
        if r > 1:
            for _ in range(num_samples // 30):
                throughput = max(10.0, (r - 2) * 450.0 + np.random.uniform(0, 200.0))
                p90 = np.random.uniform(0.01, 0.15)
                pct = 100.0
                undelivered = 0.0
                rows.append({
                    'replicas': r,
                    'm_delivered': throughput,
                    'm_undelivered': undelivered,
                    'm_percentage__delivered': pct,
                    'm_quantile_90': p90,
                    'label': 3,
                })

    # Add idle zero-load anchors
    for _ in range(20):
        rows.append({
            'replicas': 1,
            'm_delivered': np.random.uniform(0, 10.0),
            'm_undelivered': 0.0,
            'm_percentage__delivered': 100.0,
            'm_quantile_90': 0.02,
            'label': 2,
        })

    return pd.DataFrame(rows)


def prepare_features(df: pd.DataFrame, max_total_msg_override: float = None, max_undelivered_override: float = None):
    """
    Computes 4 normalized features and target label.
    """
    data = df.copy()

    # Required columns check
    for col in ['m_delivered', 'm_undelivered', 'replicas', 'label']:
        if col not in data.columns:
            raise KeyError(f"Missing required column: {col}")

    # Convert numerical columns and fill NaNs
    for col in ['m_delivered', 'm_undelivered', 'replicas', 'label']:
        data[col] = pd.to_numeric(data[col], errors='coerce').fillna(0)

    data['total_msg'] = data['m_delivered'] + data['m_undelivered']

    # 1. Delivery %
    if 'm_percentage__delivered' in data.columns:
        data['m_percentage__delivered'] = pd.to_numeric(data['m_percentage__delivered'], errors='coerce')
        data['pct_delivered_norm'] = data['m_percentage__delivered'].fillna(100.0).clip(0, 100) / 100.0
    else:
        data['pct_delivered_norm'] = 1.0

    # 2. Latency Ratio
    if 'm_quantile_90' in data.columns:
        data['m_quantile_90'] = pd.to_numeric(data['m_quantile_90'], errors='coerce')
        data['latency_ratio'] = data['m_quantile_90'].fillna(0.0).clip(0, LATENCY_CAP) / LATENCY_CAP
    else:
        data['latency_ratio'] = 0.0

    # 3. Undelivered norm
    max_undelivered = max_undelivered_override or float(data['m_undelivered'].fillna(0).max())
    if max_undelivered <= 0 or np.isnan(max_undelivered):
        max_undelivered = 1.0
    data['undelivered_norm'] = data['m_undelivered'].fillna(0).clip(0, max_undelivered) / max_undelivered

    # 4. Target computation
    data['desired_replicas'] = data['replicas'].astype(float)
    data.loc[data['label'] == 1, 'desired_replicas'] = (data['replicas'] + 1).clip(upper=10)
    data.loc[data['label'] == 3, 'desired_replicas'] = (data['replicas'] - 1).clip(lower=1)

    # 5. Total msg normalization [0, 300]
    max_total_msg = max_total_msg_override or float(data['total_msg'].max())
    if max_total_msg <= 0 or np.isnan(max_total_msg):
        max_total_msg = 5000.0  # reasonable fallback for 10 pods
    data['total_msg'] = data['total_msg'].fillna(0)
    data['total_msg'] = np.clip((data['total_msg'] / max_total_msg) * 300, 0, 300).round().astype(int)

    X = data[FEATURES].copy().fillna(0)
    y = data['desired_replicas'].copy().fillna(1.0)

    scaler_dict = {
        "max_total_msg": float(max_total_msg),
        "latency_cap": float(LATENCY_CAP),
        "max_undelivered": float(max_undelivered),
        "features": FEATURES,
    }

    return X, y, scaler_dict


def build_neural_network(input_dim: int = 4) -> Sequential:
    """Build the MASA replica prediction neural network architecture."""
    model = Sequential([
        Input(shape=(input_dim,)),
        Dense(64, activation='relu'),
        BatchNormalization(),
        Dropout(0.2),
        Dense(32, activation='relu'),
        BatchNormalization(),
        Dropout(0.2),
        Dense(16, activation='relu'),
        Dense(1, activation='linear')
    ])
    model.compile(
        optimizer=Adam(learning_rate=0.001),
        loss='mean_squared_error',
        metrics=['mae']
    )
    return model


def train_and_export(
    df: pd.DataFrame,
    output_dir: str = "training",
    epochs: int = 150,
    batch_size: int = 32,
    augment: bool = True
):
    """Full training pipeline with evaluation and artifact export."""
    os.makedirs(output_dir, exist_ok=True)

    if augment:
        synthetic_df = generate_synthetic_samples(num_samples=600)
        combined_df = pd.concat([df, synthetic_df], ignore_index=True)
    else:
        combined_df = df

    logger.info(f"Total training dataset size: {len(combined_df)} records.")

    X, y, scaler_dict = prepare_features(combined_df)

    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=0.2, random_state=42, shuffle=True
    )

    logger.info(f"Training set: {len(X_train)} samples, Test set: {len(X_test)} samples.")
    logger.info(f"Feature normalizers: max_total_msg={scaler_dict['max_total_msg']:.1f}, max_undelivered={scaler_dict['max_undelivered']:.1f}")

    model = build_neural_network(input_dim=len(FEATURES))

    model_path = os.path.join(output_dir, "best_replica_predictor_model.h5")
    scaler_path = os.path.join(output_dir, "scaler.json")

    callbacks = [
        EarlyStopping(monitor='val_loss', patience=15, restore_best_weights=True, verbose=1)
    ]

    history = model.fit(
        X_train, y_train,
        validation_data=(X_test, y_test),
        epochs=epochs,
        batch_size=batch_size,
        callbacks=callbacks,
        verbose=1
    )

    test_loss, test_mae = model.evaluate(X_test, y_test, verbose=0)
    logger.info(f"Evaluation results -> Test MSE: {test_loss:.4f}, Test MAE: {test_mae:.4f} replicas")

    # Save model in H5 format (compatible with Keras 2/3 and cluster agent runtime)
    try:
        model.save(model_path)
    except Exception:
        # Fallback for Keras 3 if save() defaults require .keras extension
        if not model_path.endswith('.keras'):
            alt_path = os.path.splitext(model_path)[0] + '.keras'
            model.save(alt_path)
            # Also export H5 via weights/legacy format if possible
            try:
                import h5py
                model.save(model_path, save_format='h5')
            except Exception:
                pass

    # Save scaler.json
    with open(scaler_path, "w") as f:
        json.dump(scaler_dict, f, indent=2)
    logger.info(f"Saved scaler metadata to: {scaler_path}")
    logger.info(f"Saved trained neural network to: {model_path}")

    # Spot check predictions
    logger.info("=== Spot Check Model Predictions ===")
    test_cases = [
        # total_msg_raw, pct_delivered, p90, undelivered, expected
        (100.0, 100.0, 0.05, 0.0, "Low traffic (~1 pod)"),
        (900.0, 99.5, 0.15, 0.0, "Medium traffic (~2 pods)"),
        (2200.0, 99.0, 0.25, 0.0, "High traffic (~5 pods)"),
        (4500.0, 98.5, 0.40, 10.0, "Peak traffic (~10 pods)"),
    ]
    for raw_tot, pct, p90, undeliv, desc in test_cases:
        t_msg = int((raw_tot / scaler_dict['max_total_msg']) * 300)
        pct_n = pct / 100.0
        lat_n = min(p90, LATENCY_CAP) / LATENCY_CAP
        und_n = min(undeliv, scaler_dict['max_undelivered']) / scaler_dict['max_undelivered']
        vec = np.array([[t_msg, pct_n, lat_n, und_n]])
        pred = model.predict(vec, verbose=0)[0][0]
        logger.info(f"  {desc} -> raw_msg={raw_tot:.0f} => Predicted Replicas: {pred:.2f} (rounded: {round(pred)})")

    return model, scaler_dict


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Train MASA model offline")
    parser.add_argument("--csv", type=str, default=None, help="Path to input CSV file")
    parser.add_argument("--output-dir", type=str, default="ginga/masa-i/training", help="Output directory for model and scaler")
    parser.add_argument("--no-augment", action="store_true", help="Disable synthetic data augmentation")
    parser.add_argument("--epochs", type=int, default=150, help="Training epochs")
    args = parser.parse_args()

    # If no CSV is passed, use a sample or synthetic dataset
    if args.csv and os.path.exists(args.csv):
        df = load_dataset(csv_path=args.csv)
    else:
        logger.info("No CSV file provided or file not found. Generating initial synthetic/baseline dataset...")
        df = generate_synthetic_samples(num_samples=1000)

    train_and_export(
        df=df,
        output_dir=args.output_dir,
        epochs=args.epochs,
        augment=not args.no_augment
    )
