#!/usr/bin/env bash
# =============================================================================
# teardown-keda-hpa.sh
#
# Clean removal of the KEDA + HPA baseline autoscaler resources to avoid
# conflict when switching back to MASA or resetting the cluster.
#
# Deletion order (reverse of deploy-keda-hpa.sh):
#   1. ScaledObject          — stops KEDA from issuing scale decisions; KEDA
#                              automatically deletes the owned HPA.
#   2. TriggerAuthentication — removes Thanos bearer token binding.
#   3. ServiceAccount        — keda-thanos-reader in iotoccupancy.
#   4. Token Secret          — keda-thanos-reader-token in iotoccupancy.
#   5. ClusterRoleBinding    — keda-thanos-reader-monitoring-view.
#   6. (Optional) KedaController — removes the KEDA runtime pods.
#   7. (Optional) Operator resources — Subscription, OperatorGroup, Namespace.
#      Deleting the Namespace also removes any remaining operator state.
#
# Flags:
#   --full    : Also delete the KedaController CR, Subscription, OperatorGroup,
#               and openshift-keda namespace (full operator removal).
#   --yes     : Skip the interactive confirmation prompt.
#
# Usage:
#   chmod +x teardown-keda-hpa.sh
#   ./teardown-keda-hpa.sh           # removes scaling/auth resources only
#   ./teardown-keda-hpa.sh --full    # removes everything including the operator
#   ./teardown-keda-hpa.sh --full --yes  # non-interactive full removal
# =============================================================================

set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration — override via env vars
# ---------------------------------------------------------------------------
OPERATOR_NS="${OPERATOR_NS:-openshift-keda}"
LOAD_NS="${LOAD_NS:-iotoccupancy}"
MASA_NS="${MASA_NS:-masa-system}"
ACTION_AGENT_DEPLOY="${ACTION_AGENT_DEPLOY:-masautoscaling-sample-action-agent}"

# ---------------------------------------------------------------------------
# Parse flags
# ---------------------------------------------------------------------------
FULL_TEARDOWN=0
SKIP_CONFIRM=0
for arg in "$@"; do
    case "$arg" in
        --full) FULL_TEARDOWN=1 ;;
        --yes)  SKIP_CONFIRM=1  ;;
        *) echo "Unknown argument: $arg" >&2; exit 1 ;;
    esac
done

# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------
log()  { echo "[$(date '+%H:%M:%S')] $*"; }
ok()   { echo "[$(date '+%H:%M:%S')] ✓ $*"; }
warn() { echo "[$(date '+%H:%M:%S')] WARN: $*" >&2; }
skip() { echo "[$(date '+%H:%M:%S')] — skipped (not found): $*"; }

delete_if_exists() {
    # delete_if_exists <resource> <name> [-n <namespace>]
    local resource="$1" name="$2"
    shift 2
    if oc get "${resource}" "${name}" "$@" &>/dev/null; then
        oc delete "${resource}" "${name}" "$@" --ignore-not-found=true
        ok "Deleted ${resource}/${name}"
    else
        skip "${resource}/${name}"
    fi
}

# ---------------------------------------------------------------------------
# Pre-flight checks
# ---------------------------------------------------------------------------
log "=== Pre-flight checks ==="

if ! command -v oc &>/dev/null; then
    echo "ERROR: 'oc' CLI not found in PATH." >&2; exit 1
fi

if ! oc whoami &>/dev/null; then
    echo "ERROR: Not logged in to OpenShift. Run 'oc login ...' first." >&2; exit 1
fi
log "Logged in as: $(oc whoami)"

# ---------------------------------------------------------------------------
# Confirmation prompt
# ---------------------------------------------------------------------------
if [ "${SKIP_CONFIRM}" -eq 0 ]; then
    echo ""
    if [ "${FULL_TEARDOWN}" -eq 1 ]; then
        echo "  This will delete ALL KEDA + HPA baseline resources including the"
        echo "  KEDA operator, KedaController, openshift-keda namespace, and all"
        echo "  auth/scaling resources in namespace '${LOAD_NS}'."
    else
        echo "  This will delete KEDA scaling and auth resources in namespace '${LOAD_NS}':"
        echo "  ScaledObject, HPA (owned), TriggerAuthentication, ServiceAccount,"
        echo "  Token Secret, and ClusterRoleBinding."
        echo "  The KEDA operator in '${OPERATOR_NS}' will NOT be removed."
        echo "  Run with --full to also remove the operator."
    fi
    echo ""
    read -r -p "Continue? [y/N] " response
    if [[ ! "$response" =~ ^[Yy]$ ]]; then
        echo "Aborted."
        exit 0
    fi
fi

log ""
log "=== Step 1: Remove Scaling Resources ==="

# Delete ScaledObject — KEDA will automatically garbage-collect the owned HPA
log "Deleting ScaledObject 'iotoccupancy-sample-mosquitto-scaler' in '${LOAD_NS}' ..."
delete_if_exists scaledobject iotoccupancy-sample-mosquitto-scaler -n "${LOAD_NS}"

# Wait briefly for KEDA to remove the owned HPA
log "Waiting up to 30s for KEDA to remove the owned HPA ..."
ELAPSED=0
while oc get hpa -n "${LOAD_NS}" \
        -o jsonpath='{.items[?(@.metadata.ownerReferences[0].kind=="ScaledObject")].metadata.name}' \
        2>/dev/null | grep -q '.'; do
    if [ "${ELAPSED}" -ge 30 ]; then
        warn "HPA still present after 30s — deleting manually."
        HPA_NAME=$(oc get hpa -n "${LOAD_NS}" \
            -o jsonpath='{.items[?(@.metadata.ownerReferences[0].kind=="ScaledObject")].metadata.name}' \
            2>/dev/null | awk '{print $1}')
        if [ -n "${HPA_NAME}" ]; then
            oc delete hpa "${HPA_NAME}" -n "${LOAD_NS}" --ignore-not-found=true
            ok "Deleted HPA '${HPA_NAME}' manually."
        fi
        break
    fi
    log "  Waiting for HPA cleanup ... (${ELAPSED}s)"
    sleep 5
    ELAPSED=$(( ELAPSED + 5 ))
