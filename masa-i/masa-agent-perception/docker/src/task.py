"""
task.py — Perception Agent
==========================
Long-running Deployment that continuously collects metrics from Thanos/Prometheus,
computes an SLA label, and persists a time-series entry to MongoDB on every cycle.

Runs as a Deployment (not a CronJob) so it can sample at sub-minute intervals
and provide a tighter feedback loop for the planning and reflection agents.

Environment variables
---------------------
  MONITOR_INTERVAL_SECONDS   Seconds between collection cycles (default: 15)
  T_QUANTILE                 Target P90 latency threshold (default: 0.5)
  T_MESSAGE_DELIVERED_PERCENTAGE  Target delivery % (default: 95)
  NAMESPACE                  Target microservice namespace
  DEPLOYMENT                 Target microservice Deployment name
  MONGO_SECRET_DIR           Path to mounted MongoDB credentials (default: /etc/secrets/mongo)
  THANOS_SECRET_DIR          Path to mounted Thanos token (default: /etc/secrets/thanos)
  THANOS_URL                 Full Thanos query URL (operator injects this automatically)
"""

import sys
import signal
import logging
from datetime import datetime
import data_monitoring.monitor as monitor
import os
import time

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

T_QUANTILE                   = os.environ["T_QUANTILE"]
T_MESSAGE_DELIVERED_PERC     = os.environ["T_MESSAGE_DELIVERED_PERCENTAGE"]
NAMESPACE                    = os.environ["NAMESPACE"]
DEPLOYMENT                   = os.environ["DEPLOYMENT"]
MONITOR_INTERVAL_SECONDS     = int(os.getenv("MONITOR_INTERVAL_SECONDS", "15"))

_running = True


def _shutdown(signum, frame):
    global _running
    logger.info("Received signal %s – shutting down perception agent.", signum)
    _running = False


signal.signal(signal.SIGTERM, _shutdown)
signal.signal(signal.SIGINT, _shutdown)


def run_once():
    """Collect one round of metrics and persist to MongoDB."""

    ms = monitor.Microservice(
        name="mosquitto",
        job="mqtt-load-tester-metrics",
        target_percentage_delivered=T_MESSAGE_DELIVERED_PERC,
        target_quantile_90=T_QUANTILE,
        namespace=NAMESPACE,
        deployment=DEPLOYMENT,
    )

    time_series = ms.get_metrics()
    entry = ms.get_time_series_entry(time_series)
    monitor.persist_data(entry)

    label = entry.get("label")
    label_str = {1: "BREACH", 2: "OK", 3: "EXCEEDING"}.get(label, "UNKNOWN")

    def _fmt(v, decimals=3):
        return f"{v:.{decimals}f}" if v is not None else "N/A"

    logger.info(
        "┌─ Perception cycle ──────────────────────────────────────────\n"
        "│  deployment : %s/%s\n"
        "│  replicas   : %s\n"
        "├─ Metrics ───────────────────────────────────────────────────\n"
        "│  delivered %%: %s  (target: %s%%)\n"
        "│  undelivered: %s msg/s\n"
        "│  latency P90: %s s  (target: %s s)\n"
        "│  avg latency: %s s\n"
        "│  cpu        : %s cores\n"
        "│  memory     : %s MB\n"
        "├─ Label ─────────────────────────────────────────────────────\n"
        "│  label=%s  → %s\n"
        "└─────────────────────────────────────────────────────────────",
        NAMESPACE, DEPLOYMENT,
        entry.get("replicas", "N/A"),
        _fmt(entry.get("m_percentage__delivered"), 2), T_MESSAGE_DELIVERED_PERC,
        _fmt(entry.get("m_undelivered"), 4),
        _fmt(entry.get("m_quantile_90"), 4), T_QUANTILE,
        _fmt(entry.get("m_messages_perf"), 4),
        _fmt(entry.get("m_cpu"), 4),
        _fmt(entry.get("m_memory"), 1),
        label, label_str,
    )


def run():
    logger.info(
        "Perception Agent started – collecting every %ds (namespace=%s deployment=%s)",
        MONITOR_INTERVAL_SECONDS, NAMESPACE, DEPLOYMENT,
    )
    while _running:
        try:
            run_once()
        except Exception as e:
            logger.error("Data Monitoring cycle failed: %s", e)

        # Interruptible sleep so SIGTERM is acted on quickly.
        for _ in range(MONITOR_INTERVAL_SECONDS):
            if not _running:
                break
            time.sleep(1)

    logger.info("Perception Agent stopped.")


if __name__ == "__main__":
    try:
        run()
    except Exception as e:
        logger.error("Perception Agent crashed: %s", e)
        sys.exit(1)
