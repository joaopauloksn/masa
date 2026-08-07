import os
import logging
import pandas as pd
import matplotlib.pyplot as plt
import seaborn as sns
from pymongo import MongoClient

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Function to connect to MongoDB and get data
def get_data_from_mongo():
    """Connects to MongoDB and retrieves records."""
    mongo_user = os.getenv('MONGO_USERNAME')
    mongo_password = os.getenv('MONGO_PASSWORD')
    if not mongo_user or not mongo_password:
        logger.error("MongoDB credentials not set in environment variables.")
        return None

    mongo_host = os.getenv(
        'MONGO_HOST',
        "mas-mongo-ce-0.mas-mongo-ce-svc.mongoce.svc.cluster.local:27017,"
        "mas-mongo-ce-1.mas-mongo-ce-svc.mongoce.svc.cluster.local:27017,"
        "mas-mongo-ce-2.mas-mongo-ce-svc.mongoce.svc.cluster.local:27017",
    )
    mongo_uri = f"mongodb://{mongo_user}:{mongo_password}@{mongo_host}"

    mongo_tls = os.getenv('MONGO_TLS', 'true').lower() == 'true'
    mongo_tls_insecure = os.getenv('MONGO_TLS_INSECURE', 'true').lower() == 'true'

    try:
        client = MongoClient(
            mongo_uri,
            authMechanism="SCRAM-SHA-256",
            tlsInsecure=mongo_tls_insecure,
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

# Function to plot data
def plot_data(data):
    """Plots the data to visualize class separability."""
    if 'label' not in data.columns:
        logger.error("The 'label' column is missing from the data.")
        return

    # Select the features used in the neural network
    feature_columns = [
        'm_messages_perf', 'm_cpu', 't_percentage__delivered',
        'm_percentage__delivered', 'm_memory'
    ]
    
    # Check if all required features are present
    missing_features = [col for col in feature_columns if col not in data.columns]
    if missing_features:
        logger.error(f"Missing features in the data: {missing_features}")
        return

    # Limit the data to the selected features and the label
    data_filtered = data[feature_columns + ['label']].sample(n=min(1000, len(data)))

    # Pair plot using seaborn for visualization of feature interaction
    sns.pairplot(data_filtered, hue='label', diag_kind='hist', plot_kws={'alpha': 0.7})
    plt.suptitle("Feature Pair Plots with Class Labels", y=1.02)
    plt.show()

if __name__ == "__main__":
    # Fetch data from MongoDB
    data = get_data_from_mongo()
    if data is not None:
        plot_data(data)
    else:
        logger.warning("No data available for plotting.")

