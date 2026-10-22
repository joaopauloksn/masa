# Operator & CRD Reference

## Overview

The MASA Operator is an **Ansible Operator** (built with Operator SDK). It watches `MSAgenticScaling` custom resources and reconciles the desired state by creating/updating all agent CronJobs, Deployments, the model PVC, and supporting Kubernetes objects.

- **API group:** `scaling.masa.com`
- **Kind:** `MSAgenticScaling`
- **API version:** `v1alpha1`
- **Operator image:** `jpksn/masa-operator:1.0.2`

---

## Namespaces

MASA uses **two independent namespaces** — one for the operator itself, one for the target microservice being scaled. They can be the same or different.

| Namespace | What lives here | How to configure |
|---|---|---|
| **Operator namespace** | Operator manager pod, all agent CronJobs/Deployments, model PVC, Secrets | `NAMESPACE` variable on `make deploy` (default: `masa-system`) |
| **Microservice namespace** | The target Deployment that MASA scales | `spec.microserviceNamespace` in the `MSAgenticScaling` CR |

### Operator namespace

The operator namespace is set at **deploy time** via kustomize:

```bash
# Deploy into the default namespace (masa-system):
make deploy IMG=jpksn/masa-operator:1.0.1

# Deploy into a custom namespace:
make deploy IMG=jpksn/masa-operator:1.0.1 NAMESPACE=my-ops-namespace
```

You can also edit [`config/default/kustomization.yaml`](../config/default/kustomization.yaml) directly before deploying:

```yaml
namespace: my-ops-namespace
namePrefix: masa-
```

All agents — Understanding, Perception, Planning (training + prediction), Action, and Reflection — are deployed into this same namespace. The `MSAgenticScaling` CR must also be created in this namespace.

### Microservice namespace

The microservice being autoscaled can live in **any namespace** — it does not need to be in the operator namespace. Set it in the CR:

```yaml
spec:
  microserviceNamespace: "my-app-namespace"   # namespace of the Deployment to scale
  microserviceDeployment: "my-service"
```

The Action Agent uses the operator's `controller-manager` ServiceAccount to patch the target Deployment. If the microservice is in a different namespace, ensure the ClusterRole (`config/rbac/role.yaml`) grants `deployments` patch/update verbs cluster-wide (it does — it is a `ClusterRole`, not a `Role`).

---

## Installation

### Prerequisites

- OpenShift 4.x or Kubernetes 1.24+
- `prometheus-operator` installed (for ServiceMonitor support)
- A StorageClass that supports `ReadWriteMany` / `Filesystem` (auto-detected; `ocs-storagecluster-cephfs`, NFS, Azure Files, EFS, etc.) — or override via `spec.storageClassName`
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

**Thanos token** — the operator uses a **projected ServiceAccount token** (auto-rotates every hour). No static token secret is needed; the volume is configured automatically in the perception-agent Deployment.

### Step 3 — Deploy the operator

```bash
cd masa-autoscaler-operator
make deploy IMG=jpksn/masa-operator:1.0.1
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

  # ── Training toggle ──────────────────────────────────────────────
  # Keep false while collecting data. Flip to true when ready to train.
  trainingEnabled: false   # default: false

  # ── Perception interval & CronJob schedules (all optional) ──────
  # dataMonitoringInterval: 15               # seconds between perception cycles (default: 15)
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
| `trainingEnabled` | boolean | ❌ | `false` | When `false` the planning-training CronJob is suspended. Set to `true` once enough data has been collected by the perception agent. |
| `dataMonitoringInterval` | integer | ❌ | `15` | Seconds between perception-agent collection cycles (minimum 5) |
| `modelTrainingSchedule` | string | ❌ | `*/30 * * * *` | Cron for planning-training |
| `qaSchedule` | string | ❌ | `*/5 * * * *` | Cron for reflection-agent |
| `storageClassName` | string | ❌ | auto-detected | StorageClass for the model PVC (RWX + Filesystem). Auto-discovers first matching class from the cluster; falls back to cluster default if none found. |
| `modelPvcSize` | string | ❌ | `500Mi` | Storage size for the model PVC (e.g. `1Gi`) |
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
| Understanding | Job (event-driven) | `{cr-name}-understanding-agent-{hash}` |
| Perception | Deployment | `{cr-name}-perception-agent` |
| Planning Training | CronJob | `{cr-name}-planning-training` |
| Planning Prediction | Deployment | `{cr-name}-planning-agent` |
| Action | Deployment | `{cr-name}-action-agent` |
| Reflection | CronJob | `{cr-name}-reflection-agent` |
| Reflection metrics | Service | `{cr-name}-reflection-agent-metrics` |
| Reflection scraping | ServiceMonitor | `{cr-name}-reflection-agent` |
| Dashboard | Deployment | `{cr-name}-dashboard` |
| Dashboard | Service | `{cr-name}-dashboard` |
| Dashboard | Route | `{cr-name}-dashboard` |

---

## Training workflow

MASA follows a two-phase workflow: **data collection** then **training**.

### Phase 1 — Collect data (default state)

Deploy the CR with `trainingEnabled: false` (the default). The perception agent starts collecting metrics every 15 seconds (configurable via `spec.dataMonitoringInterval`) and writing them to MongoDB. The planning-training CronJob is created but **suspended** — it never fires.

Let the system run with HPA or manual scaling variations to build up a representative dataset.

### Phase 2 — Start training

Once you have enough data, flip the toggle:

```bash
kubectl patch masautoscaling masautoscaling-sample -n masa-system \
  --type=merge -p '{"spec":{"trainingEnabled":true}}'
```

The operator reconciles within seconds, unsuspends the training CronJob, and the next scheduled run (within 30 minutes, or triggered manually) trains the model and writes it to the shared PVC. The planning agent picks it up on its next prediction cycle.

### Trigger training immediately

```bash
kubectl create job --from=cronjob/masautoscaling-sample-planning-training \
  manual-training-001 -n masa-system
```

### Pause training again

```bash
kubectl patch masautoscaling masautoscaling-sample -n masa-system \
  --type=merge -p '{"spec":{"trainingEnabled":false}}'
```

---

## RBAC

The operator's `masa-controller-manager` ServiceAccount (note: kustomize adds the `masa-` prefix) holds a `ClusterRole` with the following permissions:

| API Group | Resources | Verbs |
|---|---|---|
| `` (core) | secrets, pods, pvcs, services, serviceaccounts | CRUD |
| `apps` | deployments, daemonsets, replicasets, statefulsets | CRUD |
| `batch` | jobs, cronjobs | CRUD |
| `route.openshift.io` | routes | get, list, watch |
| `scaling.masa.com` | masautoscalings + status + finalizers | CRUD |
| `monitoring.coreos.com` | servicemonitors | CRUD |
| `storage.k8s.io` | storageclasses | get, list, watch |

Additionally, the `cluster-monitoring-view` ClusterRole must be granted manually so the perception agent can query Thanos:

```bash
oc adm policy add-cluster-role-to-user cluster-monitoring-view \
  -z masa-controller-manager \
  -n masa-system
```
