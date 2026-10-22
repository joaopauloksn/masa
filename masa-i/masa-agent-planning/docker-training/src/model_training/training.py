import os
import gc
import logging
import pandas as pd
import numpy as np
from pymongo import MongoClient
import tensorflow as tf
from tensorflow.keras import mixed_precision
from sklearn.model_selection import train_test_split
from sklearn.metrics import mean_squared_error
from tensorflow.keras.models import Sequential
from tensorflow.keras.layers import Dense, Input, Dropout
from tensorflow.keras.optimizers import Adam
from tensorflow.keras.callbacks import EarlyStopping, ReduceLROnPlateau
from tensorflow.keras import backend as K

# TensorFlow threading and mixed precision settings
tf.config.threading.set_intra_op_parallelism_threads(2)
mixed_precision.set_global_policy('mixed_float16')

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

BEST_MSE_FILE   = os.getenv('BEST_MSE_FILE',   '/mnt/model/best_mse.txt')
SCALER_FILE     = os.getenv('SCALER_FILE',      '/mnt/model/scaler.json')


# ---------------------------------------------------------------------------
# Secret helpers – prefer mounted-secret files over env vars for passwords.
# ---------------------------------------------------------------------------

def _read_secret_file(path: str, fallback_env: str) -> str:
    """Return the contents of a mounted secret file, falling back to an env var."""
    try:
        with open(path) as f:
            value = f.read().strip()
            if value:
                return value
    except (OSError, IOError):
        pass
    return os.environ.get(fallback_env, "")


def get_data_from_mongo():
    """Connects to MongoDB and retrieves records."""
    secret_dir = os.getenv("MONGO_SECRET_DIR", "/etc/secrets/mongo")
    mongo_user     = _read_secret_file(f"{secret_dir}/username", "MONGO_USERNAME")
    mongo_password = _read_secret_file(f"{secret_dir}/password", "MONGO_PASSWORD")

    if not mongo_user or not mongo_password:
        logger.error(
            "MongoDB credentials not found. Mount a secret at MONGO_SECRET_DIR "
            "(/etc/secrets/mongo) or set MONGO_USERNAME / MONGO_PASSWORD."
        )
        return None

    mongo_host = os.getenv(
        'MONGO_HOST',
        "mas-mongo-ce-0.mas-mongo-ce-svc.mongoce.svc.cluster.local:27017,"
        "mas-mongo-ce-1.mas-mongo-ce-svc.mongoce.svc.cluster.local:27017,"
        "mas-mongo-ce-2.mas-mongo-ce-svc.mongoce.svc.cluster.local:27017",
    )
    mongo_tls = os.getenv('MONGO_TLS', 'true').lower() == 'true'
    mongo_tls_insecure = os.getenv('MONGO_TLS_INSECURE', 'true').lower() == 'true'
    mongo_uri = f"mongodb://{mongo_user}:{mongo_password}@{mongo_host}"

    try:
        client = MongoClient(
            mongo_uri,
            authMechanism="SCRAM-SHA-256",
            tlsInsecure=mongo_tls_insecure,   # was tlsinsecure (wrong casing) in training.py
            authSource="admin",
            connectTimeoutMS=60000,
            socketTimeoutMS=60000,
            maxPoolSize=100,
            retryWrites=True,
            tls=mongo_tls,
        )
        mongodb = client["mongo_scaling"]
        collection = mongodb['microservices']
        client.server_info()
        logger.info("Connected to MongoDB successfully.")
        data = pd.DataFrame(list(collection.find().sort("timestamp", -1)))

        if len(data) < 50:
            logger.warning(f"Insufficient data: {len(data)} records found.")
            return None

        data = data.drop(['_id', 'timestamp', 'metadata'], axis=1, errors='ignore')
        logger.info(f"Retrieved {len(data)} latest records from the database.")
        return data

    except Exception as e:
        logger.error(f"Failed to connect to MongoDB: {e}")
        return None

