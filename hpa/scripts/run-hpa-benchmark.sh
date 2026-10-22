#!/usr/bin/env bash
# =============================================================================
# run-hpa-benchmark.sh
#
# Executes the 30-minute KEDA + HPA baseline benchmark and exports evaluation
# metrics to CSV for comparison with MASA.
#
# Steps:
#   1.  Verify KEDA ScaledObject and HPA are active and healthy.
#   2.  Verify MASA action agent is at 0 replicas (no competing scaler).
#   3.  Reset the target deployment (mosquitto) to 1 replica.
#   4.  Record UTC start time.
#   5.  Launch the 14-step load test via the load-test-scaler deployment.
#   6.  Monitor execution until completion (~30 minutes).
#   7.  Record UTC end time.
#   8.  Call collect_metrics.py with Thanos credentials and time window.
#   9.  Save results to hpa_load_test_30min.csv.
#
# Prerequisites:
#   - oc CLI installed and logged in.
#   - KEDA + HPA baseline deployed (run ./deploy-keda-hpa.sh first).
#   - collect_metrics.py available in REPO_ROOT/masa-i/ (or set COLLECT_METRICS_PY).
#   - Python 3 with packages: requests pandas urllib3.
#
# Usage:
#   chmod +x run-hpa-benchmark.sh
#   ./run-hpa-benchmark.sh
#
# Override defaults via environment variables:
#   LOAD_NS=iotoccupancy MASA_NS=masa-system BENCHMARK_DURATION=1800 \
#   OUTPUT_CSV=hpa_load_test_30min.csv ./run-hpa-benchmark.sh
# =============================================================================

set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration — override via env vars
# ---------------------------------------------------------------------------
LOAD_NS="${LOAD_NS:-iotoccupancy}"
MASA_NS="${MASA_NS:-masa-system}"
MOSQ_DEPLOY="${MOSQ_DEPLOY:-iotoccupancy-sample-mosquitto}"
SCALER_DEPLOY="${SCALER_DEPLOY:-load-test-scaler}"
ACTION_AGENT_DEPLOY="${ACTION_AGENT_DEPLOY:-masautoscaling-sample-action-agent}"

# ScaledObject and HPA names (must match ginga/hpa/scaling/01-scaledobject.yaml)
SCALEDOBJECT_NAME="${SCALEDOBJECT_NAME:-iotoccupancy-sample-mosquitto-scaler}"

# Load test duration in seconds (14 steps × ~128s each ≈ 1800s / 30 min)
BENCHMARK_DURATION="${BENCHMARK_DURATION:-1800}"

# Poll interval while monitoring load test progress (seconds)
POLL_INTERVAL="${POLL_INTERVAL:-30}"

# Output CSV path (relative to current working directory or absolute)
OUTPUT_CSV="${OUTPUT_CSV:-hpa_load_test_30min.csv}"

# collect_metrics.py location — auto-detected relative to this script's repo root
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../../.." && pwd)"
COLLECT_METRICS_PY="${COLLECT_METRICS_PY:-${REPO_ROOT}/ginga/masa-i/collect_metrics.py}"

# Prometheus / Thanos job label that the load tester exposes metrics under
METRICS_JOB="${METRICS_JOB:-mqtt-load-tester-metrics}"

# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------
log()  { echo "[$(date '+%H:%M:%S')] $*"; }
ok()   { echo "[$(date '+%H:%M:%S')] ✓ $*"; }
warn() { echo "[$(date '+%H:%M:%S')] WARN: $*" >&2; }
fail() { echo "[$(date '+%H:%M:%S')] ERROR: $*" >&2; exit 1; }

format_duration() {
    local secs="$1"
    printf '%dm %02ds' $(( secs / 60 )) $(( secs % 60 ))
}

# ---------------------------------------------------------------------------
# Pre-flight checks
# ---------------------------------------------------------------------------
log "=== Pre-flight checks ==="

if ! command -v oc &>/dev/null; then
    fail "'oc' CLI not found in PATH."
fi

if ! oc whoami &>/dev/null; then
    fail "Not logged in to OpenShift. Run 'oc login ...' first."
fi
log "Logged in as: $(oc whoami)"

# 1. Verify KEDA ScaledObject is Ready
log "Checking ScaledObject '${SCALEDOBJECT_NAME}' in namespace '${LOAD_NS}' ..."
if ! oc get scaledobject "${SCALEDOBJECT_NAME}" -n "${LOAD_NS}" &>/dev/null; then
    fail "ScaledObject '${SCALEDOBJECT_NAME}' not found in '${LOAD_NS}'. Run ./deploy-keda-hpa.sh first."
fi

SO_READY=$(oc get scaledobject "${SCALEDOBJECT_NAME}" -n "${LOAD_NS}" \
    -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null || echo "")
if [ "${SO_READY}" != "True" ]; then
    fail "ScaledObject '${SCALEDOBJECT_NAME}' is not Ready (status='${SO_READY}'). Investigate with: oc describe scaledobject/${SCALEDOBJECT_NAME} -n ${LOAD_NS}"
