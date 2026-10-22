# Agent Reference

Each agent is a Docker image deployed by the MASA Operator. This document covers responsibilities, environment variables, and PVC file usage for every agent.

---

## Understanding Agent

**Folder:** `masa-agent-understanding/docker/`  
**Image:** `jpksn/masa-agent-understanding:1.0.1`
**Type:** Kubernetes `Job` (event-driven, triggered on CR creation or spec change)
**Entry point:** `src/task.py`

### Responsibility
Reads the active `MSAgenticScaling` CR from the Kubernetes API and upserts a `governance_context` document to MongoDB. This document is the shared source of truth for all other agents — they read desired state from here rather than relying solely on env vars.

### Environment variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `OPERATOR_NAMESPACE` | ✅ | — | Namespace where the CR lives |
| `CR_NAME` | ❌ | auto-discovered | Name of the CR to process; if omitted all CRs in the namespace are processed |
| `MONGO_SECRET_DIR` | ❌ | `/etc/secrets/mongo` | Mount path for MongoDB credentials secret |
| `MONGO_HOST` | ❌ | MAS replica set | MongoDB connection string |
| `MONGO_TLS` | ❌ | `true` | Enable TLS |
| `MONGO_TLS_INSECURE` | ❌ | `true` | Skip TLS certificate verification |

---

## Perception Agent

**Folder:** `masa-agent-perception/docker/`
**Image:** `jpksn/masa-agent-perception:1.0.1`
**Type:** Deployment (long-running, samples every `MONITOR_INTERVAL_SECONDS`)
**Entry point:** `src/task.py`

### Responsibility
Queries Thanos/Prometheus for real-time MQTT broker metrics, computes an SLA label, and writes one time-series document to `mongo_scaling.microservices` on every cycle. Runs as a long-running Deployment (not a CronJob) to enable sub-minute sampling intervals and a tighter feedback loop.

### SLA labels
| Label | Meaning |
|---|---|
| `1` | SLA breach — delivered % or latency below target |
| `2` | Acceptable — meeting but not exceeding target |
| `3` | Exceeding — significantly above target |

### Environment variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `MONGO_SECRET_DIR` | ❌ | `/etc/secrets/mongo` | MongoDB credentials mount path |
| `THANOS_SECRET_DIR` | ❌ | `/etc/secrets/thanos` | Thanos token mount path (projected ServiceAccount token) |
| `THANOS_URL` | ❌ | in-cluster fallback | Full Thanos query URL; operator injects this automatically |
| `MONGO_HOST` | ❌ | MAS replica set | MongoDB connection string |
| `NAMESPACE` | ❌ | operator namespace | Target microservice namespace |
| `DEPLOYMENT` | ❌ | — | Target Deployment name |
| `T_QUANTILE` | ❌ | `0.5` | Target latency quantile threshold (seconds) |
| `T_MESSAGE_DELIVERED_PERCENTAGE` | ❌ | `95` | Target delivery percentage |
| `MONITOR_INTERVAL_SECONDS` | ❌ | `15` | Seconds between collection cycles; set via `spec.dataMonitoringInterval` in the CR |

### Volumes
- `/etc/secrets/mongo` — Secret `mongo-credentials` (username, password)
- `/etc/secrets/thanos` — Projected ServiceAccount token (auto-rotates every hour)

---

## Planning Agent — Training

**Folder:** `masa-agent-planning/docker-training/`
**Image:** `jpksn/masa-agent-planning-training:1.0.1`
**Type:** CronJob `*/30 * * * *`, `concurrencyPolicy: Forbid`, `restartPolicy: Never`
**Entry point:** `src/task.py`

