#!/usr/bin/env python3
"""Context-length sweep — recompute vs L2 (native GDS) TTFT.

Evidence for the break-even curve. Measures both arms at each context length and
self-validates tier purity by recording per-request kernel disk-read bytes.
"""
import json, subprocess, time, statistics as st, os, csv, sys, tempfile

# Wall-clock cost of a run is planning information for a shared host, so every
# run records its own. time.monotonic() measures the elapsed span; the stamps
# are UTC so bundles from different sessions line up.
_STARTED_MONO = time.monotonic()
_STARTED_AT = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


RUN        = os.environ.get("RUN_DIR", "/tmp/ctx-sweep")
GDS_PORT   = int(os.environ.get("GDS_PORT", "8102"))
GDS_CTNR   = os.environ.get("GDS_CTNR", "vllm-lmcache-sweep")
RECO_PORT  = int(os.environ.get("RECO_PORT", "8100"))
RECO_CTNR  = os.environ.get("RECO_CTNR", "vllm-baseline")
SKIP_GDS   = os.environ.get("SKIP_GDS", "0") == "1"
TOK_PORT   = int(os.environ.get("TOKENIZE_PORT", os.environ.get("GDS_PORT", "8102")))
N          = int(os.environ.get("N", "10"))
WARMUP     = int(os.environ.get("WARMUP", "2"))
FILLERS    = int(os.environ.get("FILLERS", "6"))   # push >= 2x the L1+pool capacity through
FILLER_TOK = int(os.environ.get("FILLER_TOK", "8192"))
CONTEXTS   = [int(x) for x in os.environ.get(
    "CONTEXTS", "512,1024,2048,4096,5632,8192").split(",")]
MODEL      = os.environ.get("PROBE_MODEL", "Qwen2.5-32B-Instruct")
# KV bytes per 256-token chunk, set by the model's KV geometry. Qwen2.5-32B =
# 64 MiB (256 KiB/token); Llama-3.1-8B = 32 MiB (128 KiB/token). Drives the
# expected-read used by the tier-purity check, so override per model.
MIB_PER_CHUNK = int(os.environ.get("PROBE_MIB_PER_CHUNK", "64"))
os.makedirs(RUN, exist_ok=True)

# Portable English transport payload. Keep it byte-for-byte stable within this
# workload version: changing it changes tokenization and therefore the measured
# KV shape.
SENT = (
    "LMCache reuses previously computed key-value cache blocks when the same long "
    "context appears again. This deterministic sentence exists only to control "
    "the token length of this storage experiment. "
)
TAIL = "\n\nDo not summarize the context. Reply with exactly 'STAGE1_D6_OK'."


def _bodyfile(obj):
    """A long payload hits the Linux single-argument limit (128 KB) on the command line -> pass it through a file."""
    f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
    json.dump(obj, f); f.close()
    return f.name


def post(port, body, stream=False, timeout=600):
    b = dict(body); b["stream"] = stream
    bf = _bodyfile(b)
    t0 = time.perf_counter()
    p = subprocess.Popen(
        ["curl", "-sN", "--max-time", str(timeout), "-X", "POST",
         f"http://127.0.0.1:{port}/v1/chat/completions",
         "-H", "Content-Type: application/json", "--data-binary", "@" + bf],
        stdout=subprocess.PIPE, text=True)
    ttft = None
    for line in p.stdout:
        if stream and line.startswith("data: "):
            d = line[6:].strip()
            if d == "[DONE]":
                break
            try:
                o = json.loads(d)
            except Exception:
                continue
            c = (o.get("choices") or [{}])[0].get("delta", {}).get("content")
            if c and ttft is None:
                ttft = time.perf_counter() - t0
    p.wait()
    os.unlink(bf)
    return ttft, time.perf_counter() - t0


