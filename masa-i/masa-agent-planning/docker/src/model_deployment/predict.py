"""
predict.py — model-deployment component (Planning Agent)
=========================================================
Responsibility:
  1. Read the latest governance_context from MongoDB (written by the Understanding Agent).
  2. Predict the desired replica count using the trained model.
  3. Write a scaling plan to the MongoDB ``scaling_plans`` collection.

It does NOT touch the Kubernetes API to scale any Deployment.
That is exclusively the job of the autoscaler-agent.
"""

import os
import logging
import json
import pandas as pd
import numpy as np
from pymongo import MongoClient
from tensorflow.keras.models import load_model
from kubernetes import client, config

from model_deployment.store import (
    connect as store_connect,
    get_collection as store_get_collection,
    write_plan,
)

# Setup logging
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

SCALER_FILE = os.getenv('SCALER_FILE', '/mnt/model/scaler.json')


# ---------------------------------------------------------------------------
# Secret helper
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


# ---------------------------------------------------------------------------
# Scaler
# ---------------------------------------------------------------------------

def load_scaler() -> float:
    """
    Load the normalisation max_total_msg written by the training job.
    Falls back to 300 if the file is missing so old models keep working.
    """
    try:
        with open(SCALER_FILE) as f:
            data = json.load(f)
        max_val = float(data["max_total_msg"])
        logger.info("Loaded scaler: max_total_msg=%.2f from %s", max_val, SCALER_FILE)
        return max_val
    except FileNotFoundError:
        logger.warning("Scaler file not found at %s – falling back to default max 300.", SCALER_FILE)
        return 300.0
    except Exception as e:
        logger.error("Error loading scaler file: %s – falling back to default max 300.", e)
        return 300.0


# ---------------------------------------------------------------------------
# MongoDB — governance_context (written by Understanding Agent)
# ---------------------------------------------------------------------------

def _read_governance_context() -> dict | None:
    """
    Return the latest governance_context document from MongoDB.
    Falls back gracefully to env-var values if the collection is empty or unreachable.
    """
    try:
        secret_dir  = os.getenv("MONGO_SECRET_DIR", "/etc/secrets/mongo")
        mongo_user  = _read_secret_file(f"{secret_dir}/username", "MONGO_USERNAME")
        mongo_pass  = _read_secret_file(f"{secret_dir}/password", "MONGO_PASSWORD")
        if not mongo_user or not mongo_pass:
            return None
        mongo_host         = os.getenv(
            "MONGO_HOST",
            "mas-mongo-ce-0.mas-mongo-ce-svc.mongoce.svc.cluster.local:27017,"
            "mas-mongo-ce-1.mas-mongo-ce-svc.mongoce.svc.cluster.local:27017,"
            "mas-mongo-ce-2.mas-mongo-ce-svc.mongoce.svc.cluster.local:27017",
        )
        mongo_tls          = os.getenv("MONGO_TLS", "true").lower() == "true"
        mongo_tls_insecure = os.getenv("MONGO_TLS_INSECURE", "true").lower() == "true"
        mc = MongoClient(
            f"mongodb://{mongo_user}:{mongo_pass}@{mongo_host}",
            authMechanism="SCRAM-SHA-256",
            tlsInsecure=mongo_tls_insecure,
            authSource="admin",
            connectTimeoutMS=10000,
            socketTimeoutMS=10000,
            maxPoolSize=2,
            tls=mongo_tls,
        )
        ctx = mc["mongo_scaling"]["governance_context"].find_one(
            sort=[("updated_at", -1)]
        )
        mc.close()
        if ctx:
            logger.info(
                "Loaded governance_context: deployment=%s ns=%s sla=%s",
                ctx.get("microservice_deployment"),
                ctx.get("microservice_namespace"),
                ctx.get("sla_strategy"),
            )
        return ctx
    except Exception as e:
        logger.warning("Could not load governance_context: %s – using env vars.", e)
        return None


# ---------------------------------------------------------------------------
# MongoDB — time-series collection (source of truth for metrics)
# ---------------------------------------------------------------------------

