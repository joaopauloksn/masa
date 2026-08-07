"""
task.py — Understanding Agent entry point
==========================================
Runs as a Kubernetes CronJob. Reads the active MSAgenticScaling CR spec and
writes/updates the governance_context document in MongoDB so all other agents
share a consistent view of the desired state.

Environment variables
---------------------
Required:
  OPERATOR_NAMESPACE   Namespace where the MSAgenticScaling CR lives.
  CR_NAME              Name of the MSAgenticScaling CR to read.
                       Defaults to discovering all CRs in the namespace.

Optional:
  MONGO_SECRET_DIR     Mount path for MongoDB credentials secret (default: /etc/secrets/mongo)
  MONGO_HOST           MongoDB replica-set connection string
  MONGO_TLS            (default: true)
  MONGO_TLS_INSECURE   (default: true)
"""

import os
import sys
import logging
from datetime import datetime

from understanding.agent import run_understanding, _list_crs, _load_k8s_client

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

OPERATOR_NAMESPACE = os.getenv("OPERATOR_NAMESPACE", "")
CR_NAME            = os.getenv("CR_NAME", "")


def run_once():
    logger.info("Understanding Agent run started at %s", datetime.now())

    if not OPERATOR_NAMESPACE:
        logger.error(
            "OPERATOR_NAMESPACE env var is required. "
            "Set it to the namespace where MSAgenticScaling CRs live."
        )
        sys.exit(1)

    # ---- Resolve which CRs to process ----------------------------------------
    cr_names: list[str] = []

    if CR_NAME:
        cr_names = [CR_NAME]
        logger.info("Processing single CR: %s/%s", OPERATOR_NAMESPACE, CR_NAME)
    else:
        # Auto-discover all CRs in the operator namespace
        try:
            items = _list_crs(OPERATOR_NAMESPACE)
            cr_names = [item["metadata"]["name"] for item in items]
            logger.info(
                "Discovered %d CR(s) in namespace '%s': %s",
                len(cr_names), OPERATOR_NAMESPACE, cr_names,
            )
        except Exception as e:
            logger.error("Failed to list MSAgenticScaling CRs: %s", e)
            sys.exit(1)

    if not cr_names:
        logger.warning(
            "No MSAgenticScaling CRs found in namespace '%s'. "
            "Nothing to do — exiting.",
            OPERATOR_NAMESPACE,
        )
        sys.exit(0)

    # ---- Process each CR -------------------------------------------------------
    exit_code = 0
    for name in cr_names:
        rc = run_understanding(cr_name=name, cr_namespace=OPERATOR_NAMESPACE)
        if rc != 0:
            logger.error("run_understanding failed for CR '%s'.", name)
            exit_code = 1

    logger.info("Understanding Agent run completed at %s", datetime.now())
    sys.exit(exit_code)


if __name__ == "__main__":
    try:
        run_once()
    except Exception as e:
        logger.error("Understanding Agent crashed: %s", e)
        sys.exit(1)