def ntokens(port, text):
    """Get the exact token count from vLLM /tokenize (do not estimate)."""
    bf = _bodyfile({"model": MODEL, "prompt": text})
    out = subprocess.run(
        ["curl", "-s", "-X", "POST", f"http://127.0.0.1:{TOK_PORT}/tokenize",
         "-H", "Content-Type: application/json", "--data-binary", "@" + bf],
        capture_output=True, text=True).stdout
    os.unlink(bf)
    try:
        return json.loads(out)["count"]
    except Exception:
        print(f"  !! /tokenize failed: {out[:200]}", file=sys.stderr)
        raise SystemExit(1)


def build(port, target, prefix=""):
    """Size the body so the cacheable tokens hit target (a multiple of 256)."""
    lo, hi = 1, 4000
    best = None
    while lo <= hi:                      # binary search on repetition count
        mid = (lo + hi) // 2
        text = prefix + SENT * mid + TAIL
        n = ntokens(port, text)
        if n < target:
            lo = mid + 1
        else:
            best = (mid, n, text); hi = mid - 1
    if best is None:
        raise SystemExit(f"could not construct {target} tokens")
    reps, n, text = best
    return {"model": MODEL, "messages": [{"role": "user", "content": text}],
            "max_tokens": 7, "temperature": 0}, n


def nvfs():
    out = subprocess.run(
        ["bash", "-c", "grep -m1 '^Reads' /proc/driver/nvidia-fs/stats"],
        capture_output=True, text=True).stdout
    for tok in out.split():
        if tok.startswith("readMiB="):
            return int(tok.split("=")[1])
    return -1


def docker_logs(ctnr):
    """Return container logs without interpolating the name into a shell command."""
    return subprocess.run(
        ["docker", "logs", ctnr],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
        text=True,
    ).stdout.splitlines()


def logcount(ctnr):
    return len(docker_logs(ctnr))


def lastline(ctnr, mark, pat):
    matches = [line for line in docker_logs(ctnr)[mark:] if pat.lower() in line.lower()]
    return matches[-1] if matches else ""


def gbs(line):
    if "throughput:" in line:
        try:
            return float(line.split("throughput:")[1].split("GB/s")[0])
        except Exception:
            pass
    return None


# Dropping the host page cache affects everyone on a shared host, and a native GDS read
# bypasses the page cache anyway (O_DIRECT). It runs only when the operator opts in, for
# example to investigate a compat-mode (POSIX) path.
DROP_HOST_CACHE = os.environ.get("PROBE_DROP_HOST_CACHE") == "1"

def drop_host_cache():
    """Drop the host page cache, only when the operator opted in."""
    if DROP_HOST_CACHE:
        subprocess.run(["bash", "-c",
                        "sync; echo 3 | sudo tee /proc/sys/vm/drop_caches >/dev/null"])


def evict(fillers):
    for f in fillers:
        post(GDS_PORT, f)
    drop_host_cache()


def _measure_once(port, payload, ctnr):
    """One request — TTFT, retrieve throughput, kernel disk-read bytes."""
    mark = logcount(ctnr) if ctnr else 0
    r0 = nvfs()
    ttft, total = post(port, payload, stream=True)
    r1 = nvfs()
    g = gbs(lastline(ctnr, mark, "Retrieved")) if ctnr else None
    return {"ttft": ttft, "total": total, "retrieve_gbs": g, "disk_read_mib": r1 - r0}


def _summary(name, rows):
    m = [r for r in rows if r["phase"] == "measure"]
    t = sorted(r["ttft"] for r in m)
    rd = [r["disk_read_mib"] for r in m]
    s = {"arm": name, "n": len(m),
         "ttft_p50": st.median(t), "ttft_min": t[0], "ttft_max": t[-1],
         "disk_read_mib_median": st.median(rd),
         "disk_read_unique": sorted(set(rd)),
         "zero_disk_read": sum(1 for x in rd if x == 0)}
    gs = [r["retrieve_gbs"] for r in m if r["retrieve_gbs"]]
    if gs:
        s["retrieve_gbs_median"] = st.median(gs)
    return s


def arm_recompute(name, port, payloads):
    rows = []
    for i, p in enumerate(payloads):
        r = _measure_once(port, p, None)
        r["phase"] = "warmup" if i < WARMUP else "measure"
        rows.append(r)
        print(f"    [{r['phase']}] {i:2d} ttft={r['ttft']:.4f}s", flush=True)
    return _summary(name, rows), rows