def _connect_metrics_mongo():
    """Return the microservices metrics collection."""
    secret_dir     = os.getenv("MONGO_SECRET_DIR", "/etc/secrets/mongo")
    mongo_user     = _read_secret_file(f"{secret_dir}/username", "MONGO_USERNAME")
    mongo_password = _read_secret_file(f"{secret_dir}/password", "MONGO_PASSWORD")

    if not mongo_user or not mongo_password:
        logger.error(
            "MongoDB credentials not found. Mount a secret at MONGO_SECRET_DIR "
            "or set MONGO_USERNAME / MONGO_PASSWORD."
        )
        return None

    mongo_host = os.getenv(
        'MONGO_HOST',
        "mas-mongo-ce-0.mas-mongo-ce-svc.mongoce.svc.cluster.local:27017,"
        "mas-mongo-ce-1.mas-mongo-ce-svc.mongoce.svc.cluster.local:27017,"
        "mas-mongo-ce-2.mas-mongo-ce-svc.mongoce.svc.cluster.local:27017",
    )
    mongo_tls          = os.getenv('MONGO_TLS', 'true').lower() == 'true'
    mongo_tls_insecure = os.getenv('MONGO_TLS_INSECURE', 'true').lower() == 'true'

    try:
        mongo_client = MongoClient(
            f"mongodb://{mongo_user}:{mongo_password}@{mongo_host}",
            authMechanism="SCRAM-SHA-256",
            tlsInsecure=mongo_tls_insecure,
            authSource="admin",
            connectTimeoutMS=60000,
            socketTimeoutMS=60000,
            maxPoolSize=100,
            retryWrites=True,
            tls=mongo_tls,
        )
        collection = mongo_client["mongo_scaling"]["microservices"]
        logger.info("Connected to metrics MongoDB successfully.")
        return collection
    except Exception as e:
        logger.error(f"Error connecting to metrics MongoDB: {e}")
        return None


def get_latest_data():
    collection = _connect_metrics_mongo()
    if collection is None:
        logger.error("Failed to connect to MongoDB.")
        return None
    data = pd.DataFrame(list(collection.find().sort("timestamp", -1).limit(1)))
    if data.empty:
        logger.error("No data found for prediction.")
        return None
    logger.info(f"Data used for prediction:\n{data}")
    return data


# ---------------------------------------------------------------------------
# Feature preparation
# ---------------------------------------------------------------------------

def prepare_data_for_prediction(data):
    if 'm_delivered' not in data.columns or 'm_undelivered' not in data.columns:
        logger.error("Required columns 'm_delivered' and 'm_undelivered' are missing.")
        return None, None

    data['total_messages'] = data['m_delivered'] + data['m_undelivered']
    logger.debug(f"Delivered: {data['m_delivered'].iloc[0]}, Undelivered: {data['m_undelivered'].iloc[0]}, Total: {data['total_messages'].iloc[0]}")

    if pd.isna(data['total_messages'].iloc[0]):
        logger.warning("Total messages is NaN.")
        return None, None

    max_total = load_scaler()
    total_raw = float(data['total_messages'].iloc[0])   # keep raw value for confidence

    data['total_messages'] = (data['total_messages'] / max_total) * 300
    data['total_messages'] = data['total_messages'].fillna(0).astype(int)

    X_scaled = data[['total_messages']].to_numpy().astype(np.float32).reshape(1, -1)
    return X_scaled, total_raw


# ---------------------------------------------------------------------------
# Current replica count (read-only K8s call)
# ---------------------------------------------------------------------------

def get_current_replicas(deployment_name: str, namespace: str) -> int | None:
    """Read the current replica count from the Kubernetes API."""
    try:
        config.load_incluster_config()
        api = client.AppsV1Api()
        deployment = api.read_namespaced_deployment(name=deployment_name, namespace=namespace)
        replicas = deployment.spec.replicas
        logger.debug(f"Current replicas for '{deployment_name}' in '{namespace}': {replicas}")
        return replicas
    except client.exceptions.ApiException as e:
        logger.error(f"K8s API error reading replicas: {e}")
        return None
    except Exception as e:
        logger.error(f"Unexpected error reading replicas: {e}")
        return None


