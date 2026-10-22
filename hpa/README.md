# KEDA + HPA Autoscaling Baseline (MASA Comparison)

This directory contains a reproducible **reactive autoscaling baseline** for the
MASA (Microservices Agentic Scaling Architecture) evaluation. It uses the
**Red Hat Custom Metrics Autoscaler** (the supported OpenShift distribution of
KEDA) to drive a native **Kubernetes Horizontal Pod Autoscaler (HPA)** from the
same Thanos/Prometheus telemetry that MASA's agents consume.

The goal is a fair *A/B* comparison: same cluster, same workload, same metrics
source, same sampling cadence, same replica bounds, same load profile. The only
variable is the scaling decision logic:

| Aspect | MASA | KEDA + HPA baseline |
|---|---|---|
| Decision model | 4-feature neural network + governance rules (Planning/Action agents) | Proportional control: `ceil(metric / target)` |
| Control style | Proactive / learned | Reactive / threshold |
| Signals used | Throughput, delivery %, P90 latency, undelivered rate | Total MQTT message rate (single signal, optional extra triggers) |
| Feedback loop | Reflection Agent (MAE, SLA QA, retraining) | None (stateless, apart from HPA stabilization) |
| State store | MongoDB (`mongo_scaling.*`) | None (Kubernetes API only) |

---

## 1. Directory Layout

```
ginga/hpa/
├── README.md        # This document
├── operator/        # KEDA operator install: Namespace, OperatorGroup, Subscription, KedaController
├── auth/            # ServiceAccount, cluster-monitoring-view RBAC, token Secret, TriggerAuthentication
├── scaling/         # ScaledObject (generates the HPA) + alternative trigger variants
└── scripts/         # deploy / teardown / benchmark automation
```

| Folder | Planned contents | Plan sub-task |
|---|---|---|
| `operator/` | `01-namespace.yaml`, `02-operatorgroup.yaml`, `03-subscription.yaml`, `04-kedacontroller.yaml`, `kustomization.yaml` | Sub-Task 2 |
| `auth/` | `01-serviceaccount.yaml`, `02-rbac.yaml`, `03-trigger-auth.yaml`, `kustomization.yaml` | Sub-Task 3 |
| `scaling/` | `01-scaledobject.yaml`, alternative trigger variants, `kustomization.yaml` | Sub-Task 4 |
| `scripts/` | `deploy-keda-hpa.sh`, `teardown-keda-hpa.sh`, `run-hpa-benchmark.sh` | Sub-Task 5 |

The full implementation plan lives in
[`keda-hpa-baseline-plan.md`](../../keda-hpa-baseline-plan.md).

---

## 2. Architecture

```
 ┌───────────────────────────────────────────────────────────┐
 │            OpenShift Monitoring (openshift-monitoring)    │
 │  thanos-querier.openshift-monitoring.svc:9091             │
 │  scrapes mqtt-load-tester-metrics (port 8000 /metrics)    │
 └─────────────────────────────┬─────────────────────────────┘
                               │ PromQL over HTTPS (Bearer token)
                               ▼
 ┌───────────────────────────────────────────────────────────┐
 │  openshift-keda                                           │
 │  KedaController "keda" ─► keda-operator                   │
 │                         ─► keda-metrics-apiserver         │
 │                            (external.metrics.k8s.io)      │
 └─────────────────────────────┬─────────────────────────────┘
                               │
 ┌─────────────────────────────▼─────────────────────────────┐
 │  iotoccupancy                                             │
 │  ServiceAccount keda-thanos-reader                        │
 │     └─ ClusterRoleBinding → cluster-monitoring-view       │
 │  TriggerAuthentication thanos-trigger-auth (bearer)       │
 │  ScaledObject iotoccupancy-mosquitto-scaler               │
 │     └─ generates HPA keda-hpa-iotoccupancy-mosquitto-...  │
 │           └─ scales Deployment iotoccupancy-sample-mosquitto (1..15) │
 │  Load generation: mqtt-load-tester  (driven by load-test-scaler)     │
 └───────────────────────────────────────────────────────────┘
```

### Control-loop sequence

1. **Every 15 s** (`pollingInterval`) the KEDA operator evaluates the Prometheus
   trigger against Thanos (used for activation, i.e. 0↔1, which is disabled here
   because `minReplicaCount: 1`).
