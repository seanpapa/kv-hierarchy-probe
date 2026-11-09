# SPDX-License-Identifier: Apache-2.0
"""Gate 3 — determine when realized reuse repays admission to the tier.

Gate 2 asks whether a verified target-tier hit beats recompute. Gate 3 adds the
cost of admitting an entry and the outcomes that a real reuse attempt can take.
Every requested store/reuse pair is retained. Counter observations classify an
attempt; they never select which latencies enter the economic calculation.

Derived quantities:

* ``g3_admission_cost_s`` is the mean non-negative TTFT cost of the cold store.
* ``realized_reuse_saving_s`` credits positive saving only to target-tier hits.
  Non-target outcomes contribute zero when fast and retain any negative cost
  when slower than recompute.
* ``breakeven_reuse_count`` is admission cost divided by realized reuse saving.

The admission cost is only the cost visible on the time-to-first-token path. A
near-zero value means that storing did not measurably delay the first token; it
does not mean that the store consumed no device or system resources.

The server is started by ``g3_admission_cost.sh``.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Any


# Wall-clock cost of a run is planning information for a shared host, so every
# run records its own. time.monotonic() measures the elapsed span; the stamps
# are UTC so bundles from different sessions line up.
_STARTED_MONO = time.monotonic()
_STARTED_AT = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

#: The accelerator every arm of this run uses. The byte counters this tool
#: reads are host-wide, so the device that produced a run is not recoverable
#: from the evidence afterwards — the run records it here and ``kvprobe
#: ingest`` holds the configuration's runtime block to it.
GPU_INDEX = int(os.environ.get("PROBE_GPU_INDEX", "0"))


def gpu_identity() -> dict[str, Any]:
    """Return the measured device's identity for the run envelope.

    The PCI address becomes ``device_id`` because it is the stable identity
    this platform exposes. The contract itself takes any value that stays
    stable across the campaign and differs between devices, so a platform
    without PCI addressing records whatever names its device instead.

    Returns:
        A mapping with ``device_id`` and ``gpu_index``, or an empty mapping
        when nvidia-smi cannot name the device. An unrecorded device is left
        unrecorded rather than guessed: ingest then has nothing to check the
        configuration against, which is the honest outcome.
    """
    result = subprocess.run(
        [
            "nvidia-smi",
            "-i",
            str(GPU_INDEX),
            "--query-gpu=pci.bus_id",
            "--format=csv,noheader",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    bus_id = result.stdout.strip()
    if result.returncode != 0 or not bus_id:
        return {}
    return {"device_id": bus_id, "gpu_index": GPU_INDEX}


def report_device_identity(identity: dict[str, Any]) -> None:
    """Print the ``device_id`` to copy into the configuration.

    The operator writes the configuration by hand, and ``kvprobe ingest``
    refuses one that names a device the run did not use. Printing the value
    the run recorded is what keeps the two from drifting; a PCI address is
    what this platform exposes, so that is what the scripts read.

    Args:
        identity: The mapping :func:`gpu_identity` returned. Empty when the
            device could not be named.
    """
    if not identity:
        print(
            f"\ndevice_id: NOT RECORDED — nvidia-smi could not name device "
            f"{GPU_INDEX}.\n"
            "  This run carries no device identity, so ingest cannot check "
            "the configuration's\n  runtime.device_id against it. The claim "
            "will stand unverified."
        )
        return
    print(
        f"\ndevice_id: {identity['device_id']}\n"
        f"  PCI address of device {identity['gpu_index']}, from "
        "'nvidia-smi --query-gpu=pci.bus_id'.\n"
        "  Copy it verbatim into the configuration's runtime.device_id. "
        "Use the PCI address\n  wherever the platform exposes one: it is "
        "stable across reboots and distinguishes\n  devices on the same host."
    )


def attributable_reuse_saving(
    recompute_ttft_s: float, reuse_ttft_s: float, outcome: str
) -> float:
    """Return saving attributable to admission into the target tier.

    Args:
        recompute_ttft_s: Recompute reference TTFT in seconds.
        reuse_ttft_s: Actual reuse-attempt TTFT in seconds.
        outcome: Path classification for the reuse attempt.

    Returns:
        The full saving for a target-tier hit. A non-target outcome receives no
        positive credit, because another tier may have produced it, while any
        slower-than-recompute penalty remains part of admission economics.
    """
    raw_saving = recompute_ttft_s - reuse_ttft_s
    if outcome == "target_tier":
        return raw_saving
    return min(0.0, raw_saving)

def run_timing() -> dict[str, object]:
    """Return when this run started and ended, and how long it took.

    Returns:
        Mapping with ``started_at`` and ``ended_at`` as UTC ISO-8601 stamps and
        ``elapsed_s`` as seconds rounded to one decimal.
    """
    return {
        "started_at": _STARTED_AT,
        "ended_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "elapsed_s": round(time.monotonic() - _STARTED_MONO, 1),
    }


MODEL = os.environ.get("PROBE_MODEL", "Qwen2.5-32B-Instruct")
# Every key this run creates is prefixed with a per-run nonce so it is globally fresh.
# Without it, re-running collides with blocks left in the tier by an earlier run: LMCache
# sees a "hit", tries to load a stale block that "can't be retrieved", rolls the request
# back, and the client gets an empty completion.
RUN_NONCE = f"{int(time.time())}-{os.getpid()}"
# Portable English transport payload. Keep it byte-for-byte stable within this
# workload version because its token count sets the KV size under test.
SENTENCE = (
    "LMCache reuses previously computed key-value cache blocks when the same long "
    "context appears again. This deterministic sentence exists only to control "
    "the token length of this storage experiment. "
)
TAIL = "\n\nDo not summarize the context. Reply with exactly 'STAGE1_D6_OK'."
CHUNK_TOKENS = 256
# KV bytes per 256-token chunk (Qwen2.5-32B 64 MiB; Llama-3.1-8B 32 MiB).
# Override per model with PROBE_MIB_PER_CHUNK.
MIB_PER_CHUNK = int(os.environ.get("PROBE_MIB_PER_CHUNK", "64"))


def stream_ttft(port: int, body: dict[str, Any]) -> dict[str, Any]:
    """Run one streamed completion and measure TTFT and total latency.

    Args:
        port: Local vLLM HTTP port.
        body: Chat-completion request without the ``stream`` field.

    Returns:
        A dictionary with ``ttft_s``, ``total_s``, and ``output``.

    Raises:
        RuntimeError: If no content token is returned.
    """
    payload = dict(body)
    payload["stream"] = True
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    started = time.perf_counter()
    first = None
    output: list[str] = []
    seen: list[str] = []
    with urllib.request.urlopen(request, timeout=900) as response:
        for raw_line in response:
            line = raw_line.decode(errors="replace").strip()
            if not line:
                continue
            seen.append(line)
            if not line.startswith("data: "):
                continue
            data = line[6:]
            if data == "[DONE]":
                break
            event = json.loads(data)
            content = (event.get("choices") or [{}])[0].get("delta", {}).get("content")
            if content:
                if first is None:
                    first = time.perf_counter() - started
                output.append(content)
    if first is None:
        # Surface what the server actually sent (e.g. an error payload) instead of a
        # bare exception, so a degraded arm is diagnosable from the console.
        raise RuntimeError("completion returned no content token; server said: " + " ".join(seen)[:500])
    return {"ttft_s": first, "total_s": time.perf_counter() - started, "output": "".join(output)}


def token_count(port: int, text: str) -> int:
    """Return the exact tokenizer count reported by vLLM.

    Args:
        port: Local vLLM HTTP port.
        text: Text to tokenize.

    Returns:
        The exact token count.
    """
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/tokenize",
        data=json.dumps({"model": MODEL, "prompt": text}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        return int(json.loads(response.read())["count"])


def build_payload(port: int, target: int, unique: str = "") -> tuple[dict[str, Any], int]:
    """Build a payload whose cacheable prefix is aligned to ``target`` tokens.

    Args:
        port: Local vLLM HTTP port used for tokenization.
        target: Desired cacheable token count, a positive multiple of 256.
        unique: Per-payload prefix that prevents cross-request cache hits.

    Returns:
        A tuple of request body and actual prompt-token count.

    Raises:
        ValueError: If target is not a positive multiple of 256.
        RuntimeError: If a suitable payload cannot be built.
    """
    if target <= 0 or target % CHUNK_TOKENS:
        raise ValueError("target must be a positive multiple of 256")
    low, high = 1, 8000
    best: tuple[int, str] | None = None
    while low <= high:
        middle = (low + high) // 2
        text = f"{unique} " + SENTENCE * middle + TAIL
        count = token_count(port, text)
        if count < target:
            low = middle + 1
        else:
            best = (count, text)
            high = middle - 1
    if best is None:
        raise RuntimeError(f"could not construct a {target}-token payload")
    count, text = best
    return (
        {"model": MODEL, "messages": [{"role": "user", "content": text}], "max_tokens": 7, "temperature": 0},
        count,
    )


def nvfs_counter(kind: str) -> int:
    """Return a cumulative native-GDS counter in MiB.

    Args:
        kind: ``Reads`` or ``Writes``.

    Returns:
        The cumulative ``readMiB``/``writeMiB`` value, or -1 if unavailable.
    """
    field = "readMiB=" if kind == "Reads" else "writeMiB="
    try:
        for line in Path("/proc/driver/nvidia-fs/stats").read_text().splitlines():
            if line.startswith(kind):
                for token in line.split():
                    if token.startswith(field):
                        return int(token.split("=", 1)[1])
    except OSError:
        pass
    return -1


def expected_mib(cacheable_tokens: int) -> int:
    """Convert aligned Qwen2.5-32B cacheable tokens to KV MiB.

    Args:
        cacheable_tokens: Token count aligned to 256.

    Returns:
        Expected KV size in MiB.
    """
    return cacheable_tokens // CHUNK_TOKENS * MIB_PER_CHUNK


# Dropping the host page cache affects everyone on a shared host, and a native GDS read
# bypasses the page cache anyway (O_DIRECT). It runs only when the operator opts in, for
# example to investigate a compat-mode (POSIX) path.
DROP_HOST_CACHE = os.environ.get("PROBE_DROP_HOST_CACHE") == "1"


def drop_page_cache() -> None:
    """Drop the host page cache before a warm read, only when the operator opted in."""
    if not DROP_HOST_CACHE:
        return
    subprocess.run(
        ["bash", "-c", "sync; echo 3 | sudo tee /proc/sys/vm/drop_caches >/dev/null"],
        check=False,
    )


def evict(port: int, fillers: list[dict[str, Any]], iteration: int) -> None:
    """Displace the entry under test from L1 and the cuFile pool.

    Pushes each filler (made unique per iteration) through the tier, so the next read
    of the entry must come from the device. The host page cache is dropped only when
    ``PROBE_DROP_HOST_CACHE=1``; a native GDS read does not go through it.

    Args:
        port: GDS arm port.
        fillers: Base filler payloads.
        iteration: Sample index used to make every filler unique.
    """
    for index, base in enumerate(fillers):
        payload = json.loads(json.dumps(base))
        payload["messages"][0]["content"] = f"EVICT-{RUN_NONCE}-{iteration}-{index} " + payload["messages"][0]["content"]
        stream_ttft(port, payload)
    drop_page_cache()


def measure_recompute(port: int, payload: dict[str, Any], samples: int, warmup: int) -> float:
    """Return median recompute TTFT with no cache connector.

    Args:
        port: Recompute arm port.
        payload: Request body.
        samples: Measured repetitions.
        warmup: Discarded leading repetitions.

    Returns:
        Median TTFT in seconds over the measured repetitions.
    """
    values: list[float] = []
    for index in range(warmup + samples):
        result = stream_ttft(port, payload)
        if index >= warmup:
            values.append(result["ttft_s"])
    return statistics.median(values)


def measure_store_and_retrieve(
    port: int,
    payload: dict[str, Any],
    unique_prefix: str,
    expected: int,
    samples: int,
    fillers: list[dict[str, Any]],
    warmup: int = 0,
) -> dict[str, Any]:
    """Measure every cold-store and reuse attempt without survivor filtering.

    Each sample uses a fresh entry. Fillers then try to evict it before one reuse
    attempt. Host-wide ``nvidia-fs`` counters classify the observed path. Missing
    write/read observations remain in ``attempts`` so fallback cost cannot vanish
    from the economic result.

    Args:
        port: GDS arm port.
        payload: Base request body (its content is prefixed per sample).
        unique_prefix: Prefix root used to make each entry distinct.
        expected: Expected KV size in MiB for one entry.
        samples: Number of distinct entries to measure.
        fillers: Filler payloads used to evict L1 / the cuFile pool.
        warmup: Store/reuse pairs to execute before counters and timings are
            retained. This warms the GDS arm just as ``measure_recompute``
            warms the recompute arm.

    Returns:
        Per-attempt records, all-attempt latency summaries, and outcome counts.
    """
    store_ttft: list[float] = []
    retrieve_ttft: list[float] = []
    attempts: list[dict[str, Any]] = []
    outcomes = {
        "target_tier": 0,
        "fallback": 0,
        "store_unattributed": 0,
        "unattributed": 0,
    }
    for index in range(warmup):
        entry = json.loads(json.dumps(payload))
        entry["messages"][0]["content"] = (
            f"{unique_prefix}-warmup-{index} "
            + entry["messages"][0]["content"]
        )
        stream_ttft(port, entry)
        evict(port, fillers, -index - 1)
        stream_ttft(port, entry)

    for index in range(samples):
        entry = json.loads(json.dumps(payload))
        entry["messages"][0]["content"] = (
            f"{unique_prefix}-{index} " + entry["messages"][0]["content"]
        )

        # Cold store: attribute the write for this entry only (before any fillers).
        write_before = nvfs_counter("Writes")
        store = stream_ttft(port, entry)
        write_after = nvfs_counter("Writes")
        wrote = (
            write_after - write_before
            if min(write_before, write_after) >= 0
            else None
        )
        store_ttft.append(store["ttft_s"])

        # Try to evict this entry, then execute one reuse regardless of whether
        # the write counter observed the admission attempt.
        evict(port, fillers, index)
        read_before = nvfs_counter("Reads")
        retrieve = stream_ttft(port, entry)
        read_after = nvfs_counter("Reads")
        read = read_after - read_before if min(read_before, read_after) >= 0 else None
        retrieve_ttft.append(retrieve["ttft_s"])

        write_seen = wrote is not None and wrote >= expected * 0.95
        read_seen = read is not None and read >= expected * 0.95
        if wrote is None or read is None:
            outcome = "unattributed"
        elif write_seen and read_seen:
            outcome = "target_tier"
        elif write_seen:
            outcome = "fallback"
        elif not read_seen:
            outcome = "store_unattributed"
        else:
            outcome = "unattributed"
        outcomes[outcome] += 1
        attempts.append(
            {
                "index": index,
                "store_ttft_s": store["ttft_s"],
                "reuse_ttft_s": retrieve["ttft_s"],
                "store_write_mib": wrote,
                "reuse_read_mib": read,
                "outcome": outcome,
            }
        )
    return {
        "store_ttft_s": statistics.median(store_ttft) if store_ttft else None,
        "retrieve_ttft_s": statistics.median(retrieve_ttft) if retrieve_ttft else None,
        "attempts": attempts,
        "attempt_counts": {"attempted": len(attempts), **outcomes},
        # Legacy summary fields remain in the raw run for older readers. They
        # no longer control which timings enter the calculation.
        "stored": outcomes["target_tier"] + outcomes["fallback"],
        "retrieved": outcomes["target_tier"],
        "dropped_store": outcomes["store_unattributed"] + outcomes["unattributed"],
        "dropped_retrieve": (
            outcomes["fallback"]
            + outcomes["store_unattributed"]
            + outcomes["unattributed"]
        ),
    }


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments.

    Returns:
        Parsed arguments.
    """
    parser = argparse.ArgumentParser(description="Gate 3 — admission cost / break-even reuse count.")
    parser.add_argument("--gds-port", type=int, default=8102)
    parser.add_argument("--recompute-port", type=int, default=8100)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--contexts", type=int, nargs="+", default=[2048, 4096])
    parser.add_argument("--samples", type=int, default=10)
    parser.add_argument("--warmup", type=int, default=2)
    parser.add_argument("--fillers", type=int, default=6)
    parser.add_argument("--filler-tokens", type=int, default=8192)
    return parser.parse_args()


