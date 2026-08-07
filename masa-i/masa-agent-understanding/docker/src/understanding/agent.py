"""
agent.py — Understanding Agent
================================
Reads the active MSAgenticScaling CR from the Kubernetes API and writes a
``governance_context`` document to MongoDB.

The governance_context document is the shared contract between the
Understanding agent (producer) and the Planning / Perception agents (consumers).
It is re-written on every CronJob tick so that all agents always see the
latest desired behaviour as expressed in the CR spec.

Governance context schema (mongo_scaling.governance_context)
------------------------------------------------------------
{
  cr_name:               str   name of the MSAgenticScaling CR
  cr_namespace:          str   namespace of the CR
  microservice_namespace: str  namespace of the target Deployment
  microservice_deployment: str name of the target Deployment
  thanos_url:            str   resolved Thanos URL (may be empty if operator injects it)
  sla_strategy:          str   "Conservative" | "Normal" | "BestEffort"
  scaling_strategy:      str   "proactive" | "reactive" | "any"
  server_type:           str   e.g. "apache"
  startup_optimization:  bool
  scaling_type:          str   "horizontal" | "vertical" | "any"
  custom_metrics:        str   Prometheus job name
  data_monitoring_schedule: str  cron expression
  model_training_schedule:  str  cron expression
  qa_schedule:           str  cron expression
  updated_at:            datetime  UTC timestamp of this write
  schema_version:        int   incremented when schema changes
}
"""

import os
import logging
from datetime import datetime, timezone

from kubernetes import client, config
from pymongo import MongoClient

logger = logging.getLogger(__name__)

SCHEMA_VERSION   = 1
COLLECTION_NAME  = "governance_context"
CR_GROUP         = "scaling.masa.com"
CR_VERSION       = "v1alpha1"
CR_PLURAL        = "masautoscalings"


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
# MongoDB
# ---------------------------------------------------------------------------

def _connect_mongo() -> MongoClient:
    secret_dir = os.getenv("MONGO_SECRET_DIR", "/etc/secrets/mongo")
    username   = _read_secret_file(f"{secret_dir}/username", "MONGO_USERNAME")
    password   = _read_secret_file(f"{secret_dir}/password", "MONGO_PASSWORD")
    if not username or not password:
        raise EnvironmentError(
            "MongoDB credentials not found. Mount a secret at MONGO_SECRET_DIR "
            "or set MONGO_USERNAME / MONGO_PASSWORD."
        )
    mongo_host         = os.getenv(
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
        maxPoolSize=5,
        retryWrites=True,
        tls=mongo_tls,
    )


def _ensure_collection(db) -> object:
    """Return the governance_context collection, creating an index if new."""
    if COLLECTION_NAME not in db.list_collection_names():
        db.create_collection(COLLECTION_NAME)
        db[COLLECTION_NAME].create_index([("cr_name", 1), ("cr_namespace", 1)], unique=True)
        logger.info("Created '%s' collection with index.", COLLECTION_NAME)
    return db[COLLECTION_NAME]


# ---------------------------------------------------------------------------
# Kubernetes CR reader
# ---------------------------------------------------------------------------

def _load_k8s_client():
    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()  # fallback for local dev
    return client.CustomObjectsApi()


def _read_cr(cr_name: str, cr_namespace: str) -> dict:
    """Return the full spec dict of the MSAgenticScaling CR."""
    k8s = _load_k8s_client()
    cr  = k8s.get_namespaced_custom_object(
        group=CR_GROUP,
        version=CR_VERSION,
        namespace=cr_namespace,
        plural=CR_PLURAL,
        name=cr_name,
    )
    return cr.get("spec", {})


def _list_crs(cr_namespace: str) -> list:
    """Return all MSAgenticScaling CRs in the given namespace."""
    k8s   = _load_k8s_client()
    items = k8s.list_namespaced_custom_object(
        group=CR_GROUP,
        version=CR_VERSION,
        namespace=cr_namespace,
        plural=CR_PLURAL,
    )
    return items.get("items", [])


# ---------------------------------------------------------------------------
# Context extraction
# ---------------------------------------------------------------------------

def _build_context(cr_name: str, cr_namespace: str, spec: dict) -> dict:
    """Convert a CR spec into a flat governance_context document."""
    microservices = spec.get("microservices", [{}])
    ms            = microservices[0] if microservices else {}
    sla           = ms.get("sla", {})
    model_req     = ms.get("modelRequirements", {})
    server_req    = ms.get("serverRequirements", {})

    return {
        "cr_name":                cr_name,
        "cr_namespace":           cr_namespace,
        "microservice_namespace": spec.get(
            "microserviceNamespace", cr_namespace
        ),
        "microservice_deployment": spec.get(
            "microserviceDeployment", ""
        ),
        "thanos_url":             spec.get("thanosUrl", ""),
        # SLA
        "sla_strategy":           sla.get("strategy", "Conservative"),
        # Model requirements
        "scaling_strategy":       model_req.get("scalingStrategy", "any"),
        # Server / microservice requirements
        "server_type":            server_req.get("serverType", ""),
        "startup_optimization":   server_req.get("startupOptimization", False),
        "scaling_type":           server_req.get("scalingType", "horizontal"),
        "custom_metrics":         server_req.get("customMetrics", ""),
        # Schedules
        "data_monitoring_schedule": spec.get("dataMonitoringSchedule", "*/1 * * * *"),
        "model_training_schedule":  spec.get("modelTrainingSchedule",  "*/30 * * * *"),
        "qa_schedule":              spec.get("qaSchedule",              "*/5 * * * *"),
        # Bookkeeping
        "updated_at":     datetime.now(tz=timezone.utc),
        "schema_version": SCHEMA_VERSION,
    }


# ---------------------------------------------------------------------------
# Main Understanding logic
# ---------------------------------------------------------------------------

def run_understanding(cr_name: str, cr_namespace: str) -> int:
    """
    Read the CR spec and upsert a governance_context document in MongoDB.
    Returns 0 on success, 1 on failure.
    """
    logger.info(
        "Understanding agent run started for CR '%s/%s'", cr_namespace, cr_name
    )

    # ---- Read CR ----------------------------------------------------------------
    try:
        spec = _read_cr(cr_name, cr_namespace)
        logger.info("Read CR spec successfully.")
        logger.debug("CR spec: %s", spec)
    except Exception as e:
        logger.error("Failed to read CR '%s/%s': %s", cr_namespace, cr_name, e)
        return 1

    # ---- Build context ----------------------------------------------------------
    context = _build_context(cr_name, cr_namespace, spec)
    logger.info(
        "Built governance context: deployment=%s ns=%s sla=%s strategy=%s",
        context["microservice_deployment"],
        context["microservice_namespace"],
        context["sla_strategy"],
        context["scaling_strategy"],
    )

    # ---- Write to MongoDB -------------------------------------------------------
    try:
        mongo_client = _connect_mongo()
        db           = mongo_client["mongo_scaling"]
        collection   = _ensure_collection(db)

        # Upsert: one document per CR (identified by name+namespace)
        collection.update_one(
            {"cr_name": cr_name, "cr_namespace": cr_namespace},
            {"$set": context},
            upsert=True,
        )
        logger.info(
            "governance_context upserted for CR '%s/%s'.", cr_namespace, cr_name
        )
    except Exception as e:
        logger.error("Failed to write governance_context to MongoDB: %s", e)
        return 1
    finally:
        try:
            mongo_client.close()
        except Exception:
            pass

    logger.info("Understanding agent run completed.")
    return 0
