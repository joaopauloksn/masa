import sys
import logging
from datetime import datetime
import data_monitoring.monitor as monitor
import os

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

t_quantile = os.environ["T_QUANTILE"]
t_message_delivered_perc = os.environ["T_MESSAGE_DELIVERED_PERCENTAGE"]
NAMESPACE = os.environ["NAMESPACE"]
DEPLOYMENT = os.environ["DEPLOYMENT"]


def run_once():
    """
    Collect one round of metrics and persist to MongoDB, then exit.
    Designed to be invoked by a Kubernetes CronJob — no internal loop or timer.
    """
    logger.info("Data Monitoring run started at %s", datetime.now())

    ms = monitor.Microservice(
        name="mosquitto",
        job="mqtt-load-tester-metrics",
        target_percentage_delivered=t_message_delivered_perc,
        target_quantile_90=t_quantile,
        namespace=NAMESPACE,
        deployment=DEPLOYMENT,
    )

    time_series = ms.get_metrics()
    entry = ms.get_time_series_entry(time_series)
    monitor.persist_data(entry)

    logger.info("Entry persisted: %s", entry)
    logger.info("Data Monitoring run completed at %s", datetime.now())


if __name__ == "__main__":
    try:
        run_once()
    except Exception as e:
        logger.error("Data Monitoring run failed: %s", e)
        sys.exit(1)