def main() -> None:
    """Measure admission cost at each context and write JSON evidence."""
    args = parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    print(f"building {args.fillers} fillers of ~{args.filler_tokens} tokens", flush=True)
    fillers = [build_payload(args.gds_port, args.filler_tokens, f"FILLER-{RUN_NONCE}-{i}")[0] for i in range(args.fillers)]

    rows: list[dict[str, Any]] = []
    for context in args.contexts:
        payload, actual = build_payload(args.gds_port, context, f"ADMIT-{RUN_NONCE}-{context}")
        cacheable = actual // CHUNK_TOKENS * CHUNK_TOKENS
        expected = expected_mib(cacheable)

        recompute_ttft = measure_recompute(args.recompute_port, payload, args.samples, args.warmup)
        tier = measure_store_and_retrieve(
            args.gds_port,
            payload,
            f"ADMIT-{RUN_NONCE}-{context}",
            expected,
            args.samples,
            fillers,
            warmup=args.warmup,
        )

        store = tier["store_ttft_s"]
        retrieve = tier["retrieve_ttft_s"]
        attempts = tier["attempts"]
        for attempt in attempts:
            admission_delta = attempt["store_ttft_s"] - recompute_ttft
            attempt["admission_delta_s"] = admission_delta
            attempt["admission_cost_s"] = max(0.0, admission_delta)
            attempt["raw_reuse_delta_s"] = (
                recompute_ttft - attempt["reuse_ttft_s"]
            )
            attempt["realized_reuse_saving_s"] = attributable_reuse_saving(
                recompute_ttft,
                attempt["reuse_ttft_s"],
                attempt["outcome"],
            )
        g3_admission_cost = (
            statistics.mean(attempt["admission_cost_s"] for attempt in attempts)
            if attempts
            else None
        )
        realized_reuse_saving = (
            statistics.mean(
                attempt["realized_reuse_saving_s"] for attempt in attempts
            )
            if attempts
            else None
        )

        if tier["attempt_counts"]["unattributed"] > 0:
            breakeven = None
            verdict = (
                "INDETERMINATE: one or more attempts could not be classified; "
                "no repayment claim"
            )
        elif (
            g3_admission_cost is not None
            and realized_reuse_saving is not None
            and realized_reuse_saving > 0
        ):
            breakeven = g3_admission_cost / realized_reuse_saving
            verdict = (
                f"reuse >= {breakeven:.1f} times "
                f"({math.ceil(breakeven)} whole attempts) to repay admission"
            )
        elif realized_reuse_saving is not None and realized_reuse_saving <= 0:
            breakeven = None
            verdict = "realized reuse does not beat recompute; admission never pays"
        else:
            breakeven = None
            verdict = "insufficient attributed samples; not claimed"

        row = {
            "context_target": context,
            "prompt_tokens": actual,
            "cacheable_tokens": cacheable,
            "kv_expected_mib": expected,
            "recompute_ttft_s": recompute_ttft,
            "store_ttft_s": store,
            "retrieve_ttft_s": retrieve,
            "g3_admission_cost_s": g3_admission_cost,
            "realized_reuse_saving_s": realized_reuse_saving,
            "breakeven_reuse_count": breakeven,
            "break_even_reuse_attempt": (
                math.ceil(breakeven) if breakeven is not None else None
            ),
            "attempts": attempts,
            "attempt_counts": tier["attempt_counts"],
            "stored": tier["stored"],
            "retrieved": tier["retrieved"],
            "dropped_store": tier["dropped_store"],
            "dropped_retrieve": tier["dropped_retrieve"],
            "verdict": verdict,
        }
        rows.append(row)
        print(
            f"ctx={cacheable} recompute={recompute_ttft:.4f}s "
            f"store={'-' if store is None else f'{store:.4f}s'} "
            f"retrieve={'-' if retrieve is None else f'{retrieve:.4f}s'} "
            "admission="
            f"{'-' if g3_admission_cost is None else f'{g3_admission_cost*1000:.0f}ms'} "
            f"outcomes={tier['attempt_counts']} -> {verdict}",
            flush=True,
        )

    identity = gpu_identity()
    envelope = identity | {
        "gate": "g3_admission_cost",
        "arguments": vars(args) | {"output": str(args.output)},
        "timing": run_timing(),
        "rows": rows,
    }
    args.output.write_text(json.dumps(envelope, indent=2))
    print(f"\nevidence: {args.output}")
    report_device_identity(identity)


if __name__ == "__main__":
    main()
