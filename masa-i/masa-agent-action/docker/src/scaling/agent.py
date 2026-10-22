"""
agent.py — autoscaler-agent executor
======================================
Polls the MongoDB `scaling_plans` collection for pending plans written by
model-deployment, executes the Kubernetes scaling action, and updates the
plan status to applied / skipped / failed.

Separation of duties
--------------------
  model-deployment  →  writes plan (status=pending)
  autoscaler-agent  →  reads plan, applies K8s patch, updates status
"""

import os
import logging
from datetime import datetime, timezone

from kubernetes import client, config

logger = logging.getLogger(__name__)

MAX_REPLICAS      = int(os.getenv("MAX_REPLICAS",       "20"))
MIN_REPLICAS      = int(os.getenv("MIN_REPLICAS",       "1"))
# Skip scaling if the delta is smaller than this value — prevents constant
# ±1-replica churn from noisy predictions.
# Default is 1 (act on every replica change).  Set to 2 to add a deadband
# that ignores single-replica fluctuations (useful once the model is well-trained).
MIN_REPLICA_DELTA = int(os.getenv("MIN_REPLICA_DELTA",  "1"))


# ---------------------------------------------------------------------------
# Kubernetes helpers
# ---------------------------------------------------------------------------

def _k8s_api():
    config.load_incluster_config()
    return client.AppsV1Api()


def get_current_replicas(deployment_name: str, namespace: str) -> int | None:
    try:
        api = _k8s_api()
        deployment = api.read_namespaced_deployment(
            name=deployment_name, namespace=namespace
        )
        replicas = deployment.spec.replicas
        logger.debug("Current replicas for '%s/%s': %d", namespace, deployment_name, replicas)
        return replicas
    except client.exceptions.ApiException as e:
        logger.error("K8s API error reading replicas for '%s/%s': %s", namespace, deployment_name, e)
        return None
    except Exception as e:
        logger.error("Unexpected error reading replicas: %s", e)
        return None


def scale_to(deployment_name: str, namespace: str, desired_replicas: int) -> bool:
    """
    Patch the Deployment replica count.
    Clamps the value to [MIN_REPLICAS, MAX_REPLICAS].
    Returns True on success, False on failure.
    """
    clamped = max(MIN_REPLICAS, min(desired_replicas, MAX_REPLICAS))
    if clamped != desired_replicas:
        logger.warning(
            "Desired replicas %d clamped to %d (min=%d, max=%d)",
            desired_replicas, clamped, MIN_REPLICAS, MAX_REPLICAS,
        )
        desired_replicas = clamped

    try:
        api = _k8s_api()
        api.patch_namespaced_deployment_scale(
            name=deployment_name,
            namespace=namespace,
            body={"spec": {"replicas": desired_replicas}},
        )
        logger.info(
            "Scaled '%s/%s' to %d replicas.", namespace, deployment_name, desired_replicas
        )
        return True
    except client.exceptions.ApiException as e:
        logger.error(
            "K8s API error scaling '%s/%s': %s", namespace, deployment_name, e
        )
        return False
    except Exception as e:
        logger.error("Unexpected error scaling deployment: %s", e)
        return False


# ---------------------------------------------------------------------------
# Plan executor
# ---------------------------------------------------------------------------

def execute_pending_plan(plans_collection) -> bool:
    """
    Claim the oldest pending scaling plan, execute it, and update its status.
    Returns True if a plan was processed (regardless of outcome), False if none found.
    """
    # Import here to avoid circular imports; store.py lives in model-deployment
    # but is reachable because both images share the same Python path setup.
    # The autoscaler-agent image must include store.py at this import path.
    from model_deployment.store import (
        claim_pending_plan,
        mark_applied,
        mark_skipped,
        mark_failed,
    )

    plan = claim_pending_plan(plans_collection)
    if plan is None:
        logger.debug("No pending scaling plans.")
        return False

    plan_id          = plan["_id"]
    deployment_name  = plan.get("target_service", "")
    namespace        = plan.get("namespace", "")
    desired_replicas = plan.get("desired_replicas", 1)
    decision         = plan.get("decision", "")
    confidence       = plan.get("confidence", 0.0)
    current_in_plan  = plan.get("current_replicas", 0)

    logger.info(
        "Executing plan %s | service=%s ns=%s decision=%s "
        "current=%d desired=%d confidence=%.4f",
        plan_id, deployment_name, namespace, decision,
        current_in_plan, desired_replicas, confidence,
    )

    # Skip plans that require no change
    if decision == "no_scaling_needed":
        logger.info("Plan %s decision is no_scaling_needed – marking skipped.", plan_id)
        mark_skipped(plans_collection, plan_id)
        return True

    # Verify the current live replica count has not drifted since the plan was written
    live_replicas = get_current_replicas(deployment_name, namespace)
    if live_replicas is None:
        error_msg = f"Could not read live replicas for '{namespace}/{deployment_name}'"
        logger.error(error_msg)
        mark_failed(plans_collection, plan_id, error_msg)
        return True

    if live_replicas == desired_replicas:
        logger.info(
            "Plan %s skipped – live replicas already at %d (desired=%d).",
            plan_id, live_replicas, desired_replicas,
        )
        mark_skipped(plans_collection, plan_id)
        return True

    # Deadband: ignore changes smaller than MIN_REPLICA_DELTA to avoid
    # constant churn from model noise (e.g. 3→4→3 every few minutes).
    delta = abs(desired_replicas - live_replicas)
    if delta < MIN_REPLICA_DELTA:
        logger.info(
            "Plan %s skipped – delta %d < MIN_REPLICA_DELTA %d "
            "(live=%d, desired=%d).",
            plan_id, delta, MIN_REPLICA_DELTA, live_replicas, desired_replicas,
        )
        mark_skipped(plans_collection, plan_id)
        return True

    # Apply the scaling action
    success = scale_to(deployment_name, namespace, desired_replicas)
    if success:
        mark_applied(plans_collection, plan_id, applied_at=datetime.now(tz=timezone.utc))
    else:
        mark_failed(
            plans_collection, plan_id,
            error=f"scale_to({desired_replicas}) failed for '{namespace}/{deployment_name}'",
        )

    return True
