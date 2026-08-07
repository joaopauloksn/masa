import requests
import datetime
import os
import signal
import logging
from pymongo import MongoClient

# Disable SSL warnings for requests
requests.packages.urllib3.disable_warnings()

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Secret helpers – prefer mounted-secret files over env vars for passwords.
# ---------------------------------------------------------------------------

def _read_secret_file(path: str, fallback_env: str) -> str:
    """Return the contents of a mounted secret file, falling back to an env var."""
    try:
        with open(path) as f:
            value = f.read().strip()
            if value:
                return value
    except (OSError, IOError):
        pass
    return os.environ.get(fallback_env, "")


# MongoDB credentials – mounted via Secret volume at /etc/secrets/mongo
MONGO_SECRET_DIR = os.getenv("MONGO_SECRET_DIR", "/etc/secrets/mongo")
mongo_user     = _read_secret_file(f"{MONGO_SECRET_DIR}/username", "MONGO_USER")
mongo_password = _read_secret_file(f"{MONGO_SECRET_DIR}/password", "MONGO_PASSWORD")

# Thanos / Prometheus credentials
# Priority: mounted secret file → env var → auto-discovered via OpenShift route
THANOS_SECRET_DIR = os.getenv("THANOS_SECRET_DIR", "/etc/secrets/thanos")
thanos_token = _read_secret_file(f"{THANOS_SECRET_DIR}/token", "THANOS_TOKEN")

# Thanos URL: env var takes precedence (bypass mode), then auto-discover
thanos_base_url = os.getenv("THANOS_URL", "")
if not thanos_base_url:
    # Default to in-cluster Thanos Querier (no external route, lower latency).
    # Override by setting THANOS_URL or THANOS_ROUTE_HOST in the environment
    # (the operator sets THANOS_URL automatically from the discovered route).
    thanos_route_host = os.getenv("THANOS_ROUTE_HOST", "")
    if thanos_route_host:
        thanos_base_url = f"https://{thanos_route_host}/api/v1/query"
    else:
        thanos_base_url = "https://thanos-querier.openshift-monitoring.svc:9091/api/v1/query"
    logger.info("THANOS_URL not set – using auto-configured URL: %s", thanos_base_url)

if not mongo_user or not mongo_password:
    raise EnvironmentError(
        "MongoDB credentials not found. "
        "Mount a secret at MONGO_SECRET_DIR (/etc/secrets/mongo) with 'username' and 'password' keys, "
        "or set MONGO_USER / MONGO_PASSWORD environment variables."
    )

if not thanos_token:
    raise EnvironmentError(
        "Thanos token not found. "
        "Mount a secret at THANOS_SECRET_DIR (/etc/secrets/thanos) with a 'token' key, "
        "or set the THANOS_TOKEN environment variable."
    )

# MongoDB config
mongo_host = os.getenv(
    "MONGO_HOST",
    "mas-mongo-ce-0.mas-mongo-ce-svc.mongoce.svc.cluster.local:27017,"
    "mas-mongo-ce-1.mas-mongo-ce-svc.mongoce.svc.cluster.local:27017,"
    "mas-mongo-ce-2.mas-mongo-ce-svc.mongoce.svc.cluster.local:27017",
)
mongo_tls = os.getenv("MONGO_TLS", "true").lower() == "true"
mongo_tls_insecure = os.getenv("MONGO_TLS_INSECURE", "true").lower() == "true"

# MongoDB connection setup
mongo_uri = f"mongodb://{mongo_user}:{mongo_password}@{mongo_host}"
client = MongoClient(
    mongo_uri,
    authMechanism="SCRAM-SHA-256",
    tlsInsecure=mongo_tls_insecure,   # was tlsinsecure (wrong casing)
    authSource="admin",
    maxPoolSize=100,
    retryWrites=True,
    tls=mongo_tls,
)
mongodb = client["mongo_scaling"]

# Authorization headers for Thanos / Prometheus
headersAuth = {
    "Authorization": "Bearer " + thanos_token,
}


# ---------------------------------------------------------------------------
# Microservice class
# ---------------------------------------------------------------------------

