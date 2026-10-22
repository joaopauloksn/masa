# MASA (Microservices Agentic Scaling Architecture): Architecture & Operational State Handoff

This document provides a comprehensive, production-grade technical handoff of the **MASA (Microservices Agentic Scaling Architecture)** system. It synthesizes the theoretical design, multi-agent control loops, machine learning model pipeline, credential and secret security automation, full 30-minute load test evaluation metrics, Excel reporting artifact structure, and operational runbooks.

---

## 1. System Overview & Core Architecture

MASA is an autonomous, agentic autoscaling architecture deployed on **Red Hat OpenShift**. It replaces reactive threshold-based autoscalers (such as the standard Kubernetes HPA) with an intelligent closed-loop control system governed by four specialized autonomous agents, MongoDB shared state, and neural network regression models.

```
                    ┌────────────────────────────────────────────────────────┐
                    │               MASA Operator (Ansible)                  │
                    │  - CRD: masautoscalings.scaling.masa.com               │
                    │  - Secret Projection & Thanos Route Discovery          │
                    └──────────────────────────┬─────────────────────────────┘
                                               │ Deploys & Manages
                                               ▼
   ┌────────────────────────────────────────────────────────────────────────────────────────┐
   │                                  masa-system Namespace                                 │
   │                                                                                        │
   │  ┌───────────────────────┐                    ┌──────────────────────────────────────┐ │
   │  │  Understanding Agent  │──(Telemetry ETL)──►│       Planning Agent (Predict)       │ │
   │  │  - Scrapes Thanos API │                    │  - 4-Feature Neural Network (PVC)    │ │
   │  │  - P90, Tput, Loss    │                    │  - Zero-load bypass & Low-load calib │ │
   │  │  - Writes to MongoDB  │                    │  - Generates Scaling Plans           │ │
   │  └───────────────────────┘                    └──────────────────┬───────────────────┘ │
   │                                                                  │                     │
   │                                                             (Plan Created)             │
   │                                                                  ▼                     │
   │  ┌───────────────────────┐                    ┌──────────────────────────────────────┐ │
   │  │   Reflection Agent    │◄──(Post-Scale QA)──│             Action Agent             │ │
   │  │  - Evaluates MAE      │                    │  - Validates Governance Rules        │ │
   │  │  - Checks SLA Health  │                    │  - Applies K8s Deployment Scale API  │ │
   │  │  - Model Feedback Loop│                    │  - Cooldown Management               │ │
   │  └───────────────────────┘                    └──────────────────────────────────────┘ │
   └───────────────────────────────────────────┬────────────────────────────────────────────┘
                                               │ Scales Replicas
                                               ▼
   ┌────────────────────────────────────────────────────────────────────────────────────────┐
   │                                  Target Workload & Test                                │
   │  - Namespace: `iotoccupancy`                                                           │
   │  - Target Broker: `iotoccupancy-sample-mosquitto` (Dynamic 1-15 Pods)                  │
   │  - Load Generator: `mqtt-load-tester` (Paho MQTT QoS 2, up to 6,000 devices)          │
   │  - Load Controller: `load-test-scaler` (14-step multi-phase test sequence)              │
   └────────────────────────────────────────────────────────────────────────────────────────┘
```

### Key Cluster Context & Namespaces
- **Cluster Context**: OpenShift (`oc whoami` $\rightarrow$ `kube:admin`).
- **`masa-system`**: Houses the MASA Operator, Understanding Agent, Planning Agent, Action Agent, and Reflection Agent.
- **`iotoccupancy`**: Houses the target application under test (`iotoccupancy-sample-mosquitto`), simulated IoT clients (`mqtt-load-tester`), and the test orchestrator (`load-test-scaler`).
- **`openshift-monitoring`**: Houses OpenShift Thanos Querier (`thanos-querier` route / internal service) aggregating cluster Prometheus metrics.
- **`mongoce`**: Houses MongoDB replica set providing multi-agent persistent storage (`mongo_scaling.microservices`, `mongo_scaling.scaling_plans`, `mongo_scaling.governance_context`, `mongo_scaling.model_feedback`).
- **`model-pvc`**: Persistent Volume Claim mounted at `/mnt/model/` across Planning, Training, and Reflection agents hosting `best_replica_predictor_model.h5`, `scaler.json`, `best_mse.txt`, and `qa_state.json`.