done
ok "HPA removed (or was already absent)."

log ""
log "=== Step 2: Remove Authentication Resources ==="

log "Deleting TriggerAuthentication 'thanos-trigger-auth' in '${LOAD_NS}' ..."
delete_if_exists triggerauthentication thanos-trigger-auth -n "${LOAD_NS}"

log "Deleting Token Secret 'keda-thanos-reader-token' in '${LOAD_NS}' ..."
delete_if_exists secret keda-thanos-reader-token -n "${LOAD_NS}"

log "Deleting ServiceAccount 'keda-thanos-reader' in '${LOAD_NS}' ..."
delete_if_exists serviceaccount keda-thanos-reader -n "${LOAD_NS}"

log "Deleting ClusterRoleBinding 'keda-thanos-reader-monitoring-view' ..."
delete_if_exists clusterrolebinding keda-thanos-reader-monitoring-view

log ""
log "=== Scaling & Auth Resources Removed ==="

# ---------------------------------------------------------------------------
# Optional: full operator removal
# ---------------------------------------------------------------------------
if [ "${FULL_TEARDOWN}" -eq 1 ]; then
    log ""
    log "=== Step 3 (--full): Remove KEDA Operator ==="

    log "Deleting KedaController 'keda' in '${OPERATOR_NS}' ..."
    delete_if_exists keda keda -n "${OPERATOR_NS}"

    # Allow KEDA pods to terminate before removing the subscription
    log "Waiting up to 60s for KEDA controller pods to terminate ..."
    ELAPSED=0
    while oc get pods -n "${OPERATOR_NS}" \
            -l 'app.kubernetes.io/part-of=openshift-custom-metrics-autoscaler-operator' \
            --no-headers 2>/dev/null | grep -qv "^$"; do
        if [ "${ELAPSED}" -ge 60 ]; then
            warn "KEDA pods still present — proceeding with Subscription deletion."
            break
        fi
        sleep 5
        ELAPSED=$(( ELAPSED + 5 ))
    done

    log "Deleting Subscription 'openshift-custom-metrics-autoscaler' in '${OPERATOR_NS}' ..."
    delete_if_exists subscription openshift-custom-metrics-autoscaler -n "${OPERATOR_NS}"

    # Remove any remaining CSVs for the operator
    CSV_NAME=$(oc get csv -n "${OPERATOR_NS}" \
        -o jsonpath='{.items[?(@.spec.displayName=="Custom Metrics Autoscaler")].metadata.name}' \
        2>/dev/null | awk '{print $1}')
    if [ -n "${CSV_NAME}" ]; then
        log "Deleting CSV '${CSV_NAME}' in '${OPERATOR_NS}' ..."
        oc delete csv "${CSV_NAME}" -n "${OPERATOR_NS}" --ignore-not-found=true
        ok "Deleted CSV '${CSV_NAME}'."
    else
        skip "CSV (Custom Metrics Autoscaler) not found"
    fi

    log "Deleting OperatorGroup 'openshift-keda-og' in '${OPERATOR_NS}' ..."
    delete_if_exists operatorgroup openshift-keda-og -n "${OPERATOR_NS}"

    log "Deleting Namespace '${OPERATOR_NS}' ..."
    if oc get namespace "${OPERATOR_NS}" &>/dev/null; then
        oc delete namespace "${OPERATOR_NS}" --ignore-not-found=true
        log "Waiting for namespace '${OPERATOR_NS}' to be fully removed (up to 120s) ..."
        ELAPSED=0
        while oc get namespace "${OPERATOR_NS}" &>/dev/null; do
            if [ "${ELAPSED}" -ge 120 ]; then
                warn "Namespace '${OPERATOR_NS}' still terminating — it will finish in the background."
                break
            fi
            sleep 5
            ELAPSED=$(( ELAPSED + 5 ))
        done
        ok "Namespace '${OPERATOR_NS}' removed (or terminating)."
    else
        skip "Namespace '${OPERATOR_NS}'"
    fi
fi

# ---------------------------------------------------------------------------
# Offer to resume MASA action agent if it was paused
# ---------------------------------------------------------------------------
log ""
if oc get deployment/"${ACTION_AGENT_DEPLOY}" -n "${MASA_NS}" &>/dev/null 2>&1; then
    AGENT_REPLICAS=$(oc get deployment/"${ACTION_AGENT_DEPLOY}" -n "${MASA_NS}" \
        -o jsonpath='{.spec.replicas}' 2>/dev/null || echo "")
    if [ "${AGENT_REPLICAS}" = "0" ]; then
        echo ""
        if [ "${SKIP_CONFIRM}" -eq 1 ]; then
            response="N"
        else
            read -r -p "MASA action agent is at 0 replicas. Restore it to 1 now? [y/N] " response
        fi
        if [[ "$response" =~ ^[Yy]$ ]]; then
            oc scale deployment/"${ACTION_AGENT_DEPLOY}" -n "${MASA_NS}" --replicas=1
            ok "MASA action agent restored to 1 replica."
        fi
    fi
fi

log ""
log "=== Teardown Complete ==="
log ""
if [ "${FULL_TEARDOWN}" -eq 0 ]; then
    log "KEDA operator remains installed in '${OPERATOR_NS}'."
    log "Re-run with --full to remove the operator entirely."
fi
log "To redeploy: ./deploy-keda-hpa.sh"
