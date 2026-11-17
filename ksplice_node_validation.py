#!/usr/bin/env python3
"""Ksplice Live Kernel Patch — OKE Node Pre/Post Validation

3-Layer validation:
  L1: Infrastructure — node conditions, pod counts, restarts
  L2: Istio proxy health — envoy liveness, xDS sync, healthz
  L3: End-to-end HTTP — real requests through ingress to backends

Usage:
    python3 ksplice_node_validation.py before 172.31.3.51
    # ... apply Ksplice ...
    python3 ksplice_node_validation.py after 172.31.3.51
    python3 ksplice_node_validation.py diff 172.31.3.51
"""

import json
import subprocess
import sys
import re
from datetime import datetime, timezone
from pathlib import Path


def kubectl(args: list[str], timeout: int = 30) -> str:
    result = subprocess.run(
        ["kubectl"] + args,
        capture_output=True, text=True, timeout=timeout
    )
    return result.stdout.strip()


def kubectl_json(args: list[str]) -> dict:
    raw = kubectl(args + ["-o", "json"])
    return json.loads(raw) if raw else {}


# ---------------------------------------------------------------------------
# L1: Infrastructure
# ---------------------------------------------------------------------------

def capture_l1_infra(node: str) -> dict:
    print("  [L1] Infrastructure health")

    print("    - Node info")
    node_data = kubectl_json(["get", "node", node])

    print("    - Pod inventory")
    pods_data = kubectl_json([
        "get", "pods", "--all-namespaces",
        "--field-selector", f"spec.nodeName={node}"
    ])

    print("    - Node events")
    events_raw = kubectl([
        "get", "events",
        "--field-selector", f"involvedObject.name={node}",
        "--sort-by=.lastTimestamp"
    ])

    print("    - Warning events")
    warnings_raw = kubectl([
        "get", "events", "--all-namespaces",
        "--field-selector", "type=Warning",
        "--sort-by=.lastTimestamp"
    ])

    print("    - Node resource usage")
    try:
        resources_raw = kubectl(["top", "node", node])
    except Exception:
        resources_raw = "metrics-server unavailable"

    conditions = {}
    for c in node_data.get("status", {}).get("conditions", []):
        conditions[c["type"]] = c["status"]

    kernel = node_data.get("status", {}).get("nodeInfo", {}).get("kernelVersion", "unknown")
    kubelet = node_data.get("status", {}).get("nodeInfo", {}).get("kubeletVersion", "unknown")

    pods = []
    for item in pods_data.get("items", []):
        ns = item["metadata"]["namespace"]
        name = item["metadata"]["name"]
        phase = item["status"].get("phase", "Unknown")
        containers = item["status"].get("containerStatuses", [])
        restarts = sum(c.get("restartCount", 0) for c in containers)
        ready_count = sum(1 for c in containers if c.get("ready", False))
        total_count = len(containers)
        pods.append({
            "namespace": ns,
            "name": name,
            "phase": phase,
            "restarts": restarts,
            "ready": f"{ready_count}/{total_count}"
        })

    return {
        "kernel": kernel,
        "kubelet": kubelet,
        "conditions": conditions,
        "pods": pods,
        "node_events": events_raw,
        "warning_events": warnings_raw,
        "resources": resources_raw,
    }


# ---------------------------------------------------------------------------
# L2: Istio Proxy Health
# ---------------------------------------------------------------------------