---

## 2. Autonomous Agent Ecosystem & Control Loops

### A. Understanding Agent (Data Monitoring & Ingestion)
- **Role**: Continuously observes the performance of monitored microservices by querying Thanos/Prometheus.
- **Interval**: Runs on a configurable tick (default: 15 seconds).
- **Core Queries**:
  - P90 Latency: `histogram_quantile(0.90, sum(rate(mqtt_message_delivery_time_seconds_histogram_bucket[1m])) by (le))`
  - Delivered Throughput: `sum(rate(mqtt_delivered_messages_total[1m]))`
  - Undelivered Throughput: `sum(rate(mqtt_undelivered_messages_total[1m]))`
  - Resource Usage: Pod CPU cores (`container_cpu_usage_seconds_total`) and Memory RSS / Working Set (`container_memory_working_set_bytes`).
- **Storage**: Normalizes metrics and writes time-series documents to `mongo_scaling.microservices`.

### B. Planning Agent (Inference & Predictive Sizing)
- **Role**: Pulls recent telemetry from MongoDB, constructs normalized feature vectors, executes neural network regression to predict required replicas, and outputs scaling proposals.
- **Artifacts Used**: `/mnt/model/best_replica_predictor_model.h5` and `/mnt/model/scaler.json`.
- **Classification & Scaling Logic**:
  1. **Zero-Traffic Bypass**: If `total_raw <= IDLE_TOTAL_MSG_THRESHOLD` (default: 5 msg/s), instantly proposes **1 replica** with 100% confidence without triggering ML inference.
  2. **Low-Load Calibration**: If raw continuous prediction is slightly above 1 due to minor fractional noise (`raw_value <= 1.20`) and no SLA breach is detected, clamps the output to **1 replica** (preventing 3-replica over-provisioning).
  3. **High-Load Scale-Up**: Uses responsive `ceil(raw_value)` for higher traffic loads to ensure rapid headroom.
  4. **State Writing**: Emits scaling proposals to `mongo_scaling.scaling_plans`.

### C. Action Agent (Execution & Governance Safeguards)
- **Role**: Reads proposed plans from `mongo_scaling.scaling_plans`, enforces safety constraints, and executes Kubernetes scale operations.
- **Governance Rules**:
  - Enforces cluster boundaries: `min_replicas` (1) and `max_replicas` (15).
  - Scale-down cooldown: Prevents rapid thrashing by enforcing a stabilization window between down-scale events.
  - Scale-up priority: Immediately triggers scale-up if `label = 1` (SLA breach).
- **Execution**: Updates target Deployment `spec.replicas` via the Kubernetes API.

### D. Reflection Agent (Quality Assurance & Continuous Evaluation)
- **Role**: Performs post-scaling evaluation (typically 60-120 seconds after action execution) to assess whether the scaling action successfully maintained or restored SLA compliance.
- **Evaluation Criteria**:
  - Post-scaling stability: Checks that the proportion of SLA breach windows (`label1_ratio`) is $\le 30\%$.
  - Rolling Mean Absolute Error (MAE): Compares proposed replica plans against empirical `label = 2` (SLA met) ground truth.
  - Resilient Data Handling: Gracefully handles empty Thanos query vectors without crashing DataFrame aggregations.
- **Feedback**: Records evaluation reports in `mongo_scaling.model_feedback` and writes `qa_state.json` to `/mnt/model/` for automated retraining triggers.

---

## 3. Security, Secret Projection & Thanos Automation

To ensure production security compliance and eliminate expired static tokens, all manual environment variable credentials were removed in favor of projected Kubernetes volumes and automated route discovery.

### A. Thanos Route Auto-Discovery Chain (`roles/masautoscaling/tasks/main.yml`)
1. **Explicit Bypass**: If `spec.thanosUrl` is defined on the Custom Resource (CR), the operator uses it directly.
2. **Dynamic Route Query**: Otherwise, the operator queries OpenShift routes in `openshift-monitoring` matching `app.kubernetes.io/name=thanos-querier` via the OpenShift API.
3. **Internal Fallback**: If no public route exists, falls back to cluster DNS:
   `https://thanos-querier.openshift-monitoring.svc:9091/api/v1/query`

