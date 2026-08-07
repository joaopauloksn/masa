from pymongo import MongoClient
import pandas as pd
import numpy as np
import logging
import os
from sklearn.model_selection import train_test_split
from sklearn.preprocessing import StandardScaler
from tensorflow.keras.models import Sequential
from tensorflow.keras.layers import Dense, Dropout, BatchNormalization, Input
from tensorflow.keras.optimizers import Adam, RMSprop, SGD
from tensorflow.keras.callbacks import EarlyStopping
from sklearn.metrics import mean_absolute_error
from tensorflow.keras.initializers import HeNormal, HeUniform, GlorotUniform, RandomNormal
from sklearn.model_selection import ParameterGrid
from tensorflow.keras.regularizers import l1, l2


# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


# MongoDB Connection
def get_data_from_mongo():
    # Read MongoDB credentials from environment variables
    username = os.getenv('MONGO_USERNAME')
    password = os.getenv('MONGO_PASSWORD')
    host = os.getenv(
        'MONGO_HOST',
        "mas-mongo-ce-0.mas-mongo-ce-svc.mongoce.svc.cluster.local:27017,"
        "mas-mongo-ce-1.mas-mongo-ce-svc.mongoce.svc.cluster.local:27017,"
        "mas-mongo-ce-2.mas-mongo-ce-svc.mongoce.svc.cluster.local:27017",
    )
    database = 'mongo_scaling'
    auth_db = 'admin'  # Authentication database
    replica_set = os.getenv('MONGO_REPLICA_SET', 'rs0')  # Set your replica set name

    if not username or not password:
        logger.error("MongoDB username or password not set in environment variables.")
        return None

    # Construct MongoDB URI with SSL, replica set, and no cert validation
    mongo_uri = f"mongodb://{username}:{password}@{host}"
    mongo_tls = os.getenv('MONGO_TLS', 'true').lower() == 'true'
    mongo_tls_insecure = os.getenv('MONGO_TLS_INSECURE', 'true').lower() == 'true'

    logger.info("Connecting to MongoDB with SSL, replica set, and without certificate validation...")
    try:
        client = MongoClient(
            mongo_uri,
            authMechanism="SCRAM-SHA-256",
            tlsinsecure=mongo_tls_insecure,
            authSource="admin",
            maxPoolSize=100,
            retryWrites=True,
            tls=mongo_tls,
        )
        db = client[database]
        collection = db['microservices']

        # Test the connection
        client.server_info()  # Forces connection attempt
        logger.info("Connected to MongoDB successfully.")
        
        # Fetch data from collection
        data = pd.DataFrame(list(collection.find()))
        if data.empty:
            logger.error("No data found in the 'microservices' collection.")
            return None
        
        data = data.drop(['_id', 'timestamp', 'metadata'], axis=1, errors='ignore')
        logger.info(f"Retrieved {len(data)} records from the database.")
        return data

    except pymongo.errors.ServerSelectionTimeoutError as err:
        logger.error(f"Connection timed out: {err}")
        return None
    except pymongo.errors.PyMongoError as err:
        logger.error(f"Failed to connect to MongoDB: {err}")
        return None


# Data Preparation
def prepare_data(data):
    logger.info("Preparing data for training...")

    # Convert target column 'label' to numeric
    data['label'] = data['label'].astype(int)

    # Convert necessary fields to float
    data['t_quantile_90'] = data['t_quantile_90'].astype(float)
    data['t_percentage__delivered'] = data['t_percentage__delivered'].astype(float)

    # Define features and target
    X = data.drop(['replicas', 'label'], axis=1)
    y = data['replicas']

    # Handle NaN values in X by replacing with 0
    X.fillna(0, inplace=True)

    # Handle NaN values in the target by replacing with 0
    y.fillna(0, inplace=True)

    # Splitting the data
    X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42)

    # Normalize input features
    scaler = StandardScaler()
    X_train_scaled = scaler.fit_transform(X_train)
    X_test_scaled = scaler.transform(X_test)

    # Add small value to prevent division by zero during normalization
    X_train_scaled = (X_train_scaled - np.mean(X_train_scaled, axis=0)) / (np.std(X_train_scaled, axis=0) + 1e-8)
    X_test_scaled = (X_test_scaled - np.mean(X_test_scaled, axis=0)) / (np.std(X_test_scaled, axis=0) + 1e-8)

    # Convert input features to float32
    X_train_scaled = X_train_scaled.astype(np.float32)
    X_test_scaled = X_test_scaled.astype(np.float32)

    # Convert target to float32 for compatibility with TensorFlow
    y_train = y_train.astype(np.float32)
    y_test = y_test.astype(np.float32)

    # Check for NaNs after preprocessing and log warning if found
    if np.isnan(X_train_scaled).any() or np.isnan(X_test_scaled).any() or np.isnan(y_train).any() or np.isnan(y_test).any():
        logger.warning("NaN values found in processed data!")

    logger.info("Data preparation complete.")
    return X_train_scaled, X_test_scaled, y_train, y_test