2. The HPA controller (default sync period **15 s**) asks the
   `keda-metrics-apiserver` for the external metric; the adapter executes the
   PromQL query against Thanos using the `TriggerAuthentication` credentials.
3. The HPA computes the desired replica count (Section 4), applies its
   `behavior` policies (Section 5) and patches `spec.replicas` of the target
   Deployment.

> **Mutual exclusion with MASA.** Both systems write `spec.replicas` of
> `iotoccupancy-sample-mosquitto`. Never run them simultaneously. Before a
> baseline run, scale the MASA Action Agent to 0 (or delete the
> `MSAgenticScaling` CR); before a MASA run, delete the `ScaledObject`
> (the generated HPA is removed with it).

---

## 3. Telemetry: Thanos Prometheus Metrics

### Endpoint and authentication

| Item | Value |
|---|---|
| Server address | `https://thanos-querier.openshift-monitoring.svc:9091` |
| Auth | Bearer token of ServiceAccount `keda-thanos-reader` (namespace `iotoccupancy`) |
| Required permission | ClusterRole `cluster-monitoring-view` |
| TLS | Service CA signed certificate (`/var/run/secrets/kubernetes.io/serviceaccount/service-ca.crt`) or `unsafeSsl: "true"` for lab clusters |

Port `9091` is the cluster-wide query port and requires `cluster-monitoring-view`.
The tenancy port `9092` (namespace-scoped, requires a `namespace` parameter) is an
alternative if cluster-wide read access is not acceptable.

This mirrors MASA's internal fallback endpoint in
[`roles/masautoscaling/tasks/main.yml`](../masa-i/masa-autoscaler-operator/roles/masautoscaling/tasks/main.yml).

### Metrics exported by the workload

The metrics are produced client-side by the Paho MQTT load tester
([`load-test-py/loadtest.py`](../load-test-py/loadtest.py)) under Prometheus job
`mqtt-load-tester-metrics` (same job name MASA uses via
`customMetrics: mqtt-load-tester-metrics`).

| Metric | Type | Meaning |
|---|---|---|
| `mqtt_delivered_messages_total` | Counter | Messages acknowledged by the broker (QoS 2) |
| `mqtt_undelivered_messages_total` | Counter | Messages that failed / timed out |
| `mqtt_message_delivery_time_seconds_histogram_bucket` | Histogram | End-to-end delivery latency |

### PromQL mapping (identical to MASA's Understanding Agent)

The queries below are taken from
[`monitor.py`](../masa-i/masa-agent-perception/docker/src/data_monitoring/monitor.py:187)
and [`collect_metrics.py`](../masa-i/collect_metrics.py:105).

| Signal | PromQL | Used by baseline |
|---|---|---|
| Delivered rate | `sum(rate(mqtt_delivered_messages_total{job="mqtt-load-tester-metrics"}[1m]))` | part of primary trigger |
| Undelivered rate | `sum(rate(mqtt_undelivered_messages_total{job="mqtt-load-tester-metrics"}[1m]))` | part of primary trigger |
| **Total offered rate (primary trigger)** | `sum(rate(mqtt_delivered_messages_total{job="mqtt-load-tester-metrics"}[1m])) + sum(rate(mqtt_undelivered_messages_total{job="mqtt-load-tester-metrics"}[1m]))` | **yes** |
| Delivery % | `(delivered / (delivered + undelivered)) * 100` | evaluation only |
| P90 latency | `histogram_quantile(0.90, sum(rate(mqtt_message_delivery_time_seconds_histogram_bucket{job="mqtt-load-tester-metrics"}[1m])) by (le))` | optional variant / evaluation |
| Replicas | `kube_deployment_status_replicas{deployment="iotoccupancy-sample-mosquitto",namespace="iotoccupancy"}` | evaluation only |

**Why delivered + undelivered?** Delivered throughput alone *falls* when the
broker saturates (messages move to the undelivered counter), which would make a
throughput-only HPA scale *down* exactly when it should scale up. The sum
approximates the **offered load**, which is monotonic in demand regardless of
broker health. This is the same reasoning behind MASA's `undelivered_norm`
feature.

---

## 4. Mathematical Threshold Formulation

### HPA algorithm

For an external metric with `metricType: AverageValue`, the HPA divides the
query result by the current replica count and compares it to the target:

