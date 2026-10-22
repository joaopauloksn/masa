# MASA: Microservices Agentic Scaling Architecture

**MASA** is an open-source implementation of an adaptive, multi-agent architecture for autoscaling microservices in Kubernetes environments. It was developed as part of the master's dissertation *"An Adaptive Data-Driven Architecture for Microservices Autoscaling"* by João Paulo Karol Santos Nunes (USP, 2025).

> MASA combines real-time monitoring, machine-learning predictions, and infrastructure awareness to scale microservices efficiently — reducing cloud costs while meeting service-level objectives.

---

## Repository layout

```
ginga/                          ← repository root
├── masa-i/                     ← MASA implementation (Multi-Agent Autoscaler)
│   ├── docs/                   ← full documentation & operational guides
│   ├── masa-autoscaler-operator/
│   ├── masa-agent-understanding/
│   ├── masa-agent-perception/
│   ├── masa-agent-planning/
│   ├── masa-agent-action/
│   ├── masa-agent-reflection/
│   ├── masa-dashboard/
│   ├── training/               ← offline training pipeline & model artifacts
│   └── masa-baseimage/
├── hpa/                        ← KEDA + Native HPA Baseline Benchmark suite
│   ├── operator/               ← OpenShift KEDA operator manifests
│   ├── auth/                   ← Thanos Querier RBAC & TriggerAuthentication
│   ├── scaling/                ← ScaledObject & HPA configuration
│   └── scripts/                ← Automated benchmark deployment & evaluation scripts
├── load-test-manage/           ← 14-step multi-phase load test controller
└── load-test-py/               ← Paho MQTT client (QoS 2, Prometheus metrics)
```

---

## What is MASA?

`masa-i/` implements the MASA architecture — an **agentic multi-agent environment** where five specialised agents collaborate to observe, learn, plan, act and reflect on scaling decisions for a target microservice. Each agent has a single well-defined responsibility and communicates with others exclusively through **MongoDB collections** (no direct inter-agent API calls).

A **Kubernetes Operator** (`MSAgenticScaling` CRD) deploys and manages all agents as CronJobs and Deployments.

---

## Agents

| Agent | Folder | Role | K8s type |
|---|---|---|---|
| **Understanding** | [`masa-agent-understanding/`](./masa-i/masa-agent-understanding/) | Reads the `MSAgenticScaling` CR → writes `governance_context` to MongoDB | Job / Event-driven |
| **Perception** | [`masa-agent-perception/`](./masa-i/masa-agent-perception/) | Scrapes OpenShift Thanos Querier (15s tick) → computes SLA labels → writes time-series metrics to MongoDB | Deployment |
| **Planning** | [`masa-agent-planning/`](./masa-i/masa-agent-planning/) | Runs 4-feature neural network inference (every 30s) + handles offline/online training | Deployment + CronJob |
| **Action** | [`masa-agent-action/`](./masa-i/masa-agent-action/) | Claims pending scaling plans, applies cooldown governance, and scales Kubernetes Deployment | Deployment |
| **Reflection** | [`masa-agent-reflection/`](./masa-i/masa-agent-reflection/) | Evaluates post-scaling quality ($N=5$), tracks rolling MAE, and resets MSE gate on persistent degradation | CronJob |

---

## Documentation

| Document | Description |
|---|---|
| [Architecture](./masa-i/docs/architecture.md) | Data-flow diagram, agent responsibilities, MongoDB collections, shared PVC files |
| [Operational State Handoff](./masa-i/docs/session_handoff_summary.md) | Comprehensive production handoff: agents, security, load test evaluation, runbooks |
| [Operator & CRD](./masa-i/docs/operator.md) | Installation, `MSAgenticScaling` CR spec reference, RBAC, Thanos auto-discovery |
| [Agents](./masa-i/docs/agents.md) | Per-agent reference: env vars, volumes, SLA labels, plan status lifecycle |
| [Secrets & Security](./masa-i/docs/secrets.md) | Credential management, projected ServiceAccount tokens, resolution order |
| [Offline Training Guide](./masa-i/docs/offline_training_guide.md) | Parameter sweep data collection, neural network retraining, model PVC deployment |
| [KEDA + HPA Baseline](./hpa/README.md) | Custom metrics baseline setup, PromQL triggers, comparison runbook |
| [Threshold Derivation & Analysis](./hpa/threshold_derivation_and_comparative_analysis.md) | Mathematical capacity derivation ($T=400$) and Oracle Baseline analysis |
| [Observability](./masa-i/docs/observability.md) | Prometheus metrics, ServiceMonitor config, alert rules, MongoDB audit trail |
| [Live Dashboard](./masa-i/docs/dashboard.md) | MASA live system health dashboard setup and UI overview |

