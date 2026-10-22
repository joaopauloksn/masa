#!/usr/bin/env python3
"""
collect_metrics.py — Extract Evaluation Metrics from Thanos & MongoDB for HPA vs MASA Analysis
=============================================================================================
This script exports time-series metrics from OpenShift Thanos Querier and/or MongoDB
for evaluating MASA vs HPA over a specified time window.

Usage:
  python collect_metrics.py --hours 2 --source both --output hpa_vs_masa_evaluation.csv

Prerequisites:
  - Logged into OpenShift: `oc login ...`
  - Required packages: pip install requests pandas pymongo urllib3
"""

import argparse
import logging
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests
import urllib3

# Disable insecure HTTPS warnings for Thanos / OpenShift internal certs
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 1. Thanos / Prometheus Extractor
# ---------------------------------------------------------------------------
def get_oc_thanos_credentials(thanos_url_override: str = None, token_override: str = None):
    """Retrieve the OAuth token and Thanos route hostname via `oc` CLI or parameters."""
    token = token_override or os.getenv("THANOS_TOKEN")
    if not token:
        try:
            token = subprocess.check_output(["oc", "whoami", "--show-token"]).decode().strip()
        except Exception as e:
            logger.error("Failed to get oc token automatically via 'oc whoami --show-token': %s", e)
            return None, None

    if thanos_url_override:
        thanos_url = thanos_url_override.rstrip("/")
        if not thanos_url.endswith("/api/v1/query_range"):
            if thanos_url.endswith("/api/v1/query"):
                thanos_url = thanos_url.replace("/api/v1/query", "/api/v1/query_range")
            else:
                thanos_url = f"{thanos_url}/api/v1/query_range"
        return thanos_url, token

    route_host = os.getenv("THANOS_HOST")
    if not route_host:
        try:
            route_host = (
                subprocess.check_output(
                    [
                        "oc",
                        "get",
                        "route",
                        "thanos-querier",
                        "-n",
                        "openshift-monitoring",
                        "-o",
                        "jsonpath={.spec.host}",
                    ]
                )
                .decode()
                .strip()
            )
        except Exception as e:
            logger.warning("Could not auto-discover Thanos route: %s. Using default in-cluster fallback.", e)
            route_host = "thanos-querier-openshift-monitoring.apps.idp91-m1.cp.fyre.ibm.com"

    thanos_url = f"https://{route_host}/api/v1/query_range"
    return thanos_url, token


def fetch_from_thanos(
    namespace: str,
    deployment: str,
    metrics_job: str,
    start_time: datetime,
    end_time: datetime,
    step: str = "15s",
    thanos_url: str = None,
    token: str = None,
) -> pd.DataFrame:
    """Query Thanos for all SLA and infrastructure metrics in the specified range."""
    thanos_url, token = get_oc_thanos_credentials(thanos_url_override=thanos_url, token_override=token)
    if not thanos_url or not token:
        logger.error("Thanos credentials/URL unavailable. Skipping Thanos collection.")
        return pd.DataFrame()

    headers = {"Authorization": f"Bearer {token}"}

    queries = {
        "replicas": f'kube_deployment_status_replicas{{namespace="{namespace}", deployment="{deployment}"}}',
        "memory_mib": f'sum(container_memory_usage_bytes{{namespace="{namespace}", pod=~"{deployment}-.*", container!="POD"}}) / 1048576',
        "memory_working_set_mib": f'sum(container_memory_working_set_bytes{{namespace="{namespace}", pod=~"{deployment}-.*", container!="POD"}}) / 1048576',
        "cpu_cores": f'sum(rate(container_cpu_usage_seconds_total{{namespace="{namespace}", pod=~"{deployment}-.*", container!="POD"}}[1m]))',
        "p90_latency_s": f'histogram_quantile(0.90, sum(rate(mqtt_message_delivery_time_seconds_histogram_bucket{{job="{metrics_job}"}}[1m])) by (le))',
        "delivery_pct": f'(sum(rate(mqtt_delivered_messages_total{{job="{metrics_job}"}}[1m])) / (sum(rate(mqtt_delivered_messages_total{{job="{metrics_job}"}}[1m])) + sum(rate(mqtt_undelivered_messages_total{{job="{metrics_job}"}}[1m])))) * 100',
        "delivered_rate": f'sum(rate(mqtt_delivered_messages_total{{job="{metrics_job}"}}[1m]))',
        "undelivered_rate": f'sum(rate(mqtt_undelivered_messages_total{{job="{metrics_job}"}}[1m]))',
    }

    start_epoch = int(start_time.timestamp())
    end_epoch = int(end_time.timestamp())

    dataframes = {}
    logger.info("Querying Thanos (%s) from %s to %s (step=%s)...", thanos_url, start_time, end_time, step)

    for metric_name, query_str in queries.items():
        try:
            resp = requests.get(
                thanos_url,
                headers=headers,
                params={
                    "query": query_str,
                    "start": start_epoch,
                    "end": end_epoch,
                    "step": step,
                },
                verify=False,
                timeout=30,
            )
            if resp.status_code != 200:
                logger.warning("HTTP %d querying %s: %s", resp.status_code, metric_name, resp.text[:200])
                continue

            res = resp.json()
            if res.get("status") == "success" and res.get("data", {}).get("result"):
                values = res["data"]["result"][0]["values"]
                df_metric = pd.DataFrame(values, columns=["timestamp", metric_name])
                df_metric["timestamp"] = pd.to_datetime(df_metric["timestamp"], unit="s", utc=True)
                df_metric[metric_name] = pd.to_numeric(df_metric[metric_name], errors="coerce")
                dataframes[metric_name] = df_metric.set_index("timestamp")
            else:
                logger.warning("No data returned for metric '%s'", metric_name)
        except Exception as e:
            logger.error("Error querying Thanos for '%s': %s", metric_name, e)

    if not dataframes:
        return pd.DataFrame()

    merged = pd.concat(dataframes.values(), axis=1).sort_index().reset_index()
    logger.info("Successfully fetched %d rows from Thanos.", len(merged))
    return merged


