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
IDLE_TOTAL_MSG_THRESHOLD = float(os.getenv('IDLE_TOTAL_MSG_THRESHOLD', '1.0'))


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

_SCALER_DEFAULTS = {
    "max_total_msg":   300.0,
    "latency_cap":     2.0,
    "max_undelivered": 1.0,
    "features":        ["total_msg", "pct_delivered_norm", "latency_ratio", "undelivered_norm"],
}

def load_scaler() -> dict:
    """
    Load the normalisation constants written by the training job.
    Returns a dict with keys: max_total_msg, latency_cap, features.
    Falls back to safe defaults if the file is missing or uses the old
    single-value format so legacy models keep working.
    """
    try:
        with open(SCALER_FILE) as f:
            raw = json.load(f)
        # Backwards-compat: old scaler.json only had {"max_total_msg": X}
        if isinstance(raw, (int, float)):
            raw = {"max_total_msg": float(raw)}
        scaler = {**_SCALER_DEFAULTS, **raw}
        logger.info("Loaded scaler from %s: %s", SCALER_FILE, scaler)
        return scaler
    except FileNotFoundError:
        logger.warning("Scaler file not found at %s – using defaults.", SCALER_FILE)
        return dict(_SCALER_DEFAULTS)
    except Exception as e:
        logger.error("Error loading scaler file: %s – using defaults.", e)
        return dict(_SCALER_DEFAULTS)


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


# How many of the most-recent perception rows to inspect when deciding
# whether to suppress a scale-up plan.  Majority vote among these rows.
_ROLLING_ROWS   = 5
_LABEL_SUPPRESS_ROWS = 3   # look at the N most-recent labels for suppression check


def get_latest_data():
    collection = _connect_metrics_mongo()
    if collection is None:
        logger.error("Failed to connect to MongoDB.")
        return None
    # Fetch the last N rows and average them so a single noisy sample
    # does not cause erratic replica changes.
    rows = list(collection.find().sort("timestamp", -1).limit(_ROLLING_ROWS))
    data = pd.DataFrame(rows)
    if data.empty:
        logger.error("No data found for prediction.")
        return None
    if len(data) < _ROLLING_ROWS:
        logger.warning(
            "Only %d row(s) available for rolling average (expected %d) – using what is present.",
            len(data), _ROLLING_ROWS,
        )
    # Average numeric columns; keep the most-recent value for non-numeric ones
    numeric_cols = data.select_dtypes(include="number").columns.tolist()
    averaged = data[numeric_cols].mean().to_frame().T
    # Preserve the most-recent metadata columns (timestamp, _id, etc.) for logging
    for col in ["timestamp", "_id", "metadata"]:
        if col in data.columns:
            averaged[col] = data.iloc[0][col]
    logger.info("Rolling average over %d row(s) used for prediction.", len(data))
    logger.debug("Averaged data:\n%s", averaged)
    return averaged


def get_recent_labels() -> list[int]:
    """Return the labels of the N most-recent perception rows, newest first."""
    collection = _connect_metrics_mongo()
    if collection is None:
        return []
    rows = list(
        collection.find(
            {"label": {"$exists": True}},
            {"label": 1, "_id": 0},
        ).sort("timestamp", -1).limit(_LABEL_SUPPRESS_ROWS)
    )
    return [int(r["label"]) for r in rows if "label" in r]


# ---------------------------------------------------------------------------
# Feature preparation
# ---------------------------------------------------------------------------

