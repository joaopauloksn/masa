#!/usr/bin/env bash
# =============================================================================
# resume-cluster.sh
#
# Resumes the MASA cluster components after offline data collection or model retraining.
#
# Actions performed:
#   1. Restores the Planning and Action agents in MASA_NS (scales to 1).
#   2. Restores the Operator manager in MASA_NS (scales to 1).
#   3. Optionally enables online training on the CR (spec.trainingEnabled: true).
#   4. Re-enables the load-test-scaler in LOAD_NS (scales to 1).
#
# Usage:
#   ./resume-cluster.sh                  # resume controllers, online training remains disabled
#   ENABLE_TRAINING=1 ./resume-cluster.sh # resume controllers AND enable online training CronJob
# =============================================================================

set -euo pipefail

LOAD_NS="${LOAD_NS:-iotoccupancy}"
MASA_NS="${MASA_NS:-masa-system}"
SCALER_DEPLOY="${SCALER_DEPLOY:-load-test-scaler}"
OPERATOR_DEPLOY="${OPERATOR_DEPLOY:-masa-controller-manager}"
ACTION_AGENT_DEPLOY="${ACTION_AGENT_DEPLOY:-masautoscaling-sample-action-agent}"
PLANNING_AGENT_DEPLOY="${PLANNING_AGENT_DEPLOY:-masautoscaling-sample-planning-agent}"
CR_NAME="${CR_NAME:-masautoscaling-sample}"
CR_RESOURCE="${CR_RESOURCE:-masautoscalings}"
ENABLE_TRAINING="${ENABLE_TRAINING:-0}"

log() { echo "[$(date '+%H:%M:%S')] $*"; }

scale() {
    local ns="$1" deploy="$2" replicas="$3"
    oc scale deployment/"$deploy" -n "$ns" --replicas="$replicas"
}

wait_rollout() {
    local ns="$1" deploy="$2"
    log "  Waiting for $deploy rollout in $ns ..."
    oc rollout status deployment/"$deploy" -n "$ns" --timeout=120s \
        > /dev/null 2>&1 || log "  WARN: rollout status timed out for $deploy (continuing)"
}

log "=== Resuming MASA Cluster Components ==="

log "1. Restoring planning and action agents in $MASA_NS ..."
for agent_deploy in "$PLANNING_AGENT_DEPLOY" "$ACTION_AGENT_DEPLOY"; do
    if oc get deployment/"$agent_deploy" -n "$MASA_NS" &>/dev/null; then
        log "   Scaling $agent_deploy to 1 ..."
        scale "$MASA_NS" "$agent_deploy" 1
    fi
done

log "2. Restoring Operator manager in $MASA_NS ..."
if oc get deployment/"$OPERATOR_DEPLOY" -n "$MASA_NS" &>/dev/null; then
    log "   Scaling $OPERATOR_DEPLOY to 1 ..."
    scale "$MASA_NS" "$OPERATOR_DEPLOY" 1
fi

if [ "$ENABLE_TRAINING" = "1" ]; then
    log "3. Enabling online training on CR $CR_NAME ..."
    oc patch "${CR_RESOURCE}/${CR_NAME}" \
        -n "$MASA_NS" \
        --type=merge \
        -p '{"spec":{"trainingEnabled":true}}' || true
else
    log "3. Online training remains disabled (ENABLE_TRAINING=0). Set ENABLE_TRAINING=1 to enable."
fi

log "4. Re-enabling $SCALER_DEPLOY in $LOAD_NS (scaling to 1) ..."
if oc get deployment/"$SCALER_DEPLOY" -n "$LOAD_NS" &>/dev/null; then
    scale "$LOAD_NS" "$SCALER_DEPLOY" 1
    wait_rollout "$LOAD_NS" "$SCALER_DEPLOY"
else
    log "   WARN: $SCALER_DEPLOY not found — skipping."
fi

log ""
log "=== Cluster resumed successfully ==="
log "Status check:"
log "  oc get pods -n $MASA_NS"
log "  oc get pods -n $LOAD_NS"