### B. Dynamic Secret & Token Projection
- **No Plaintext Passwords in Pod Specs**: Passwords and tokens are mounted as volume projections rather than raw environment variables.
- **Thanos OAuth Token**: Uses Kubernetes projected ServiceAccount tokens mounted at `/etc/secrets/thanos/token`. The kubelet automatically refreshes and rotates this token hourly.
- **MongoDB Credentials**: Secret `mongo-credentials` is mounted at `/etc/secrets/mongo/` (`username`, `password`).
- **Python Secret Helper**: All agents implement `_read_secret_file(path, fallback_env)` to dynamically read fresh secrets from disk on every connection attempt.

---

## 4. Machine Learning Model Pipeline & Calibration

### A. 4-Feature Input Vector
The regression model maps multi-dimensional microservice load signals into optimal replica sizing:

1. `total_msg`: Total message throughput normalized to $[0, 300]$ via `max_total_msg`.
2. `pct_delivered_norm`: Percentage of successfully delivered messages $[0, 1]$ (`m_percentage__delivered / 100`).
3. `latency_ratio`: P90 latency normalized to $[0, 1]$ with a 2.0s ceiling (`m_quantile_90 / 2.0`).
4. `undelivered_norm`: Dropped message rate $[0, 1]$ (`m_undelivered / max_undelivered`), providing a saturation signal when total throughput collapses under extreme stress.

### B. Model Scaling Response Matrix

Trained artifact: `best_replica_predictor_model.h5` + `scaler.json` (deployed to `/mnt/model/`):

| Load Pattern | Throughput | Delivery % | P90 Latency | Raw NN Output | Scaled Decision |
|---|---|---|---|---|---|
| **Zero Traffic / Idle** | 0 msg/s | 100% | 0.02s | *(Bypassed)* | **1 replica** |
| **Light Traffic** | 50 msg/s | 100% | 0.03s | 1.062 | **1 replica** |
| **Light Traffic** | 100 msg/s | 100% | 0.04s | 0.869 | **1 replica** |
| **Light Traffic** | 200 msg/s | 100% | 0.05s | 0.843 | **1 replica** |
| **Light Traffic** | 350 msg/s | 100% | 0.06s | 0.931 | **1 replica** |
| **1 Load Tester** | 450 msg/s | 100% | 0.08s | 1.197 | **1 replica** |
| **2 Load Testers** | 900 msg/s | 100% | 0.10s | 2.594 | **3 replicas** |
| **4 Load Testers** | 1,800 msg/s | 99% | 0.15s | 4.452 | **5 replicas** |
| **6 Load Testers** | 2,700 msg/s | 98% | 0.25s | 6.329 | **7 replicas** |
| **10 Load Testers** | 4,500 msg/s | 95% | 0.50s | 9.607 | **10 replicas** |

---

## 5. 30-Minute Full Load Test Performance Evaluation

### A. Load Test Execution Window
- **Evaluation Window**: `2026-09-25 09:33:00 UTC` to `2026-09-25 10:03:00 UTC` (30 minutes duration, 125 samples @ 15s interval).
- **Target Microservice**: `iotoccupancy-sample-mosquitto`.
- **Load Test Orchestrator**: `load_test_scaler.py` executing 14 steps across 5 phases.

### B. Summary of Key Performance Indicators (KPIs)

| KPI Category | Metric Name | Observed Result | Evaluation & Compliance Notes |
|---|---|---|---|
| **Workload Scale** | Max Simulated Devices | **6,000 devices** | 12 Load Tester pods $\times$ 500 clients each |
| **Workload Scale** | Peak Expected Generation | **12,000 msg/s** | Theoretical maximum (2 msg/s per device) |
| **Throughput** | Peak Delivered Throughput | **9,917.99 msg/s** | Handled during Flash Crowd Spike Phase |
| **Throughput** | Average Delivered Throughput | **3,721.64 msg/s** | Continuous 30-minute mean |
| **Elasticity** | Replica Scaling Range | **3 to 15 pods** | Dynamic adaptation across all 14 steps (Mean: 8.86 pods) |
| **SLA Compliance** | P90 Latency $\le 1.0\text{s}$ Target | **98.4%** | 123 of 125 evaluation samples met SLA |
| **SLA Compliance** | Sub-500ms P90 ($\le 0.5\text{s}$) | **84.0%** | Optimal instantaneous delivery |
| **Delivery Quality** | Delivery Rate $\ge 95\%$ Target | **92.8%** | 116 of 125 evaluation samples met target |
| **Delivery Quality** | Delivery Rate $\ge 99\%$ Target | **84.0%** | Ultra-high reliability delivery |
| **Delivery Quality** | Overall Mean Delivery Rate | **96.47%** | High overall fidelity despite heavy spikes |
| **Resource Usage** | Peak Aggregate CPU | **3.58 cores** | Scaled across 15 Mosquitto pods |
| **Resource Usage** | Peak Memory RSS | **2,476.3 MiB** | ~165 MiB per Mosquitto pod at peak |

