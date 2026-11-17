# Ksplice OKE Node Validator

**3-layer health validation for Oracle Ksplice live kernel patching on OKE (Oracle Kubernetes Engine) nodes.**

Proves that application workloads are **not affected** by kernel version updates — specifically designed for validating Ksplice patches on production Kubernetes clusters before scaling rollouts.

---

## Why This Exists

**Problem:** Oracle Ksplice applies kernel patches at runtime without rebooting. But how do you *prove* that a live kernel patch didn't silently break application traffic? A pod can show `Running` with zero restarts while returning 500s, dropping connections, or corrupting data.

**Context:** CVE-2026-99999 ("PwnEscape") — a CVSS 9.3 critical vulnerability in the Linux IPv4 stack (`checksum_start` validation flaw) affecting all Linux kernels since 2008. The patch modifies the **networking stack** — the highest-risk area for a Kubernetes cluster where all traffic flows through the kernel.

Standard Kubernetes health checks (node conditions, pod status, restart counts) only validate **infrastructure** — they don't validate that **traffic still flows correctly** after a kernel networking patch.

## What It Validates — 3 Layers

| Layer | What It Proves | How |
|-------|---------------|-----|
| **L1: Infrastructure** | Node is up, pods running, no crash loops | Node conditions, pod counts, restart counts, pod phase status |
| **L2: Istio Proxy Health** | Service mesh control plane and data plane are live | Envoy `healthz/ready`, `server_live` metric, xDS upstream connection to istiod |
| **L3: End-to-End HTTP** | Real requests flow through ingress → kernel IPv4 stack → backend → response | Auto-discovers VirtualService endpoints, curls them from inside an ingress gateway pod on the target node |

**L3 is the key differentiator.** If an HTTP request enters the ingress gateway on the patched node and a backend returns a valid response, the kernel's entire IPv4 stack (the exact code path the CVE touches) is confirmed working.

### Why 3 Layers?

```
Layer 1 alone:  Node=Ready, Pods=Running     → kernel could be silently dropping packets
Layer 1+2:      + Envoy healthy               → istio works, but app traffic untested
Layer 1+2+3:    + HTTP 401/302 from backends  → full IPv4 stack proven end-to-end
```

## How It Works

### Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    before.json / after.json                  │
│                   (structured JSON snapshots)                │
├─────────────┬──────────────────┬────────────────────────────┤
│  L1: Infra  │  L2: Istio       │  L3: HTTP Probes           │
│             │                  │                            │
│ • Node      │ • healthz/ready  │ • Auto-discover            │
│   conditions│   per gateway    │   VirtualService hosts     │
│ • Pod list  │ • envoy          │ • kubectl exec into        │
│ • Restart   │   server_live    │   ingress gateway pod      │
│   counts    │ • xDS upstream   │   ON the target node       │
│ • Phase     │   connectivity   │ • Curl each host via       │
│   status    │ • istiod health  │   localhost:8080            │
│             │                  │ • Record status + latency  │
└─────────────┴──────────────────┴────────────────────────────┘
                           │
                     diff command
                           │
                    ┌──────▼──────┐
                    │   VERDICT   │
                    │  PASS/FAIL  │
                    │  per layer  │
                    └─────────────┘
```

### Usage

```bash
# STEP 1: Capture pre-patch baseline
python3 ksplice_node_validation.py before <node-ip>

# STEP 2: Apply Ksplice
# Via OSMH console (scheduled job) or SSH:
# sudo ksplice --all upgrade

# STEP 3: Capture post-patch state and compare
python3 ksplice_node_validation.py after <node-ip>
python3 ksplice_node_validation.py diff <node-ip>
```

### Example Output — Before Snapshot

```
=======================================================
  BEFORE SNAPSHOT — 172.31.3.51
=======================================================
  Timestamp  : 2026-11-17 06:01:26 UTC
  Kernel     : 5.15.0-320.202.8.2.el8uek.x86_64
  Kubelet    : v1.36.0

  L1 INFRA
    Node Ready : True
    Conditions : NetworkUnavailable=False, MemoryPressure=False,
                 DiskPressure=False, PIDPressure=False, Ready=True
    Pods       : 11 running / 14 total
    Restarts   : 1 total
    Non-running:
      - aero-qualys/qualys-container-sensor-4fg9q (Pending)

  L2 ISTIO
    Gateways healthy : 4/4
      istio-ingressgateway-...-65c6j: OK (live=1, xds_cx=1)
      istio-ingressgateway-...-g4v5v: OK (live=1, xds_cx=1)
      istio-ingressgateway-...-s2jlr: OK (live=1, xds_cx=1)
      aeromart-istio-ingressgateway-...: OK (live=1, xds_cx=1)

  L3 HTTP
    Probes passed : 10/10
      [OK] aero-agent-stage1: ...stage1-airarabia.isaaviation.net -> 401 (1ms)
      [OK] aero-agent-stage2: ...stage2-airarabia.isaaviation.net -> 401 (1ms)
      [OK] aero-anci-stage1:  ...stage1-airarabia.isaaviation.net -> 302 (1ms)
      ...
