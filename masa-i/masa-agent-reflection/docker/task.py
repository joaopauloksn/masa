"""
Quality Assurance component
===========================
Runs as a Kubernetes CronJob (every 5 minutes by default).

Responsibilities
----------------
1. POST-SCALING LABEL CHECK
   For every scaling event in the last evaluation window (i.e. a replica-count
   change recorded in MongoDB), look at the N rows that followed it and count
   how many landed on label=1 (SLA breach).  Exposes the ratio as a Prometheus
   gauge so the ops team can alert on it.

2. ROLLING MAE (model drift indicator)
   For every recent label=2 row (operating at the "correct" replica count),
   run the same prediction the model-deployment component would have made and
   compare it to the actual replica count.  A rising MAE means the model is
   drifting and needs retraining.

3. MODEL AGE
   Report how old model.h5 is (seconds since last modification) so an alert
   can fire when no new model has been produced for too long.

4. CORRECTIVE RESET
   If the post-scaling label-1 ratio exceeds RESET_THRESHOLD for
   RESET_CONSECUTIVE_FAILURES consecutive QA runs, delete best_mse.txt from
   the shared PVC.  This forces the next model-development CronJob run to
   unconditionally overwrite the stale model with whatever it trains next.

Environment variables
---------------------
Required:
  MONGO_SECRET_DIR      Path to mounted secret dir containing username/password
                        (default: /etc/secrets/mongo)

Optional:
  MONGO_HOST            MongoDB connection string (default: MAS mongo replica set)
  MONGO_TLS             (default: true)
  MONGO_TLS_INSECURE    (default: true)
  MODEL_PATH            Path to model.h5 on shared PVC (default: /mnt/model/best_replica_predictor_model.h5)
  SCALER_FILE           Path to scaler.json on shared PVC (default: /mnt/model/scaler.json)
  BEST_MSE_FILE         Path to best_mse.txt on shared PVC (default: /mnt/model/best_mse.txt)
  DEPLOYMENT_NAME       Name of the monitored microservice Deployment
  NAMESPACE             Namespace of the monitored microservice Deployment
  EVAL_WINDOW_MINUTES   How many minutes back to look for scaling events (default: 30)
  POST_SCALING_ROWS     How many rows after a scaling event to evaluate (default: 5)
  RESET_THRESHOLD       label-1 ratio above which a model reset is considered (default: 0.3)
  RESET_CONSECUTIVE_FAILURES  How many consecutive bad QA runs before reset (default: 3)
  METRICS_PORT          Port for the Prometheus /metrics HTTP endpoint (default: 8000)
  QA_STATE_FILE         File to persist consecutive-failure counter across CronJob runs
                        (default: /mnt/model/qa_state.json)
"""

import os
import sys
import json
import logging
import signal
import time
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
from pymongo import MongoClient
from prometheus_client import Gauge, CollectorRegistry, push_to_gateway, start_http_server

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

MONGO_SECRET_DIR   = os.getenv("MONGO_SECRET_DIR",   "/etc/secrets/mongo")
MONGO_HOST         = os.getenv(
    "MONGO_HOST",
    "mas-mongo-ce-0.mas-mongo-ce-svc.mongoce.svc.cluster.local:27017,"
    "mas-mongo-ce-1.mas-mongo-ce-svc.mongoce.svc.cluster.local:27017,"
    "mas-mongo-ce-2.mas-mongo-ce-svc.mongoce.svc.cluster.local:27017",
)
MONGO_TLS          = os.getenv("MONGO_TLS", "true").lower() == "true"
MONGO_TLS_INSECURE = os.getenv("MONGO_TLS_INSECURE", "true").lower() == "true"

MODEL_PATH     = os.getenv("MODEL_PATH",     "/mnt/model/best_replica_predictor_model.h5")
SCALER_FILE    = os.getenv("SCALER_FILE",    "/mnt/model/scaler.json")
BEST_MSE_FILE  = os.getenv("BEST_MSE_FILE",  "/mnt/model/best_mse.txt")
QA_STATE_FILE  = os.getenv("QA_STATE_FILE",  "/mnt/model/qa_state.json")

DEPLOYMENT_NAME = os.getenv("DEPLOYMENT_NAME", "")
NAMESPACE       = os.getenv("NAMESPACE", "")

EVAL_WINDOW_MINUTES        = int(os.getenv("EVAL_WINDOW_MINUTES", "30"))
POST_SCALING_ROWS          = int(os.getenv("POST_SCALING_ROWS", "5"))
RESET_THRESHOLD            = float(os.getenv("RESET_THRESHOLD", "0.3"))
RESET_CONSECUTIVE_FAILURES = int(os.getenv("RESET_CONSECUTIVE_FAILURES", "3"))
METRICS_PORT               = int(os.getenv("METRICS_PORT", "8000"))

