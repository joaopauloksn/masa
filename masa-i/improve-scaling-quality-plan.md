# MASA Scaling Quality Improvement Plan

## Goal

Reduce label=1 (SLA breach, 25%) and label=3 (SLA exceeding, 64.2%) observations,
and increase the frequency of label=2 (SLA meeting target, 10.8%), which is the
stable operating zone the system should stay in.

## Diagnosis

### Confirmed: Training uses label=2 data ONLY

Yes — `prepare_data()` in `training.py` (line 102) explicitly filters:
```python
data = data[data['label'] == 2].copy()
```
The model learns the `(total_messages → replicas)` relationship **only from moments
when the system was already healthy**. This is the intended design.

### Root Causes of the current 25/10.8/64.2 distribution

1. **Label boundaries are too tight.**
   - `performance_ratio >= 0.92` → label=3. This threshold is high but reachable
     when delivery ≥99% AND latency ≤85% of target simultaneously.
   - `performance_ratio >= 0.60` → label=2.
   - The wide gap between 0.60 and 0.92 means label=2 is a narrow band; anything
     that performs really well (e.g. extra replicas) immediately becomes label=3.

2. **Model over-scales due to conservative training data bias.**
   - The training set only includes label=2 moments. During those moments,
     a certain number of replicas was present. If the system spent most of
     its label=2 time at **higher-than-needed** replica counts (because
     auto-scaling was reactive/erratic), the model learns to recommend
     those same high replica counts — keeping performance comfortably in
     label=3 territory.

3. **Prediction uses only `total_messages` as input.**
   - A single feature (message throughput → replica count) cannot capture
     the actual delivery-rate or latency directly. If message volume is low
     but latency is high (e.g. a single slow connection), the model still
     predicts few replicas.

4. **Planning Agent reads only the most-recent 1 row of metrics.**
   - `get_latest_data()` fetches `.limit(1)` from the microservices collection.
     A single instantaneous snapshot is noisy; using a short rolling average
     (e.g. last 3–5 rows) would produce more stable predictions.

5. **No hysteresis / deadband in the Action Agent.**
   - The Action Agent executes every plan that differs from the current
     replica count by even 1 replica. With a noisy model this causes constant
     replica churn, which itself prevents the system from settling into label=2.

6. **Label=3 threshold (0.92) may be too easy to exceed.**
   - Once the system has extra replicas (any over-scale from a label=1 event),
     delivery percentage can easily hit 99%+ and latency drops well below target,
     pushing `performance_ratio` above 0.92 → label=3. The system then
     auto-scales down (or at least the model recommends fewer replicas), and the
     cycle repeats: label=1 → scale up → label=3 → scale down → label=1.

---

## Sub-Tasks

---

### Sub-Task 1 — Widen the label=2 band in `monitor.py`

**Status:** `[ ] pending`

**Intent:**
Widen the label=2 zone by raising the label=1 threshold slightly (from 0.60
to 0.65) and, more importantly, lowering the label=3 threshold (from 0.92 to 0.85).
This directly increases the number of observations that fall into the "healthy"
band rather than triggering label=3, which starves the training set of useful
label=2 samples and creates oscillation.

**Expected Outcomes:**
- More consecutive perception observations labelled 2.
- Fewer label=3 observations (the model's training set becomes richer and more
  representative of healthy steady-state operation).
- Reduced oscillation between over-scaled (label=3) and under-scaled (label=1).

**Todo List:**
1. In `monitor.py` `get_label()`, change label=3 threshold from `>= 0.92` to `>= 0.85`.
2. Optionally raise label=1 threshold from `< 0.60` to `< 0.65` to make label=2
   slightly easier to achieve on the lower end too.
3. Rebuild and push the perception image: `./build-and-push.sh perception`
4. Restart the perception CronJob: `kubectl rollout restart cronjob/masa-perception -n masa-system`
5. Monitor dashboard for a few cycles to confirm label distribution shifts toward label=2.

