#!/bin/bash
# Ksplice Live Kernel Patch — OKE Node Validation Script
# Captures node + pod health snapshot BEFORE and AFTER patching
# Usage:
#   ./ksplice_node_validation.sh before 172.31.3.51
#   # ... apply Ksplice ...
#   ./ksplice_node_validation.sh after 172.31.3.51
#   ./ksplice_node_validation.sh diff 172.31.3.51

set -euo pipefail

PHASE="${1:-}"
NODE="${2:-}"
OUTDIR="ksplice-validation-${NODE}"

if [[ -z "$PHASE" || -z "$NODE" ]]; then
    echo "Usage: $0 <before|after|diff> <node-ip>"
    exit 1
fi

mkdir -p "$OUTDIR"

capture_snapshot() {
    local tag="$1"
    local prefix="${OUTDIR}/${tag}"

    echo "[$tag] Capturing snapshot for node $NODE ..."

    # 1. Node status
    echo "  - Node status"
    kubectl get node "$NODE" -o wide > "${prefix}_node_status.txt" 2>&1

    # 2. Node conditions
    echo "  - Node conditions"
    kubectl get node "$NODE" -o jsonpath='{range .status.conditions[*]}{.type}={.status}{"\n"}{end}' > "${prefix}_node_conditions.txt" 2>&1

    # 3. Kernel version (from kubectl)
    echo "  - Kernel version"
    kubectl get node "$NODE" -o jsonpath='{.status.nodeInfo.kernelVersion}' > "${prefix}_kernel.txt" 2>&1
    echo "" >> "${prefix}_kernel.txt"

    # 4. All pods on this node — name, namespace, status, ready, restarts
    echo "  - Pod inventory"
    kubectl get pods --all-namespaces --field-selector "spec.nodeName=${NODE}" \
        -o custom-columns='NAMESPACE:.metadata.namespace,NAME:.metadata.name,STATUS:.status.phase,READY:.status.containerStatuses[0].ready,RESTARTS:.status.containerStatuses[0].restartCount' \
        --sort-by='.metadata.namespace' > "${prefix}_pods.txt" 2>&1

    # 5. Pod restart counts (for diff)
    echo "  - Pod restart counts"
    kubectl get pods --all-namespaces --field-selector "spec.nodeName=${NODE}" \
        -o jsonpath='{range .items[*]}{.metadata.namespace}/{.metadata.name} restarts={.status.containerStatuses[0].restartCount}{"\n"}{end}' \
        > "${prefix}_restarts.txt" 2>&1

    # 6. Pod readiness (ready containers / total containers)
    echo "  - Pod readiness"
    kubectl get pods --all-namespaces --field-selector "spec.nodeName=${NODE}" \
        -o custom-columns='NAMESPACE:.metadata.namespace,NAME:.metadata.name,READY_CONTAINERS:.status.containerStatuses[*].ready' \
        > "${prefix}_readiness.txt" 2>&1

    # 7. Node events (last 10 mins worth)
    echo "  - Node events"
    kubectl get events --field-selector "involvedObject.name=${NODE}" --sort-by='.lastTimestamp' \
        > "${prefix}_node_events.txt" 2>&1 || echo "No events" > "${prefix}_node_events.txt"

    # 8. Pod events on this node (warnings only)
    echo "  - Pod warning events"
    kubectl get events --all-namespaces --field-selector "type=Warning" --sort-by='.lastTimestamp' \
        > "${prefix}_warnings_all.txt" 2>&1 || echo "No warnings" > "${prefix}_warnings_all.txt"

    # 9. Node resource usage
    echo "  - Node resource usage"
    kubectl top node "$NODE" > "${prefix}_node_resources.txt" 2>&1 || echo "metrics-server unavailable" > "${prefix}_node_resources.txt"

    # 10. Timestamp
    echo "  - Timestamp"
    date -u '+%Y-%m-%d %H:%M:%S UTC' > "${prefix}_timestamp.txt"

    echo "[$tag] Snapshot saved to ${prefix}_*.txt"
    echo ""

    # Summary
    echo "===== $tag SUMMARY ====="
    echo "Timestamp:  $(cat ${prefix}_timestamp.txt)"
    echo "Kernel:     $(cat ${prefix}_kernel.txt)"
    echo "Node:       $(kubectl get node $NODE -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}')"

    local total_pods=$(grep -c '/' "${prefix}_restarts.txt" 2>/dev/null || echo 0)
    local running_pods=$(grep -c 'Running' "${prefix}_pods.txt" 2>/dev/null || echo 0)
    echo "Pods:       ${running_pods} running / ${total_pods} total"

    local total_restarts=$(awk -F'=' '{sum+=$2} END{print sum}' "${prefix}_restarts.txt" 2>/dev/null || echo 0)
    echo "Restarts:   ${total_restarts} total across all pods"

    echo "Conditions: $(cat ${prefix}_node_conditions.txt | tr '\n' ' ')"
    echo "========================="
}

