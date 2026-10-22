#!/usr/bin/env bash
# =============================================================================
# collect-training-data.sh
#
# Sweeps mqtt-load-tester replicas 1-10 (outer loop) × mosquitto replicas 1-10
# (inner loop), waiting WAIT_SECONDS between each combination so the perception
# agent records diverse (replicas, label) data points in MongoDB.
#
# Two distinct deployments are involved:
#   mqtt-load-tester  — the MQTT load generator itself (outer loop variable)
#   load-test-scaler  — a separate controller that normally auto-manages
#                       mqtt-load-tester replicas; it must be SCALED TO 0
#                       before the sweep starts so it doesn't fight the script,
#                       and is restored to 1 at the very end.
#
# After the sweep:
#   1. mqtt-load-tester  → 0        (stop all load)
#   2. mosquitto         → 1        (back to baseline)
#   3. planning-agent    → 1        (kept alive for PVC access / inspection)
#   4. Controllers (load-test-scaler, action-agent, operator) and online training
#      remain paused so you have time to extract data and train offline.
#   5. Run ./resume-cluster.sh when you are ready to resume normal scaling.
#
# Usage:
#   chmod +x collect-training-data.sh
#   ./collect-training-data.sh
#
# Override defaults via environment:
#   WAIT_SECONDS=90 ./collect-training-data.sh
#   LOAD_NS=iotoccupancy MASA_NS=masa-system ./collect-training-data.sh
# =============================================================================

set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration — override with env vars if needed
# ---------------------------------------------------------------------------
LOAD_NS="${LOAD_NS:-iotoccupancy}"
MASA_NS="${MASA_NS:-masa-system}"
LOAD_DEPLOY="${LOAD_DEPLOY:-mqtt-load-tester}"
MOSQ_DEPLOY="${MOSQ_DEPLOY:-iotoccupancy-sample-mosquitto}"
SCALER_DEPLOY="${SCALER_DEPLOY:-load-test-scaler}"
OPERATOR_DEPLOY="${OPERATOR_DEPLOY:-masa-controller-manager}"
ACTION_AGENT_DEPLOY="${ACTION_AGENT_DEPLOY:-masautoscaling-sample-action-agent}"
PLANNING_AGENT_DEPLOY="${PLANNING_AGENT_DEPLOY:-masautoscaling-sample-planning-agent}"
CR_NAME="${CR_NAME:-masautoscaling-sample}"
# Plural resource name used by 'oc patch' — must match the CRD (.spec.names.plural)
CR_RESOURCE="${CR_RESOURCE:-masautoscalings}"

# Clear MongoDB collection before sweeping (1=yes, 0=no)
CLEAR_MONGO="${CLEAR_MONGO:-1}"

# Wait time between each (load × mosquitto) combination, in seconds.
# 60s = 4 perception cycles at 15s/cycle — sufficient for stable metric samples.
WAIT_SECONDS="${WAIT_SECONDS:-60}"

MAX_LOAD="${MAX_LOAD:-10}"
MAX_MOSQ="${MAX_MOSQ:-10}"

# Maximum allowed gap between mosquitto and load-tester replicas (in either direction).
# Combos where |mosquitto - load_tester| > REPLICA_GAP_MAX are skipped:
#   mosquitto >> load_tester  → clearly over-provisioned  (label=3, low signal)
#   load_tester >> mosquitto  → clearly under-provisioned (label=1, low signal)
# The boundary combos (gap ≤ 2) are where the interesting label=1/2/3 transitions happen.
# Set to a large number (e.g. 99) to disable filtering and run all 100 combos.
REPLICA_GAP_MAX="${REPLICA_GAP_MAX:-2}"

# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------
log() { echo "[$(date '+%H:%M:%S')] $*"; }

wait_rollout() {
    local ns="$1" deploy="$2"
    log "  Waiting for $deploy rollout in $ns ..."
    oc rollout status deployment/"$deploy" -n "$ns" --timeout=120s \
        > /dev/null 2>&1 || log "  WARN: rollout status timed out for $deploy (continuing)"
}

scale() {
    local ns="$1" deploy="$2" replicas="$3"
    oc scale deployment/"$deploy" -n "$ns" --replicas="$replicas"
}

# ---------------------------------------------------------------------------
# Pre-flight checks
# ---------------------------------------------------------------------------
log "=== Pre-flight checks ==="

if ! oc whoami &>/dev/null; then
    echo "ERROR: Not logged in to OpenShift. Run 'oc login ...' first." >&2
    exit 1
fi

log "Logged in as: $(oc whoami)"

for ns in "$LOAD_NS" "$MASA_NS"; do
    if ! oc get namespace "$ns" &>/dev/null; then
        echo "ERROR: Namespace '$ns' not found." >&2
        exit 1
    fi
done

