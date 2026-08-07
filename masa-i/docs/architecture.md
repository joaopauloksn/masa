# Architecture

## Overview

MASA uses a **multi-agent environment** pattern. Each agent has a single well-defined responsibility. Agents communicate exclusively through **MongoDB collections** — no direct inter-agent API calls exist. This decouples agents operationally: any agent can be restarted, redeployed or scaled independently.

```
┌─────────────────────────────────────────────────────────────────┐
│                    MSAgenticScaling CR                          │
│                  (Kubernetes Custom Resource)                   │
└───────────────────────────┬─────────────────────────────────────┘
                            │ reads spec
                            ▼
                 ┌─────────────────────┐
                 │  Understanding      │  CronJob  */1 * * * *
                 │  Agent              │  masa-agent-understanding
                 └──────────┬──────────┘
                            │ upserts
                            ▼
              ┌─────────────────────────┐
              │   governance_context    │  MongoDB collection
              └──────┬──────────────┬──┘
                     │ reads        │ reads
                     ▼              ▼
       ┌─────────────────┐    ┌─────────────────────┐
       │  Perception     │    │  Planning Agent      │  Deployment
       │  Agent          │    │  (prediction)        │  masa-agent-planning
       │  CronJob        │    └────────┬────────────-┘
       │  */1 * * * *    │             │ writes scaling_plan
       └────────┬────────┘             │
                │ writes               ▼
                ▼              ┌─────────────────────┐
       ┌────────────────┐      │   scaling_plans     │  MongoDB collection
       │  microservices │      └────────┬────────────┘
       │  (time-series) │               │ reads + claims
       └────────┬───────┘               ▼
                │ reads         ┌─────────────────────┐
                ▼               │  Action Agent       │  Deployment
       ┌─────────────────┐      │  masa-agent-action  │
       │  Planning       │      └────────┬────────────┘
       │  Training       │               │ patches K8s Deployment
       │  CronJob        │               ▼
       │  */30 * * * *   │      ┌─────────────────────┐
       └─────────────────┘      │  Target Microservice│
                                │  (replica count     │
                                │   updated)          │
                                └─────────────────────┘
                                         │
                         ┌───────────────┘
                         │ evaluates outcome
                         ▼
               ┌─────────────────────┐
               │  Reflection Agent   │  CronJob  */5 * * * *
               │  masa-agent-        │
               │  reflection         │
               └──────────┬──────────┘
                          │ writes
                          ▼
               ┌─────────────────────┐
               │   model_feedback    │  MongoDB collection
               └─────────────────────┘
```

---

## Agent responsibilities

### Understanding Agent
- **Image:** `jpksn/masa-agent-understanding:0.0.1`
- **Type:** CronJob (`*/1 * * * *`)
- Reads the `MSAgenticScaling` CR spec via the Kubernetes API
- Flattens it into a `governance_context` document in MongoDB
- All other agents read governance_context instead of relying solely on env vars

### Perception Agent
- **Image:** `jpksn/masa-agent-perception:0.0.1`
- **Type:** CronJob (`*/1 * * * *`, `concurrencyPolicy: Forbid`)
- Queries Thanos/Prometheus for real-time microservice metrics (messages delivered/undelivered, CPU, memory, replica count, quantile latency)
- Computes an SLA label (1=breach, 2=acceptable, 3=exceeding) for each observation
- Writes one time-series document per run to `mongo_scaling.microservices`

### Planning Agent — Training
- **Image:** `jpksn/masa-agent-planning-training:0.0.1`
- **Type:** CronJob (`*/30 * * * *`, `concurrencyPolicy: Forbid`, `restartPolicy: Never`)
- Reads `label=2` rows from `mongo_scaling.microservices`
- Trains a small neural network (`total_messages → replica_count`)
- Saves `best_replica_predictor_model.h5`, `scaler.json`, and `best_mse.txt` to the shared PVC only when the new model improves on the current best MSE

### Planning Agent — Prediction
- **Image:** `jpksn/masa-agent-planning:0.0.1`
- **Type:** Deployment (long-running, `PREDICT_INTERVAL_SECONDS=30`)
- Reads governance_context to resolve target deployment/namespace
- Loads the trained model from the shared PVC
- Runs a prediction on the latest metrics row
- Writes a `scaling_plan` document to `mongo_scaling.scaling_plans`

### Action Agent
- **Image:** `jpksn/masa-agent-action:0.0.1`
- **Type:** Deployment (long-running, polls every `POLL_INTERVAL_SECONDS=10`)
- Atomically claims the oldest `pending` scaling plan (FIFO, no double-execution)
- Patches the target Deployment's `spec.replicas` via the Kubernetes API
- Updates the plan status to `applied` / `skipped` / `failed`
- Clamps replica count to `[MIN_REPLICAS, MAX_REPLICAS]`

### Reflection Agent
- **Image:** `jpksn/masa-agent-reflection:0.0.1`
- **Type:** CronJob (`*/5 * * * *`)
- Evaluates post-scaling outcomes: what fraction of observations after a scaling event had `label=1` (SLA breach)?
- Computes rolling MAE between model predictions and actual replica counts
- If the label-1 ratio exceeds the reset threshold for N consecutive runs, deletes `best_mse.txt` to force the Planning Training agent to unconditionally retrain
- Writes a structured `model_feedback` verdict (`good` / `degraded` / `poor`) to MongoDB
- Exposes Prometheus metrics on port 8000 for 30 s after each run

---

## MongoDB collections

All collections live in the `mongo_scaling` database.

| Collection | Written by | Read by | Purpose |
|---|---|---|---|
| `microservices` | Perception | Planning-Training, Reflection | Time-series metrics + SLA labels |
| `governance_context` | Understanding | Planning-Prediction, Perception | Desired state from the CR spec |
| `scaling_plans` | Planning-Prediction | Action | Scaling intent; status lifecycle: `pending → applied/skipped/failed` |
| `model_feedback` | Reflection | (future Planning) | QA verdict for audit and feedback loop |

### `scaling_plans` status lifecycle

```
Planning writes → status: "pending"
Action claims  → status: "applying"   (atomic findAndModify, prevents double-exec)
Action done    → status: "applied"    (replica count changed)
               → status: "skipped"    (live count already equals desired)
               → status: "failed"     (K8s API error)
```

---

## Shared PVC files (`/mnt/model/`)

| File | Written by | Read by | Purpose |
|---|---|---|---|
| `best_replica_predictor_model.h5` | Planning-Training | Planning-Prediction, Reflection | Trained Keras model |
| `scaler.json` | Planning-Training | Planning-Prediction, Reflection | `{"max_total_msg": float}` — normalisation parameter |
| `best_mse.txt` | Planning-Training | Planning-Training | Best MSE baseline; deleted by Reflection to trigger forced retraining |
| `qa_state.json` | Reflection | Reflection | Consecutive failure counter across CronJob runs |