---

## Quick start

**Prerequisites:** OpenShift 4.x (or Kubernetes 1.24+), `prometheus-operator`, MAS MongoDB replica set, `ocs-storagecluster-cephfs` StorageClass.

```bash
# 1. Create the MongoDB credentials secret
kubectl apply -f masa-i/masa-autoscaler-operator/config/samples/mongo-credentials-secret.yaml

# 2. Install the CRD
kubectl apply -f masa-i/masa-autoscaler-operator/config/crd/bases/scaling.masa.com_masautoscalings.yaml

# 3. Deploy the operator
cd masa-i/masa-autoscaler-operator
make deploy IMG=jpksn/masa-operator:1.0.0

# 4. Apply your custom resource (edit namespace/deployment name first)
kubectl apply -f masa-i/masa-autoscaler-operator/config/samples/scaling_v1alpha1_masautoscaling.yaml
```

The operator will automatically discover the Thanos Querier URL from the OpenShift route (set `spec.thanosUrl` to override), create the shared model PVC, and deploy all agents in the correct dependency order. See [Operator & CRD](./masa-i/docs/operator.md) for the full CR spec reference.

---

## Machine Learning Model

The Planning agent evaluates a 4-dimensional neural network regression model on real-world operational telemetry labelled with service-level objectives (SLOs).

- **Input Features (4-Vector):**
  1. `total_msg`: Total message throughput (delivered + undelivered), normalised to $[0, 300]$ via `max_total_msg`
  2. `pct_delivered_norm`: Percentage of successfully delivered messages $[0, 1]$
  3. `latency_ratio`: P90 latency normalised to $[0, 1]$ with a 2.0s ceiling
  4. `undelivered_norm`: Dropped message rate $[0, 1]$ (acts as a saturation pressure indicator)
- **Output:** Recommended replica count (continuous sizing mapped via zero-load bypass, low-load calibration, and ceil headroom)
- **Label-Adjusted Target Mapping:** Uses all operational data without discarding breach samples:
  - $\text{label}=1$ (SLA Breach) $\rightarrow \text{Target} = \text{Replicas} + 1$ (learns under-provisioning)
  - $\text{label}=2$ (SLA Met) $\rightarrow \text{Target} = \text{Replicas}$ (optimal baseline)
  - $\text{label}=3$ (Over-provisioned) $\rightarrow \text{Target} = \max(1, \text{Replicas} - 1)$ (learns efficiency trimming)
- **Quality Gate:** Reflection agent monitors rolling MAE and post-scaling SLA breaches ($N=5$). On 3 consecutive failure runs, it deletes `/mnt/model/best_mse.txt` to trigger unconditional model replacement on the next training cycle.

---

## Evaluation Summary

In an IoT case study (MQTT broker under variable load), MASA outperformed Kubernetes HPA:

| Metric | Improvement |
|---|---|
| CPU usage | ↓ 50 % |
| Memory usage | ↓ 87 % |
| Replica count | ↓ 90 % |

See Chapter 4 of the dissertation for full experiment details.

---

## Citation

> Nunes, J. P. K. S. (2025). *An Adaptive Data-Driven Architecture for Microservices Autoscaling*. Master's Dissertation – ICMC/USP.

---

## License

Licensed under the [Apache License, Version 2.0](https://www.apache.org/licenses/LICENSE-2.0).

---

## Acknowledgements

- Instituto de Ciências Matemáticas e de Computação – USP
- Prof. Elisa Yumi Nakagawa for supervision and guidance