def prepare_data(data):
    """Prepares the data for training.

    Features (4 total):
      - total_msg            : normalised to [0, 300] via max_total_msg
      - pct_delivered_norm   : m_percentage__delivered / 100      → [0, 1]
      - latency_ratio        : m_quantile_90 / LATENCY_CAP        → [0, 1] (capped at 2 s)
      - undelivered_norm     : m_undelivered / max_undelivered     → [0, 1]
                               Rises under stress even when total_msg drops —
                               direct demand-pressure signal independent of broker saturation.

    Training target:
      desired_replicas — label-adjusted version of the observed replica count:
        label=1 (SLA breach)  → replicas + 1  (system needed more capacity)
        label=2 (SLA met)     → replicas       (count was correct)
        label=3 (idle/excess) → max(1, replicas - 1)  (could have used fewer)

    All labels are used (no label==2 filter).

    Returns (X_train, X_test, y_train, y_test, scaler_dict) where scaler_dict
    holds all normalisation constants so predict.py can reproduce them.
    """
    logger.info("Preparing data for training...")

    data = data.copy()
    logger.info(f"Total records available for training: {len(data)}")

    if len(data) < 50:
        logger.warning(f"Insufficient data (< 50 records) at {pd.Timestamp.now()}.")
        return None, None, None, None, None

    required = ['m_delivered', 'm_undelivered', 'replicas', 'label']
    if any(c not in data.columns for c in required):
        logger.error("Required columns %s are missing.", required)
        return None, None, None, None, None

    # ── Feature 1: total message throughput ──────────────────────────────────
    data['total_msg'] = data['m_delivered'] + data['m_undelivered']

    # ── Feature 2: delivery percentage (normalised to [0, 1]) ────────────────
    PCTCOL = 'm_percentage__delivered'
    if PCTCOL in data.columns:
        data['pct_delivered_norm'] = (
            data[PCTCOL].fillna(0).clip(0, 100) / 100.0
        )
    else:
        logger.warning("Column '%s' missing – using 1.0 as default.", PCTCOL)
        data['pct_delivered_norm'] = 1.0

    # ── Feature 3: latency ratio (normalised by a 2 s cap) ───────────────────
    LATENCY_CAP = 2.0   # seconds — values above this are treated as equally bad
    LATCOL = 'm_quantile_90'
    if LATCOL in data.columns:
        data['latency_ratio'] = (
            data[LATCOL].fillna(LATENCY_CAP).clip(0, LATENCY_CAP) / LATENCY_CAP
        )
    else:
        logger.warning("Column '%s' missing – using 0.0 (no latency) as default.", LATCOL)
        data['latency_ratio'] = 0.0

    # ── Feature 4: undelivered message rate (demand-pressure signal) ─────────
    # Unlike total_msg, m_undelivered rises when the broker is saturated —
    # it directly signals that demand exceeds current capacity.
    # max_undelivered is computed from the full dataset and saved in scaler.json
    # so predict.py can reproduce the same normalisation at inference time.
    max_undelivered = float(data['m_undelivered'].fillna(0).max())
    if max_undelivered > 0:
        data['undelivered_norm'] = (
            data['m_undelivered'].fillna(0).clip(0, max_undelivered) / max_undelivered
        )
    else:
        logger.warning("max_undelivered is 0 — all rows have zero undelivered. Defaulting to 0.0.")
        data['undelivered_norm'] = 0.0
        max_undelivered = 1.0   # avoid div-by-zero; predict.py gets the same fallback

    # ── Training target: label-adjusted desired replica count ────────────────
    # The observed 'replicas' tells us what was running; the label tells us
    # whether that was the right answer. We correct it by ±1 accordingly.
    data['desired_replicas'] = data['replicas'].astype(float)
    data.loc[data['label'] == 1, 'desired_replicas'] = data['replicas'] + 1
    data.loc[data['label'] == 3, 'desired_replicas'] = (data['replicas'] - 1).clip(lower=1)
    label_counts = data['label'].value_counts().to_dict()
    logger.info(
        "Label distribution: %s  (desired_replicas range: %.0f – %.0f)",
        label_counts,
        data['desired_replicas'].min(),
        data['desired_replicas'].max(),
    )

    FEATURES = ['total_msg', 'pct_delivered_norm', 'latency_ratio', 'undelivered_norm']
    X = data[FEATURES].copy()
    y = data['desired_replicas']

    if y.isnull().any():
        logger.warning(f"Dropping {y.isnull().sum()} NaN values in target.")
        non_nan_indices = ~y.isnull()
        X = X[non_nan_indices]
        y = y[non_nan_indices]

    X = X.fillna(0)

    # Normalise total_msg to [0, 300]; the other features are already in [0, 1]
    max_total_msg = float(X['total_msg'].max())
    if max_total_msg == 0:
        logger.error("max_total_msg is 0 — cannot normalise. Aborting.")
        return None, None, None, None, None
    X['total_msg'] = (X['total_msg'] / max_total_msg) * 300
    X['total_msg'] = X['total_msg'].astype(int)

    X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42)

    scaler_dict = {
        "max_total_msg":   max_total_msg,
        "latency_cap":     LATENCY_CAP,
        "max_undelivered": max_undelivered,
        "features":        FEATURES,
    }
    logger.info(
        "Data preparation complete. max_total_msg=%.2f latency_cap=%.2f "
        "max_undelivered=%.4f features=%s",
        max_total_msg, LATENCY_CAP, max_undelivered, FEATURES,
    )
    return X_train, X_test, y_train, y_test, scaler_dict

