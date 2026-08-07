"""
task.py — model-deployment (Planning agent, prediction half)
=============================================================
Long-running Deployment that continuously reads the latest metrics from
MongoDB, runs the model, and writes a scaling plan every PREDICT_INTERVAL_SECONDS.

The actual Kubernetes scaling is performed exclusively by the autoscaler-agent;
this component only writes the intent (scaling plan) to MongoDB.
"""

import os
import sys
import signal
import logging
import time
from datetime import datetime
from model_deployment.predict import predict_and_write_plan

logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

MODEL_PATH             = os.getenv('MODEL_PATH', '/mnt/model/best_replica_predictor_model.h5')
PREDICT_INTERVAL_SECONDS = int(os.getenv('PREDICT_INTERVAL_SECONDS', '30'))

_running = True


def _shutdown(signum, frame):
    global _running
    logger.info("Received signal %s – shutting down model-deployment.", signum)
    _running = False


signal.signal(signal.SIGTERM, _shutdown)
signal.signal(signal.SIGINT, _shutdown)


def run():
    logger.info("Model Deployment component started at %s", datetime.now())

    while _running:
        if os.path.exists(MODEL_PATH):
            logger.info("Running prediction cycle...")
            try:
                predict_and_write_plan(MODEL_PATH)
            except Exception as e:
                logger.error("Prediction cycle failed: %s", e)
        else:
            logger.warning(
                "Model file not found at %s – waiting for training to complete.",
                MODEL_PATH,
            )

        # Interruptible sleep: check _running every second so SIGTERM is
        # acted on quickly without waiting a full PREDICT_INTERVAL_SECONDS.
        for _ in range(PREDICT_INTERVAL_SECONDS):
            if not _running:
                break
            time.sleep(1)

    logger.info("Model Deployment component stopped.")


if __name__ == "__main__":
    try:
        run()
    except Exception as e:
        logger.error("Model Deployment component crashed: %s", e)
        sys.exit(1)