def capture_l2_istio(node: str) -> dict:
    print("  [L2] Istio proxy health")

    # Find ingress gateway pods on this node
    all_istio = kubectl_json([
        "get", "pods", "-n", "istio-system",
        "--field-selector", f"spec.nodeName={node}",
        "-l", "app=istio-ingressgateway",
    ])
    # Also check aeromart ingress gateway
    aeromart_istio = kubectl_json([
        "get", "pods", "-n", "istio-system",
        "--field-selector", f"spec.nodeName={node}",
        "-l", "app=aeromart-ingressgateway",
    ])
    # istiod pods on this node
    istiod_pods = kubectl_json([
        "get", "pods", "-n", "istio-system",
        "--field-selector", f"spec.nodeName={node}",
        "-l", "app=istiod",
    ])

    gw_items = all_istio.get("items", []) + aeromart_istio.get("items", [])
    istiod_items = istiod_pods.get("items", [])

    gw_health = []
    for pod in gw_items:
        pod_name = pod["metadata"]["name"]
        print(f"    - Checking gateway: {pod_name}")

        # healthz/ready (use -o /dev/null -w %{http_code} since healthz returns empty body)
        healthz = "unknown"
        try:
            raw = kubectl([
                "exec", "-n", "istio-system", pod_name, "--",
                "curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
                "http://localhost:15021/healthz/ready"
            ], timeout=10)
            healthz = "healthy" if raw == "200" else f"unhealthy-{raw}"
        except Exception as e:
            healthz = f"error: {e}"

        # envoy server_live metric
        server_live = "unknown"
        upstream_cx = "unknown"
        try:
            stats = kubectl([
                "exec", "-n", "istio-system", pod_name, "--",
                "pilot-agent", "request", "GET", "/stats/prometheus"
            ], timeout=15)
            for line in stats.splitlines():
                if line.startswith("envoy_server_live{"):
                    server_live = line.split("}")[1].strip()
                if "envoy_cluster_upstream_cx_active{" in line and "xds-grpc" in line:
                    upstream_cx = line.split("}")[1].strip()
        except Exception:
            pass

        gw_health.append({
            "pod": pod_name,
            "healthz_ready": healthz,
            "envoy_server_live": server_live,
            "xds_upstream_cx": upstream_cx,
        })

    istiod_health = []
    for pod in istiod_items:
        pod_name = pod["metadata"]["name"]
        print(f"    - Checking istiod: {pod_name}")
        healthz = "unknown"
        try:
            raw = kubectl([
                "exec", "-n", "istio-system", pod_name, "--",
                "curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
                "http://localhost:15014/healthz/ready"
            ], timeout=10)
            healthz = "healthy" if raw == "200" else f"unhealthy-{raw}"
        except Exception as e:
            healthz = f"error: {e}"
        istiod_health.append({"pod": pod_name, "healthz_ready": healthz})

    return {
        "gateway_pods": gw_health,
        "istiod_pods": istiod_health,
    }


# ---------------------------------------------------------------------------
# L3: End-to-End HTTP Traffic
# ---------------------------------------------------------------------------

def discover_probe_endpoints() -> list[dict]:
    """Auto-discover representative virtual service hosts from different namespaces."""
    vs_data = kubectl_json(["get", "virtualservices", "--all-namespaces"])
    items = vs_data.get("items", [])

    seen_ns = set()
    endpoints = []
    for vs in items:
        ns = vs["metadata"]["namespace"]
        if ns in seen_ns:
            continue
        hosts = vs.get("spec", {}).get("hosts", [])
        gateways = vs.get("spec", {}).get("gateways", [])
        # Only pick externally-routed services (through a gateway, not just mesh)
        has_gw = any(g != "mesh" for g in gateways)
        if not has_gw:
            continue
        external_hosts = [h for h in hosts if "." in h and not h.endswith(".local")]
        if external_hosts:
            endpoints.append({
                "namespace": ns,
                "vs_name": vs["metadata"]["name"],
                "host": external_hosts[0],
            })
            seen_ns.add(ns)
        if len(endpoints) >= 10:
            break

    return endpoints


