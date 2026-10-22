# Offline Model Training & Bootstrap Plan

## Overview

Bootstrap and train the MASA (Microservices Agentic Scaling Architecture) Neural Network model offline using a balanced sweep across replicas $1 \rightarrow 10$, augmented with synthetic edge cases, to replace the biased 7–8 replica baseline and enable dynamic autonomous scaling ($1 \rightarrow 10$ replicas) on `iotoccupancy/iotoccupancy-sample-mosquitto`.

---

## Sub-Tasks

### Sub-Task 1: Prepare the Sweep Script & Cluster Pre-flight
- **Intent**: Update `collect-training-data.sh` to safely pause conflicting controllers (`load-test-scaler`, `masa-agent-action`), disable online training (`trainingEnabled: false`), and purge biased historical MongoDB records before running the parameter sweep.
- **Expected Outcomes**:
  - `collect-training-data.sh` scales `masa-agent-action` to 0 at start and restores to 1 at end.
  - MongoDB `mongo_scaling.microservices` is wiped before starting.
  - Active sweep covers 44 combinations ($\pm 2$ replica gap, 60s each $\approx$ 44 mins).
- **Status**: `[ ] pending`

---

### Sub-Task 2: Build the Offline Training Pipeline (`training/train_offline.py`)
- **Intent**: Implement an offline training script that ingests the balanced sweep dataset, calculates empirical per-pod capacity, augments with synthetic edge-case samples, normalizes all 4 features, and exports `.h5` model and `scaler.json`.
- **Expected Outcomes**:
  - Ingests CSV from `collect_metrics.py` or MongoDB directly.
  - 4 Features: `total_msg` (normalized to [0, 300]), `pct_delivered_norm` [0, 1], `latency_ratio` [0, 1], `undelivered_norm` [0, 1].
  - Derives label-adjusted target `desired_replicas` (label 1: $+1$, label 2: $+0$, label 3: $-1$).
  - Synthetic edge-case generator to ensure robust bounds (0 load $\rightarrow$ 1 pod; max traffic $\rightarrow$ 10 pods).
  - Exports `best_replica_predictor_model.h5` and `scaler.json` (containing `max_total_msg`, `max_undelivered`, `latency_cap`, `features`).
- **Status**: `[ ] pending`

---

### Sub-Task 3: Execute Sweep & Offline Training
- **Intent**: Run the data collection sweep on the live cluster, pull metrics, train the model, and verify model metrics/accuracy offline.
- **Expected Outcomes**:
  - Generated CSV containing balanced data across 1..10 replicas.
  - Trained `best_replica_predictor_model.h5` and `scaler.json` with MAE $< 0.5$ replicas.
- **Status**: `[ ] pending`

---

### Sub-Task 4: Deploy Artifacts to Cluster PVC & Verify Autonomous Scaling
- **Intent**: Deploy the new model artifacts to the cluster shared PVC (`/mnt/model/`) and observe dynamic scaling under load.
- **Expected Outcomes**:
  - Model artifacts copied to `/mnt/model/` on `masa-agent-planning` and `masa-agent-reflection`.
  - Planning and reflection pods restarted.
  - `load-test-scaler` enabled, verifying Mosquitto scales dynamically from 1 to 10 and down to 1.
- **Status**: `[ ] pending`
