# Operator & CRD Reference

## Overview

The MASA Operator is an **Ansible Operator** (built with Operator SDK). It watches `MSAgenticScaling` custom resources and reconciles the desired state by creating/updating all agent CronJobs, Deployments, the model PVC, and supporting Kubernetes objects.

- **API group:** `scaling.masa.com`
- **Kind:** `MSAgenticScaling`
- **API version:** `v1alpha1`
- **Operator image:** `jpksn/masa-operator:1.0.0`

---

## Installation

### Prerequisites

- OpenShift 4.x or Kubernetes 1.24+
- `prometheus-operator` installed (for ServiceMonitor support)
- `ocs-storagecluster-cephfs` StorageClass available (for the model PVC)
- MongoDB replica set accessible from within the cluster
- Thanos Querier accessible (in-cluster or via Route)

### Step 1 — Install the CRD

```bash
kubectl apply -f masa-autoscaler-operator/config/crd/bases/scaling.masa.com_masautoscalings.yaml
```

### Step 2 — Create credentials secrets

The operator expects two secrets in the same namespace as the CR:

**MongoDB credentials** (`mongo-credentials`):
```bash
kubectl apply -f masa-autoscaler-operator/config/samples/mongo-credentials-secret.yaml
# or manually:
kubectl create secret generic mongo-credentials \
  --from-literal=username=<user> \
  --from-literal=password=<password>
```

**Thanos token** — the operator uses a **projected ServiceAccount token** (auto-rotates every hour). No static token secret is needed; the volume is configured automatically in the perception-agent CronJob.

### Step 3 — Deploy the operator

```bash
cd masa-autoscaler-operator
make deploy IMG=jpksn/masa-operator:1.0.0
```

### Step 4 — Apply a CR

```bash
kubectl apply -f masa-autoscaler-operator/config/samples/scaling_v1alpha1_masautoscaling.yaml
```

---

## `MSAgenticScaling` CR spec reference

```yaml
apiVersion: scaling.masa.com/v1alpha1
kind: MSAgenticScaling
metadata:
  name: masautoscaling-sample
spec:
  # ── Target microservice ──────────────────────────────────────────
  microserviceNamespace: "iotoccupancy"       # required
  microserviceDeployment: "my-service"        # required

  # ── Thanos URL (optional) ────────────────────────────────────────
  # Omit to auto-discover from the OpenShift route.
  # Set to bypass auto-discovery (local testing, restricted RBAC).
  # thanosUrl: "https://thanos-querier-openshift-monitoring.apps.cluster.example.com/api/v1/query"

  # ── Replica count for Deployments ───────────────────────────────
  size: 1   # default: 1

  # ── CronJob schedules (all optional) ────────────────────────────
  # dataMonitoringSchedule: "*/1 * * * *"    # default
  # modelTrainingSchedule:  "*/30 * * * *"   # default
  # qaSchedule:             "*/5 * * * *"    # default

  # ── Microservice intent ──────────────────────────────────────────
  microservices:
    - name: my-service
      sla:
        strategy: Conservative   # Conservative | Normal | BestEffort
      modelRequirements:
        scalingStrategy: proactive   # proactive | reactive | any
      serverRequirements:
        serverType: apache           # liberty | gunicorn | apache | nodejs | tomcat
        startupOptimization: true
        scalingType: horizontal      # horizontal | vertical | any
        customMetrics: my-prometheus-job   # Prometheus job label
```

### Field reference

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `microserviceNamespace` | string | ✅ | — | Namespace of the target Deployment |
| `microserviceDeployment` | string | ✅ | — | Name of the target Deployment |
| `thanosUrl` | string | ❌ | auto-discovered | Full Thanos query API URL |
| `size` | integer | ❌ | `1` | Replicas for action-agent and planning-agent Deployments |
| `dataMonitoringSchedule` | string | ❌ | `*/1 * * * *` | Cron for perception-agent |
| `modelTrainingSchedule` | string | ❌ | `*/30 * * * *` | Cron for planning-training |
| `qaSchedule` | string | ❌ | `*/5 * * * *` | Cron for reflection-agent |
| `microservices[].sla.strategy` | enum | ✅ | `Conservative` | SLA aggressiveness |
| `microservices[].modelRequirements.scalingStrategy` | enum | ✅ | `any` | Model prediction mode |
| `microservices[].serverRequirements.serverType` | enum | ✅ | — | Application server type |
| `microservices[].serverRequirements.customMetrics` | string | ❌ | — | Prometheus job name for MQTT metrics |

---

## Thanos URL auto-discovery

When `spec.thanosUrl` is not set, the operator runs the following resolution chain:

1. Query the OpenShift route `app.kubernetes.io/name=thanos-querier` in `openshift-monitoring`
2. If found → `https://<route.spec.host>/api/v1/query`
3. If not found → `https://thanos-querier.openshift-monitoring.svc:9091/api/v1/query` (in-cluster fallback)

> **Note:** the in-cluster address (port 9091) requires the operator's ServiceAccount to have the `cluster-monitoring-view` ClusterRole. The external route requires no additional permissions but may be slower.

---

## Kubernetes resources created per CR

| Resource | Kind | Name pattern |
|---|---|---|
| Model storage | PersistentVolumeClaim | `model-pvc` |
| Understanding | CronJob | `{cr-name}-understanding-agent` |
| Perception | CronJob | `{cr-name}-perception-agent` |
| Planning Training | CronJob | `{cr-name}-planning-training` |
| Planning Prediction | Deployment | `{cr-name}-planning-agent` |
| Action | Deployment | `{cr-name}-action-agent` |
| Reflection | CronJob | `{cr-name}-reflection-agent` |
| Reflection metrics | Service | `{cr-name}-reflection-agent-metrics` |
| Reflection scraping | ServiceMonitor | `{cr-name}-reflection-agent` |

---

## RBAC

The operator's `controller-manager` ServiceAccount holds a `ClusterRole` with the following permissions:

| API Group | Resources | Verbs |
|---|---|---|
| `` (core) | secrets, pods, pvcs, services, serviceaccounts | CRUD |
| `apps` | deployments, daemonsets, replicasets, statefulsets | CRUD |
| `batch` | jobs, cronjobs | CRUD |
| `route.openshift.io` | routes | get, list, watch |
| `scaling.masa.com` | masautoscalings + status + finalizers | CRUD |
| `monitoring.coreos.com` | servicemonitors | CRUD |