def prepare_data_for_prediction(data):
    """Build the feature vector that matches the training feature set.

    Features (must match training.py prepare_data):
      1. total_msg            normalised to [0, 300]
      2. pct_delivered_norm   delivery % / 100
      3. latency_ratio        P90 latency / latency_cap  (capped at 1.0)
      4. undelivered_norm     m_undelivered / max_undelivered (from scaler.json)
                              Rises under broker saturation — demand-pressure signal.
                              Falls back to 0.0 if column absent.
    """
    # When there is no traffic, Prometheus returns no metric samples for rate(),
    # so m_delivered and m_undelivered will be None / missing in MongoDB.
    # Default absent/NaN metrics to 0.0 (idle state).
    data = data.copy()
    for col in ['m_delivered', 'm_undelivered']:
        if col not in data.columns:
            logger.info("'%s' column missing from metrics – treating as 0.0 (idle).", col)
            data[col] = 0.0
        else:
            data[col] = data[col].fillna(0.0)

    scaler = load_scaler()
    max_total        = scaler["max_total_msg"]
    latency_cap      = scaler["latency_cap"]
    max_undelivered  = scaler.get("max_undelivered") or 1.0

    # ── Feature 1: total throughput ──────────────────────────────────────────
    data['total_msg'] = data['m_delivered'] + data['m_undelivered']
    total_raw = float(data['total_msg'].iloc[0]) if not data.empty else 0.0
    if pd.isna(total_raw):
        total_raw = 0.0

    data['total_msg'] = (data['total_msg'] / max_total) * 300
    data['total_msg'] = data['total_msg'].fillna(0).astype(int)

    # ── Feature 2: delivery percentage ───────────────────────────────────────
    PCTCOL = 'm_percentage__delivered'
    if PCTCOL in data.columns:
        data['pct_delivered_norm'] = data[PCTCOL].fillna(0).clip(0, 100) / 100.0
    else:
        logger.warning("'%s' column missing for prediction – defaulting to 1.0.", PCTCOL)
        data['pct_delivered_norm'] = 1.0

    # ── Feature 3: P90 latency ratio ─────────────────────────────────────────
    LATCOL = 'm_quantile_90'
    if LATCOL in data.columns:
        data['latency_ratio'] = data[LATCOL].fillna(latency_cap).clip(0, latency_cap) / latency_cap
    else:
        logger.warning("'%s' column missing for prediction – defaulting to 0.0.", LATCOL)
        data['latency_ratio'] = 0.0

    # ── Feature 4: undelivered message rate ──────────────────────────────────
    UDCOL = 'm_undelivered'
    if UDCOL in data.columns:
        data['undelivered_norm'] = (
            data[UDCOL].fillna(0).clip(0, max_undelivered) / max_undelivered
        )
    else:
        logger.warning("'%s' column missing for prediction – defaulting to 0.0.", UDCOL)
        data['undelivered_norm'] = 0.0

    FEATURES = ['total_msg', 'pct_delivered_norm', 'latency_ratio', 'undelivered_norm']
    logger.debug(
        "Prediction features: total_msg=%.1f pct_delivered_norm=%.4f "
        "latency_ratio=%.4f undelivered_norm=%.4f",
        float(data['total_msg'].iloc[0]),
        float(data['pct_delivered_norm'].iloc[0]),
        float(data['latency_ratio'].iloc[0]),
        float(data['undelivered_norm'].iloc[0]),
    )

    X_scaled = data[FEATURES].to_numpy().astype(np.float32).reshape(1, -1)
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

    Shape compatibility: if the saved model was trained with fewer features than
    the current feature vector (e.g. old 1-feature model is still on the PVC),
    the input is automatically trimmed to the model's expected width so
    predictions keep working until a fresh retrain produces a 3-feature model.
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
        # Detect expected input width and trim X if the model is older
        expected_features = model.input_shape[-1]
        actual_features   = X_scaled.shape[-1]
        if actual_features != expected_features:
            logger.warning(
                "Model expects %d feature(s) but feature vector has %d. "
                "Trimming to %d – retrain to use the full feature set.",
                expected_features, actual_features, expected_features,
            )
            X_scaled = X_scaled[:, :expected_features]

        # If no data / no traffic is being sent (idle state), directly predict 1 replica
        if total_raw <= IDLE_TOTAL_MSG_THRESHOLD:
            logger.info(
                "No traffic / idle detected (total_raw=%.2f <= threshold=%.2f) – predicting 1 replica.",
                total_raw, IDLE_TOTAL_MSG_THRESHOLD,
            )
            predicted_replicas = 1
            confidence = 1.0
        else:
            raw_output = model.predict(X_scaled, verbose=0)
            raw_value  = float(raw_output[0][0])
            logger.debug(f"Raw model output: {raw_value}")

            # If raw value is within 0.2 of 1.0 (e.g. <= 1.20) and no severe stress, round to 1 replica
            # Otherwise use standard ceiling for responsive scale-up
            if raw_value <= 1.20:
                predicted_replicas = 1
            else:
                predicted_replicas = max(1, int(np.ceil(raw_value)))
            logger.info(f"Predicted replicas: {predicted_replicas}")

            # Confidence: how close the raw output is to the discrete prediction (0 = fractional, 1 = exact)
            fractional_part = abs(raw_value - predicted_replicas)
            confidence = round(max(0.0, 1.0 - min(fractional_part, 1.0)), 4)

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

    # ── Label-aware suppression ───────────────────────────────────────────────
    # If the system is consistently label=3 (SLA exceeding / over-replicated)
    # AND the model wants to scale UP, suppress the plan.  The model was trained
    # only on label=2 data so it has no concept of "already too many replicas" —
    # it will keep recommending more replicas as long as message volume is high,
    # even when latency is already well below the SLA target.
    #
    # Suppression rule: if ALL of the last N labels are 3 AND
    # predicted > current → skip writing the plan entirely.
    recent_labels = get_recent_labels()
    if recent_labels:
        all_label3 = all(lbl == 3 for lbl in recent_labels)
        wants_scaleup = predicted_replicas > current_replicas
        if all_label3 and wants_scaleup:
            logger.info(
                "SUPPRESSED scale-up plan (%d→%d): last %d labels are all 3 "
                "(system already exceeding SLA – no scale-up needed).",
                current_replicas, predicted_replicas, len(recent_labels),
            )
            return
        logger.debug(
            "Label suppression check: recent_labels=%s all_label3=%s wants_scaleup=%s",
            recent_labels, all_label3, wants_scaleup,
        )

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