def classify(r):
    """Classify the failure type — the fixes are opposite.

    HIT_DISK : read from disk (normal)
    HIT_MEM  : zero read but a retrieve log exists -> resident GPU/CPU hit; eviction insufficient
    MISS     : zero read and no retrieve log -> not in cache at all; store was truncated or evicted
    PARTIAL  : read less than expected -> only some chunks were stored
    """
    if r["disk_read_mib"] >= r["expected_mib"] * 0.95:
        return "HIT_DISK"
    if r["disk_read_mib"] > 0:
        return "PARTIAL"
    return "HIT_MEM" if r["retrieve_gbs"] else "MISS"


def arm_gds(name, port, payload, fillers, ctnr, expected_mib):
    """Evict memory with fillers before each request, then read (the approach validated 20/20 in the 2026-08-25 D arm).

    Pre-storing several payloads exceeded the L1 staging capacity and truncated the
    store (2026-08-27 second sweep: many misses and partial reads). Keep a single target.
    """
    rows = []
    for i in range(N + WARMUP):
        for k, f in enumerate(fillers):
            post(port, f(i, k))
        drop_host_cache()
        r = _measure_once(port, payload, ctnr)
        r["expected_mib"] = expected_mib
        r["phase"] = "warmup" if i < WARMUP else "measure"
        r["kind"] = classify(r)
        rows.append(r)
        print(f"    [{r['phase']}] {i:2d} ttft={r['ttft']:.4f}s"
              + (f" retrieve={r['retrieve_gbs']:.2f}GB/s" if r["retrieve_gbs"] else "")
              + f" read+{r['disk_read_mib']}MiB  {r['kind']}", flush=True)
    s = _summary(name, rows)
    m = [r for r in rows if r["phase"] == "measure"]
    s["kinds"] = {k: sum(1 for r in m if r["kind"] == k)
                  for k in ("HIT_DISK", "HIT_MEM", "PARTIAL", "MISS")}
    return s, rows


