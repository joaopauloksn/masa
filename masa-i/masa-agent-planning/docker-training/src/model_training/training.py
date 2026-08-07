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
    """Prepares the data for training. Returns splits and the max_total_msg used for normalisation."""
    logger.info("Preparing data for training...")

    data = data[data['label'] == 2].copy()
    logger.info(f"Number of records after filtering by label 2: {len(data)}")

    if len(data) < 50:
        logger.warning(f"Insufficient data (< 50 records after label filter) at {pd.Timestamp.now()}.")
        return None, None, None, None, None

    if 'm_delivered' not in data.columns or 'm_undelivered' not in data.columns:
        logger.error("Required columns 'm_delivered' and 'm_undelivered' are missing.")
        return None, None, None, None, None

    data['total_msg'] = data['m_delivered'] + data['m_undelivered']

    X = data[['total_msg']]
    y = data['replicas']

    if y.isnull().any():
        logger.warning(f"Dropping {y.isnull().sum()} NaN values in target 'replicas'.")
        non_nan_indices = ~y.isnull()
        X = X[non_nan_indices]
        y = y[non_nan_indices]

    X = X.fillna(0)
    # Capture the max before normalising so prediction can reproduce the same scaling.
    max_total_msg = float(X['total_msg'].max())
    if max_total_msg == 0:
        logger.error("max_total_msg is 0 — cannot normalise. Aborting.")
        return None, None, None, None, None
    X['total_msg'] = (X['total_msg'] / max_total_msg) * 300
    X['total_msg'] = X['total_msg'].astype(int)

    X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42)

    logger.info("Data preparation complete. max_total_msg=%.2f", max_total_msg)
    return X_train, X_test, y_train, y_test, max_total_msg

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

def save_scaler(max_total_msg: float):
    """Persist the normalisation max value so prediction can reproduce the same scaling."""
    import json
    try:
        with open(SCALER_FILE, 'w') as f:
            json.dump({"max_total_msg": max_total_msg}, f)
        logger.info("Saved scaler: max_total_msg=%.2f → %s", max_total_msg, SCALER_FILE)
    except Exception as e:
        logger.error(f"Error saving scaler file: {e}")

def build_model(input_dim):
    """Builds an enhanced neural network model using the total replicas feature."""
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

    X_train, X_test, y_train, y_test, max_total_msg = prepare_data(data)
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
        save_scaler(max_total_msg)
    else:
        logger.info(f"Model did not improve. Best MSE remains: {best_mse}.")

    K.clear_session()
    gc.collect()
    logger.info("Final memory cleared after training.")

    return final_mse, best_mse

if __name__ == "__main__":
    run_training()
