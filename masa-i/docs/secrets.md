# Secrets & Security

MASA follows the principle that **passwords are never stored in environment variables or ConfigMaps**. All credentials are read from **mounted Kubernetes Secret volumes**. Env vars remain as an emergency fallback only.

---

## MongoDB credentials

### Secret format

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: mongo-credentials
  namespace: masa-autoscaler-operator-system
type: Opaque
stringData:
  username: "<your-mongo-username>"
  password: "<your-mongo-password>"
```

### Mount path

Every agent that connects to MongoDB mounts this secret at `/etc/secrets/mongo` (configurable via `MONGO_SECRET_DIR`):

```yaml
volumeMounts:
  - name: mongo-credentials
    mountPath: /etc/secrets/mongo
    readOnly: true
volumes:
  - name: mongo-credentials
    secret:
      secretName: mongo-credentials
```

### Resolution order (all agents)

1. File `/etc/secrets/mongo/username` + `/etc/secrets/mongo/password`
2. Env vars `MONGO_USERNAME` / `MONGO_PASSWORD` (fallback, not set by operator)

If neither source provides credentials the agent raises `EnvironmentError` and exits.

---

## Thanos / Prometheus token

### Why a projected token instead of a static token

`oc whoami --show-token` produces a short-lived token tied to your personal session. It will expire, cannot be rotated automatically, and embedding it in a Secret is a security risk.

MASA uses a **Kubernetes projected ServiceAccount token** instead:

```yaml
volumes:
  - name: thanos-token
    projected:
      sources:
        - serviceAccountToken:
            path: token
            expirationSeconds: 3600   # auto-rotated every hour by kubelet
            audience: https://kubernetes.default.svc
```

The kubelet automatically rotates the file at `/etc/secrets/thanos/token` before expiry. The Perception Agent reads the file on every request, so it always uses a fresh token.

### Resolution order (Perception Agent)

1. File `/etc/secrets/thanos/token`
2. Env var `THANOS_TOKEN` (fallback, useful for local testing)

If no token is found the agent raises `EnvironmentError` and exits.

---

## Thanos URL

The Thanos URL itself is **not a secret** but is resolved at runtime to avoid hard-coding cluster-specific hostnames.

### Resolution order (operator → Perception Agent)

1. `spec.thanosUrl` in the CR → operator injects it as `THANOS_URL` env var
2. OpenShift route auto-discovery: `route.openshift.io/v1` in `openshift-monitoring` with label `app.kubernetes.io/name=thanos-querier`
3. In-cluster service: `https://thanos-querier.openshift-monitoring.svc:9091/api/v1/query`

### Resolution order (Perception Agent container)

1. `THANOS_URL` env var (set by operator)
2. `THANOS_ROUTE_HOST` env var → builds `https://{host}/api/v1/query`
3. In-cluster service URL (hardcoded fallback)

---

## Secret sample files

| File | Purpose |
|---|---|
| `config/samples/mongo-credentials-secret.yaml` | MongoDB username/password template |
| `config/samples/thanos-credentials-secret.yaml` | Placeholder — not used by default (projected token is used instead) |

---

## Security checklist

- [x] No passwords in env vars or ConfigMaps in production
- [x] Mongo secret mounted read-only
- [x] Thanos token uses projected ServiceAccount (auto-rotates, audience-scoped)
- [x] `tlsInsecure: true` — acceptable for internal cluster traffic; set to `false` and provide CA bundle for production hardening
- [x] All agents use `authMechanism: SCRAM-SHA-256` for MongoDB authentication
