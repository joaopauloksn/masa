import sys
import logging
import numpy as np
from datetime import datetime
from model_training.training import run_training

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


def run_once():
    """
    Execute a single training iteration and exit.
    Designed to be invoked by a Kubernetes CronJob — no internal loop or sleep.

    The best_mse.txt file on the shared PVC carries the best MSE across runs,
    so there is no need to reset it at startup — each job simply reads the
    current best and overwrites it only when it improves.
    """
    logger.info("Model Development training run started at %s", datetime.now())

    final_mse, best_mse = run_training()

    if final_mse is None:
        logger.warning("Training could not start — insufficient data (< 50 records).")
        # Exit 0: not an error, just nothing to do yet. CronJob will retry on schedule.
        sys.exit(0)
    elif final_mse < best_mse:
        logger.info(
            "New best MSE achieved: %.4f (previous best: %.4f). Model updated.",
            final_mse, best_mse,
        )
    else:
        logger.info(
            "Training complete. Final MSE: %.4f, Best MSE: %.4f. No improvement.",
            final_mse, best_mse,
        )

    logger.info("Model Development run completed at %s", datetime.now())


if __name__ == "__main__":
    try:
        run_once()
    except Exception as e:
        logger.error("Model Development run failed: %s", e)
        sys.exit(1)
