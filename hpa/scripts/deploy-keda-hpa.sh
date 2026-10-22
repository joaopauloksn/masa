#!/usr/bin/env bash
# =============================================================================
# deploy-keda-hpa.sh
#
# One-click deployment of the KEDA + HPA baseline autoscaler for the MASA
# comparison benchmark.  Applies manifests in order:
#   1. Operator (Namespace, OperatorGroup, Subscription, KedaController)
#   2. Auth     (ServiceAccount + Token Secret, ClusterRoleBinding, TriggerAuthentication)
#   3. Scaling  (ScaledObject  →  native HPA)
#
# Prerequisites:
#   - oc CLI installed and logged in to the target OpenShift cluster.
#   - Cluster-admin or equivalent permissions (needed for CRD/CSV installation
#     and ClusterRoleBinding creation).
#
# Usage:
#   chmod +x deploy-keda-hpa.sh
#   ./deploy-keda-hpa.sh
#
# Override defaults via environment variables:
#   OPERATOR_NS=openshift-keda LOAD_NS=iotoccupancy MASA_NS=masa-system \
#   SKIP_MASA_CHECK=0 ./deploy-keda-hpa.sh
# =============================================================================

set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration — override via env vars
# ---------------------------------------------------------------------------
OPERATOR_NS="${OPERATOR_NS:-openshift-keda}"
LOAD_NS="${LOAD_NS:-iotoccupancy}"
MASA_NS="${MASA_NS:-masa-system}"
ACTION_AGENT_DEPLOY="${ACTION_AGENT_DEPLOY:-masautoscaling-sample-action-agent}"

# Set to 1 to skip the MASA action-agent conflict check (e.g. MASA not deployed)
SKIP_MASA_CHECK="${SKIP_MASA_CHECK:-0}"

# Timeout values (seconds)
CSV_TIMEOUT="${CSV_TIMEOUT:-180}"
KEDA_PODS_TIMEOUT="${KEDA_PODS_TIMEOUT:-120}"
TOKEN_TIMEOUT="${TOKEN_TIMEOUT:-60}"
SCALEDOBJECT_TIMEOUT="${SCALEDOBJECT_TIMEOUT:-120}"

# Resolve the directory containing this script and find the repo root
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HPA_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------
log()  { echo "[$(date '+%H:%M:%S')] $*"; }
ok()   { echo "[$(date '+%H:%M:%S')] ✓ $*"; }
warn() { echo "[$(date '+%H:%M:%S')] WARN: $*" >&2; }
fail() { echo "[$(date '+%H:%M:%S')] ERROR: $*" >&2; exit 1; }

wait_for() {
    # wait_for <interval_s> <timeout_s> <description> <condition_command...>
    local interval="$1" timeout="$2" description="$3"
    shift 3
    local elapsed=0
    while ! "$@" &>/dev/null; do
        if [ "$elapsed" -ge "$timeout" ]; then
            fail "Timed out after ${timeout}s waiting for: ${description}"
        fi
        log "  Waiting for ${description} ... (${elapsed}s / ${timeout}s)"
        sleep "$interval"
        elapsed=$(( elapsed + interval ))
    done
    ok "${description}"
}

# ---------------------------------------------------------------------------
# Pre-flight checks
# ---------------------------------------------------------------------------
log "=== Pre-flight checks ==="

if ! command -v oc &>/dev/null; then
    fail "'oc' CLI not found. Install the OpenShift CLI and ensure it is in PATH."
fi

if ! oc whoami &>/dev/null; then
    fail "Not logged in to OpenShift. Run 'oc login <cluster-api-url> ...' first."
fi
log "Logged in as: $(oc whoami)"
log "Current context: $(oc config current-context 2>/dev/null || echo 'unknown')"

# Verify operator and target namespaces exist (or can be created)
if ! oc get namespace "${LOAD_NS}" &>/dev/null; then
    fail "Namespace '${LOAD_NS}' does not exist. The MQTT workload must be deployed first."
fi
ok "Target namespace '${LOAD_NS}' exists."