# ---------------------------------------------------------------------------
# Secret helper
# ---------------------------------------------------------------------------

def _read_secret_file(path: str, fallback_env: str) -> str:
    try:
        with open(path) as f:
            value = f.read().strip()
            if value:
                return value
    except (OSError, IOError):
        pass
    return os.environ.get(fallback_env, "")

# ---------------------------------------------------------------------------
# MongoDB helpers
# ---------------------------------------------------------------------------

def _connect_mongo() -> MongoClient:
    username = _read_secret_file(f"{MONGO_SECRET_DIR}/username", "MONGO_USERNAME")
    password = _read_secret_file(f"{MONGO_SECRET_DIR}/password", "MONGO_PASSWORD")
    if not username or not password:
        raise EnvironmentError(
            "MongoDB credentials not found. Mount a secret at MONGO_SECRET_DIR "
            "or set MONGO_USERNAME / MONGO_PASSWORD."
        )
    mongo_uri = f"mongodb://{username}:{password}@{MONGO_HOST}"
    return MongoClient(
        mongo_uri,
        authMechanism="SCRAM-SHA-256",
        tlsInsecure=MONGO_TLS_INSECURE,
        authSource="admin",
        connectTimeoutMS=30000,
        socketTimeoutMS=30000,
        maxPoolSize=5,
        retryWrites=True,
        tls=MONGO_TLS,
    )


def _fetch_recent_rows(collection, minutes: int) -> pd.DataFrame:
    """Return all MongoDB rows from the last `minutes` minutes as a DataFrame."""
    since = datetime.now(tz=timezone.utc) - timedelta(minutes=minutes)
    cursor = collection.find({"timestamp": {"$gte": since}}).sort("timestamp", 1)
    df = pd.DataFrame(list(cursor))
    return df

# ---------------------------------------------------------------------------
# Scaler
# ---------------------------------------------------------------------------

def _load_scaler() -> float:
    try:
        with open(SCALER_FILE) as f:
            return float(json.load(f)["max_total_msg"])
    except FileNotFoundError:
        logger.warning("scaler.json not found – using default max 300.")
        return 300.0
    except Exception as e:
        logger.error("Error reading scaler.json: %s – using default max 300.", e)
        return 300.0

# ---------------------------------------------------------------------------
# Model prediction helper (same logic as model-deployment/predict.py)
# ---------------------------------------------------------------------------

def _predict_replicas(total_msg: float, max_total_msg: float, model) -> int:
    """Run a single prediction through the loaded model."""
    normalised = (total_msg / max_total_msg) * 300
    X = np.array([[normalised]], dtype=np.float32)
    raw = model.predict(X, verbose=0)
    return max(1, int(np.ceil(raw[0][0])))

# ---------------------------------------------------------------------------
# QA state persistence (consecutive-failure counter)
# ---------------------------------------------------------------------------

def _load_qa_state() -> dict:
    try:
        with open(QA_STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"consecutive_failures": 0}

def _save_qa_state(state: dict):
    try:
        with open(QA_STATE_FILE, "w") as f:
            json.dump(state, f)
    except Exception as e:
        logger.error("Could not save QA state: %s", e)

# ---------------------------------------------------------------------------
# Corrective reset
# ---------------------------------------------------------------------------

def _reset_model():
    """
    Delete best_mse.txt so the next model-development CronJob run treats
    any model it trains as an improvement and overwrites the stale one.
    """
    try:
        os.remove(BEST_MSE_FILE)
        logger.warning("MODEL RESET: deleted %s to force model retraining.", BEST_MSE_FILE)
    except FileNotFoundError:
        logger.info("best_mse.txt already absent – model reset not needed.")
    except Exception as e:
        logger.error("Failed to delete best_mse.txt: %s", e)

# ---------------------------------------------------------------------------
# Core QA logic
# ---------------------------------------------------------------------------