# ---------------------------------------------------------------------------
# 2. MongoDB Extractor (MASA perception data & decisions)
# ---------------------------------------------------------------------------
def fetch_from_mongo(
    start_time: datetime,
    mongo_host: str = "localhost:27017",
    mongo_user: str = None,
    mongo_pass: str = None,
) -> pd.DataFrame:
    """Connect to MongoDB and extract the perception time-series dataset."""
    mongo_user = mongo_user or os.getenv("MONGO_USERNAME") or os.getenv("MONGO_USER")
    mongo_pass = mongo_pass or os.getenv("MONGO_PASSWORD")

    if not mongo_user or not mongo_pass:
        logger.warning("MONGO_USERNAME / MONGO_PASSWORD not supplied. Skipping MongoDB collection.")
        return pd.DataFrame()

    from pymongo import MongoClient

    mongo_tls = os.getenv("MONGO_TLS", "false").lower() == "true"
    mongo_tls_insecure = os.getenv("MONGO_TLS_INSECURE", "true").lower() == "true"
    uri = f"mongodb://{mongo_user}:{mongo_pass}@{mongo_host}"

    logger.info("Connecting to MongoDB at %s...", mongo_host)
    try:
        client = MongoClient(
            uri,
            authMechanism="SCRAM-SHA-256",
            tls=mongo_tls,
            tlsInsecure=mongo_tls_insecure,
            authSource="admin",
            connectTimeoutMS=10000,
        )
        db = client["mongo_scaling"]
        cursor = db["microservices"].find(
            {"timestamp": {"$gte": start_time}},
            {
                "timestamp": 1,
                "replicas": 1,
                "m_memory": 1,
                "m_cpu": 1,
                "m_delivered": 1,
                "m_undelivered": 1,
                "m_percentage__delivered": 1,
                "m_quantile_90": 1,
                "label": 1,
            },
        ).sort("timestamp", 1)

        records = list(cursor)
        client.close()

        if not records:
            logger.warning("No records found in MongoDB for the given time range.")
            return pd.DataFrame()

        df = pd.DataFrame(records)
        df.drop(columns=["_id"], inplace=True, errors="ignore")
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
        logger.info("Retrieved %d records from MongoDB.", len(df))
        return df

    except Exception as e:
        logger.error("Failed to query MongoDB: %s", e)
        return pd.DataFrame()


# ---------------------------------------------------------------------------
# Main CLI
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Collect MASA / HPA metrics for comparison")
    parser.add_argument("--hours", type=float, default=2.0, help="Number of hours back to extract (default: 2)")
    parser.add_argument("--start", type=str, default=None, help="Start time ISO string (e.g. 2026-09-25T09:33:00Z)")
    parser.add_argument("--end", type=str, default=None, help="End time ISO string (e.g. 2026-09-25T10:04:00Z)")
    parser.add_argument("--namespace", type=str, default="iotoccupancy", help="Target microservice namespace")
    parser.add_argument("--deployment", type=str, default="iotoccupancy-sample-mosquitto", help="Target deployment")
    parser.add_argument("--job", type=str, default="mqtt-load-tester-metrics", help="Prometheus metric job name")
    parser.add_argument("--source", choices=["thanos", "mongo", "both"], default="thanos", help="Data source to query")
    parser.add_argument("--output", type=str, default="metrics_last_2h.csv", help="Output CSV path")
    parser.add_argument("--thanos-url", type=str, default=None, help="Thanos base URL or endpoint override")
    parser.add_argument("--token", type=str, default=None, help="OAuth token override for Thanos API")
    parser.add_argument("--mongo-host", type=str, default="localhost:27017", help="MongoDB host for local port-forward")

    args = parser.parse_args()

    if args.end:
        end_time = datetime.fromisoformat(args.end.replace("Z", "+00:00"))
    else:
        end_time = datetime.now(timezone.utc)

    if args.start:
        start_time = datetime.fromisoformat(args.start.replace("Z", "+00:00"))
    else:
        start_time = end_time - timedelta(hours=args.hours)

    logger.info("Collecting metrics for the last %.1f hour(s) [%s to %s]...", args.hours, start_time, end_time)

    df_thanos = pd.DataFrame()
    df_mongo = pd.DataFrame()

    if args.source in ["thanos", "both"]:
        df_thanos = fetch_from_thanos(
            namespace=args.namespace,
            deployment=args.deployment,
            metrics_job=args.job,
            start_time=start_time,
            end_time=end_time,
            thanos_url=args.thanos_url,
            token=args.token,
        )

    if args.source in ["mongo", "both"]:
        df_mongo = fetch_from_mongo(
            start_time=start_time,
            mongo_host=args.mongo_host,
        )

    # Save outputs
    if not df_thanos.empty:
        df_thanos.to_csv(args.output, index=False)
        logger.info("Saved Thanos metrics to: %s", args.output)

    if not df_mongo.empty:
        mongo_out = args.output.replace(".csv", "_mongo.csv") if args.source == "both" else args.output
        df_mongo.to_csv(mongo_out, index=False)
        logger.info("Saved MongoDB metrics to: %s", mongo_out)

    if df_thanos.empty and df_mongo.empty:
        logger.error("No metrics were collected. Please check cluster connectivity and authentication.")
        sys.exit(1)


if __name__ == "__main__":
    main()
