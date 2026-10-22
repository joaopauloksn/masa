"""
store.py — scaling_plans collection helpers
============================================
Shared by model-deployment (writer) and autoscaler-agent (executor).

Collection: mongo_scaling.scaling_plans

Document schema
---------------
{
  target_service:   str      e.g. "iotoccupancy-sample-mosquitto"
  namespace:        str      e.g. "iotoccupancy"
  decision:         str      "scale_horizontal" | "no_scaling_needed"
  current_replicas: int      replica count at time of prediction
  desired_replicas: int      replica count recommended by the model
  reason:           str      "predicted_slo_violation" | "replicas_unchanged" | ...
  confidence:       float    model output normalised to [0, 1]
  status:           str      "pending" | "applied" | "skipped" | "failed"
  created_at:       datetime written by model-deployment
  applied_at:       datetime written by autoscaler-agent (None until executed)
  error:            str      written by autoscaler-agent on failure (None otherwise)
}

Status lifecycle
----------------
  model-deployment writes  → status = "pending"
  autoscaler-agent reads   → status = "pending"  (claim it immediately)
  autoscaler-agent applies → status = "applied"
  autoscaler-agent skips   → status = "skipped"  (desired == current, nothing to do)
  autoscaler-agent error   → status = "failed",  error = <message>
"""

import os
import logging
from datetime import datetime, timezone
from pymongo import MongoClient, ASCENDING, DESCENDING

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Status constants
# ---------------------------------------------------------------------------

STATUS_PENDING  = "pending"
STATUS_APPLIED  = "applied"
STATUS_SKIPPED  = "skipped"
STATUS_FAILED   = "failed"

DECISION_SCALE    = "scale_horizontal"
DECISION_NO_SCALE = "no_scaling_needed"

REASON_SLO_VIOLATION  = "predicted_slo_violation"
REASON_UNCHANGED      = "replicas_unchanged"
REASON_DEFAULT        = "model_predicted"

COLLECTION_NAME = "scaling_plans"

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
# Connection
# ---------------------------------------------------------------------------

def connect(secret_dir: str | None = None) -> MongoClient:
    """Return a connected MongoClient using mounted-secret credentials."""
    secret_dir = secret_dir or os.getenv("MONGO_SECRET_DIR", "/etc/secrets/mongo")
    username = _read_secret_file(f"{secret_dir}/username", "MONGO_USERNAME")
    password = _read_secret_file(f"{secret_dir}/password", "MONGO_PASSWORD")
    if not username or not password:
        raise EnvironmentError(
            "MongoDB credentials not found. Mount a secret at MONGO_SECRET_DIR "
            "or set MONGO_USERNAME / MONGO_PASSWORD."
        )
    mongo_host = os.getenv(
        "MONGO_HOST",
        "mas-mongo-ce-0.mas-mongo-ce-svc.mongoce.svc.cluster.local:27017,"
        "mas-mongo-ce-1.mas-mongo-ce-svc.mongoce.svc.cluster.local:27017,"
        "mas-mongo-ce-2.mas-mongo-ce-svc.mongoce.svc.cluster.local:27017",
    )
    mongo_tls          = os.getenv("MONGO_TLS", "true").lower() == "true"
    mongo_tls_insecure = os.getenv("MONGO_TLS_INSECURE", "true").lower() == "true"
    return MongoClient(
        f"mongodb://{username}:{password}@{mongo_host}",
        authMechanism="SCRAM-SHA-256",
        tlsInsecure=mongo_tls_insecure,
        authSource="admin",
        connectTimeoutMS=30000,
        socketTimeoutMS=30000,
        maxPoolSize=20,
        retryWrites=True,
        tls=mongo_tls,
    )


def get_collection(mongo_client: MongoClient):
    """Return the scaling_plans collection, creating it with an index if new."""
    db = mongo_client["mongo_scaling"]
    if COLLECTION_NAME not in db.list_collection_names():
        db.create_collection(COLLECTION_NAME)
        # Speed up the agent's pending-plan lookup
        db[COLLECTION_NAME].create_index([("status", 1), ("created_at", ASCENDING)])
        logger.info("Created '%s' collection with index.", COLLECTION_NAME)
    return db[COLLECTION_NAME]


# ---------------------------------------------------------------------------
# Writer API  (used by model-deployment)
# ---------------------------------------------------------------------------

def write_plan(
    collection,
    *,
    target_service: str,
    namespace: str,
    current_replicas: int,
    desired_replicas: int,
    confidence: float,
) -> str:
    """
    Insert a new scaling plan with status=pending.
    Returns the inserted document _id as a string.
    """
    if desired_replicas != current_replicas:
        decision = DECISION_SCALE
        reason   = REASON_SLO_VIOLATION
    else:
        decision = DECISION_NO_SCALE
        reason   = REASON_UNCHANGED

    doc = {
        "target_service":   target_service,
        "namespace":        namespace,
        "decision":         decision,
        "current_replicas": current_replicas,
        "desired_replicas": desired_replicas,
        "reason":           reason,
        "confidence":       round(confidence, 4),
        "status":           STATUS_PENDING,
        "created_at":       datetime.now(tz=timezone.utc),
        "applied_at":       None,
        "error":            None,
    }
    result = collection.insert_one(doc)
    logger.info(
        "Scaling plan written | service=%s ns=%s decision=%s "
        "current=%d desired=%d confidence=%.4f id=%s",
        target_service, namespace, decision,
        current_replicas, desired_replicas, confidence,
        result.inserted_id,
    )
    return str(result.inserted_id)


# ---------------------------------------------------------------------------
# Executor API  (used by autoscaler-agent)
# ---------------------------------------------------------------------------

def claim_pending_plan(collection):
    """
    Atomically find the oldest pending plan and mark it as 'applying'
    by setting status=applied temporarily (prevents double-execution if
    two agent instances ever run simultaneously).
    Returns the plan document or None if nothing is pending.
    """
    # Oldest-first: claim the plan that has waited longest, preventing starvation.
    plan = collection.find_one_and_update(
        {"status": STATUS_PENDING},
        {"$set": {"status": "applying"}},
        sort=[("created_at", ASCENDING)],
        return_document=True,
    )
    return plan


def mark_applied(collection, plan_id, applied_at: datetime | None = None):
    collection.update_one(
        {"_id": plan_id},
        {"$set": {
            "status":     STATUS_APPLIED,
            "applied_at": applied_at or datetime.now(tz=timezone.utc),
            "error":      None,
        }},
    )
    logger.info("Plan %s marked as applied.", plan_id)


def mark_skipped(collection, plan_id):
    collection.update_one(
        {"_id": plan_id},
        {"$set": {
            "status":     STATUS_SKIPPED,
            "applied_at": datetime.now(tz=timezone.utc),
            "error":      None,
        }},
    )
    logger.info("Plan %s marked as skipped (no replica change needed).", plan_id)


def mark_failed(collection, plan_id, error: str):
    collection.update_one(
        {"_id": plan_id},
        {"$set": {
            "status": STATUS_FAILED,
            "error":  error,
        }},
    )
    logger.warning("Plan %s marked as failed: %s", plan_id, error)
