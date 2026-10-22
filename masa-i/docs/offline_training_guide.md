# MASA Offline Training & Dynamic Scaling Guide

This guide details how to collect balanced training data across 1–10 replicas, train the 4-feature neural network offline, and deploy the updated model artifacts to enable dynamic autonomous scaling.

---

## 1. Prerequisites & Clean Sweep Collection

Ensure you are logged in to OpenShift:
```bash
oc whoami
```

Run the parameter sweep script to collect unbiased metrics across $1 \rightarrow 10$ replicas:
```bash
cd ginga/masa-i
chmod +x collect-training-data.sh
./collect-training-data.sh
```

### What the sweep script does automatically:
1. **Pauses `load-test-scaler`** (scales to 0) so it doesn't fight the sweep.
2. **Pauses `masa-agent-action`** (scales to 0) so MASA doesn't auto-scale during data collection.
3. **Disables online training** on the CR (`spec.trainingEnabled: false`).
4. **Purges biased historical records** from MongoDB (`mongo_scaling.microservices`).
5. Sweeps the **44 active combinations** ($\pm 2$ replica gap, 60s each $\approx 44$ min).
6. Resets Mosquitto to 1 replica and leaves online training / external scalers paused by default (`RESTORE_CONTROLLERS=0`, `ENABLE_TRAINING=0`) so you have time to extract the data, train offline, and copy model artifacts to the PVC before resuming normal scaling.

---

## 2. Extract Data to CSV

Extract the sweep data from MongoDB / Thanos using `collect_metrics.py`:
```bash
python3 collect_metrics.py \
  --thanos-url "https://$(oc get routes thanos-querier -n openshift-monitoring -o jsonpath='{.spec.host}')" \
  --token "$(oc whoami --show-token)" \
  --output sweep_training_data.csv
```

---

## 3. Train the Model Offline

Run the offline training pipeline:
```bash
python3 training/train_offline.py --csv sweep_training_data.csv --output-dir training
```

### Features & Model Structure
- **Inputs (4 features)**:
  1. `total_msg`: Throughput normalised to $[0, 300]$ via `max_total_msg`
  2. `pct_delivered_norm`: Percentage delivered $[0, 1]$
  3. `latency_ratio`: P90 latency normalised by `LATENCY_CAP` (2.0s) $[0, 1]$
  4. `undelivered_norm`: Dropped msg/s normalised by `max_undelivered` $[0, 1]$
- **Training Target**: Label-adjusted `desired_replicas` (label 1: $+1$, label 2: $+0$, label 3: $-1$).
- **Outputs generated in `training/`**:
  - `best_replica_predictor_model.h5`
  - `scaler.json`

---

## 4. Deploy Artifacts to Cluster PVC

Find the Planning Agent pod:
```bash
PLANNING_POD=$(oc get pods -n masa-system -l app=planning-agent -o jsonpath='{.items[0].metadata.name}')
echo "Planning Pod: $PLANNING_POD"
```

Copy the trained artifacts directly into the mounted PVC (`/mnt/model/`):
```bash
oc cp training/best_replica_predictor_model.h5 masa-system/${PLANNING_POD}:/mnt/model/best_replica_predictor_model.h5
oc cp training/scaler.json masa-system/${PLANNING_POD}:/mnt/model/scaler.json
```

Restart the Planning and Reflection agents so they load the new model and scaler immediately:
```bash
oc rollout restart deployment/masautoscaling-sample-planning-agent -n masa-system
oc rollout status deployment/masautoscaling-sample-planning-agent -n masa-system
```

---

## 5. Resume Cluster & Validate Scaling

1. Resume the cluster controllers:
   ```bash
   ./resume-cluster.sh
   ```
   *(Optionally pass `ENABLE_TRAINING=1 ./resume-cluster.sh` if you also want the periodic online retraining CronJob active).*

2. Watch Mosquitto adapt dynamically to varying traffic:
   ```bash
   oc get pods -n iotoccupancy -w
   ```

3. View the live Reflection QA verdict and rolling MAE:
   ```bash
   oc logs -n masa-system -l app=masa-agent-reflection --tail=20 -f
   ```