$$
\text{desiredReplicas} = \left\lceil \text{currentReplicas} \cdot \frac{M / \text{currentReplicas}}{T} \right\rceil = \left\lceil \frac{M}{T} \right\rceil
$$

where $M$ is the total offered message rate (msg/s) and $T$ is the per-pod
target (msg/s/pod). The result is clamped to $[\text{min}, \text{max}] = [1, 15]$.

The HPA skips scaling when the ratio is inside the global tolerance
(`--horizontal-pod-autoscaler-tolerance`, default $0.1$):

$$
\left| \frac{M / \text{currentReplicas}}{T} - 1 \right| \le 0.1 \;\Rightarrow\; \text{no change}
$$

### Deriving $T = 400$ msg/s/pod

The target is calibrated from MASA's empirical model response matrix
([`session_handoff_summary.md`](../masa-i/docs/session_handoff_summary.md), §4.B),
so the baseline sizes the broker comparably at steady state:

| Offered load $M$ (msg/s) | MASA decision | $M/T$ with $T=400$ | HPA decision $\lceil M/400 \rceil$ |
|---|---|---|---|
| 0 – 350 | 1 | ≤ 0.875 | 1 |
| 450 | 1 | 1.125 | 2 |
| 900 | 3 | 2.25 | 3 |
| 1,800 | 5 | 4.5 | 5 |
| 2,700 | 7 | 6.75 | 7 |
| 4,500 | 10 | 11.25 | 12 |
| ≥ 5,600 | up to 15 | ≥ 14 | 15 (capped at 6,000 msg/s) |

$T = 400$ reproduces MASA's decisions in the mid-load band (900–2,700 msg/s),
is slightly more conservative at the edges, and reaches `maxReplicaCount` at
$15 \times 400 = 6{,}000$ msg/s. Equivalently, the threshold provides headroom
relative to the observed per-pod capacity of roughly 450 msg/s/pod
($4{,}500 / 10$) at which MASA still meets the P90 ≤ 1.0 s SLA:

$$
T = \underbrace{450}_{\text{capacity/pod}} \times \underbrace{0.9}_{\text{headroom}} \approx 400 \text{ msg/s/pod}
$$

Sensitivity runs can vary $T \in \{300, 400, 500\}$ to show the
over-provisioning vs. SLA trade-off inherent to a fixed threshold.

### Load-profile reference

Each `mqtt-load-tester` pod runs 500 clients publishing every 0.5 s:

$$
\text{expected rate per pod} = 500 \times \frac{1}{0.5} = 1{,}000 \text{ msg/s}
$$

so the 12-pod peak offers up to 12,000 msg/s, well above the 6,000 msg/s at
which the baseline saturates at 15 replicas.

---

## 5. Scaling Behaviour Parameters

| Parameter | Value | MASA equivalent | Notes |
|---|---|---|---|
| `minReplicaCount` | `1` | `MIN_REPLICAS=1` | Prevents scale-to-zero; KEDA activation is never exercised |
| `maxReplicaCount` | `15` | `maxReplicas` set to 15 for the evaluation (operator default is 20) | Same ceiling as the 30-min MASA run (3–15 pods observed) |
| `pollingInterval` | `15` s | `MONITOR_INTERVAL_SECONDS=15` | Matches Understanding Agent cadence |
| `cooldownPeriod` | `60` s | Action Agent scale-down cooldown | Only applies to the 1→0 transition in KEDA; with `min=1` scale-down is governed by HPA `behavior` |
| PromQL rate window | `[1m]` | `[1m]` | Identical smoothing |
| `scaleUp.stabilizationWindowSeconds` | `0` | Immediate scale-up on SLA breach | Responsive scale-up |
| `scaleUp.policies` | `Percent 100 / 15s`, `Pods 4 / 15s`, `selectPolicy: Max` | — | At most doubles (or +4 pods) per 15 s |
| `scaleDown.stabilizationWindowSeconds` | `60` | Scale-down cooldown | HPA uses the highest recommendation of the last 60 s |
| `scaleDown.policies` | `Pods 2 / 30s` | — | Gradual scale-down to limit thrashing |

Planned `ScaledObject` skeleton (implemented in Sub-Task 4):