for deploy in "$LOAD_DEPLOY" "$MOSQ_DEPLOY"; do
    if ! oc get deployment/"$deploy" -n "$LOAD_NS" &>/dev/null; then
        echo "ERROR: Deployment '$deploy' not found in namespace '$LOAD_NS'." >&2
        exit 1
    fi
done

# Pause load-test-scaler so it doesn't fight the sweep
if oc get deployment/"$SCALER_DEPLOY" -n "$LOAD_NS" &>/dev/null; then
    log "Pausing $SCALER_DEPLOY (scaling to 0) so it won't interfere with the sweep ..."
    scale "$LOAD_NS" "$SCALER_DEPLOY" 0
else
    log "INFO: $SCALER_DEPLOY not found — nothing to pause."
fi

# Pause the Operator manager first so it doesn't reconcile and restart action/planning agents
if oc get deployment/"$OPERATOR_DEPLOY" -n "$MASA_NS" &>/dev/null; then
    log "Pausing $OPERATOR_DEPLOY (scaling to 0) in $MASA_NS to prevent auto-reconciliation..."
    scale "$MASA_NS" "$OPERATOR_DEPLOY" 0
fi

# Pause MASA action-agent & planning-agent so they don't generate or execute scaling plans during the sweep
for agent_deploy in "$ACTION_AGENT_DEPLOY" "$PLANNING_AGENT_DEPLOY"; do
    if oc get deployment/"$agent_deploy" -n "$MASA_NS" &>/dev/null; then
        log "Pausing $agent_deploy (scaling to 0) in $MASA_NS ..."
        scale "$MASA_NS" "$agent_deploy" 0
    fi
done

# Disable training cronjob on CR during sweep
if oc get "${CR_RESOURCE}/${CR_NAME}" -n "$MASA_NS" &>/dev/null; then
    log "Disabling training on CR $CR_NAME during the sweep ..."
    oc patch "${CR_RESOURCE}/${CR_NAME}" \
        -n "$MASA_NS" \
        --type=merge \
        -p '{"spec":{"trainingEnabled":false}}' || true
fi

# Clean up MongoDB historical records if requested
if [ "$CLEAR_MONGO" = "1" ]; then
    log "Clearing existing records in MongoDB (mongo_scaling.microservices) to ensure unbiased data ..."
    
    # Method 1: Execute directly inside an agent pod in masa-system (matches masa-agent-perception, masa-agent-planning, etc.)
    AGENT_POD=$(oc get pods -n "$MASA_NS" -l 'app in (masa-agent-perception, perception-agent, masa-agent-planning, planning-agent)' -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)
    
    # Fallback to any running pod with secret mounted if app selector didn't match
    if [ -z "$AGENT_POD" ]; then
        AGENT_POD=$(oc get pods -n "$MASA_NS" --field-selector=status.phase=Running -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)
    fi

    WIPED=0
    if [ -n "$AGENT_POD" ]; then
        log "Purging collection via agent pod ($AGENT_POD in $MASA_NS)..."
        oc exec -n "$MASA_NS" "$AGENT_POD" -- python3 -c '
from pymongo import MongoClient
import os
secret_dir = os.getenv("MONGO_SECRET_DIR", "/etc/secrets/mongo")
try:
    user = open(secret_dir + "/username").read().strip()
    pwd = open(secret_dir + "/password").read().strip()
except Exception as e:
    user = os.getenv("MONGO_USERNAME", "")
    pwd = os.getenv("MONGO_PASSWORD", "")
host = os.getenv("MONGO_HOST", "mas-mongo-ce-0.mas-mongo-ce-svc.mongoce.svc.cluster.local:27017,mas-mongo-ce-1.mas-mongo-ce-svc.mongoce.svc.cluster.local:27017,mas-mongo-ce-2.mas-mongo-ce-svc.mongoce.svc.cluster.local:27017")
client = MongoClient("mongodb://" + user + ":" + pwd + "@" + host, authMechanism="SCRAM-SHA-256", tls=True, tlsInsecure=True, authSource="admin")
res = client["mongo_scaling"]["microservices"].delete_many({})
print(f"Successfully deleted {res.deleted_count} documents from mongo_scaling.microservices")
' && WIPED=1 || true
    fi

    if [ "$WIPED" -eq 0 ]; then
        log "Agent pod exec fallback: Searching for MongoDB pod directly across namespaces..."
        MONGO_POD=$(oc get pods --all-namespaces -l 'app.kubernetes.io/name in (mas-mongo-ce, mongodb)' -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)
        MONGO_NS=$(oc get pods --all-namespaces -l 'app.kubernetes.io/name in (mas-mongo-ce, mongodb)' -o jsonpath='{.items[0].metadata.namespace}' 2>/dev/null || true)
        if [ -n "$MONGO_POD" ] && [ -n "$MONGO_NS" ]; then
            log "Found MongoDB pod: $MONGO_POD in $MONGO_NS. Purging collection..."
            oc exec -n "$MONGO_NS" "$MONGO_POD" -- mongosh mongo_scaling --eval "db.microservices.deleteMany({})" 2>/dev/null || \
            oc exec -n "$MONGO_NS" "$MONGO_POD" -- mongo mongo_scaling --eval "db.microservices.remove({})" 2>/dev/null || \
            log "WARN: Could not directly wipe Mongo collection via mongosh/mongo (continuing)."
        else
            log "INFO: Could not purge Mongo automatically. You can proceed, or purge manually via Python."
        fi
    fi