def main():
    subprocess.run(["bash", "-c", "sudo -v"])
    print(f"context sweep: {CONTEXTS}  (n={N}, warmup={WARMUP})\n")

    filler_makers = []
    if SKIP_GDS:
        print("== SKIP_GDS=1 — measuring the recompute arm only (no fillers needed) ==")
    else:
        print("== building fillers (each iteration must be unique to apply pressure) ==")
    for k in range(0 if SKIP_GDS else FILLERS):
        p0, n0 = build(GDS_PORT, FILLER_TOK, prefix=f"FILLER-{k} ")
        print(f"  filler{k}: {n0} tokens")
        def mk(i, k, base=p0):
            b = json.loads(json.dumps(base))
            b["messages"][0]["content"] = f"EVICT-{i}-{k} " + b["messages"][0]["content"]
            return b
        filler_makers.append(mk)

    results, allrows = [], {}
    for ctx in CONTEXTS:
        print(f"\n===== context target {ctx} tokens =====")
        payload, actual = build(GDS_PORT, ctx)
        cacheable = (actual // 256) * 256
        exp_mib = round(cacheable // 256 * MIB_PER_CHUNK)   # KV per 256-tok chunk (Qwen 64 MiB); PROBE_MIB_PER_CHUNK
        print(f"  prompt {actual} tokens -> cacheable {cacheable} (256x{cacheable//256}), "
              f"KV expected {exp_mib} MiB")

        print(f"  -- arm R: recompute (GPU1, {RECO_CTNR})")
        sR, rR = arm_recompute(f"R-{ctx}", RECO_PORT, [payload] * (N + WARMUP))

        if SKIP_GDS:
            sG = {"arm": f"G-{ctx}", "n": 0, "ttft_p50": float("nan"),
                  "disk_read_mib_median": 0, "zero_disk_read": 0,
                  "kinds": {}, "disk_read_unique": []}
            rG = []
        else:
            print(f"  -- arm G: L2 native GDS (GPU0, {GDS_CTNR})  {FILLERS} fillers/request")
            sG, rG = arm_gds(f"G-{ctx}", GDS_PORT, payload, filler_makers, GDS_CTNR, exp_mib)

        ok = True if SKIP_GDS else (
            sG["zero_disk_read"] == 0
            and abs(sG["disk_read_mib_median"] - exp_mib) <= max(32, exp_mib * 0.05))
        row = {"kinds": sG["kinds"], "context_target": ctx, "prompt_tokens": actual, "cacheable_tokens": cacheable,
               "kv_expected_mib": exp_mib,
               "recompute_ttft_p50": sR["ttft_p50"], "gds_ttft_p50": sG["ttft_p50"],
               "speedup": sR["ttft_p50"] / sG["ttft_p50"],
               "saving_s": sR["ttft_p50"] - sG["ttft_p50"],
               "gds_retrieve_gbs": sG.get("retrieve_gbs_median"),
               "gds_disk_read_mib_median": sG["disk_read_mib_median"],
               "gds_zero_disk_read": sG["zero_disk_read"],
               "tier_purity_ok": ok}
        results.append(row)
        allrows[ctx] = {"recompute": rR, "gds": rG, "summary_R": sR, "summary_G": sG}
        if SKIP_GDS:
            print(f"  -> recompute {sR['ttft_p50']:.4f}s (GDS arm skipped)")
            results.append({"kinds": {}, "context_target": ctx, "prompt_tokens": actual,
                            "cacheable_tokens": cacheable, "kv_expected_mib": exp_mib,
                            "recompute_ttft_p50": sR["ttft_p50"], "gds_ttft_p50": None,
                            "speedup": None, "saving_s": None, "gds_retrieve_gbs": None,
                            "gds_disk_read_mib_median": None, "gds_zero_disk_read": None,
                            "tier_purity_ok": True})
            allrows[ctx] = {"recompute": rR, "gds": [], "summary_R": sR, "summary_G": sG}
            continue
        print(f"     kind distribution: {sG['kinds']}")
        print(f"  -> recompute {sR['ttft_p50']:.4f}s / GDS {sG['ttft_p50']:.4f}s "
              f"= {row['speedup']:.2f}x  saving {row['saving_s']*1000:.0f} ms  "
              + ("OK tier purity" if ok else "FAIL tier purity — discard this point"))

    with open(f"{RUN}/sweep.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(results[0].keys())); w.writeheader(); w.writerows(results)
    json.dump(allrows, open(f"{RUN}/sweep-raw.json", "w"), indent=2)
    json.dump(results, open(f"{RUN}/sweep-summary.json", "w"), indent=2)
    # sweep-summary.json is a list (the baseline comparator reads it that way),
    # so timing goes in its own file rather than changing that shape.
    json.dump({"gate": "g2_context_sweep",
               "started_at": _STARTED_AT,
               "ended_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
               "elapsed_s": round(time.monotonic() - _STARTED_MONO, 1)},
              open(f"{RUN}/run-timing.json", "w"), indent=2)

    print("\n===== summary =====")
    print(f"{'ctx':>6} {'recompute':>10} {'GDS':>9} {'mult':>7} {'save_ms':>9} {'pure':>5}")
    for r in results:
        if r["gds_ttft_p50"] is None:
            print(f"{r['cacheable_tokens']:>6} {r['recompute_ttft_p50']:>10.4f} "
                  f"{'-':>9} {'-':>7} {'-':>9} {'-':>5}")
        else:
            print(f"{r['cacheable_tokens']:>6} {r['recompute_ttft_p50']:>10.4f} "
                  f"{r['gds_ttft_p50']:>9.4f} {r['speedup']:>6.2f}x {r['saving_s']*1000:>8.0f} "
                  f"{'OK' if r['tier_purity_ok'] else 'FAIL':>5}")
    bad = [r for r in results if not r["tier_purity_ok"]]
    if bad:
        print(f"\n!! tier purity failed for {len(bad)} point(s) — not an L2 hit, do not use")
    print(f"\nevidence: {RUN}")


main()
