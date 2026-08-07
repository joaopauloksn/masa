# masa-quality-assurance

Runs as a **Kubernetes CronJob (every 5 minutes by default)** alongside the rest of the MS-DDA autoscaler pipeline.

Its single responsibility is to answer the question the rest of the system never asks:

> **Is the autoscaler actually working?**

---

## Where it sits in the pipeline

```
Thanos / Prometheus
       │  (metrics every 1 min)
       ▼
data-monitoring ──► MongoDB (time-series rows: label 1/2/3, replicas, delivered%, p90…)
                                  │
              ┌───────────────────┴─────────────────────┐
              │ (every 30 min)                           │ (every 5 sec)
              ▼                                          ▼
    model-development                          model-deployment
    trains NN on label=2 rows                  reads latest Mongo row
    saves model.h5 + scaler.json               predicts replicas → scales Deployment
    to shared PVC
              │
              │  (every 5 min)
              ▼
    quality-assurance   ◄────────────── shared PVC (model.h5, scaler.json, best_mse.txt)
    reads MongoDB                       reads MongoDB
    evaluates outcomes                  exposes Prometheus metrics
```

### Label meaning (set by `data-monitoring`)

| Label | Meaning |
|-------|---------|
| `1`   | Poor — SLA breached (delivery % or p90 latency below target) |
| `2`   | Achieved — operating within SLA at the current replica count |
| `3`   | Exceeds — comfortably above all targets |

The model is **trained only on label=2 rows** — it learns which replica count produces the "just right" outcome. Quality Assurance measures whether that mapping still holds in production.

---

## What it checks

### 1 — Post-scaling label check

After the autoscaler changes the replica count (a scaling event), the next few time-series rows recorded by `data-monitoring` should be label=2 or label=3. If they are label=1, the scaling decision made things worse.

QA detects every row where `replicas` changed compared to the previous row, then looks at the following `POST_SCALING_ROWS` rows (default: 5) and counts how many are label=1.

**Prometheus metric:** `masa_post_scaling_label1_ratio{deployment, namespace}`

A value near `0` means the autoscaler is working correctly. A value above `0.3` (the default `RESET_THRESHOLD`) triggers the consecutive-failure counter.

---

### 2 — Rolling MAE (model drift indicator)

Even if the model had low MSE when it was trained, traffic patterns change over time and the model can silently drift. QA catches this by replaying the model's prediction logic against recent label=2 MongoDB rows:

1. For each label=2 row, compute `total_msg = m_delivered + m_undelivered`
2. Normalise using `scaler.json` (the same max value used during training)
3. Run the loaded `model.h5` → get predicted replica count
4. Compare against the actual replica count that produced label=2
5. Average the absolute errors → **rolling MAE**

A rising MAE means the model needs retraining, even if the MSE on the training set was fine.

**Prometheus metric:** `masa_rolling_mae{deployment, namespace}`

---

### 3 — Model age

Reads `os.path.getmtime(model.h5)` from the shared PVC and reports how many seconds have passed since the model was last written by `model-development`.

**Prometheus metric:** `masa_model_age_seconds{deployment, namespace}`

Useful for alerting when no new model has been produced in a long time (e.g. because `model-development` is failing silently or there is insufficient data).

---

### 4 — Corrective reset

QA is the only component with the authority to force the pipeline to retrain from scratch.

The consecutive-failure counter is persisted in `qa_state.json` on the shared PVC so it survives between CronJob runs. When `label1_ratio > RESET_THRESHOLD` for `RESET_CONSECUTIVE_FAILURES` consecutive runs (default: 3), QA:

1. Deletes `best_mse.txt` from the shared PVC
2. Resets the counter to 0
3. Logs a `WARNING: MODEL RESET` message

The next `model-development` CronJob run reads `load_best_mse()`, finds no file, returns `inf` as the baseline, and **unconditionally saves whatever model it trains** — overwriting the stale one.

**Prometheus metric:** `masa_consecutive_qa_failures{deployment, namespace}`

---

## Prometheus metrics summary