fi
ok "ScaledObject '${SCALEDOBJECT_NAME}' is Ready."

# 2. Verify HPA is active
HPA_NAME=$(oc get hpa -n "${LOAD_NS}" \
    -o jsonpath='{.items[?(@.metadata.ownerReferences[0].kind=="ScaledObject")].metadata.name}' \
    2>/dev/null | awk '{print $1}')
if [ -z "${HPA_NAME}" ]; then
    fail "No HPA owned by '${SCALEDOBJECT_NAME}' found in '${LOAD_NS}'. KEDA may not have created it yet — wait 15s and retry."
fi
ok "HPA '${HPA_NAME}' is active in '${LOAD_NS}'."
oc get hpa "${HPA_NAME}" -n "${LOAD_NS}" 2>/dev/null || true

# 3. Verify MASA action agent is suspended (no competing scaler)
log "Checking MASA action agent '${ACTION_AGENT_DEPLOY}' in '${MASA_NS}' ..."
if oc get deployment/"${ACTION_AGENT_DEPLOY}" -n "${MASA_NS}" &>/dev/null; then
    AGENT_REPLICAS=$(oc get deployment/"${ACTION_AGENT_DEPLOY}" -n "${MASA_NS}" \
        -o jsonpath='{.spec.replicas}' 2>/dev/null || echo "0")
    if [ "${AGENT_REPLICAS}" != "0" ]; then
        fail "MASA action agent is running with ${AGENT_REPLICAS} replica(s). Scale it to 0 first to avoid competing scale decisions: oc scale deployment/${ACTION_AGENT_DEPLOY} -n ${MASA_NS} --replicas=0"
    fi
    ok "MASA action agent is at 0 replicas — no competing scaler."
else
    log "INFO: '${ACTION_AGENT_DEPLOY}' not found in '${MASA_NS}' — no MASA conflict check needed."
fi

# 4. Check load-test-scaler deployment exists
if ! oc get deployment/"${SCALER_DEPLOY}" -n "${LOAD_NS}" &>/dev/null; then
    fail "load-test-scaler deployment '${SCALER_DEPLOY}' not found in '${LOAD_NS}'. Ensure the MQTT load test infrastructure is deployed."
fi

# 5. Verify collect_metrics.py is available
if [ ! -f "${COLLECT_METRICS_PY}" ]; then
    fail "collect_metrics.py not found at '${COLLECT_METRICS_PY}'. Set COLLECT_METRICS_PY env var to the correct path."
fi
if ! command -v python3 &>/dev/null; then
    fail "python3 not found. Install Python 3 and the packages: requests pandas urllib3."
fi

log ""
ok "All pre-flight checks passed."

# ---------------------------------------------------------------------------
# Step 1: Reset mosquitto to 1 replica
# ---------------------------------------------------------------------------
log ""
log "=== Resetting '${MOSQ_DEPLOY}' to 1 replica ==="
oc scale deployment/"${MOSQ_DEPLOY}" -n "${LOAD_NS}" --replicas=1
log "Waiting for rollout ..."
oc rollout status deployment/"${MOSQ_DEPLOY}" -n "${LOAD_NS}" --timeout=120s || \
    warn "Rollout status timed out (continuing)."
ok "'${MOSQ_DEPLOY}' is at 1 replica."

# Give the ScaledObject a moment to register the reset
sleep 5

# ---------------------------------------------------------------------------
# Step 2: Record start time and launch load test
# ---------------------------------------------------------------------------
log ""
log "=== Starting KEDA + HPA Benchmark ==="

START_UTC=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
START_EPOCH=$(date +%s)
log "Benchmark start time (UTC): ${START_UTC}"

# Ensure load-test-scaler is stopped before launching fresh (clean state)
CURRENT_SCALER_REPLICAS=$(oc get deployment/"${SCALER_DEPLOY}" -n "${LOAD_NS}" \
    -o jsonpath='{.spec.replicas}' 2>/dev/null || echo "0")
if [ "${CURRENT_SCALER_REPLICAS}" != "0" ]; then
    log "Stopping load-test-scaler to reset state ..."
    oc scale deployment/"${SCALER_DEPLOY}" -n "${LOAD_NS}" --replicas=0
    sleep 3
fi

log "Launching load-test-scaler (14-step load profile) ..."
oc scale deployment/"${SCALER_DEPLOY}" -n "${LOAD_NS}" --replicas=1
oc rollout status deployment/"${SCALER_DEPLOY}" -n "${LOAD_NS}" --timeout=60s || \
    warn "load-test-scaler rollout status timed out (continuing)."
ok "load-test-scaler started. The 14-step load profile is now running."

# ---------------------------------------------------------------------------
# Step 3: Monitor benchmark progress
# ---------------------------------------------------------------------------
log ""
log "=== Monitoring benchmark (target duration: $(format_duration ${BENCHMARK_DURATION})) ==="
log "  Poll interval : ${POLL_INTERVAL}s"
log "  HPA           : ${HPA_NAME}"
log "  ScaledObject  : ${SCALEDOBJECT_NAME}"
log ""

