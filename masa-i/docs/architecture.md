# Architecture

## Overview

MASA uses an **agentic multi-agent environment** pattern. Each agent has a single well-defined responsibility. Agents communicate exclusively through **MongoDB collections** — no direct inter-agent API calls exist. This decouples agents operationally: any agent can be restarted, redeployed or scaled independently.

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
       │  Deployment     │    └────────┬────────────-┘
       │  (15s tick)     │             │ writes scaling_plan
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
- **Image:** `jpksn/masa-agent-understanding:1.0.1`
- **Type:** Job / Event-driven
- Reads the `MSAgenticScaling` CR spec via the Kubernetes API
- Flattens it into a `governance_context` document in MongoDB
- All other agents read `governance_context` dynamically instead of relying solely on env vars

### Perception Agent
- **Image:** `jpksn/masa-agent-perception:1.0.1`
- **Type:** Deployment (Continuous monitoring, `MONITOR_INTERVAL_SECONDS=15`)
- Queries OpenShift Thanos Querier for real-time microservice metrics (delivered/undelivered throughput, CPU cores, memory RSS, replica count, P90 latency)
- Computes an SLA label (1=breach, 2=acceptable, 3=exceeding) for each observation
- Writes time-series documents to `mongo_scaling.microservices`

### Planning Agent — Training
- **Image:** `jpksn/masa-agent-planning-training:1.0.2` (and offline pipeline `train_offline.py`)
- **Type:** CronJob (`*/30 * * * *`, `concurrencyPolicy: Forbid`, `restartPolicy: Never`)
- Uses label-adjusted target mapping: `label=1` ($\text{rep}+1$), `label=2` ($\text{rep}$), `label=3` ($\max(1, \text{rep}-1)$)
- Trains a 4-feature neural network: `[total_msg, pct_delivered_norm, latency_ratio, undelivered_norm]`
- Saves `best_replica_predictor_model.h5`, `scaler.json`, and `best_mse.txt` to the shared `/mnt/model/` PVC when the new model improves on the best historical MSE

### Planning Agent — Prediction
- **Image:** `jpksn/masa-agent-planning:1.0.2`
- **Type:** Deployment (long-running, `PREDICT_INTERVAL_SECONDS=30`)
- Reads `governance_context` to resolve target deployment/namespace
- Hot-reloads the trained model and scaler from `/mnt/model/`
- Executes 4-feature neural network evaluation with zero-load bypass ($\le 5\text{ msg/s}$) and low-load calibration ($\le 1.20 \rightarrow 1\text{ pod}$)
- Writes scaling proposals to `mongo_scaling.scaling_plans`

### Action Agent
- **Image:** `jpksn/masa-agent-action:1.0.1`
- **Type:** Deployment (long-running, polls every `POLL_INTERVAL_SECONDS=10`)
- Atomically claims the oldest `pending` scaling plan (FIFO, no double-execution)
- Enforces governance rules (scale-down cooldown, scale-up priority, boundaries `[minReplicas, maxReplicas]`)
- Patches the target Deployment's `spec.replicas` via the Kubernetes API
- Updates the plan status to `applied` / `skipped` / `failed`

### Reflection Agent
- **Image:** `jpksn/masa-agent-reflection:1.0.3`
- **Type:** CronJob (`*/5 * * * *`)
- Evaluates post-scaling outcomes ($N=5$ observations following scale-up vs scale-down)
- Computes rolling MAE against empirical ground truth
- If the label-1 ratio exceeds 0.30 for 3 consecutive runs, deletes `/mnt/model/best_mse.txt` to reset the quality gate and force unconditional model replacement on the next training cycle
- Writes structured evaluation summaries to `mongo_scaling.model_feedback` and `/mnt/model/qa_state.json`
- Exposes Prometheus metrics on port 8000

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
