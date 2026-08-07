# Ginga: Adaptive Microservices Autoscaling Framework

**Ginga** is an open‑source implementation of an adaptive, multi-agent architecture for autoscaling microservices in Kubernetes environments. It was developed as part of the master's dissertation *"An Adaptive Data‑Driven Architecture for Microservices Autoscaling"* by João Paulo Karol Santos Nunes (USP, 2025).

> Ginga combines real‑time monitoring, machine‑learning predictions, and infrastructure awareness to scale microservices efficiently — reducing cloud costs and improving performance.

---

## Repository layout

```
ginga/
├── masa-i/          ← MASA: Multi-Agent Scaling Architecture (current implementation)
├── load-test-manage/
└── load-test-py/
```

---

## MASA — Multi-Agent Scaling Architecture

`masa-i/` is the current implementation. It replaces the original single-component architecture with a **multi-agent environment** where six specialised agents collaborate to observe, learn, plan, act and reflect on scaling decisions.

### Agents at a glance

| Agent | Role | K8s type |
|---|---|---|
| **Understanding** | Reads the CR spec → writes `governance_context` to MongoDB | CronJob |
| **Perception** | Queries Thanos/Prometheus → writes time-series metrics to MongoDB | CronJob |
| **Planning — Training** | Trains a neural net on historical metrics → saves model to shared PVC | CronJob |
| **Planning — Prediction** | Loads model → predicts replica count → writes scaling plan to MongoDB | Deployment |
| **Action** | Claims oldest pending scaling plan → patches Kubernetes Deployment replicas | Deployment |
| **Reflection** | Evaluates post-scaling quality → writes `model_feedback` → triggers model reset if needed | CronJob |

Agents communicate exclusively through **MongoDB collections** — no direct inter-agent API calls.

### Quick start

```bash
# 1. Create the MongoDB credentials secret
kubectl apply -f masa-i/masa-autoscaler-operator/config/samples/mongo-credentials-secret.yaml

# 2. Install the CRD
kubectl apply -f masa-i/masa-autoscaler-operator/config/crd/bases/scaling.masa.com_masautoscalings.yaml

# 3. Deploy the operator
cd masa-i/masa-autoscaler-operator
make deploy IMG=jpksn/masa-operator:1.0.0

# 4. Apply your custom resource
kubectl apply -f masa-i/masa-autoscaler-operator/config/samples/scaling_v1alpha1_masautoscaling.yaml
```

The operator auto-discovers the Thanos Querier URL from the OpenShift route (or use `spec.thanosUrl` to override), creates the shared model PVC, and deploys all agents in the correct dependency order.

### Documentation

| Document | Description |
|---|---|
| [Architecture](./masa-i/docs/architecture.md) | Data-flow diagram, agent responsibilities, MongoDB collections, shared PVC files |
| [Operator & CRD](./masa-i/docs/operator.md) | Installation, `MSAgenticScaling` CR spec reference, RBAC, Thanos auto-discovery |
| [Agents](./masa-i/docs/agents.md) | Per-agent reference: env vars, volumes, SLA labels, plan status lifecycle |
| [Secrets & Security](./masa-i/docs/secrets.md) | Credential management, projected ServiceAccount tokens, resolution order |
| [Observability](./masa-i/docs/observability.md) | Prometheus metrics, ServiceMonitor config, alert rules, MongoDB audit trail |

---

## Machine‑Learning Model

The planning agent trains a small neural network on real-world operational metrics labelled with service-level objectives (SLOs).

- **Input:** total MQTT messages (delivered + undelivered), normalised
- **Output:** recommended number of replicas
- **Training data:** `label=2` rows from the `mongo_scaling.microservices` collection (observations where the current replica count was meeting the SLA)
- **Retraining:** automatic, via the Planning-Training CronJob; a new model is saved to the shared PVC only when MSE improves
- **Quality gate:** the Reflection agent monitors MAE drift and post-scaling SLA breaches; it deletes `best_mse.txt` to force unconditional retraining when quality degrades

---

## Evaluation Summary

In an IoT case study (MQTT broker load), the adaptive autoscaler outperformed Kubernetes HPA:

| Metric | Improvement |
|---|---|
| CPU usage | ↓ 50 % |
| Memory usage | ↓ 87 % |
| Replica count | ↓ 90 % |

See Chapter 4 of the dissertation for full experiment details.

---

## Citation

> Nunes, J. P. K. S. (2025). *An Adaptive Data‑Driven Architecture for Microservices Autoscaling*. Master's Dissertation – ICMC/USP.

---

## License

Licensed under the [Apache License, Version 2.0](https://www.apache.org/licenses/LICENSE-2.0).

---

## Acknowledgements

- Instituto de Ciências Matemáticas e de Computação – USP
- Prof. Elisa Yumi Nakagawa for supervision and guidance