def run_quality_assurance() -> int:
    """
    Execute one QA evaluation pass.
    Returns 0 on success, 1 on hard failure.
    """
    logger.info("Quality Assurance run started at %s", datetime.now())

    # --- Prometheus metrics registry -------------------------------------------
    registry = CollectorRegistry()
    g_label1_ratio = Gauge(
        "masa_post_scaling_label1_ratio",
        "Fraction of post-scaling observations with label=1 (SLA breach)",
        ["deployment", "namespace"],
        registry=registry,
    )
    g_rolling_mae = Gauge(
        "masa_rolling_mae",
        "Rolling MAE between model-predicted replicas and actual replicas at label=2",
        ["deployment", "namespace"],
        registry=registry,
    )
    g_model_age = Gauge(
        "masa_model_age_seconds",
        "Seconds since the model file was last modified",
        ["deployment", "namespace"],
        registry=registry,
    )
    g_consecutive_failures = Gauge(
        "masa_consecutive_qa_failures",
        "Number of consecutive QA runs that exceeded the label-1 ratio threshold",
        ["deployment", "namespace"],
        registry=registry,
    )

    labels = {"deployment": DEPLOYMENT_NAME, "namespace": NAMESPACE}

    # --- Model age ----------------------------------------------------------------
    model_age = None
    if os.path.exists(MODEL_PATH):
        model_age = time.time() - os.path.getmtime(MODEL_PATH)
        g_model_age.labels(**labels).set(model_age)
        logger.info("Model age: %.0f seconds (%.1f hours)", model_age, model_age / 3600)
    else:
        logger.warning("Model file not found at %s – skipping model-based checks.", MODEL_PATH)

    # --- MongoDB ------------------------------------------------------------------
    try:
        mongo_client = _connect_mongo()
        db = mongo_client["mongo_scaling"]
        collection = db["microservices"]
        df = _fetch_recent_rows(collection, EVAL_WINDOW_MINUTES)
    except Exception as e:
        logger.error("Cannot connect to MongoDB: %s", e)
        return 1
    finally:
        try:
            mongo_client.close()
        except Exception:
            pass

    if df.empty:
        logger.warning("No MongoDB rows in the last %d minutes – nothing to evaluate.", EVAL_WINDOW_MINUTES)
        return 0

    # Filter to rows belonging to this deployment when metadata is present
    if "metadata" in df.columns and DEPLOYMENT_NAME:
        mask = df["metadata"].apply(
            lambda m: isinstance(m, dict) and m.get("deployment") == DEPLOYMENT_NAME
        )
        df = df[mask].copy()

    if df.empty:
        logger.warning("No rows for deployment '%s' in the evaluation window.", DEPLOYMENT_NAME)
        return 0

    df = df.sort_values("timestamp").reset_index(drop=True)

    # --- 1. Post-scaling label check --------------------------------------------
    # Detect rows where the replica count changed from the previous row.
    post_scaling_labels = []
    if "replicas" in df.columns and "label" in df.columns:
        replicas = df["replicas"].fillna(method="ffill")
        scaling_events = replicas.diff().abs() > 0
        for idx in df.index[scaling_events]:
            # Gather the next POST_SCALING_ROWS rows after this event
            following = df.loc[idx + 1 : idx + POST_SCALING_ROWS, "label"].dropna().tolist()
            post_scaling_labels.extend(following)

    if post_scaling_labels:
        label1_ratio = post_scaling_labels.count(1) / len(post_scaling_labels)
        g_label1_ratio.labels(**labels).set(label1_ratio)
        logger.info(
            "Post-scaling label distribution: %s → label-1 ratio=%.2f",
            {l: post_scaling_labels.count(l) for l in set(post_scaling_labels)},
            label1_ratio,
        )
    else:
        label1_ratio = 0.0
        g_label1_ratio.labels(**labels).set(0.0)
        logger.info("No scaling events found in the evaluation window.")

    # --- 2. Rolling MAE ---------------------------------------------------------
    rolling_mae = None
    if model_age is not None and "label" in df.columns:
        label2_rows = df[df["label"] == 2].copy()
        if not label2_rows.empty and "m_delivered" in label2_rows.columns and "m_undelivered" in label2_rows.columns:
            try:
                from tensorflow.keras.models import load_model as tf_load_model
                model = tf_load_model(MODEL_PATH, compile=False)
                max_total_msg = _load_scaler()

                errors = []
                for _, row in label2_rows.iterrows():
                    delivered   = row.get("m_delivered")
                    undelivered = row.get("m_undelivered")
                    actual      = row.get("replicas")
                    if pd.isna(delivered) or pd.isna(undelivered) or pd.isna(actual):
                        continue
                    total_msg = delivered + undelivered
                    predicted = _predict_replicas(total_msg, max_total_msg, model)
                    errors.append(abs(predicted - actual))

                if errors:
                    rolling_mae = float(np.mean(errors))
                    g_rolling_mae.labels(**labels).set(rolling_mae)
                    logger.info(
                        "Rolling MAE over %d label=2 rows: %.4f", len(errors), rolling_mae
                    )
                else:
                    logger.info("Not enough valid label=2 rows to compute MAE.")

            except Exception as e:
                logger.error("MAE computation failed: %s", e)
        else:
            logger.info("No label=2 rows or missing message columns – skipping MAE.")

    # --- 3. Consecutive-failure tracking & corrective reset ---------------------
    state = _load_qa_state()
    if label1_ratio > RESET_THRESHOLD:
        state["consecutive_failures"] = state.get("consecutive_failures", 0) + 1
        logger.warning(
            "label-1 ratio %.2f exceeds threshold %.2f "
            "(consecutive failures: %d/%d)",
            label1_ratio, RESET_THRESHOLD,
            state["consecutive_failures"], RESET_CONSECUTIVE_FAILURES,
        )
    else:
        if state.get("consecutive_failures", 0) > 0:
            logger.info("label-1 ratio back below threshold – resetting failure counter.")
        state["consecutive_failures"] = 0

    g_consecutive_failures.labels(**labels).set(state["consecutive_failures"])

    if state["consecutive_failures"] >= RESET_CONSECUTIVE_FAILURES:
        logger.warning(
            "Consecutive failure limit reached (%d). Triggering model reset.",
            RESET_CONSECUTIVE_FAILURES,
        )
        _reset_model()
        state["consecutive_failures"] = 0  # reset counter after triggering

    _save_qa_state(state)

    # --- 4. Write model_feedback to MongoDB -------------------------------------
    # The model_feedback collection is the structured verdict consumed by the
    # Planning Agent to decide whether to re-request governance_context refresh
    # and by operators for audit trails.
    try:
        mongo_client2 = _connect_mongo()
        db2 = mongo_client2["mongo_scaling"]
        if "model_feedback" not in db2.list_collection_names():
            db2.create_collection("model_feedback")
            db2["model_feedback"].create_index([("evaluated_at", -1)])
        feedback_doc = {
            "evaluated_at":        datetime.now(tz=timezone.utc),
            "deployment":          DEPLOYMENT_NAME,
            "namespace":           NAMESPACE,
            "eval_window_minutes": EVAL_WINDOW_MINUTES,
            "post_scaling_rows_evaluated": len(post_scaling_labels) if post_scaling_labels else 0,
            "label1_ratio":        round(label1_ratio, 4),
            "rolling_mae":         round(rolling_mae, 4) if rolling_mae is not None else None,
            "model_age_seconds":   round(model_age, 1)   if model_age  is not None else None,
            "consecutive_failures": state["consecutive_failures"],
            "model_reset_triggered": state["consecutive_failures"] == 0
                                     and label1_ratio > RESET_THRESHOLD,
            "verdict":             (
                "good"     if label1_ratio <= RESET_THRESHOLD and rolling_mae is not None and rolling_mae < 1.0
                else "degraded" if label1_ratio <= RESET_THRESHOLD
                else "poor"
            ),
        }
        db2["model_feedback"].insert_one(feedback_doc)
        logger.info(
            "model_feedback written: verdict=%s label1_ratio=%.4f rolling_mae=%s",
            feedback_doc["verdict"], label1_ratio,
            f"{rolling_mae:.4f}" if rolling_mae is not None else "n/a",
        )
    except Exception as e:
        logger.error("Failed to write model_feedback: %s", e)
    finally:
        try:
            mongo_client2.close()
        except Exception:
            pass

    # --- 5. Expose metrics via HTTP (stays up briefly for scraping) -------------
    # The CronJob pod lives just long enough to be scraped by Prometheus.
    # Start the server and hold for METRICS_HOLD_SECONDS before exiting.
    metrics_hold = int(os.getenv("METRICS_HOLD_SECONDS", "30"))
    try:
        start_http_server(METRICS_PORT, registry=registry)
        logger.info(
            "Prometheus metrics available on :%d/metrics for %d seconds",
            METRICS_PORT, metrics_hold,
        )
        time.sleep(metrics_hold)
    except Exception as e:
        logger.error("Could not start metrics HTTP server: %s", e)

    logger.info(
        "Quality Assurance run completed at %s | label1_ratio=%.2f | rolling_mae=%s | model_age=%s",
        datetime.now(),
        label1_ratio,
        f"{rolling_mae:.4f}" if rolling_mae is not None else "n/a",
        f"{model_age:.0f}s" if model_age is not None else "n/a",
    )
    return 0


# ---------------------------------------------------------------------------
# Graceful shutdown
# ---------------------------------------------------------------------------

def _shutdown(signum, frame):
    logger.info("Received signal %s – shutting down QA.", signum)
    raise SystemExit(0)

signal.signal(signal.SIGTERM, _shutdown)
signal.signal(signal.SIGINT,  _shutdown)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    try:
        rc = run_quality_assurance()
        sys.exit(rc)
    except Exception as e:
        logger.error("Quality Assurance run failed with unhandled exception: %s", e)
        sys.exit(1)