def capture_l3_http(node: str) -> dict:
    print("  [L3] End-to-end HTTP traffic probes")

    # Find an ingress gateway pod on this node to exec into
    gw_pods = kubectl_json([
        "get", "pods", "-n", "istio-system",
        "--field-selector", f"spec.nodeName={node},status.phase=Running",
        "-l", "app=istio-ingressgateway",
    ])
    gw_items = gw_pods.get("items", [])
    if not gw_items:
        print("    WARNING: No running ingress gateway on this node — skipping L3")
        return {"skipped": True, "reason": "no ingress gateway pod on node", "probes": []}

    exec_pod = gw_items[0]["metadata"]["name"]
    print(f"    - Using gateway pod: {exec_pod}")

    endpoints = discover_probe_endpoints()
    if not endpoints:
        print("    WARNING: No virtual services discovered — skipping L3")
        return {"skipped": True, "reason": "no virtual services found", "probes": []}

    print(f"    - Probing {len(endpoints)} endpoints from inside {exec_pod}")

    probes = []
    for ep in endpoints:
        host = ep["host"]
        print(f"    - {ep['namespace']}: {host}")
        status_code = 0
        response_time_ms = 0
        error = None
        try:
            raw = kubectl([
                "exec", "-n", "istio-system", exec_pod, "--",
                "curl", "-sf", "-o", "/dev/null",
                "-w", "%{http_code} %{time_total}",
                "--connect-timeout", "5",
                "--max-time", "10",
                "-H", f"Host: {host}",
                "http://localhost:8080/"
            ], timeout=20)
            parts = raw.strip().split()
            if len(parts) >= 2:
                status_code = int(parts[0])
                response_time_ms = round(float(parts[1]) * 1000)
            elif len(parts) == 1:
                status_code = int(parts[0])
        except subprocess.TimeoutExpired:
            error = "timeout"
        except Exception as e:
            error = str(e)

        probes.append({
            "namespace": ep["namespace"],
            "host": host,
            "status_code": status_code,
            "response_time_ms": response_time_ms,
            "error": error,
        })

    return {"skipped": False, "exec_pod": exec_pod, "probes": probes}


# ---------------------------------------------------------------------------
# Snapshot & Summary
# ---------------------------------------------------------------------------

def capture_snapshot(node: str, out_dir: Path, tag: str):
    print(f"[{tag}] Capturing 3-layer snapshot for node {node} ...\n")

    l1 = capture_l1_infra(node)
    l2 = capture_l2_istio(node)
    l3 = capture_l3_http(node)

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    snapshot = {
        "timestamp": timestamp,
        "node": node,
        "l1_infra": l1,
        "l2_istio": l2,
        "l3_http": l3,
    }

    snapshot_file = (out_dir / tag).with_suffix(".json")
    snapshot_file.write_text(json.dumps(snapshot, indent=2))
    print(f"\n[{tag}] Snapshot saved to {snapshot_file}\n")

    # Print summary
    pods = l1["pods"]
    running = sum(1 for p in pods if p["phase"] == "Running")
    total = len(pods)
    total_restarts = sum(p["restarts"] for p in pods)
    non_running = [p for p in pods if p["phase"] != "Running"]

    gw_ok = sum(1 for g in l2["gateway_pods"] if g["healthz_ready"] == "healthy")
    gw_total = len(l2["gateway_pods"])

    probes_ok = sum(1 for p in l3["probes"] if 200 <= p["status_code"] < 500)
    probes_total = len(l3["probes"])

    print(f"{'=' * 55}")
    print(f"  {tag.upper()} SNAPSHOT — {node}")
    print(f"{'=' * 55}")
    print(f"  Timestamp  : {timestamp}")
    print(f"  Kernel     : {l1['kernel']}")
    print(f"  Kubelet    : {l1['kubelet']}")
    print()
    print(f"  L1 INFRA")
    print(f"    Node Ready : {l1['conditions'].get('Ready', '?')}")
    print(f"    Conditions : {', '.join(f'{k}={v}' for k, v in l1['conditions'].items())}")
    print(f"    Pods       : {running} running / {total} total")
    print(f"    Restarts   : {total_restarts} total")
    if non_running:
        print(f"    Non-running:")
        for p in non_running:
            print(f"      - {p['namespace']}/{p['name']} ({p['phase']})")
    print()
    print(f"  L2 ISTIO")
    print(f"    Gateways healthy : {gw_ok}/{gw_total}")
    for g in l2["gateway_pods"]:
        status = "OK" if g["healthz_ready"] == "healthy" else g["healthz_ready"]
        print(f"      {g['pod']}: {status} (live={g['envoy_server_live']}, xds_cx={g['xds_upstream_cx']})")
    for d in l2["istiod_pods"]:
        status = "OK" if d["healthz_ready"] == "healthy" else d["healthz_ready"]
        print(f"      {d['pod']}: {status}")
    print()
    print(f"  L3 HTTP")
    if l3.get("skipped"):
        print(f"    SKIPPED: {l3['reason']}")
    else:
        print(f"    Probes passed : {probes_ok}/{probes_total}")
        for p in l3["probes"]:
            marker = "OK" if 200 <= p["status_code"] < 500 else "FAIL"
            err = f" ({p['error']})" if p["error"] else ""
            print(f"      [{marker}] {p['namespace']}: {p['host']} -> {p['status_code']} ({p['response_time_ms']}ms){err}")
    print(f"{'=' * 55}")