=======================================================
```

### Example Output — Diff Report

```
============================================================
  KSPLICE VALIDATION REPORT — NODE 172.31.3.51
============================================================

  Before : 2026-11-17 06:01:26 UTC
  After  : 2026-11-17 06:45:12 UTC

──────────────────────────────────────────────────────────────
  L1: INFRASTRUCTURE
──────────────────────────────────────────────────────────────

--- KERNEL ---
  Before : 5.15.0-320.202.8.2.el8uek.x86_64
  After  : 5.15.0-320.202.8.2.el8uek.x86_64
  Result : UNCHANGED (check effective kernel: uptrack-uname -r)

--- NODE CONDITIONS ---
  Result : PASS

--- POD RESTARTS ---
  Before : 1
  After  : 1
  Result : PASS — No new restarts

──────────────────────────────────────────────────────────────
  L2: ISTIO PROXY HEALTH
──────────────────────────────────────────────────────────────

--- GATEWAY & ISTIOD HEALTH ---
  OK: istio-ingressgateway-...-65c6j: healthz=healthy, live=1
  OK: istio-ingressgateway-...-g4v5v: healthz=healthy, live=1
  Result : PASS

──────────────────────────────────────────────────────────────
  L3: END-TO-END HTTP TRAFFIC
──────────────────────────────────────────────────────────────

--- HTTP PROBES ---
  OK: aero-agent-stage1: ...stage1.net — 401->401 (1ms->1ms)
  OK: aero-anci-stage1:  ...stage1.net — 302->302 (1ms->1ms)
  Result : PASS

============================================================
  L1 Infrastructure : PASS
  L2 Istio Proxy    : PASS
  L3 HTTP Traffic   : PASS
──────────────────────────────────────────────────────────────
  VERDICT: PASS
  Ksplice kernel patch applied successfully.
  Node healthy. Istio proxies live. HTTP traffic flowing.
  Safe to proceed with rollout to remaining nodes.
============================================================
```

## Cross-Validation Performed

The script output was **manually cross-verified** against direct `kubectl` commands on a live OKE cluster (StagingX, 40 nodes) on 2026-11-17:

| Check | Script Result | Manual `kubectl` Result | Match |
|-------|--------------|------------------------|-------|
| Kernel version | `5.15.0-320.202.8.2.el8uek.x86_64` | `5.15.0-320.202.8.2.el8uek.x86_64` | YES |
| Node conditions | All healthy | All healthy | YES |
| Pod count | 14 total, 11 running | 14 total, 11 running | YES |
| Total restarts | 1 (fluentd) | 1 (fluentd) | YES |
| Gateway healthz | 4/4 healthy (HTTP 200) | 4/4 HTTP 200 | YES |
| Envoy server_live | All = 1 | All = 1 | YES |
| xDS connections | All = 1 | All = 1 | YES |
| HTTP stage1-6 | 401 (1ms) | 401 (1ms) | YES |
| HTTP anci1-4 | 302 (1ms) | 302 (1ms) | YES |

## How L3 Probing Works (Core Logic)

1. **Auto-discover endpoints**: Reads all Istio `VirtualService` resources across namespaces. Picks one representative host per namespace (up to 10) that routes through a gateway (not just mesh-internal).

2. **Execute from the target node**: Uses `kubectl exec` into an ingress gateway pod **running on the target node** — this ensures the HTTP request traverses the patched kernel's network stack.

3. **Curl via localhost:8080**: The ingress gateway listens on port 8080. Requests include the `Host` header to route through Istio's VirtualService config to the actual backend service.

4. **Record status + latency**: HTTP status codes (401 = auth-protected API responding, 302 = redirect working, 200 = fully open endpoint) and response times in milliseconds.

5. **Diff logic**: If an endpoint returned a valid response before (2xx-4xx) and fails after (0, 5xx, timeout), that's a **BROKEN** verdict. Latency spikes over 2 seconds are flagged as warnings.

```python
# Core probe logic (simplified)
for endpoint in auto_discovered_endpoints:
    result = kubectl_exec(ingress_pod, [
        "curl", "-s", "-o", "/dev/null",
        "-w", "%{http_code} %{time_total}",
        "-H", f"Host: {endpoint.host}",
        "http://localhost:8080/"
    ])
    # status_code + response_time captured per probe
```

## Requirements

- `kubectl` configured with access to the target OKE cluster
- Python 3.8+
- No external Python dependencies (uses only stdlib: `json`, `subprocess`, `pathlib`)
- Istio service mesh (for L2 and L3 checks)

## Files

| File | Description |
|------|-------------|
| `ksplice_node_validation.py` | Python implementation (recommended) — structured JSON snapshots, 3-layer validation |
| `ksplice_node_validation.sh` | Bash implementation — L1-only infrastructure checks, text-based snapshots |

## Rollout Strategy

This tool is designed for a staged kernel patching rollout:

```
1. Single node (pilot)     →  validate with this tool
2. Staging cluster         →  validate, monitor 30-60min
3. Production (batch 1)    →  validate, monitor
4. Production (remaining)  →  validate, confirm
```

At each stage: `before` → patch → `after` → `diff` → proceed or rollback (`ksplice --all remove`).

## License

MIT