run_diff() {
    local before="${OUTDIR}/before"
    local after="${OUTDIR}/after"

    if [[ ! -f "${before}_kernel.txt" ]]; then
        echo "ERROR: No 'before' snapshot found. Run: $0 before $NODE"
        exit 1
    fi
    if [[ ! -f "${after}_kernel.txt" ]]; then
        echo "ERROR: No 'after' snapshot found. Run: $0 after $NODE"
        exit 1
    fi

    echo "============================================"
    echo " KSPLICE VALIDATION REPORT — NODE $NODE"
    echo "============================================"
    echo ""

    # Timestamps
    echo "Before: $(cat ${before}_timestamp.txt)"
    echo "After:  $(cat ${after}_timestamp.txt)"
    echo ""

    # Kernel change
    local k_before=$(cat "${before}_kernel.txt" | tr -d '\n')
    local k_after=$(cat "${after}_kernel.txt" | tr -d '\n')
    echo "--- KERNEL ---"
    echo "Before: $k_before"
    echo "After:  $k_after"
    if [[ "$k_before" == "$k_after" ]]; then
        echo "Result: UNCHANGED (Ksplice patches at runtime, uname may not change — check effective kernel in OSMH)"
    else
        echo "Result: CHANGED"
    fi
    echo ""

    # Node conditions
    echo "--- NODE CONDITIONS ---"
    local cond_diff=$(diff "${before}_node_conditions.txt" "${after}_node_conditions.txt" 2>&1 || true)
    if [[ -z "$cond_diff" ]]; then
        echo "Result: PASS — No change in node conditions"
    else
        echo "Result: CHANGED"
        echo "$cond_diff"
    fi
    echo ""

    # Pod count
    local pods_before=$(grep -c '/' "${before}_restarts.txt" 2>/dev/null || echo 0)
    local pods_after=$(grep -c '/' "${after}_restarts.txt" 2>/dev/null || echo 0)
    echo "--- POD COUNT ---"
    echo "Before: $pods_before"
    echo "After:  $pods_after"
    if [[ "$pods_before" -eq "$pods_after" ]]; then
        echo "Result: PASS — Same pod count"
    else
        echo "Result: CHANGED — Pod count differs (may be normal scaling)"
    fi
    echo ""

    # Restart count diff
    echo "--- POD RESTARTS ---"
    local restarts_before=$(awk -F'=' '{sum+=$2} END{print sum+0}' "${before}_restarts.txt" 2>/dev/null)
    local restarts_after=$(awk -F'=' '{sum+=$2} END{print sum+0}' "${after}_restarts.txt" 2>/dev/null)
    echo "Before: $restarts_before total"
    echo "After:  $restarts_after total"
    local restart_increase=$((restarts_after - restarts_before))
    if [[ "$restart_increase" -le 0 ]]; then
        echo "Result: PASS — No new restarts"
    else
        echo "Result: WARNING — $restart_increase new restart(s) detected"
        echo "Changed pods:"
        diff "${before}_restarts.txt" "${after}_restarts.txt" 2>&1 || true
    fi
    echo ""

    # Pod status changes
    echo "--- POD STATUS CHANGES ---"
    local status_diff=$(diff "${before}_pods.txt" "${after}_pods.txt" 2>&1 || true)
    if [[ -z "$status_diff" ]]; then
        echo "Result: PASS — No pod status changes"
    else
        echo "Result: CHANGED"
        echo "$status_diff"
    fi
    echo ""

    # Verdict
    echo "============================================"
    if [[ "$restart_increase" -le 0 && -z "$cond_diff" ]]; then
        echo " VERDICT: PASS"
        echo " Ksplice kernel patch applied successfully."
        echo " Node healthy, no pod restarts, no condition changes."
    else
        echo " VERDICT: REVIEW NEEDED"
        echo " Check the changes above before proceeding."
    fi
    echo "============================================"
}

case "$PHASE" in
    before) capture_snapshot "before" ;;
    after)  capture_snapshot "after" ;;
    diff)   run_diff ;;
    *)      echo "Unknown phase: $PHASE. Use before|after|diff"; exit 1 ;;
esac
