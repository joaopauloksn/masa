# Model Improvement Plan — Approach A + B

## Overview

The current model is stuck predicting low replica counts because:

1. **Features are output-side only** — `total_msg`, `pct_delivered`, `latency_ratio` all measure
   what mosquitto *managed* to do. When mosquitto is saturated it drops messages, so `total_msg`
   actually *decreases* under stress — making a stressed system look idle to the model.

2. **Training target is observed replicas, label=2 only** — the model only ever saw rows where
   the SLA was being met. It learned "given these metrics, N replicas was adequate" but never
   learned "when the SLA was breached at N replicas, you needed N+1".

### Approach

**A — Add `undelivered_norm` as feature 4**
`m_undelivered` (msg/s rate of dropped messages) *rises* under stress regardless of broker
saturation. It is already collected and persisted by the perception agent. Adding it as a
standalone normalised feature gives the model a direct demand-pressure signal.
Normalisation constant `max_undelivered` is computed from the training data and saved in
`scaler.json` (same pattern as `max_total_msg`).

**B — Fix training target and use all labels**
Remove the `label==2` filter. Compute `desired_replicas` from the observed `replicas` adjusted
by the label:
- `label=1` (SLA breach)   → `desired_replicas = replicas + 1`
- `label=2` (SLA met)      → `desired_replicas = replicas`
- `label=3` (idle/excess)  → `desired_replicas = max(1, replicas - 1)`

The model then learns the correct direction from every data point, not just the balanced ones.

### Files changed

| File | Sub-task |
|---|---|
| `masa-agent-planning/docker-training/src/model_training/training.py` | 1 + 2 |
| `masa-agent-planning/docker/src/model_deployment/predict.py` | 3 |

No perception agent changes. No operator changes. No new Docker builds until validated.

---

## Sub-Task 1 — Add `undelivered_norm` feature to `training.py`

**Status:** `[x] done`

**Intent**
Add `m_undelivered` as a 4th normalised feature. Unlike `total_msg` which drops when mosquitto
saturates, `m_undelivered` rises — directly signalling that demand exceeds capacity.

**Expected Outcomes**
- `FEATURES = ['total_msg', 'pct_delivered_norm', 'latency_ratio', 'undelivered_norm']`
- `max_undelivered` computed as `float(data['m_undelivered'].max())` from the full dataset
  (before any label filter, to capture the true observed range)
- `max_undelivered` saved in `scaler_dict` alongside `max_total_msg`
- Falls back gracefully if `m_undelivered` column is absent (defaults to 0.0)
- `scaler_dict` gains key `"max_undelivered"`

**Todo List**
- [ ] Compute `max_undelivered = float(data['m_undelivered'].max())` before the label/target
      computation block
- [ ] After the latency feature block, add: normalise `m_undelivered` to `[0,1]` using
      `max_undelivered`; handle absent/all-zero column gracefully
- [ ] Add `'undelivered_norm'` to `FEATURES` list
- [ ] Add `"max_undelivered": max_undelivered` to `scaler_dict`
- [ ] Update docstring to reflect 4 features

**Relevant Context**
- File: `masa-agent-planning/docker-training/src/model_training/training.py`
- Function: `prepare_data()` lines ~98–178
- Pattern to follow: `max_total_msg` normalisation at lines ~160–165
- `scaler_dict` at lines ~185–193

---

## Sub-Task 2 — Fix training target and use all labels in `training.py`

**Status:** `[x] done`

**Intent**
Remove the `label==2` filter and replace `y = data['replicas']` with
`y = data['desired_replicas']` where `desired_replicas` is the label-adjusted target.
This teaches the model what replica count *should have been* at every observed state, not just
what it was when the SLA happened to be met.

**Expected Outcomes**
- The `data = data[data['label'] == 2].copy()` line is removed
- A new column `desired_replicas` is computed as described above
- Minimum row count check (`< 50`) still applies to the full dataset
- `y = data['desired_replicas']`
- Log messages updated to reflect the new filter-free approach and target

**Todo List**
- [ ] Remove `data = data[data['label'] == 2].copy()` and its log line
- [ ] After feature computation, add the `desired_replicas` derivation block (3 lines with
      `.loc[]` assignments)
- [ ] Change `y = data['replicas']` to `y = data['desired_replicas']`
- [ ] Update the "insufficient data" log message — it now refers to total rows, not label=2 rows
- [ ] Ensure `desired_replicas` NaN handling is covered by the existing NaN-drop block

**Relevant Context**
- File: `masa-agent-planning/docker-training/src/model_training/training.py`
- Function: `prepare_data()` lines ~109–178
- `desired_replicas` must be clipped to `[1, MAX_REPLICAS]` — use `clip(lower=1)` for label=3
  and ensure label=1 doesn't push above a reasonable ceiling (10, matching the sweep)

---

## Sub-Task 3 — Add `undelivered_norm` feature to `predict.py`

**Status:** `[x] done`

**Intent**
Mirror the feature-4 addition from Sub-Task 1 in the prediction path so the feature vector
at inference time exactly matches the one used during training.

**Expected Outcomes**
- `FEATURES = ['total_msg', 'pct_delivered_norm', 'latency_ratio', 'undelivered_norm']`
- `max_undelivered` read from `scaler.json` via `scaler.get("max_undelivered") or 1.0`
- Falls back to `0.0` if `m_undelivered` is absent from the rolling-average row
- `_SCALER_DEFAULTS` updated to include `"max_undelivered": 1.0`
- Debug log includes `undelivered_norm` value

**Todo List**
- [ ] Add `"max_undelivered": 1.0` to `_SCALER_DEFAULTS`
- [ ] In `prepare_data_for_prediction()`, read `max_undelivered` from the loaded scaler
- [ ] After the latency feature block, add the `undelivered_norm` computation (same pattern as
      training.py)
- [ ] Add `'undelivered_norm'` to `FEATURES` list
- [ ] Update debug log line to include `undelivered_norm`
- [ ] Update docstring to reflect 4 features

**Relevant Context**
- File: `masa-agent-planning/docker/src/model_deployment/predict.py`
- Function: `prepare_data_for_prediction()` lines ~234–287
- `_SCALER_DEFAULTS` at lines ~55–59
- The model input shape compatibility check at lines ~347–355 will automatically handle the
  case where an old 3-feature model is still on the PVC — it trims the input to the model's
  expected width

---

## Post-Implementation

After all 3 sub-tasks are complete:

1. **Delete the existing model** from the PVC so training starts fresh with the new feature set:
   ```bash
   oc exec -n masa-system $(oc get pod -n masa-system -l app=planning-agent -o name | head -1) \
     -- rm -f /mnt/model/best_replica_predictor_model.h5 /mnt/model/scaler.json /mnt/model/best_mse.txt
   ```
2. **Trigger a training run** manually (or wait for the CronJob):
   ```bash
   oc create job --from=cronjob/masautoscaling-sample-training-agent manual-train-1 -n masa-system
   ```
3. **Watch training logs** and verify the new MSE:
   ```bash
   oc logs -n masa-system -l job-name=manual-train-1 --tail=50
   ```
4. **Monitor the planning agent** — it should now predict higher replica counts under label=1 load.