# Check for MASA action agent — warn and offer to pause it to avoid conflicting scalers
if [ "${SKIP_MASA_CHECK}" -eq 0 ]; then
    if oc get deployment/"${ACTION_AGENT_DEPLOY}" -n "${MASA_NS}" &>/dev/null; then
        AGENT_REPLICAS=$(oc get deployment/"${ACTION_AGENT_DEPLOY}" -n "${MASA_NS}" \
            -o jsonpath='{.spec.replicas}' 2>/dev/null || echo "0")
        if [ "${AGENT_REPLICAS}" != "0" ]; then
            warn "MASA action agent '${ACTION_AGENT_DEPLOY}' is running with ${AGENT_REPLICAS} replica(s)."
            warn "Running both MASA and the KEDA+HPA scaler simultaneously will cause conflicting scale decisions."
            echo ""
            read -r -p "Pause the MASA action agent now (scale to 0)? [Y/n] " response
            response="${response:-Y}"
            if [[ "$response" =~ ^[Yy]$ ]]; then
                log "Pausing MASA action agent ..."
                oc scale deployment/"${ACTION_AGENT_DEPLOY}" -n "${MASA_NS}" --replicas=0
                wait_for 5 60 "action agent scaled to 0" \
                    bash -c "[ \"\$(oc get deployment/${ACTION_AGENT_DEPLOY} -n ${MASA_NS} -o jsonpath='{.status.availableReplicas}' 2>/dev/null)\" = \"\" ]"
                ok "MASA action agent paused."
            else
                warn "Proceeding with MASA action agent still running. Benchmark results may be skewed."
            fi
        else
            ok "MASA action agent is already at 0 replicas — no conflict."
        fi
    else
        log "INFO: '${ACTION_AGENT_DEPLOY}' not found in namespace '${MASA_NS}' — skipping MASA conflict check."
    fi
fi

log ""
log "=== Step 1: Deploy KEDA Operator ==="

# Apply operator manifests (Namespace, OperatorGroup, Subscription, KedaController)
log "Applying operator manifests from ${HPA_DIR}/operator/ ..."
oc apply -k "${HPA_DIR}/operator/"
ok "Operator manifests applied."

# Wait for the CSV (ClusterServiceVersion) to reach Succeeded phase
log "Waiting for KEDA CSV to reach 'Succeeded' phase in namespace '${OPERATOR_NS}' ..."
ELAPSED=0
CSV_NAME=""
while [ -z "$CSV_NAME" ] || ! oc get csv "${CSV_NAME}" -n "${OPERATOR_NS}" \
        -o jsonpath='{.status.phase}' 2>/dev/null | grep -q "^Succeeded$"; do
    if [ "${ELAPSED}" -ge "${CSV_TIMEOUT}" ]; then
        fail "KEDA CSV did not reach 'Succeeded' within ${CSV_TIMEOUT}s. Check: oc get csv -n ${OPERATOR_NS}"
    fi
    CSV_NAME=$(oc get csv -n "${OPERATOR_NS}" \
        -o jsonpath='{.items[?(@.spec.displayName=="Custom Metrics Autoscaler")].metadata.name}' \
        2>/dev/null | awk '{print $1}')
    [ -z "$CSV_NAME" ] && CSV_NAME=$(oc get csv -n "${OPERATOR_NS}" \
        -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)
    log "  CSV='${CSV_NAME:-<not yet found>}' ... (${ELAPSED}s / ${CSV_TIMEOUT}s)"
    sleep 10
    ELAPSED=$(( ELAPSED + 10 ))
done
ok "KEDA CSV '${CSV_NAME}' is Succeeded."

# Wait for KedaController to be Ready
log "Waiting for KedaController 'keda' to report Ready ..."
wait_for 10 "${KEDA_PODS_TIMEOUT}" "KedaController keda is Ready" \
    bash -c "oc get keda keda -n ${OPERATOR_NS} -o jsonpath='{.status.conditions[?(@.type==\"Ready\")].status}' 2>/dev/null | grep -q '^True$'"