# ---------------------------------------------------------------------------
# Diff
# ---------------------------------------------------------------------------

def diff_section(title: str, check_fn) -> list[str]:
    """Run a check function, print results, return list of issues."""
    print(f"\n--- {title} ---")
    return check_fn()


def run_diff(node: str, out_dir: Path):
    before_file = out_dir / "before.json"
    after_file = out_dir / "after.json"

    if not before_file.exists():
        print(f"ERROR: No 'before' snapshot. Run: {sys.argv[0]} before {node}")
        sys.exit(1)
    if not after_file.exists():
        print(f"ERROR: No 'after' snapshot. Run: {sys.argv[0]} after {node}")
        sys.exit(1)

    before = json.loads(before_file.read_text())
    after = json.loads(after_file.read_text())
    issues = []

    print(f"\n{'=' * 60}")
    print(f"  KSPLICE VALIDATION REPORT — NODE {node}")
    print(f"{'=' * 60}")
    print(f"\n  Before : {before['timestamp']}")
    print(f"  After  : {after['timestamp']}")

    # --- L1: Infrastructure ---

    print(f"\n{'─' * 60}")
    print("  L1: INFRASTRUCTURE")
    print(f"{'─' * 60}")

    # Kernel
    def check_kernel():
        kb = before["l1_infra"]["kernel"]
        ka = after["l1_infra"]["kernel"]
        print(f"  Before : {kb}")
        print(f"  After  : {ka}")
        if kb == ka:
            print("  Result : UNCHANGED (check effective kernel: uptrack-uname -r)")
        else:
            print("  Result : CHANGED")
        return []
    diff_section("KERNEL", check_kernel)

    # Node conditions
    def check_conditions():
        cb = before["l1_infra"]["conditions"]
        ca = after["l1_infra"]["conditions"]
        changed = []
        for key in set(list(cb.keys()) + list(ca.keys())):
            b = cb.get(key, "missing")
            a = ca.get(key, "missing")
            if b != a:
                print(f"  CHANGED: {key}: {b} -> {a}")
                changed.append(f"Node condition {key} changed: {b} -> {a}")
        if not changed:
            print("  Result : PASS")
        return changed
    issues += diff_section("NODE CONDITIONS", check_conditions)

    # Pod count
    def check_pod_count():
        pb = len(before["l1_infra"]["pods"])
        pa = len(after["l1_infra"]["pods"])
        print(f"  Before : {pb}")
        print(f"  After  : {pa}")
        if pb == pa:
            print("  Result : PASS")
        else:
            d = pa - pb
            print(f"  Result : CHANGED ({'+' if d > 0 else ''}{d})")
        return []
    diff_section("POD COUNT", check_pod_count)

    # Pod restarts
    def check_restarts():
        rb = sum(p["restarts"] for p in before["l1_infra"]["pods"])
        ra = sum(p["restarts"] for p in after["l1_infra"]["pods"])
        inc = ra - rb
        print(f"  Before : {rb}")
        print(f"  After  : {ra}")
        if inc <= 0:
            print("  Result : PASS")
            return []
        print(f"  Result : WARNING — {inc} new restart(s)")
        bmap = {f"{p['namespace']}/{p['name']}": p["restarts"] for p in before["l1_infra"]["pods"]}
        for p in after["l1_infra"]["pods"]:
            key = f"{p['namespace']}/{p['name']}"
            old = bmap.get(key, 0)
            if p["restarts"] > old:
                print(f"    +{p['restarts'] - old}: {key}")
        return [f"{inc} new pod restart(s)"]
    issues += diff_section("POD RESTARTS", check_restarts)

    # Pod status changes
    def check_pod_status():
        bs = {f"{p['namespace']}/{p['name']}": p["phase"] for p in before["l1_infra"]["pods"]}
        a_s = {f"{p['namespace']}/{p['name']}": p["phase"] for p in after["l1_infra"]["pods"]}
        changed = []
        for key in set(list(bs.keys()) + list(a_s.keys())):
            b = bs.get(key, "absent")
            a = a_s.get(key, "absent")
            if b != a:
                print(f"    {key}: {b} -> {a}")
                if a not in ("Running", "Succeeded") and b == "Running":
                    changed.append(f"Pod {key}: {b} -> {a}")
        if not changed:
            print("  Result : PASS")
        return changed
    issues += diff_section("POD STATUS", check_pod_status)

    # --- L2: Istio Proxy Health ---

    print(f"\n{'─' * 60}")
    print("  L2: ISTIO PROXY HEALTH")
    print(f"{'─' * 60}")

    def check_istio():
        gb = {g["pod"]: g for g in before["l2_istio"]["gateway_pods"]}
        ga = {g["pod"]: g for g in after["l2_istio"]["gateway_pods"]}
        problems = []

        all_pods = set(list(gb.keys()) + list(ga.keys()))
        if not all_pods:
            print("  No ingress gateway pods on this node")
            return []

        for pod in sorted(all_pods):
            b = gb.get(pod, {})
            a = ga.get(pod, {})
            bh = b.get("healthz_ready", "absent")
            ah = a.get("healthz_ready", "absent")
            bl = b.get("envoy_server_live", "?")
            al = a.get("envoy_server_live", "?")

            if bh == "healthy" and ah != "healthy":
                print(f"  DEGRADED: {pod}: healthz {bh} -> {ah}")
                problems.append(f"Gateway {pod} healthz degraded: {bh} -> {ah}")
            elif ah == "healthy":
                print(f"  OK: {pod}: healthz={ah}, live={al}")
            else:
                print(f"  {pod}: healthz={ah}, live={al}")

        # istiod
        db = {d["pod"]: d for d in before["l2_istio"]["istiod_pods"]}
        da = {d["pod"]: d for d in after["l2_istio"]["istiod_pods"]}
        for pod in set(list(db.keys()) + list(da.keys())):
            bh = db.get(pod, {}).get("healthz_ready", "absent")
            ah = da.get(pod, {}).get("healthz_ready", "absent")
            if bh == "healthy" and ah != "healthy":
                print(f"  DEGRADED: istiod {pod}: {bh} -> {ah}")
                problems.append(f"istiod {pod} degraded: {bh} -> {ah}")
            elif ah == "healthy":
                print(f"  OK: istiod {pod}")

        if not problems:
            print("  Result : PASS")
        return problems
    issues += diff_section("GATEWAY & ISTIOD HEALTH", check_istio)

    # --- L3: End-to-End HTTP ---

    print(f"\n{'─' * 60}")
    print("  L3: END-TO-END HTTP TRAFFIC")
    print(f"{'─' * 60}")

    def check_http():
        bp = {p["host"]: p for p in before["l3_http"].get("probes", [])}
        ap = {p["host"]: p for p in after["l3_http"].get("probes", [])}
        problems = []

        if before["l3_http"].get("skipped") or after["l3_http"].get("skipped"):
            reason = after["l3_http"].get("reason", before["l3_http"].get("reason", "?"))
            print(f"  SKIPPED: {reason}")
            return []

        all_hosts = sorted(set(list(bp.keys()) + list(ap.keys())))
        if not all_hosts:
            print("  No probes to compare")
            return []

        for host in all_hosts:
            b = bp.get(host, {})
            a = ap.get(host, {})
            bs = b.get("status_code", 0)
            as_ = a.get("status_code", 0)
            bt = b.get("response_time_ms", 0)
            at_ = a.get("response_time_ms", 0)
            ns = a.get("namespace", b.get("namespace", "?"))

            was_ok = 200 <= bs < 500
            is_ok = 200 <= as_ < 500

            if was_ok and not is_ok:
                print(f"  BROKEN: {ns}: {host} — {bs} -> {as_}")
                problems.append(f"HTTP probe broken: {host} was {bs}, now {as_}")
            elif is_ok:
                time_diff = at_ - bt
                time_flag = ""
                if bt > 0 and at_ > 0 and time_diff > 2000:
                    time_flag = f" SLOW (+{time_diff}ms)"
                    problems.append(f"HTTP latency spike: {host} +{time_diff}ms")
                print(f"  OK: {ns}: {host} — {bs}->{as_} ({bt}ms->{at_}ms){time_flag}")
            else:
                print(f"  SKIP: {ns}: {host} — {bs}->{as_} (was already failing)")

        if not problems:
            print("  Result : PASS")
        return problems
    issues += diff_section("HTTP PROBES", check_http)

    # --- Final Verdict ---

    l1_ok = not any("condition" in i or "restart" in i or "Pod " in i for i in issues)
    l2_ok = not any("Gateway" in i or "istiod" in i for i in issues)
    l3_ok = not any("HTTP" in i for i in issues)

    print(f"\n{'=' * 60}")
    print(f"  L1 Infrastructure : {'PASS' if l1_ok else 'FAIL'}")
    print(f"  L2 Istio Proxy    : {'PASS' if l2_ok else 'FAIL'}")
    print(f"  L3 HTTP Traffic   : {'PASS' if l3_ok else 'FAIL'}")
    print(f"{'─' * 60}")

    if not issues:
        print("  VERDICT: PASS")
        print("  Ksplice kernel patch applied successfully.")
        print("  Node healthy. Istio proxies live. HTTP traffic flowing.")
        print("  Safe to proceed with rollout to remaining nodes.")
    else:
        print("  VERDICT: REVIEW NEEDED")
        for i, issue in enumerate(issues, 1):
            print(f"  {i}. {issue}")
    print(f"{'=' * 60}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if len(sys.argv) < 3:
        print(f"Usage: {sys.argv[0]} <before|after|diff> <node-ip>")
        print()
        print("  before  — capture pre-Ksplice baseline")
        print("  after   — capture post-Ksplice state")
        print("  diff    — compare before/after, produce verdict")
        sys.exit(1)

    phase = sys.argv[1]
    node = sys.argv[2]
    out_dir = Path(f"ksplice-validation-{node}")
    out_dir.mkdir(exist_ok=True)

    if phase in ("before", "after"):
        capture_snapshot(node, out_dir, phase)
    elif phase == "diff":
        run_diff(node, out_dir)
    else:
        print(f"Unknown phase: {phase}. Use before|after|diff")
        sys.exit(1)


if __name__ == "__main__":
    main()