**Relevant Context:**
- File: `ginga/masa-i/masa-agent-perception/docker/src/data_monitoring/monitor.py`
- Lines 316–323: `get_label()` final threshold comparisons.

---

### Sub-Task 2 — Use a rolling average instead of a single row in the Planning Agent

**Status:** `[ ] pending`

**Intent:**
Change `get_latest_data()` in `predict.py` from fetching 1 row to fetching the last
N rows and averaging the input features. This smooths out single-point spikes and
makes predictions more stable, reducing churn-driven oscillation.

**Expected Outcomes:**
- Planning Agent produces more stable replica predictions between cycles.
- Fewer unnecessary scale-up/scale-down events caused by a single noisy metric row.

**Todo List:**
1. In `predict.py` `get_latest_data()`, change `.limit(1)` to `.limit(5)`.
2. In `prepare_data_for_prediction()`, compute the mean of `m_delivered` and
   `m_undelivered` across the 5 rows before building `total_messages`.
3. Log a warning if fewer than 3 rows are available (use whatever is present).
4. Rebuild and push: `./build-and-push.sh planning`
5. Restart the planning deployment: `kubectl rollout restart deployment/masa-planning -n masa-system`

**Relevant Context:**
- File: `ginga/masa-i/masa-agent-planning/docker/src/model_deployment/predict.py`
- Lines 170–180: `get_latest_data()` — change the `.limit(1)` call.
- Lines 187–206: `prepare_data_for_prediction()` — compute mean across rows.

---

### Sub-Task 3 — Add a deadband / hysteresis to the Action Agent

**Status:** `[ ] pending`

**Intent:**
Prevent the Action Agent from executing every single +1 / -1 replica change.
A deadband (e.g. require a difference of ≥2 replicas before acting, or require
the same decision to appear in N consecutive plans) avoids constant churn from
noisy predictions.

**Expected Outcomes:**
- Mosquitto deployment stops oscillating by ±1 replica every few minutes.
- Replica count stabilises, giving the system time to reach label=2 and stay there.

**Todo List:**
1. Add an env var `MIN_REPLICA_DELTA` (default: 2) to the Action Agent.
2. In `agent.py` `execute_pending_plan()`, before calling `scale_to()`, check:
   `abs(desired_replicas - live_replicas) < MIN_REPLICA_DELTA` → mark_skipped().
3. Expose this as an operator CR field (`minReplicaDelta`) so it can be tuned
   without rebuilding the image.
4. Rebuild and push: `./build-and-push.sh action`
5. Restart: `kubectl rollout restart deployment/masa-action -n masa-system`
6. Monitor for reduced scaling event frequency in the dashboard.

**Relevant Context:**
- File: `ginga/masa-i/masa-agent-action/docker/src/scaling/agent.py`
- Lines 91–159: `execute_pending_plan()` — add delta check before `scale_to()`.
- Operator CR sample: `ginga/masa-i/masa-autoscaler-operator/config/samples/scaling_v1alpha1_masautoscaling.yaml`

---

### Sub-Task 4 — Enrich the training feature set to include delivery % and latency

**Status:** `[ ] pending`

**Intent:**
The training model currently uses only `total_messages` as a feature. Adding
`m_percentage__delivered` and `m_quantile_90` as additional inputs gives the
model direct visibility into the SLA signals that determine the label. This
allows it to learn that "high message volume + low delivery %" requires more
replicas than "high message volume + high delivery %".

**Expected Outcomes:**
- Model predictions are more aligned with actual SLA state.
- The model can distinguish between "high traffic, performing well (keep replicas)"
  and "high traffic, SLA degraded (increase replicas)".
- Reduction in label=1 events following scale actions.

**Todo List:**
1. In `training.py` `prepare_data()`:
   - Add `m_percentage__delivered` and `m_quantile_90` to the feature set `X`.
   - Normalise `m_percentage__delivered` to [0, 1] (divide by 100).
   - Normalise `m_quantile_90` by dividing by `target_quantile_90` (or a fixed
     cap like 2.0 seconds) to keep values in [0, ~1] range.
   - Store both normalisation constants in `scaler.json` alongside `max_total_msg`.