# Build Neural Network Model
def build_model(input_dim, layers, units, dropout_rate, activation, optimizer_name, init_mode, l1_reg=0.0, l2_reg=0.0, batch_norm=False):
    model = Sequential()
    model.add(Input(shape=(input_dim,)))

    # Define weight initialization based on input
    if init_mode == 'he_uniform':
        init = HeUniform()
    elif init_mode == 'he_normal':
        init = HeNormal()
    elif init_mode == 'glorot_uniform':
        init = GlorotUniform()
    else:
        init = GlorotUniform()  # Default to 'glorot_uniform' for stability

    # Add the first layer
    model.add(Dense(units[0], activation=activation, kernel_initializer=init,
                    kernel_regularizer=l2(l2_reg) if l2_reg > 0 else None))
    if batch_norm:
        model.add(BatchNormalization())
    model.add(Dropout(dropout_rate))

    # Add remaining layers
    for i in range(1, layers):
        if i < len(units):  # Ensure we don't exceed the units list
            model.add(Dense(units[i], activation=activation, kernel_initializer=init))
            if batch_norm:
                model.add(BatchNormalization())
            model.add(Dropout(dropout_rate))

    # Add the output layer
    model.add(Dense(1, activation='linear'))

    # Define optimizer
    if optimizer_name == 'adam':
        optimizer = Adam(learning_rate=0.00001, clipnorm=1.0)
    elif optimizer_name == 'sgd':
        optimizer = SGD(learning_rate=0.0001, momentum=0.9, clipnorm=1.0)  # Lower learning rate
    elif optimizer_name == 'rmsprop':
        optimizer = RMSprop(learning_rate=0.00001, clipnorm=1.0)

    # Compile the model
    model.compile(optimizer=optimizer, loss='mean_squared_error', metrics=['mae'])
    return model


def hyperparameter_tuning(X_train, X_test, y_train, y_test):
    best_mae = np.inf
    best_params = {}

    # Define parameter grid
    param_grid = {
        'layers': [1, 2],
        'units': [[32], [64, 32]],  # Match units to layers
        'dropout_rate': [0.1, 0.2],
        'activation': ['relu', 'leaky_relu'],
        'optimizer': ['adam', 'rmsprop', 'sgd'],
        'init_mode': ['he_uniform', 'he_normal', 'glorot_uniform', 'random_normal'],
        'l1_reg': [0.0, 0.01],
        'l2_reg': [0.0, 0.01],
        'batch_size': [16, 32],
        'batch_norm': [True, False]
    }

    # Calculate total number of combinations
    total_combinations = len(list(ParameterGrid(param_grid)))
    current_position = 0

    for params in ParameterGrid(param_grid):
        # Increment current position
        current_position += 1
        percentage_done = (current_position / total_combinations) * 100

        # Log current parameter combination and completion percentage
        logger.info(f"Testing parameters: {params} ({current_position}/{total_combinations}, {percentage_done:.2f}% complete)")

        # Build model with current parameters
        model = build_model(
            input_dim=X_train.shape[1],
            layers=params['layers'],
            units=params['units'],
            dropout_rate=params['dropout_rate'],
            activation=params['activation'],
            optimizer_name=params['optimizer'],
            init_mode=params['init_mode'],
            l1_reg=params['l1_reg'],
            l2_reg=params['l2_reg'],
            batch_norm=params['batch_norm']
        )

        # Early stopping
        early_stopping = EarlyStopping(monitor='val_loss', patience=5, restore_best_weights=True)

        # Train model
        try:
            history = model.fit(
                X_train, y_train,
                validation_data=(X_test, y_test),
                epochs=100,
                batch_size=params['batch_size'],
                callbacks=[early_stopping],
                verbose=0
            )

            # Predict and check for NaN in predictions
            y_pred = model.predict(X_test)
            if np.isnan(y_pred).any():
                logger.error(f"Model prediction contains NaN values for params: {params}. Skipping this combination.")
                continue

            # Flatten y_pred for compatibility with mean_absolute_error
            y_pred = y_pred.flatten()

            # Evaluate model
            mae = mean_absolute_error(y_test, y_pred)
            logger.info(f"Model MAE: {mae:.2f}")

            # Check for best model
            if mae < best_mae:
                best_mae = mae
                best_params = params
                logger.info(f"New best model found with MAE: {mae:.2f}")

        except Exception as e:
            logger.error(f"Error during training with params {params}: {e}")
            continue

    logger.info(f"Best model parameters: {best_params} with MAE: {best_mae:.2f}")
    return best_params


# Main function
if __name__ == "__main__":
    # Fetch and prepare data
    data = get_data_from_mongo()
    X_train, X_test, y_train, y_test = prepare_data(data)

    # Hyperparameter tuning
    best_params = hyperparameter_tuning(X_train, X_test, y_train, y_test)

    # Final model with best parameters
    logger.info("Training final model with the best parameters...")
    final_model = build_model(
        input_dim=X_train.shape[1],
        layers=best_params['layers'],
        units=best_params['units'],
        dropout_rate=best_params['dropout_rate'],
        activation=best_params['activation'],
        optimizer_name=best_params['optimizer'],
        init_mode=best_params['init_mode'],
        l1_reg=best_params['l1_reg'],
        l2_reg=best_params['l2_reg'],
        batch_norm=best_params['batch_norm']
    )

    # Train final model
    final_model.fit(
        X_train, y_train,
        validation_data=(X_test, y_test),
        epochs=100,
        batch_size=best_params['batch_size'],
        callbacks=[EarlyStopping(monitor='val_loss', patience=5, restore_best_weights=True)],
        verbose=1
    )

    # Save the final model
    final_model.save("best_replica_predictor_model.h5")
    logger.info("Final model training complete and saved.")