ELAPSED=0
while [ "${ELAPSED}" -lt "${BENCHMARK_DURATION}" ]; do
    REMAINING=$(( BENCHMARK_DURATION - ELAPSED ))

    # Current replica count
    CURRENT_REPLICAS=$(oc get deployment/"${MOSQ_DEPLOY}" -n "${LOAD_NS}" \
        -o jsonpath='{.status.readyReplicas}' 2>/dev/null || echo "?")
    DESIRED_REPLICAS=$(oc get hpa "${HPA_NAME}" -n "${LOAD_NS}" \
        -o jsonpath='{.status.desiredReplicas}' 2>/dev/null || echo "?")
    HPA_CURRENT_METRIC=$(oc get hpa "${HPA_NAME}" -n "${LOAD_NS}" \
        -o jsonpath='{.status.currentMetrics[0].external.current.averageValue}' 2>/dev/null || \
        oc get hpa "${HPA_NAME}" -n "${LOAD_NS}" \
        -o jsonpath='{.status.currentMetrics[0].object.current.averageValue}' 2>/dev/null || echo "n/a")
    SCALER_RUNNING=$(oc get deployment/"${SCALER_DEPLOY}" -n "${LOAD_NS}" \
        -o jsonpath='{.status.readyReplicas}' 2>/dev/null || echo "0")

    log "  elapsed=$(format_duration ${ELAPSED}) | remaining=$(format_duration ${REMAINING}) | ready=${CURRENT_REPLICAS} | desired=${DESIRED_REPLICAS} | metric=${HPA_CURRENT_METRIC} | scaler_pods=${SCALER_RUNNING:-0}"

    # Detect if load-test-scaler has stopped prematurely
    if [ "${SCALER_RUNNING:-0}" = "0" ] && [ "${ELAPSED}" -lt $(( BENCHMARK_DURATION - 60 )) ]; then
        warn "load-test-scaler has 0 ready pods with $(format_duration ${REMAINING}) remaining. It may have completed its cycle or crashed."
        warn "Check: oc logs deployment/${SCALER_DEPLOY} -n ${LOAD_NS}"
    fi

    sleep "${POLL_INTERVAL}"
    ELAPSED=$(( ELAPSED + POLL_INTERVAL ))
done

END_UTC=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
END_EPOCH=$(date +%s)
ACTUAL_DURATION=$(( END_EPOCH - START_EPOCH ))
ok "Benchmark monitoring complete. Actual duration: $(format_duration ${ACTUAL_DURATION})."

# ---------------------------------------------------------------------------
# Step 4: Stop load test
# ---------------------------------------------------------------------------
log ""
log "=== Stopping load test ==="
if oc get deployment/"${SCALER_DEPLOY}" -n "${LOAD_NS}" &>/dev/null; then
    oc scale deployment/"${SCALER_DEPLOY}" -n "${LOAD_NS}" --replicas=0
    ok "load-test-scaler stopped (replicas=0)."
fi

# ---------------------------------------------------------------------------
# Step 5: Collect metrics via collect_metrics.py
# ---------------------------------------------------------------------------
log ""
log "=== Collecting metrics from Thanos ==="
log "  Start : ${START_UTC}"
log "  End   : ${END_UTC}"
log "  Output: ${OUTPUT_CSV}"

# Obtain a bearer token for collect_metrics.py (uses oc whoami by default)
python3 "${COLLECT_METRICS_PY}" \
    --start  "${START_UTC}" \
    --end    "${END_UTC}" \
    --source thanos \
    --namespace "${LOAD_NS}" \
    --deployment "${MOSQ_DEPLOY}" \
    --job "${METRICS_JOB}" \
    --output "${OUTPUT_CSV}"

log ""
if [ -f "${OUTPUT_CSV}" ]; then
    ROW_COUNT=$(python3 -c "
import csv
with open('${OUTPUT_CSV}') as f:
    rows = sum(1 for _ in csv.reader(f)) - 1  # subtract header
print(rows)
" 2>/dev/null || echo "unknown")
    ok "Metrics exported: '${OUTPUT_CSV}' (${ROW_COUNT} data rows)."
else
    warn "Output file '${OUTPUT_CSV}' not found — check collect_metrics.py output above."
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
log ""
log "=== Benchmark Complete ==="
log ""
log "  Start time (UTC) : ${START_UTC}"
log "  End time (UTC)   : ${END_UTC}"
log "  Duration         : $(format_duration ${ACTUAL_DURATION})"
log "  Output CSV       : ${OUTPUT_CSV}"
log ""
log "Next steps:"
log "  1. Compare ${OUTPUT_CSV} with MASA results (masa_load_test_30min_sep_25.csv)"
log "  2. Generate evaluation report: python3 masa-i/generate_evaluation_excel.py"
log "  3. To restore MASA: ./teardown-keda-hpa.sh && oc scale deployment/masautoscaling-sample-action-agent -n masa-system --replicas=1"