# Confirm KEDA pods are Running
log "Current KEDA pods:"
oc get pods -n "${OPERATOR_NS}" --no-headers 2>/dev/null || true

log ""
log "=== Step 2: Deploy Authentication Resources ==="

log "Applying auth manifests from ${HPA_DIR}/auth/ ..."
oc apply -k "${HPA_DIR}/auth/"
ok "Auth manifests applied (ServiceAccount, Token Secret, ClusterRoleBinding, TriggerAuthentication)."

# Verify the token secret has been populated by the token controller
log "Waiting for keda-thanos-reader-token to be populated ..."
wait_for 5 "${TOKEN_TIMEOUT}" "keda-thanos-reader-token has a non-empty .data.token" \
    bash -c "oc get secret keda-thanos-reader-token -n ${LOAD_NS} \
        -o jsonpath='{.data.token}' 2>/dev/null | grep -q '.'"

# Verify ClusterRoleBinding exists
if oc get clusterrolebinding keda-thanos-reader-monitoring-view &>/dev/null; then
    ok "ClusterRoleBinding 'keda-thanos-reader-monitoring-view' exists."
else
    warn "ClusterRoleBinding 'keda-thanos-reader-monitoring-view' not found — Thanos queries may fail."
fi

# Verify TriggerAuthentication exists
if oc get triggerauthentication thanos-trigger-auth -n "${LOAD_NS}" &>/dev/null; then
    ok "TriggerAuthentication 'thanos-trigger-auth' exists in namespace '${LOAD_NS}'."
else
    warn "TriggerAuthentication 'thanos-trigger-auth' not found in '${LOAD_NS}'."
fi

log ""
log "=== Step 3: Deploy Scaling Resources ==="

log "Applying scaling manifests from ${HPA_DIR}/scaling/ ..."
oc apply -k "${HPA_DIR}/scaling/"
ok "Scaling manifests applied (ScaledObject)."

# Wait for ScaledObject to be Ready
log "Waiting for ScaledObject 'iotoccupancy-sample-mosquitto-scaler' to be Ready ..."
wait_for 10 "${SCALEDOBJECT_TIMEOUT}" "ScaledObject is Ready" \
    bash -c "oc get scaledobject iotoccupancy-sample-mosquitto-scaler -n ${LOAD_NS} \
        -o jsonpath='{.status.conditions[?(@.type==\"Ready\")].status}' 2>/dev/null | grep -q '^True$'"

# Verify HPA was created by KEDA
HPA_NAME=$(oc get hpa -n "${LOAD_NS}" \
    -o jsonpath='{.items[?(@.metadata.ownerReferences[0].kind=="ScaledObject")].metadata.name}' \
    2>/dev/null | awk '{print $1}')
if [ -n "${HPA_NAME}" ]; then
    ok "HPA '${HPA_NAME}' created by KEDA in namespace '${LOAD_NS}'."
    oc get hpa "${HPA_NAME}" -n "${LOAD_NS}" 2>/dev/null || true
else
    warn "No HPA owned by ScaledObject found yet in '${LOAD_NS}'. It may appear after the first polling interval (15s)."
fi

log ""
log "=== Deployment Complete ==="
log ""
log "Summary:"
log "  KEDA operator    : namespace '${OPERATOR_NS}', CSV '${CSV_NAME}'"
log "  Auth             : ServiceAccount 'keda-thanos-reader', token Secret, CRB, TriggerAuthentication"
log "  Scaler           : ScaledObject 'iotoccupancy-sample-mosquitto-scaler' in '${LOAD_NS}'"
log "  HPA              : ${HPA_NAME:-<pending first poll>}"
log ""
log "Verification commands:"
log "  oc get keda -n ${OPERATOR_NS}"
log "  oc get pods -n ${OPERATOR_NS}"
log "  oc get scaledobject -n ${LOAD_NS}"
log "  oc get hpa -n ${LOAD_NS}"
log ""
log "Next step: run the benchmark with ./run-hpa-benchmark.sh"
