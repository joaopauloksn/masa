"""
task.py — autoscaler-agent
============================
Long-running Deployment that polls MongoDB for pending scaling plans written
by model-deployment and applies them to the target Kubernetes Deployment.

Loop behaviour
--------------
- On each tick: claim the oldest pending plan, execute it, update its status.
- If no plan is pending: sleep POLL_INTERVAL_SECONDS before checking again.
- Plans execute one at a time (no concurrent scaling).
"""

import os
import sys
import signal
import logging
import time
from datetime import datetime

from model_deployment.store import (
    connect as store_connect,
    get_collection as store_get_collection,
)
from scaling.agent import execute_pending_plan

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "10"))

_running = True


def _shutdown(signum, frame):
    global _running
    logger.info("Received signal %s – shutting down autoscaler-agent.", signum)
    _running = False


signal.signal(signal.SIGTERM, _shutdown)
signal.signal(signal.SIGINT,  _shutdown)


def run():
    logger.info("Autoscaler agent started at %s", datetime.now())

    try:
        mongo_client     = store_connect()
        plans_collection = store_get_collection(mongo_client)
        logger.info("Connected to MongoDB scaling_plans collection.")
    except Exception as e:
        logger.error("Cannot connect to MongoDB: %s", e)
        sys.exit(1)

    while _running:
        try:
            processed = execute_pending_plan(plans_collection)
            if not processed:
                # Nothing pending — wait before polling again
                time.sleep(POLL_INTERVAL_SECONDS)
            # If a plan was processed, immediately check for the next one
            # (back-to-back plans execute without sleeping)
        except Exception as e:
            logger.error("Unexpected error executing plan: %s", e)
            time.sleep(POLL_INTERVAL_SECONDS)

    logger.info("Autoscaler agent stopped.")
    try:
        mongo_client.close()
    except Exception:
        pass


if __name__ == "__main__":
    run()