```yaml
apiVersion: keda.sh/v1alpha1
kind: ScaledObject
metadata:
  name: iotoccupancy-mosquitto-scaler
  namespace: iotoccupancy
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: iotoccupancy-sample-mosquitto
  minReplicaCount: 1
  maxReplicaCount: 15
  pollingInterval: 15
  cooldownPeriod: 60
  advanced:
    horizontalPodAutoscalerConfig:
      behavior:
        scaleUp:
          stabilizationWindowSeconds: 0
          selectPolicy: Max
          policies:
            - { type: Percent, value: 100, periodSeconds: 15 }
            - { type: Pods,    value: 4,   periodSeconds: 15 }
        scaleDown:
          stabilizationWindowSeconds: 60
          policies:
            - { type: Pods, value: 2, periodSeconds: 30 }
  triggers:
    - type: prometheus
      metricType: AverageValue
      authenticationRef:
        name: thanos-trigger-auth
      metadata:
        serverAddress: https://thanos-querier.openshift-monitoring.svc:9091
        authModes: bearer
        threshold: "400"
        query: >-
          sum(rate(mqtt_delivered_messages_total{job="mqtt-load-tester-metrics"}[1m]))
          + sum(rate(mqtt_undelivered_messages_total{job="mqtt-load-tester-metrics"}[1m]))
```

> In KEDA's Prometheus scaler the per-pod target is the `threshold` metadata
> field; together with `metricType: AverageValue` it becomes the HPA
> `target.averageValue`.

### Optional trigger variants (Sub-Task 4)

| Variant | Additional trigger | Purpose |
|---|---|---|
| Throughput-only | `delivered` rate, `threshold: 400` | Shows the saturation blind spot described in §3 |
| Throughput + latency | P90 latency, `metricType: Value`, `threshold: "1.0"` | Closer to MASA's SLA awareness; HPA takes the **max** of all trigger recommendations |
| Threshold sweep | Primary trigger with `threshold` 300 / 500 | Sensitivity analysis |

---

## 6. Comparison Runbook (MASA vs. KEDA + HPA)

### Prerequisites

- `oc` logged in as a cluster-admin (`oc whoami`).
- Workload deployed in `iotoccupancy` (`iotoccupancy-sample-mosquitto`,
  `mqtt-load-tester`, `load-test-scaler`).
- Python 3 with the dependencies of
  [`collect_metrics.py`](../masa-i/collect_metrics.py).

### Step 1 – Disable MASA

```bash
oc scale deployment/masautoscaling-sample-action-agent -n masa-system --replicas=0
oc scale deployment/iotoccupancy-sample-mosquitto -n iotoccupancy --replicas=1
```

### Step 2 – Install KEDA and the baseline

```bash
oc apply -k ginga/hpa/operator
oc wait --for=condition=Available deployment/keda-operator -n openshift-keda --timeout=300s
oc apply -k ginga/hpa/auth
oc apply -k ginga/hpa/scaling
# or: ./ginga/hpa/scripts/deploy-keda-hpa.sh
```

### Step 3 – Verify

```bash
oc get pods -n openshift-keda                 # operator + metrics-apiserver Running
oc get scaledobject -n iotoccupancy           # READY=True
oc get hpa -n iotoccupancy                    # TARGETS shows <value>/400 (avg)
oc describe scaledobject iotoccupancy-mosquitto-scaler -n iotoccupancy
```

Cross-check the HPA metric against Thanos directly:

```bash
THANOS=https://$(oc get route thanos-querier -n openshift-monitoring -o jsonpath='{.spec.host}')
curl -sk -H "Authorization: Bearer $(oc whoami --show-token)" \
  "$THANOS/api/v1/query" --data-urlencode \
  'query=sum(rate(mqtt_delivered_messages_total{job="mqtt-load-tester-metrics"}[1m]))+sum(rate(mqtt_undelivered_messages_total{job="mqtt-load-tester-metrics"}[1m]))'
```

### Step 4 – Run the 30-minute, 14-step load test

The profile is defined in
[`load_test_scaler.py`](../load-test-manage/load_test_scaler.py) and is the
same one used for the MASA evaluation:

| Phase | Duration | Load-tester pods per step | Steps |
|---|---|---|---|
| 1. Ramp-up | 5 min | 1 → 2 → 4 → 6 | 4 |
| 2. First spike | 5 min | 10 | 1 |
| 3. First cool-down | 5 min | 6 → 4 → 2 | 3 |
| 4. Second spike | 10 min | 10 → 12 | 2 |
| 5. Final cool-down | 5 min | 8 → 6 → 4 → 2 | 4 |
| **Total** | **30 min** | | **14** |