---

## 6. Generated Excel Report Artifact Details

- **File Path**: [`ginga/masa-i/masa_load_test_evaluation_report.xlsx`](ginga/masa-i/masa_load_test_evaluation_report.xlsx)
- **Generator Script**: [`ginga/masa-i/generate_evaluation_excel.py`](ginga/masa-i/generate_evaluation_excel.py)
- **Formula Verification**: Contains **80 dynamic Excel formulas** and **zero formula errors** (`#REF!`, `#VALUE!`, `#DIV/0!`).

### Workbook Sheet Breakdown

```
  ┌─────────────────────────────────────────────────────────────────────────────────────────┐
  │                   masa_load_test_evaluation_report.xlsx Structure                       │
  ├─────────────────────────────────────────────────────────────────────────────────────────┤
  │ Tab 1: Summary & KPI Dashboard                                                          │
  │   - 18 Dynamic KPI formulas (AVERAGE, COUNTIF, MAX, MIN, ROUND)                         │
  │   - Chart 1: Autoscaling Tracking (Delivered Throughput vs Replicas)                    │
  │   - Chart 2: SLA Quality & Stability (Delivery Rate % vs P90 Latency seconds)           │
  │   - Chart 3: Saturation & Queueing (Delivered msg/s vs Undelivered msg/s)               │
  │   - Chart 4: Resource Footprint (Aggregate CPU Cores vs Memory MiB)                     │
  │   - Chart 5: Continuous Timeline Tracking (Expected Generation vs Delivered msg/s)      │
  │                                                                                         │
  │ Tab 2: Load Test Plan                                                                   │
  │   - Section 1: Pod-level MQTT configuration (500 clients, 0.5s freq, QoS 2)             │
  │   - Section 2: 14-step phase table with dynamic formulas:                               │
  │       * Expected Generation = Clients * (1 / Interval)                                  │
  │       * Avg Observed Delivered = AVERAGE('Load Test Metrics'!C_start:C_end)             │
  │       * Generation Efficiency % = Observed / Expected                                   │
  │   - Chart 1: Planned Workload (Simulated Clients & Message Rate per Step)               │
  │   - Chart 2: Step Performance (Expected Generation vs Observed Delivered msg/s)        │
  │                                                                                         │
  │ Tab 3: Load Test Metrics                                                                │
  │   - 125 rows of 15s-sampled data across 11 columns with mapped Expected (msg/s)        │
  └─────────────────────────────────────────────────────────────────────────────────────────┘
```

---

## 7. Operational Runbooks & Quick Reference Commands

### A. Deploying Updated ML Model & Scaler Artifacts to PVC
When a new model is trained offline, deploy it directly to the shared PVC without rebuilding agent container images:

```bash
# 1. Locate active Planning Agent pod in masa-system
PLANNING_POD=$(oc get pod -n masa-system -l app=masa-agent-planning -o jsonpath='{.items[0].metadata.name}')

# 2. Copy trained model and scaler artifacts into mounted PVC
oc cp ginga/masa-i/training/best_replica_predictor_model.h5 ${PLANNING_POD}:/mnt/model/best_replica_predictor_model.h5 -n masa-system
oc cp ginga/masa-i/training/scaler.json ${PLANNING_POD}:/mnt/model/scaler.json -n masa-system

# 3. Trigger rolling restart of Planning Agent to reload weights
oc rollout restart deployment/masautoscaling-sample-planning-agent -n masa-system
oc rollout status deployment/masautoscaling-sample-planning-agent -n masa-system
```