def load_best_mse():
    """Loads the best MSE from the file."""
    try:
        with open(BEST_MSE_FILE, 'r') as file:
            best_mse = float(file.read().strip())
            logger.info(f"Loaded best MSE: {best_mse}")
            return best_mse
    except FileNotFoundError:
        logger.info("No previous best MSE found.")
        return np.inf
    except Exception as e:
        logger.error(f"Error reading best MSE file: {e}")
        return np.inf

def save_best_mse(mse):
    """Saves the best MSE to the file."""
    try:
        with open(BEST_MSE_FILE, 'w') as file:
            file.write(str(mse))
            logger.info(f"Saved new best MSE: {mse}")
    except Exception as e:
        logger.error(f"Error saving best MSE: {e}")

def save_scaler(scaler_dict: dict):
    """Persist all normalisation constants so prediction can reproduce the same feature scaling."""
    import json
    try:
        with open(SCALER_FILE, 'w') as f:
            json.dump(scaler_dict, f)
        logger.info("Saved scaler: %s → %s", scaler_dict, SCALER_FILE)
    except Exception as e:
        logger.error(f"Error saving scaler file: {e}")

def build_model(input_dim):
    """Builds the neural network model.

    input_dim is determined by the number of features returned by prepare_data()
    (currently 4: total_msg, pct_delivered_norm, latency_ratio, undelivered_norm).
    """
    model = Sequential()
    model.add(Input(shape=(input_dim,)))
    model.add(Dense(128, activation='relu'))
    model.add(Dropout(0.3))
    model.add(Dense(64, activation='relu'))
    model.add(Dense(32, activation='relu'))
    model.add(Dense(1, activation='linear'))

    optimizer = Adam(learning_rate=0.0001)
    model.compile(optimizer=optimizer, loss='mean_squared_error', metrics=['mse'])
    return model

def run_training():
    """
    Runs the training process, using 'total_msg' as the only feature.
    The model is saved only if it achieves a better MSE than the current best.
    """
    logger.info("Starting the training process...")

    gc.collect()
    logger.info("Memory cleared with garbage collection.")

    data = get_data_from_mongo()
    if data is None:
        logger.warning("No data available or insufficient records for training.")
        return None, np.inf

    X_train, X_test, y_train, y_test, scaler_dict = prepare_data(data)
    if X_train is None:
        logger.error("Data preparation failed.")
        return None, np.inf

    best_mse = load_best_mse()

    model = build_model(input_dim=X_train.shape[1])

    early_stopping = EarlyStopping(monitor='val_loss', patience=10, restore_best_weights=True)
    lr_scheduler = ReduceLROnPlateau(monitor='val_loss', factor=0.5, patience=5, min_lr=1e-6, verbose=1)

    model.fit(
        X_train, y_train,
        validation_data=(X_test, y_test),
        epochs=200,
        batch_size=32,
        callbacks=[early_stopping, lr_scheduler],
        verbose=1
    )

    y_pred = model.predict(X_test).flatten()

    if np.isnan(y_pred).any():
        logger.error("Model prediction contains NaN values. Aborting evaluation.")
        return None, np.inf

    final_mse = mean_squared_error(y_test, y_pred)
    logger.info(f"Final Model MSE: {final_mse:.2f}")

    if final_mse < best_mse:
        model_path = os.getenv('MODEL_PATH', '/mnt/model/best_replica_predictor_model.h5')
        model.save(model_path)
        logger.info(f"New best MSE achieved: {final_mse}. Model saved to {model_path}.")
        save_best_mse(final_mse)
        save_scaler(scaler_dict)
    else:
        logger.info(f"Model did not improve. Best MSE remains: {best_mse}.")

    K.clear_session()
    gc.collect()
    logger.info("Final memory cleared after training.")

    return final_mse, best_mse

if __name__ == "__main__":
    run_training()