2. Update `build_model()` in `training.py` — change `input_dim` from 1 to 3.
3. In `predict.py` `prepare_data_for_prediction()`:
   - Add the same two features using the same normalisation constants from `scaler.json`.
   - Update `load_scaler()` to return a dict with all three normalisation factors.
4. Delete the existing model and `best_mse.txt` on the PVC to force a clean retrain
   with the new feature set.
5. Rebuild and push: `./build-and-push.sh planning planning-training`
6. Trigger a training run manually, then monitor dashboard for improved label=2 rate.

**Relevant Context:**
- File: `ginga/masa-i/masa-agent-planning/docker-training/src/model_training/training.py`
  - Lines 98–136: `prepare_data()` — feature engineering.
  - Lines 161–169: `save_scaler()` / `load_scaler()`.
  - Lines 171–183: `build_model()` — update `input_dim`.
- File: `ginga/masa-i/masa-agent-planning/docker/src/model_deployment/predict.py`
  - Lines 55–71: `load_scaler()` — update to multi-field scaler.
  - Lines 187–206: `prepare_data_for_prediction()` — add the two new features.

---

### Sub-Task 5 — Retrain the model with accumulated label=2 data

**Status:** `[ ] pending`

**Intent:**
After Sub-Tasks 1–4 have run for a few cycles (so that more label=2 data has
been collected with the wider thresholds), trigger a fresh model training run.
This ensures the model is trained on a richer, less-biased label=2 dataset.

**Expected Outcomes:**
- Newly trained model has a lower MSE on the test split.
- `best_mse.txt` and `scaler.json` on the PVC are updated.
- Subsequent planning predictions match real SLA-healthy replica counts more closely.

**Todo List:**
1. Confirm label=2 data has accumulated (check dashboard or query MongoDB for
   `db.microservices.countDocuments({label: 2})`).
2. Enable training in the CR: set `trainingEnabled: true` in
   `scaling_v1alpha1_masautoscaling.yaml` and apply:
   `kubectl apply -f config/samples/scaling_v1alpha1_masautoscaling.yaml -n masa-system`
3. Watch training CronJob logs: `kubectl logs -l job-name=... -n masa-system -f`
4. Verify new `best_mse.txt` and `scaler.json` were written to the model PVC.
5. After training succeeds, optionally set `trainingEnabled: false` again to stop
   continuous retraining (unless ongoing online learning is desired).

**Relevant Context:**
- CR sample: `ginga/masa-i/masa-autoscaler-operator/config/samples/scaling_v1alpha1_masautoscaling.yaml`
- Training code: `ginga/masa-i/masa-agent-planning/docker-training/src/model_training/training.py`

---

## Implementation Order

```
Sub-Task 1 (widen label band) → let perception run 30–60 min to collect label=2 data
Sub-Task 2 (rolling average)  → reduces prediction noise immediately
Sub-Task 3 (deadband)         → stops oscillation immediately
Sub-Task 4 (feature enrichment) → improves long-term model quality (requires retrain)
Sub-Task 5 (retrain)          → apply after enough label=2 data accumulates
```

Sub-Tasks 1, 2, and 3 can be done in parallel and have immediate effect.
Sub-Task 4 requires a model retrain (Sub-Task 5) to take effect.

---

## What Was Confirmed About the Neural Network

The neural network is **trained exclusively on label=2 data** (confirmed at
`training.py` line 102: `data = data[data['label'] == 2].copy()`).

Label=1 and label=3 observations are **not** used for training — they are
discarded. This is correct behaviour: the model learns what replica count
produces a healthy (label=2) system given a certain message throughput.
The problem is not the training filter itself but the factors above that prevent
the system from spending enough time in label=2 to collect a representative
training set and to keep predictions stable.