> **Note:** This CronJob is **suspended by default** (`spec.trainingEnabled: false`). It will not fire until you explicitly enable training. See the [Training workflow](./operator.md#training-workflow) section.

### Responsibility
Reads `label=2` rows from `mongo_scaling.microservices`, trains a small neural network (`total_messages → replica_count`), and saves the model to the shared PVC only when MSE improves.

### Environment variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `MONGO_SECRET_DIR` | ❌ | `/etc/secrets/mongo` | MongoDB credentials |
| `MONGO_HOST` | ❌ | MAS replica set | — |
| `MODEL_PATH` | ❌ | `/mnt/model/best_replica_predictor_model.h5` | Where to save/load the model |
| `BEST_MSE_FILE` | ❌ | `/mnt/model/best_mse.txt` | MSE baseline file |
| `SCALER_FILE` | ❌ | `/mnt/model/scaler.json` | Normalisation parameter file |

### PVC files written

| File | Content |
|---|---|
| `best_replica_predictor_model.h5` | Keras model (only written when MSE improves) |
| `scaler.json` | `{"max_total_msg": float}` — written alongside the model |
| `best_mse.txt` | Best MSE seen so far |

---

## Planning Agent — Prediction

**Folder:** `masa-agent-planning/docker/`  
**Image:** `jpksn/masa-agent-planning:1.0.1`  
**Type:** Deployment (long-running)  
**Entry point:** `src/task.py`

### Responsibility
Loads the trained model, reads the latest metrics row from MongoDB, predicts the desired replica count, and writes a `scaling_plan` to `mongo_scaling.scaling_plans`. Does **not** touch the Kubernetes API — the Action Agent executes the plan.

### Resolution order for target deployment/namespace
1. `governance_context` document from MongoDB (written by Understanding Agent)
2. `DEPLOYMENT_NAME` / `NAMESPACE` env vars (operator fallback)

### Environment variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `MONGO_SECRET_DIR` | ❌ | `/etc/secrets/mongo` | MongoDB credentials |
| `MODEL_PATH` | ❌ | `/mnt/model/best_replica_predictor_model.h5` | Trained model path |
| `SCALER_FILE` | ❌ | `/mnt/model/scaler.json` | Normalisation parameter path |
| `DEPLOYMENT_NAME` | ❌ | `default-deployment` | Fallback target deployment name |
| `NAMESPACE` | ❌ | `default-namespace` | Fallback target namespace |
| `PREDICT_INTERVAL_SECONDS` | ❌ | `30` | Seconds between prediction cycles |
| `IDLE_TOTAL_MSG_THRESHOLD` | ❌ | `1.0` | Throughput threshold (msgs/sec) below which system is considered idle and predicts 1 replica |

### PVC files read
- `best_replica_predictor_model.h5`
- `scaler.json`

---

## Action Agent

**Folder:** `masa-agent-action/docker/`  
**Image:** `jpksn/masa-agent-action:1.0.1`  
**Type:** Deployment (long-running)  
**Entry point:** `src/task.py`

### Responsibility
Polls `mongo_scaling.scaling_plans` for `pending` plans, atomically claims the oldest one (FIFO), applies the replica-count patch to the target Deployment via the Kubernetes API, and updates the plan status.

### Plan status lifecycle
```
pending  →  applying  →  applied   (success)
                      →  skipped   (live replicas already match desired)
                      →  failed    (K8s API error)
```

### Environment variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `MONGO_SECRET_DIR` | ❌ | `/etc/secrets/mongo` | MongoDB credentials |
| `MONGO_HOST` | ❌ | MAS replica set | — |
| `NAMESPACE` | ❌ | — | Target microservice namespace |
| `DEPLOYMENT_NAME` | ❌ | — | Target Deployment name |
| `POLL_INTERVAL_SECONDS` | ❌ | `10` | Sleep between polls when no plan is pending |
| `MIN_REPLICAS` | ❌ | `1` | Safety clamp — minimum allowed replicas |
| `MAX_REPLICAS` | ❌ | `20` | Safety clamp — maximum allowed replicas |

---

## Reflection Agent

**Folder:** `masa-agent-reflection/docker/`  
**Image:** `jpksn/masa-agent-reflection:1.0.1`  
**Type:** CronJob `*/5 * * * *`  
**Entry point:** `docker/task.py`

### Responsibility
Evaluates autoscaler quality over a rolling window, exposes Prometheus metrics, and writes a `model_feedback` document to MongoDB. Triggers a corrective model reset when quality degrades beyond a threshold for N consecutive runs.

### QA checks performed

| Check | What it measures |
|---|---|
| Post-scaling label ratio | Fraction of `label=1` rows observed after a scaling event |
| Rolling MAE | Mean absolute error between model predictions and actual replica counts at `label=2` rows |
| Model age | Seconds since `model.h5` was last modified |
| Consecutive failures | How many consecutive QA runs exceeded the label-1 ratio threshold |

### Corrective reset
If `consecutive_failures >= RESET_CONSECUTIVE_FAILURES`, the agent deletes `best_mse.txt` from the shared PVC. The next Planning-Training CronJob run will then unconditionally overwrite the stale model with whatever it trains.

### Environment variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `MONGO_SECRET_DIR` | ❌ | `/etc/secrets/mongo` | MongoDB credentials |
| `NAMESPACE` | ❌ | — | Target microservice namespace |
| `DEPLOYMENT_NAME` | ❌ | — | Target Deployment name |
| `MODEL_PATH` | ❌ | `/mnt/model/best_replica_predictor_model.h5` | — |
| `SCALER_FILE` | ❌ | `/mnt/model/scaler.json` | — |
| `BEST_MSE_FILE` | ❌ | `/mnt/model/best_mse.txt` | — |
| `QA_STATE_FILE` | ❌ | `/mnt/model/qa_state.json` | Consecutive failure counter persistence |
| `EVAL_WINDOW_MINUTES` | ❌ | `30` | How far back to look for scaling events |
| `POST_SCALING_ROWS` | ❌ | `5` | Rows after a scaling event to evaluate |
| `RESET_THRESHOLD` | ❌ | `0.3` | label-1 ratio above which a run is considered a failure |
| `RESET_CONSECUTIVE_FAILURES` | ❌ | `3` | Consecutive failures before model reset |
| `METRICS_PORT` | ❌ | `8000` | Prometheus metrics HTTP port |
| `METRICS_HOLD_SECONDS` | ❌ | `30` | How long to keep the metrics server alive after evaluation |
