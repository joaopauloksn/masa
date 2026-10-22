# MASA Dashboard

The MASA Dashboard is a lightweight Flask web app that provides a live view of system health, scaling activity, and model quality. It is deployed automatically by the operator as a Kubernetes Deployment and exposed via an OpenShift Route with TLS edge termination.

---

## What it shows

| Panel | Source collection | Description |
|---|---|---|
| Current Replicas | `microservices` | Live replica count of the target Deployment |
| SLA Label | `microservices` | Latest label (1=breach, 2=ok, 3=exceeding) |
| CPU / Memory | `microservices` | Summed resource usage across all pods |
| Delivered % | `microservices` | MQTT message delivery rate vs target |
| QA Verdict | `model_feedback` | Latest reflection-agent verdict (good/degraded/poor) |
| Label-1 Ratio | `model_feedback` | Post-scaling SLA breach rate |
| Model Age | `model_feedback` | Time since the model was last trained |
| Replicas chart | `microservices` | Replica count over the last 60 minutes |
| CPU & Memory chart | `microservices` | Resource usage over time |
| Delivered % chart | `microservices` | Message delivery rate over time |
| SLA Label chart | `microservices` | Label history over time |
| Recent Scaling Plans | `scaling_plans` | Last 20 plans with status (applied/skipped/failed/pending) |
| QA History | `model_feedback` | Last 10 QA runs with verdict and metrics |

The dashboard auto-refreshes every **15 seconds**.

---

## Accessing the dashboard

The operator creates an OpenShift Route with TLS edge termination. Get the URL with:

```bash
kubectl get route masautoscaling-sample-dashboard -n masa-system \
  -o jsonpath='{.spec.host}'
```

Then open `https://<host>` in your browser.

---

## Configuration

The dashboard reads data from MongoDB using the same `mongo-credentials` secret as the agents. The time window for charts and tables defaults to 60 minutes and can be adjusted via the `WINDOW_MINUTES` environment variable in the Deployment.

| Variable | Default | Description |
|---|---|---|
| `MONGO_SECRET_DIR` | `/etc/secrets/mongo` | MongoDB credentials mount path |
| `MONGO_HOST` | MAS replica set | MongoDB connection string |
| `MONGO_TLS` | `true` | Enable TLS |
| `MONGO_TLS_INSECURE` | `true` | Skip TLS certificate verification |
| `WINDOW_MINUTES` | `60` | How many minutes of history to display |
| `PORT` | `8080` | HTTP port the Flask app listens on |

---

## Building the image

```bash
cd masa-i
./build-and-push.sh agents   # includes masa-dashboard
# or just the dashboard:
docker buildx build --platform linux/amd64,linux/arm64 \
  --tag jpksn/masa-dashboard:1.0.1 --push \
  masa-dashboard/docker
```
