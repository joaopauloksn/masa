# MASA — Microservices Agentic Scaling Architecture

MASA is a Kubernetes-native agentic autoscaler. A Kubernetes Operator deploys a set of specialised AI agents that collectively observe a target microservice, learn from its behaviour, and autonomously scale it to meet SLA objectives.

## Documentation index

| Document | Description |
|---|---|
| [Architecture](./architecture.md) | System overview, agent roles, data flows, MongoDB collections |
| [Operational State Handoff](./session_handoff_summary.md) | Full technical handoff: agents, security, load test evaluation, runbooks |
| [Operator & CRD](./operator.md) | How to install, configure and operate the operator |
| [Agents](./agents.md) | Per-agent reference: responsibilities, env vars, PVC files |
| [Secrets & Security](./secrets.md) | How credentials are managed (mounted secrets, projected tokens) |
| [Offline Training Guide](./offline_training_guide.md) | Parameter sweep data collection, neural network retraining, model PVC deployment |
| [Observability](./observability.md) | Prometheus metrics, ServiceMonitor, alerting guidance |
| [Dashboard](./dashboard.md) | Live system health dashboard — setup and access |
| [KEDA + HPA Baseline Benchmark](../../hpa/README.md) | Red Hat Custom Metrics Autoscaler baseline and comparison runbook |

---

## Project layout

```
masa-i/
├── docs/                          ← You are here
├── masa-autoscaler-operator/      ← Ansible Operator (deploys all agents)
│   ├── config/
│   │   ├── crd/bases/             ← MSAgenticScaling CRD
│   │   ├── rbac/role.yaml         ← ClusterRole
│   │   └── samples/               ← Example CR + Secrets
│   └── roles/masautoscaling/
│       └── tasks/main.yml         ← All K8s resources defined here
│
├── masa-agent-understanding/      ← Understanding Agent
│   └── docker/src/
│
├── masa-agent-perception/         ← Perception Agent (data monitoring)
│   └── docker/src/
│
├── masa-agent-planning/           ← Planning Agent (two images)
│   ├── docker/src/                ← Prediction (Deployment)
│   └── docker-training/src/       ← Model training (CronJob)
│
├── masa-agent-action/             ← Action Agent (executes scaling)
│   └── docker/src/
│
├── masa-agent-reflection/         ← Reflection Agent (QA + feedback)
│   └── docker/
│
└── masa-baseimage/                ← Shared Python base image
    └── docker/Dockerfile
```

## Quick start

### 1. Create credentials secrets

```bash
kubectl apply -f masa-autoscaler-operator/config/samples/mongo-credentials-secret.yaml
kubectl apply -f masa-autoscaler-operator/config/samples/thanos-credentials-secret.yaml
```

### 2. Install the CRD

```bash
kubectl apply -f masa-autoscaler-operator/config/crd/bases/scaling.masa.com_masautoscalings.yaml
```

### 3. Deploy the operator

```bash
cd masa-autoscaler-operator
make deploy IMG=jpksn/masa-operator:1.0.1
```

### 4. Apply your CR

```bash
kubectl apply -f masa-autoscaler-operator/config/samples/scaling_v1alpha1_masautoscaling.yaml
```

The operator will automatically:
- Discover the Thanos Querier URL from the OpenShift route (or use `spec.thanosUrl` to override)
- Create the shared model PVC
- Deploy all 5 agents in the correct order

---

## Agent startup order

The operator deploys agents in this order to ensure dependencies are satisfied before consumers start:

```
1. understanding-agent  (writes governance_context to MongoDB)
2. model-pvc            (PersistentVolumeClaim for model artefacts)
3. perception-agent     (Deployment — reads Thanos every 15s, writes microservices collection)
4. action-agent         (Deployment — polls scaling_plans, applies K8s patches)
5. planning-training    (CronJob — suspended until trainingEnabled: true)
6. planning-agent       (Deployment — waits for model; logs warning until trained)
7. reflection-agent     (CronJob — evaluates quality, writes model_feedback)
```

## Typical first-run workflow

```
Deploy CR (trainingEnabled: false)
  → perception collects data every minute
  → planning-agent logs "Model file not found – waiting for training"  ← expected
  → reflection-agent skips evaluation when all metrics are None        ← expected

[collect data for desired period with HPA variations]

kubectl patch ... trainingEnabled: true
  → planning-training CronJob unsuspended
  → model trained and saved to PVC within 30 min (or trigger manually)
  → planning-agent starts predicting and writing scaling_plans
  → action-agent executes plans
  → reflection-agent starts evaluating quality
```
