# MASA: Microservices Agentic Scaling Architecture

**MASA** is an open-source implementation of an adaptive, multi-agent architecture for autoscaling microservices in Kubernetes environments. It was developed as part of the master's dissertation *"An Adaptive Data-Driven Architecture for Microservices Autoscaling"* by João Paulo Karol Santos Nunes (USP, 2025).

> MASA combines real-time monitoring, machine-learning predictions, and infrastructure awareness to scale microservices efficiently — reducing cloud costs while meeting service-level objectives.

---

## Repository layout

```
ginga/                          ← repository root
├── masa-i/                     ← MASA implementation (this project)
│   ├── docs/                   ← full documentation
│   ├── masa-autoscaler-operator/
│   ├── masa-agent-understanding/
│   ├── masa-agent-perception/
│   ├── masa-agent-planning/
│   ├── masa-agent-action/
│   ├── masa-agent-reflection/
│   └── masa-baseimage/
├── load-test-manage/
└── load-test-py/
```

---

## What is MASA?

`masa-i/` implements the MASA architecture — an **agentic multi-agent environment** where five specialised agents collaborate to observe, learn, plan, act and reflect on scaling decisions for a target microservice. Each agent has a single well-defined responsibility and communicates with others exclusively through **MongoDB collections** (no direct inter-agent API calls).

A **Kubernetes Operator** (`MSAgenticScaling` CRD) deploys and manages all agents as CronJobs and Deployments.

---

## Agents

| Agent | Folder | Role | K8s type |
|---|---|---|---|
| **Understanding** | [`masa-agent-understanding/`](./masa-i/masa-agent-understanding/) | Reads the `MSAgenticScaling` CR → writes `governance_context` to MongoDB | CronJob |
| **Perception** | [`masa-agent-perception/`](./masa-i/masa-agent-perception/) | Queries Thanos/Prometheus → writes time-series metrics to MongoDB | CronJob |
| **Planning** | [`masa-agent-planning/`](./masa-i/masa-agent-planning/) | Trains model (`docker-training/`) + predicts replica count and writes scaling plan (`docker/`) | CronJob + Deployment |
| **Action** | [`masa-agent-action/`](./masa-i/masa-agent-action/) | Claims oldest pending scaling plan → patches Kubernetes Deployment replicas | Deployment |
| **Reflection** | [`masa-agent-reflection/`](./masa-i/masa-agent-reflection/) | Evaluates post-scaling quality → writes `model_feedback` → triggers model reset if needed | CronJob |

---

## Documentation

| Document | Description |
|---|---|
| [Architecture](./masa-i/docs/architecture.md) | Data-flow diagram, agent responsibilities, MongoDB collections, shared PVC files |
| [Operator & CRD](./masa-i/docs/operator.md) | Installation, `MSAgenticScaling` CR spec reference, RBAC, Thanos auto-discovery |
| [Agents](./masa-i/docs/agents.md) | Per-agent reference: env vars, volumes, SLA labels, plan status lifecycle |
| [Secrets & Security](./masa-i/docs/secrets.md) | Credential management, projected ServiceAccount tokens, resolution order |
| [Observability](./masa-i/docs/observability.md) | Prometheus metrics, ServiceMonitor config, alert rules, MongoDB audit trail |

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

The Planning agent trains a small neural network on real-world operational metrics labelled with service-level objectives (SLOs).

- **Input:** total MQTT messages (delivered + undelivered), normalised by observed maximum
- **Output:** recommended number of replicas
- **Training data:** `label=2` rows from `mongo_scaling.microservices` — observations where the current replica count was meeting the SLA
- **Retraining:** automatic via the Planning-Training CronJob; a new model is written to the shared PVC only when MSE improves on the current best
- **Quality gate:** the Reflection agent monitors rolling MAE and post-scaling SLA breaches; it deletes `best_mse.txt` to force unconditional retraining when quality degrades below threshold

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