```bash
START=$(date -u +%Y-%m-%dT%H:%M:%SZ)
oc rollout restart deployment/load-test-scaler -n iotoccupancy
# wait 30 minutes
END=$(date -u +%Y-%m-%dT%H:%M:%SZ)
# or: ./ginga/hpa/scripts/run-hpa-benchmark.sh
```

### Step 5 – Collect metrics

```bash
python3 ginga/masa-i/collect_metrics.py \
  --thanos-url "https://$(oc get route thanos-querier -n openshift-monitoring -o jsonpath='{.spec.host}')" \
  --token "$(oc whoami --show-token)" \
  --start "$START" --end "$END" \
  --source thanos \
  --output ginga/hpa/hpa_load_test_30min.csv
```

The output has the same 15 s-sampled schema as
[`masa_load_test_30min_sep_25.csv`](../masa-i/masa_load_test_30min_sep_25.csv),
so both datasets can be fed to the same analysis/Excel tooling
([`generate_evaluation_excel.py`](../masa-i/generate_evaluation_excel.py)).

### Step 6 – Teardown and restore MASA

```bash
oc delete -k ginga/hpa/scaling      # removes ScaledObject and generated HPA
oc delete -k ginga/hpa/auth
# optional: oc delete -k ginga/hpa/operator
oc scale deployment/masautoscaling-sample-action-agent -n masa-system --replicas=1
# or: ./ginga/hpa/scripts/teardown-keda-hpa.sh
```

### Comparison KPIs

Use the same KPI definitions as the MASA report (§5.B of the handoff summary):

| KPI | Definition | MASA reference (Sep-25 run) |
|---|---|---|
| P90 SLA compliance | % of samples with P90 ≤ 1.0 s | 98.4 % |
| Sub-500 ms P90 | % of samples with P90 ≤ 0.5 s | 84.0 % |
| Delivery ≥ 95 % | % of samples with delivery ≥ 95 % | 92.8 % |
| Delivery ≥ 99 % | % of samples with delivery ≥ 99 % | 84.0 % |
| Mean delivery rate | mean delivery % | 96.47 % |
| Peak / mean delivered throughput | msg/s | 9,917.99 / 3,721.64 |
| Replica range / mean | pods | 3–15 / 8.86 |
| Replica-minutes | $\sum_i r_i \cdot 15\text{ s} / 60$ | cost proxy |
| Scaling events | count of replica changes | stability / thrashing |
| Reaction time | time from load step to new replica count | responsiveness |
| Peak CPU / memory | cores / MiB | 3.58 / 2,476.3 |

### Fairness checklist

- [ ] Only one autoscaler controls `iotoccupancy-sample-mosquitto` during a run.
- [ ] Start every run from 1 broker replica and 0 load-tester pods, after ≥ 5 min idle.
- [ ] Same load profile, client count (500/pod), publish interval (0.5 s) and QoS (2).
- [ ] Same replica bounds (1–15) and sampling interval (15 s).
- [ ] Same metric extraction script and time window length (30 min).
- [ ] Repeat each configuration ≥ 3 times and report mean ± std.

---

## 7. Troubleshooting

| Symptom | Likely cause | Check |
|---|---|---|
| ScaledObject `READY=False`, `401/403` in operator logs | Token missing or SA lacks `cluster-monitoring-view` | `oc logs -n openshift-keda deploy/keda-operator`; `oc describe clusterrolebinding` for `keda-thanos-reader` |
| `x509: certificate signed by unknown authority` | Service CA not trusted | Provide `ca` in TriggerAuthentication or set `unsafeSsl: "true"` |
| HPA `TARGETS` shows `<unknown>` | Metrics adapter cannot query Thanos or the query returns an empty vector | `oc describe hpa -n iotoccupancy`; run the PromQL in the console |
| Replicas oscillate | Threshold too close to per-pod capacity or short scale-down window | Increase `scaleDown.stabilizationWindowSeconds` or `threshold` |
| Replicas change unexpectedly during a baseline run | MASA Action Agent still active | `oc get deploy -n masa-system` |