# ---------------------------------------------------------------------------
# Prediction
# ---------------------------------------------------------------------------

def predict_from_model(model_path: str):
    """
    Run the model against the latest metrics row.
    Returns (predicted_replicas, confidence) or (None, None) on error.

    confidence is derived from the raw model output: the closer the raw output
    is to an integer, the higher the certainty. Capped at [0, 1].
    """
    data = get_latest_data()
    if data is None or data.empty:
        logger.error("No data available for prediction.")
        return None, None

    X_scaled, total_raw = prepare_data_for_prediction(data)
    if X_scaled is None:
        logger.error("Data preparation failed.")
        return None, None

    try:
        model = load_model(model_path, compile=False)
        logger.info(f"Loaded model from {model_path}")
    except Exception as e:
        logger.error(f"Failed to load model: {e}")
        return None, None

    try:
        raw_output = model.predict(X_scaled, verbose=0)
        raw_value  = float(raw_output[0][0])
        logger.debug(f"Raw model output: {raw_value}")

        predicted_replicas = max(1, int(np.ceil(raw_value)))
        logger.info(f"Predicted replicas: {predicted_replicas}")

        # Confidence: how close the raw output is to the ceiled integer (0 = fractional, 1 = exact)
        fractional_part = abs(raw_value - np.floor(raw_value))
        confidence = round(1.0 - min(fractional_part, 1.0 - fractional_part) * 2, 4)
        logger.info(f"Confidence: {confidence:.4f}")

        return predicted_replicas, confidence
    except Exception as e:
        logger.error(f"Error during prediction: {e}")
        return None, None


# ---------------------------------------------------------------------------
# Main entry point for task.py
# ---------------------------------------------------------------------------

def predict_and_write_plan(model_path: str):
    """
    Predict the desired replica count and write a scaling plan to MongoDB.
    Does NOT call the Kubernetes API to scale anything.
    The autoscaler-agent is responsible for executing pending plans.

    Resolution order for deployment/namespace:
      1. governance_context (written by Understanding Agent from CR spec)
      2. DEPLOYMENT_NAME / NAMESPACE env vars (operator injects these as fallback)
    """
    # --- Governance context (Understanding Agent output) -----------------------
    ctx = _read_governance_context()

    deployment_name = (
        (ctx.get("microservice_deployment") if ctx else None)
        or os.getenv('DEPLOYMENT_NAME', 'default-deployment')
    )
    namespace = (
        (ctx.get("microservice_namespace") if ctx else None)
        or os.getenv('NAMESPACE', 'default-namespace')
    )

    if ctx:
        logger.info(
            "Resolved target from governance_context: %s/%s (sla=%s strategy=%s)",
            namespace, deployment_name,
            ctx.get("sla_strategy"), ctx.get("scaling_strategy"),
        )
    else:
        logger.warning(
            "governance_context unavailable – falling back to env vars: %s/%s",
            namespace, deployment_name,
        )

    predicted_replicas, confidence = predict_from_model(model_path)
    if predicted_replicas is None:
        logger.error("Prediction failed – no scaling plan written.")
        return

    current_replicas = get_current_replicas(deployment_name, namespace)
    if current_replicas is None:
        logger.warning(
            "Could not read current replicas from K8s – "
            "writing plan with current_replicas=unknown (0)."
        )
        current_replicas = 0

    try:
        mongo_client = store_connect()
        collection   = store_get_collection(mongo_client)
        plan_id = write_plan(
            collection,
            target_service=deployment_name,
            namespace=namespace,
            current_replicas=current_replicas,
            desired_replicas=predicted_replicas,
            confidence=confidence,
        )
        logger.info("Scaling plan written with id=%s", plan_id)
    except Exception as e:
        logger.error("Failed to write scaling plan: %s", e)
    finally:
        try:
            mongo_client.close()
        except Exception:
            pass


if __name__ == "__main__":
    model_path = os.getenv('MODEL_PATH', '/mnt/model/best_replica_predictor_model.h5')
    predict_and_write_plan(model_path)