class Microservice:
    def __init__(self, name, job, target_percentage_delivered, target_quantile_90, namespace, deployment):
        self.name = name
        self.job = job
        self.target_percentage_delivered = target_percentage_delivered
        self.target_quantile_90 = target_quantile_90
        self.namespace = namespace
        self.deployment = deployment

    def query_prometheus(self, query, query_name):
        try:
            response = requests.get(
                thanos_base_url,
                params={"query": query},
                verify=False,
                headers=headersAuth,
            )
            if response.status_code == 200:
                metric_result = response.json()
                if metric_result["status"] == "success":
                    if not metric_result["data"]["result"]:
                        logger.warning("No data for %s", query_name)
                        return None
                    return metric_result["data"]["result"]
                else:
                    logger.error("Prometheus error for %s: %s", query_name, metric_result["status"])
                    return None
            else:
                logger.error("HTTP %s querying %s", response.status_code, query_name)
                return None
        except requests.exceptions.RequestException as e:
            logger.error("Request error while querying Prometheus for %s: %s", query_name, e)
            return None

    def get_values(self, metrics):
        if metrics is not None:
            values = metrics[0]["value"]
            try:
                value = float(values[1])
                if value != value:  # NaN check
                    return None
                return value
            except (ValueError, IndexError):
                return None
        return None

    def get_metrics(self):
        time_series_data = {}

        time_series_data["m_percentage__delivered"] = self.get_percentage_delivered()
        time_series_data["m_undelivered"]            = self.get_undelivered_messages()
        time_series_data["m_delivered"]              = self.get_delivered_messages()
        time_series_data["m_quantile_90"]            = self.get_quantile_90()
        time_series_data["m_messages_perf"]          = self.get_message_performance()
        time_series_data["m_cpu"]                    = self.get_cpu_usage()
        time_series_data["m_memory"]                 = self.get_memory_usage()

        # Targets
        time_series_data["t_percentage__delivered"] = self.target_percentage_delivered
        time_series_data["t_quantile_90"]           = self.target_quantile_90
        time_series_data["replicas"]                = self.get_number_of_pods()

        return time_series_data

    def get_number_of_pods(self):
        query = (
            f'kube_deployment_status_replicas{{deployment="{self.deployment}",'
            f'namespace="{self.namespace}"}}'
        )
        result = self.query_prometheus(query, "number_of_pods")
        if result:
            pod_count = result[0]["value"][1] if result[0]["value"] else None
            if pod_count is not None:
                logger.info("Number of pods: %s", pod_count)
                return int(pod_count)
        logger.warning("Could not retrieve the number of pods for %s/%s.", self.namespace, self.deployment)
        return None

    def get_percentage_delivered(self):
        query = f"""
        (
            sum(rate(mqtt_delivered_messages_total{{job='{self.job}'}}[1m])) by (job)
            /
            (
                sum(rate(mqtt_delivered_messages_total{{job='{self.job}'}}[1m])) by (job)
                + sum(rate(mqtt_undelivered_messages_total{{job='{self.job}'}}[1m])) by (job)
            )
        ) * 100
        """
        results = self.query_prometheus(query, "percentage_delivered")
        return self.get_values(results)

    def get_undelivered_messages(self):
        query = f"sum(rate(mqtt_undelivered_messages_total{{job='{self.job}'}}[1m])) by (job)"
        results = self.query_prometheus(query, "undelivered_messages")
        return self.get_values(results)

    def get_delivered_messages(self):
        query = f"sum(rate(mqtt_delivered_messages_total{{job='{self.job}'}}[1m])) by (job)"
        results = self.query_prometheus(query, "delivered_messages")
        return self.get_values(results)

    def get_quantile_90(self):
        query = (
            f"histogram_quantile(0.90, sum(rate("
            f"mqtt_message_delivery_time_seconds_histogram_bucket{{job='{self.job}'}}[1m])) by (le))"
        )
        results = self.query_prometheus(query, "quantile_90")
        return self.get_values(results)

    def get_message_performance(self):
        query = f"""
        sum(rate(mqtt_message_delivery_time_seconds_summary_sum{{job='{self.job}'}}[1m]))
        /
        sum(rate(mqtt_message_delivery_time_seconds_summary_count{{job='{self.job}'}}[1m]))
        """
        results = self.query_prometheus(query, "message_performance")
        return self.get_values(results)

    def get_cpu_usage(self):
        query = f"""
        sum(
            rate(container_cpu_usage_seconds_total{{namespace="{self.namespace}", pod=~"{self.deployment}-.*", container!="POD"}}[1m])
        )
        """
        results = self.query_prometheus(query, "cpu_usage")
        return self.get_values(results)

    def get_memory_usage(self):
        query = f"""
        sum(
            container_memory_usage_bytes{{namespace="{self.namespace}", pod=~"{self.deployment}-.*", container!="POD"}}
        ) / 1048576
        """
        results = self.query_prometheus(query, "memory_usage")
        return self.get_values(results)

    def get_time_series_entry(self, time_series_value):
        entry = time_series_value
        entry["label"] = self.get_label(time_series_value)
        entry["metadata"] = {
            "name": self.name,
            "namespace": self.namespace,
            "deployment": self.deployment,
        }
        entry["timestamp"] = datetime.datetime.utcnow()
        return entry

    def get_label(self, time_series):
        # If there are no messages we always consider it achieved: 2
        if time_series["m_delivered"] is None and time_series["m_undelivered"] is None:
            return 2

        score = 0
        total_weight = 0

        weights = {
            "m_percentage__delivered": 3,
            "m_quantile_90": 3,
        }
        exceed_threshold = 1.05

        if time_series["m_percentage__delivered"] is not None and time_series["t_percentage__delivered"] is not None:
            delivered_percentage = min(float(time_series["m_percentage__delivered"]), 100)
            target_delivered = float(time_series["t_percentage__delivered"])

            if delivered_percentage >= exceed_threshold * target_delivered:
                score += weights["m_percentage__delivered"]
            elif delivered_percentage >= 0.9 * target_delivered:
                score += 0.75 * weights["m_percentage__delivered"]
            elif delivered_percentage >= 0.75 * target_delivered:
                score += 0.5 * weights["m_percentage__delivered"]

            total_weight += weights["m_percentage__delivered"]

        if time_series["m_quantile_90"] is not None and time_series["t_quantile_90"] is not None:
            quantile_90 = float(time_series["m_quantile_90"])
            target_quantile_90 = float(time_series["t_quantile_90"])

            if quantile_90 <= target_quantile_90 * 0.85:
                score += weights["m_quantile_90"]
            elif quantile_90 <= 1.1 * target_quantile_90:
                score += 0.75 * weights["m_quantile_90"]
            elif quantile_90 <= 1.25 * target_quantile_90:
                score += 0.5 * weights["m_quantile_90"]

            total_weight += weights["m_quantile_90"]

        performance_ratio = score / total_weight if total_weight > 0 else 0

        if performance_ratio >= 0.85:
            return 3
        elif 0.65 <= performance_ratio < 0.85:
            return 2
        else:
            return 1