### B. Collecting Clean Sweep Data & Retraining Offline
To retrain the neural network on fresh cluster data:

```bash
cd ginga/masa-i

# 1. Run automated replica sweep (pauses agents, sweeps 1-10 replicas across load levels)
./collect-training-data.sh

# 2. Extract metrics to CSV from Thanos
python3 collect_metrics.py \
  --thanos-url "https://$(oc get routes thanos-querier -n openshift-monitoring -o jsonpath='{.spec.host}')" \
  --token "$(oc whoami --show-token)" \
  --output sweep_training_data.csv

# 3. Retrain model offline with balanced low-load synthetic anchors
python3 training/train_offline.py --csv sweep_training_data.csv --output-dir training

# 4. Resume controllers & scaling tests
./resume-cluster.sh
```

### C. Live Diagnostics & Log Tailing
- **Planning Agent (Inference logs & decisions)**:
  ```bash
  oc logs -n masa-system deployment/masautoscaling-sample-planning-agent --tail=50 -f
  ```
- **Action Agent (Kubernetes scaling actions)**:
  ```bash
  oc logs -n masa-system deployment/masautoscaling-sample-action-agent --tail=50 -f
  ```
- **Reflection QA Agent (Evaluation logs)**:
  ```bash
  oc logs -n masa-system $(oc get pods -n masa-system -l app=masa-agent-reflection --sort-by='.metadata.creationTimestamp' -o jsonpath='{.items[-1].metadata.name}') --tail=50
  ```
- **Target Mosquitto Replicas**:
  ```bash
  oc get pods -n iotoccupancy -w
  ```
- **Load Tester Execution**:
  ```bash
  oc logs -n iotoccupancy deployment/load-test-scaler --tail=30 -f
  ```

---

## 8. Index of Core Codebase Files

| Path | Purpose & Key Implementation Details |
|---|---|
| [`roles/masautoscaling/tasks/main.yml`](ginga/masa-i/masa-autoscaler-operator/roles/masautoscaling/tasks/main.yml) | Ansible Operator tasks: Thanos route auto-discovery, Projected ServiceAccount OAuth token mounts, and MongoDB secret volume mounts. |
| [`predict.py`](ginga/masa-i/masa-agent-planning/docker/src/model_deployment/predict.py) | Planning Agent inference: 4-feature neural network evaluation, zero-load bypass (`<= 5 msg/s`), and low-load calibration (`raw <= 1.20 -> 1 replica`). |
| [`task.py`](ginga/masa-i/masa-agent-reflection/docker/task.py) | Reflection QA Agent: Post-scaling evaluation, rolling MAE tracking against ground truth, and QA state reporting. |
| [`agent.py`](ginga/masa-i/masa-agent-action/docker/src/scaling/agent.py) | Action Agent: Scaling governance rules, cooldown management, and Kubernetes Deployment scaling API calls. |
| [`generate_evaluation_excel.py`](ginga/masa-i/generate_evaluation_excel.py) | Excel generator script: Produces 3-tab workbook with 7 embedded native charts and 80 dynamic calculation formulas. |
| [`collect_metrics.py`](ginga/masa-i/collect_metrics.py) | Metric extraction CLI: Scrapes Thanos Querier and MongoDB with support for `--start` and `--end` ISO time filtering. |
| [`train_offline.py`](ginga/masa-i/training/train_offline.py) | Offline model training pipeline: 4-feature neural network training with balanced synthetic low-load anchors. |
| [`load_test_scaler.py`](ginga/load-test-manage/load_test_scaler.py) | Load test controller: Executes 14-step phased load profile scaling from 1 to 12 load-tester pods. |
| [`loadtest.py`](ginga/load-test-py/loadtest.py) | Paho MQTT client: Simulates IoT telemetry with QoS 2, latency histograms, and Prometheus metrics endpoint on port 8000. |
| [`masa_load_test_evaluation_report.xlsx`](ginga/masa-i/masa_load_test_evaluation_report.xlsx) | Final evaluation spreadsheet artifact containing KPI Dashboard, Load Test Plan, and raw evaluation metrics. |
| [`masa_load_test_30min_sep_25.csv`](ginga/masa-i/masa_load_test_30min_sep_25.csv) | 30-minute evaluation dataset (125 rows @ 15s interval) extracted from Thanos and MongoDB. |