fi

log "All checks passed."
log ""
# Pre-compute how many combos will actually run (for accurate ETA)
total=0
for load_r in $(seq 1 "$MAX_LOAD"); do
    for mosq_r in $(seq 1 "$MAX_MOSQ"); do
        gap=$(( load_r - mosq_r )); [ "$gap" -lt 0 ] && gap=$(( -gap ))
        if [ "$gap" -le "$REPLICA_GAP_MAX" ]; then
            total=$(( total + 1 ))
        fi
    done
done

log "=== Sweep parameters ==="
log "  Load tester replicas : 1 – ${MAX_LOAD}"
log "  Mosquitto replicas   : 1 – ${MAX_MOSQ}"
log "  Max replica gap      : ±${REPLICA_GAP_MAX}  (skips over/under-provisioned combos)"
log "  Wait per combination : ${WAIT_SECONDS}s"
total_time=$(( total * WAIT_SECONDS / 60 ))
log "  Active combinations  : ${total}  (~${total_time} min)"
log "  ($(( MAX_LOAD * MAX_MOSQ - total )) combos skipped as out-of-range)"
log ""

# ---------------------------------------------------------------------------
# Main sweep
# ---------------------------------------------------------------------------
combo=0
start_epoch=$(date +%s)

for load_r in $(seq 1 "$MAX_LOAD"); do
    log ">>> Setting $LOAD_DEPLOY replicas = $load_r"
    scale "$LOAD_NS" "$LOAD_DEPLOY" "$load_r"
    wait_rollout "$LOAD_NS" "$LOAD_DEPLOY"

    for mosq_r in $(seq 1 "$MAX_MOSQ"); do
        # Skip combos where either side is more than REPLICA_GAP_MAX ahead of the other
        gap=$(( load_r - mosq_r )); [ "$gap" -lt 0 ] && gap=$(( -gap ))
        if [ "$gap" -gt "$REPLICA_GAP_MAX" ]; then
            continue
        fi

        combo=$(( combo + 1 ))
        elapsed=$(( $(date +%s) - start_epoch ))
        log "--- Combo $combo/${total} | load=$load_r mosq=$mosq_r | elapsed=${elapsed}s ---"

        scale "$LOAD_NS" "$MOSQ_DEPLOY" "$mosq_r"
        wait_rollout "$LOAD_NS" "$MOSQ_DEPLOY"

        log "  Waiting ${WAIT_SECONDS}s for perception cycles ..."
        sleep "$WAIT_SECONDS"
    done
done

# ---------------------------------------------------------------------------
# Wrap-up
# ---------------------------------------------------------------------------
log ""
log "=== Sweep complete — resetting cluster ==="

log "Scaling $LOAD_DEPLOY to 0 (stop load) ..."
scale "$LOAD_NS" "$LOAD_DEPLOY" 0

log "Scaling $MOSQ_DEPLOY to 1 (baseline) ..."
scale "$LOAD_NS" "$MOSQ_DEPLOY" 1
wait_rollout "$LOAD_NS" "$MOSQ_DEPLOY"

log "Keeping Action agent, operator, and load-test-scaler paused at 0 replicas."
log "Ensuring Planning Agent is scaled to 1 for PVC access / inspection..."
if oc get deployment/"$PLANNING_AGENT_DEPLOY" -n "$MASA_NS" &>/dev/null; then
    scale "$MASA_NS" "$PLANNING_AGENT_DEPLOY" 1
fi

total_elapsed=$(( $(date +%s) - start_epoch ))
log ""
log "=== Sweep finished in ${total_elapsed}s ==="
log "Next steps:"
log "  1. Extract data:    python3 collect_metrics.py --thanos-url ... --token ... --output sweep_training_data.csv"
log "  2. Train model:     python3 training/train_offline.py --csv sweep_training_data.csv --output-dir training"
log "  3. Deploy model:    oc cp training/best_replica_predictor_model.h5 masa-system/\$PLANNING_POD:/mnt/model/"
log "                      oc cp training/scaler.json masa-system/\$PLANNING_POD:/mnt/model/"
log "  4. Resume cluster:  ./resume-cluster.sh"