# ---------------------------------------------------------------------------
# MongoDB persistence
# ---------------------------------------------------------------------------

def persist_data(entry):
    try:
        if "microservices" not in mongodb.list_collection_names():
            mongo_version = mongodb.command({"buildInfo": 1})["version"]
            if int(mongo_version[0]) >= 5:
                collection_opts = {
                    "timeField": "timestamp",
                    "metaField": "metadata",
                    "granularity": "seconds",
                }
                mongodb.create_collection(
                    "microservices", timeseries=collection_opts, expireAfterSeconds=21600
                )
            else:
                mongodb.create_collection("microservices")
                mongodb.microservices.ensure_index(
                    "timestamp", expireAfterSeconds=6 * 60 * 60
                )
        mongodb.microservices.insert_one(entry)
    except Exception as e:
        logger.error("Error inserting data into MongoDB: %s", e)


# ---------------------------------------------------------------------------
# Graceful shutdown – drain the MongoDB connection pool on SIGTERM / SIGINT
# ---------------------------------------------------------------------------

def _shutdown_handler(signum, frame):
    """Close the MongoDB connection cleanly when the pod receives SIGTERM."""
    logger.info("Received signal %s – closing MongoDB connection.", signum)
    try:
        client.close()
        logger.info("MongoDB connection closed.")
    except Exception as e:
        logger.warning("Error closing MongoDB connection: %s", e)
    raise SystemExit(0)

signal.signal(signal.SIGTERM, _shutdown_handler)
signal.signal(signal.SIGINT,  _shutdown_handler)