| Metric | Type | Meaning | Alert suggestion |
|--------|------|---------|-----------------|
| `masa_post_scaling_label1_ratio` | Gauge | Fraction of post-scaling rows with SLA breach | `> 0.3 for 15m` |
| `masa_rolling_mae` | Gauge | Mean absolute error between predicted and actual replicas on label=2 data | `> 1.5 for 30m` |
| `masa_model_age_seconds` | Gauge | Seconds since model.h5 was last written | `> 86400 for 5m` |
| `masa_consecutive_qa_failures` | Gauge | Consecutive QA runs above `RESET_THRESHOLD` | `>= RESET_CONSECUTIVE_FAILURES` |

Metrics are exposed on **`:8000/metrics`** for `METRICS_HOLD_SECONDS` (default: 30) after evaluation completes, then the pod exits. A Kubernetes `Service` (`masa-quality-assurance-metrics`) with `selector: app: masa-quality-assurance` is created by the operator so Prometheus can always find the pod while it is alive.

### Example Prometheus alert rules

```yaml
groups:
- name: masa-autoscaler
  rules:
  - alert: AutoscalerMakingThingsWorse
    expr: masa_post_scaling_label1_ratio > 0.3
    for: 15m
    annotations:
      summary: "Autoscaler scaling decisions are causing SLA breaches"

  - alert: ModelDrifting
    expr: masa_rolling_mae > 1.5
    for: 30m
    annotations:
      summary: "Autoscaler model MAE is high — consider triggering retraining"

  - alert: ModelStale
    expr: masa_model_age_seconds > 86400
    for: 5m
    annotations:
      summary: "No new model produced in the last 24 hours"
```

---

## Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `MONGO_SECRET_DIR` | `/etc/secrets/mongo` | Directory containing `username` and `password` files from a mounted Kubernetes Secret |
| `MONGO_HOST` | MAS mongo replica set | Full MongoDB connection string |
| `MONGO_TLS` | `true` | Enable TLS for MongoDB |
| `MONGO_TLS_INSECURE` | `true` | Skip TLS certificate validation |
| `MODEL_PATH` | `/mnt/model/best_replica_predictor_model.h5` | Path to the trained model on the shared PVC |
| `SCALER_FILE` | `/mnt/model/scaler.json` | Path to the normalisation parameters written by `model-development` |
| `BEST_MSE_FILE` | `/mnt/model/best_mse.txt` | Path to the best MSE tracker. QA deletes this to trigger a forced model reset |
| `QA_STATE_FILE` | `/mnt/model/qa_state.json` | Persists the consecutive-failure counter between CronJob runs |
| `DEPLOYMENT_NAME` | _(empty)_ | Name of the monitored microservice Deployment (set by operator) |
| `NAMESPACE` | _(empty)_ | Namespace of the monitored microservice (set by operator) |
| `EVAL_WINDOW_MINUTES` | `30` | How far back to look in MongoDB for scaling events |
| `POST_SCALING_ROWS` | `5` | How many rows after a scaling event to evaluate |
| `RESET_THRESHOLD` | `0.3` | `label1_ratio` above which a run is counted as a failure |
| `RESET_CONSECUTIVE_FAILURES` | `3` | Consecutive failures before a model reset is triggered |
| `METRICS_PORT` | `8000` | Port for the Prometheus `/metrics` HTTP server |
| `METRICS_HOLD_SECONDS` | `30` | Seconds the metrics server stays up before the pod exits |

---

## CronJob schedule

The default schedule is `*/5 * * * *` (every 5 minutes). Override it by setting `spec.qaSchedule` in the `MSAgenticScaling` CR:

```yaml
apiVersion: scaling.masa.com/v1alpha1
kind: MSAgenticScaling
metadata:
  name: masautoscaling-sample
spec:
  qaSchedule: "*/10 * * * *"   # evaluate every 10 minutes instead
  ...
```

---

## Shared PVC file layout

All components share the same PVC mounted at `/mnt/model`:

```
/mnt/model/
├── best_replica_predictor_model.h5   written by model-development, read by model-deployment + QA
├── scaler.json                       written by model-development, read by model-deployment + QA
├── best_mse.txt                      written by model-development, deleted by QA on corrective reset
└── qa_state.json                     written and read only by quality-assurance
```
