# Observability

## Prometheus metrics

The Reflection Agent exposes a Prometheus `/metrics` endpoint on port `8000` at the end of each CronJob run. The pod stays alive for `METRICS_HOLD_SECONDS` (default 30 s) after evaluation so Prometheus has time to scrape before the pod exits.

### Exposed metrics

| Metric | Type | Labels | Description |
|---|---|---|---|
| `masa_post_scaling_label1_ratio` | Gauge | `deployment`, `namespace` | Fraction of post-scaling observations with `label=1` (SLA breach). Higher = worse. |
| `masa_rolling_mae` | Gauge | `deployment`, `namespace` | Mean absolute error between model-predicted and actual replica counts at `label=2` rows. A rising value indicates model drift. |
| `masa_model_age_seconds` | Gauge | `deployment`, `namespace` | Seconds since `model.h5` was last modified. Alert when too old = no recent retraining. |
| `masa_consecutive_qa_failures` | Gauge | `deployment`, `namespace` | Count of consecutive QA runs exceeding the label-1 threshold. Resets to 0 after a corrective model reset. |

---

## ServiceMonitor

The operator creates a `monitoring.coreos.com/v1` `ServiceMonitor` resource alongside a `Service` that selects the Reflection Agent pod by label `app: masa-agent-reflection`.

```yaml
# Created automatically by the operator
apiVersion: monitoring.coreos.com/v1
kind: ServiceMonitor
metadata:
  name: {cr-name}-reflection-agent
  labels:
    openshift.io/cluster-monitoring: "true"   # picked up by OpenShift cluster monitoring
spec:
  selector:
    matchLabels:
      app: masa-agent-reflection
  endpoints:
    - port: metrics
      interval: 60s
      scrapeTimeout: 25s
```

> **Scrape timing note:** The CronJob pod exits 30 s after evaluation. The ServiceMonitor uses `interval: 60s` and `scrapeTimeout: 25s`. For reliable scraping, ensure the Prometheus scrape cycle aligns with the pod's lifetime window.

### Enabling scraping in non-openshift namespaces

On OpenShift the `openshift.io/cluster-monitoring: "true"` label on the ServiceMonitor tells the cluster-monitoring stack to scrape it. If you are running on vanilla Kubernetes, add the ServiceMonitor's namespace to your Prometheus `namespaceSelector` instead.

---

## Suggested alert rules

Add these to your Prometheus `PrometheusRule` or alerting config:

```yaml
groups:
  - name: masa-autoscaler
    rules:

      - alert: MASAHighSLABreachRatio
        expr: masa_post_scaling_label1_ratio > 0.3
        for: 10m
        labels:
          severity: warning
        annotations:
          summary: "High post-scaling SLA breach ratio"
          description: >
            {{ $labels.deployment }} in {{ $labels.namespace }} has a post-scaling
            label-1 ratio of {{ $value | humanizePercentage }} — model may be stale.

      - alert: MASAModelDriftDetected
        expr: masa_rolling_mae > 2
        for: 15m
        labels:
          severity: warning
        annotations:
          summary: "Model prediction drift detected"
          description: >
            Rolling MAE for {{ $labels.deployment }} is {{ $value }}.
            Consider checking if the workload pattern has shifted.

      - alert: MASAModelTooOld
        expr: masa_model_age_seconds > 7200   # 2 hours
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: "MASA model has not been retrained recently"
          description: >
            The model for {{ $labels.deployment }} is {{ $value | humanizeDuration }} old.
            Check if the planning-training CronJob is running correctly.

      - alert: MASAConsecutiveQAFailures
        expr: masa_consecutive_qa_failures >= 3
        for: 0m
        labels:
          severity: critical
        annotations:
          summary: "MASA triggering corrective model reset"
          description: >
            {{ $labels.deployment }} has had {{ $value }} consecutive QA failures.
            A model reset has been triggered — retraining will occur on the next cycle.
```

---

## MongoDB audit trail

In addition to Prometheus metrics, every Reflection Agent run writes a `model_feedback` document to `mongo_scaling.model_feedback`:

```json
{
  "evaluated_at": "2025-01-01T12:00:00Z",
  "deployment": "iotoccupancy-sample-mosquitto",
  "namespace": "iotoccupancy",
  "eval_window_minutes": 30,
  "post_scaling_rows_evaluated": 12,
  "label1_ratio": 0.083,
  "rolling_mae": 0.42,
  "model_age_seconds": 1842.5,
  "consecutive_failures": 0,
  "model_reset_triggered": false,
  "verdict": "good"
}
```

`verdict` values:
- `good` — label-1 ratio within threshold and MAE < 1.0
- `degraded` — label-1 ratio within threshold but MAE >= 1.0 or MAE unavailable
- `poor` — label-1 ratio exceeds `RESET_THRESHOLD`

Query recent verdicts:
```javascript
db.model_feedback.find({deployment: "my-service"}).sort({evaluated_at: -1}).limit(20)
```
