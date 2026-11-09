# SPDX-License-Identifier: Apache-2.0
"""Gate 4 (capacity justification) — is the NVMe tier ever the best choice?

A storage-class tier below DRAM is not justified by speed: while DRAM (LMCache's
L1) has room, DRAM serves every hit and the NVMe tier is never touched. The tier
earns its place only once the working set exceeds DRAM and entries spill onto it.

This client finds that crossover directly. It holds one entry size fixed and grows
the working set (the number of distinct cached entries), then retrieves every
entry once and classifies each retrieval by per-request ``nvidia-fs`` read bytes:

* ``dram`` (HIT_MEM)  — zero device read: still resident in L1.
* ``nvme`` (HIT_DISK) — device read ~= one entry: evicted from L1, served by NVMe.
* ``miss``            — neither: the entry is gone; not counted as a tier hit.

Every store and every retrieval is also kept as a record (entry index, observed
``nvidia-fs`` write and read MiB, TTFT, class), so a ``fallback`` can be traced to a
store that wrote nothing or to a load that failed after a write. The records are raw
evidence; the per-working-set counts are what ``kvprobe ingest`` reads.

The **capacity crossover** is the smallest working set at which any retrieval is
served from NVMe. Below it the NVMe tier adds only risk; at and above it the tier
carries the overflow and, per Gate 2, competes with recompute rather than DRAM.
The total resident size at the crossover approximates the usable L1 capacity.

The server is started by ``g4_capacity_sweep.sh`` with a known L1 size.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import time
from pathlib import Path
from typing import Any
import urllib.request


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
# Per-run nonce so every entry key is globally fresh; re-running otherwise collides with
# blocks left in the tier by an earlier run (LMCache tries to load a stale, unreadable
# block and the request fails).
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
    """Run one streamed completion and measure TTFT.

    Args:
        port: Local vLLM HTTP port.
        body: Chat-completion request without the ``stream`` field.

    Returns:
        A dictionary with ``ttft_s`` and ``output``.

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
    with urllib.request.urlopen(request, timeout=900) as response:
        for raw_line in response:
            line = raw_line.decode(errors="replace").strip()
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
        raise RuntimeError("completion returned no content token")
    return {"ttft_s": first, "output": "".join(output)}


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


def build_entry(port: int, target: int, unique: str) -> dict[str, Any]:
    """Build one distinct cache entry aligned to ``target`` tokens.

    Args:
        port: Local vLLM HTTP port used for tokenization.
        target: Cacheable token count, a positive multiple of 256.
        unique: Per-entry prefix that makes the entry distinct.

    Returns:
        A chat-completion request body.

    Raises:
        ValueError: If target is not a positive multiple of 256.
        RuntimeError: If a suitable entry cannot be built.
    """
    if target <= 0 or target % CHUNK_TOKENS:
        raise ValueError("target must be a positive multiple of 256")
    low, high = 1, 8000
    best: str | None = None
    while low <= high:
        middle = (low + high) // 2
        text = f"{unique} " + SENTENCE * middle + TAIL
        if token_count(port, text) < target:
            low = middle + 1
        else:
            best = text
            high = middle - 1
    if best is None:
        raise RuntimeError(f"could not construct a {target}-token entry")
    return {"model": MODEL, "messages": [{"role": "user", "content": best}], "max_tokens": 7, "temperature": 0}


def nvfs_mib(kind: str) -> int:
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


def nvfs_read_mib() -> int:
    """Return the cumulative native-GDS read counter in MiB.

    Returns:
        The cumulative ``readMiB`` value, or -1 if unavailable.
    """
    return nvfs_mib("Reads")


def nvfs_write_mib() -> int:
    """Return the cumulative native-GDS write counter in MiB.

    Returns:
        The cumulative ``writeMiB`` value, or -1 if unavailable.
    """
    return nvfs_mib("Writes")


def _delta(before: int, after: int) -> int | None:
    """Return a counter delta, or ``None`` when either reading was unavailable."""
    return after - before if min(before, after) >= 0 else None


def expected_mib(entry_tokens: int) -> int:
    """Convert aligned cacheable tokens to KV MiB.

    Args:
        entry_tokens: Token count aligned to 256.

    Returns:
        Expected KV size in MiB.
    """
    return entry_tokens // CHUNK_TOKENS * MIB_PER_CHUNK


def classify(read_delta: int | None, expected: int, ttft_s: float, recompute_ref_s: float) -> str:
    """Classify one retrieval by device read bytes and latency.

    A zero device read is ambiguous: it is either a genuine fast resident hit (served
    from L1/GPU memory) or a load that silently failed and fell back to recompute (which
    also reads nothing, but is slow). They are told apart by latency against a recompute
    reference — otherwise a silent fallback is miscounted as a DRAM hit.

    Args:
        read_delta: Native-GDS read MiB for the request, or None.
        expected: Expected per-entry read in MiB.
        ttft_s: Measured TTFT for this request.
        recompute_ref_s: Reference recompute TTFT at this entry size.

    Returns:
        ``nvme`` (device read), ``dram`` (fast resident hit), ``fallback`` (slow zero-read
        = silent recompute fallback), or ``miss``.
    """
    if read_delta is None:
        return "miss"
    if read_delta >= expected * 0.95:
        return "nvme"
    if read_delta == 0:
        if recompute_ref_s and ttft_s < 0.5 * recompute_ref_s:
            return "dram"
        return "fallback"
    return "miss"


def store_all(port: int, entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Store (or refresh) every entry in the working set, recording each store.

    Args:
        port: GDS arm port.
        entries: The distinct entries of the working set, in index order.

    Returns:
        One record per store: ``entry`` index, ``write_mib`` (host-wide
        ``nvidia-fs`` write delta during the request, ``None`` if unavailable)
        and ``ttft_s``. A refresh of an entry the tier already holds may
        legitimately write nothing.
    """
    records: list[dict[str, Any]] = []
    for index, entry in enumerate(entries):
        before = nvfs_write_mib()
        result = stream_ttft(port, entry)
        after = nvfs_write_mib()
        records.append(
            {"entry": index, "write_mib": _delta(before, after), "ttft_s": result["ttft_s"]}
        )
    return records


def retrieve_all(port: int, entries: list[dict[str, Any]], expected: int, recompute_ref_s: float) -> dict[str, Any]:
    """Retrieve every entry once and tally the serving tier.

    Args:
        port: GDS arm port.
        entries: The distinct entries currently stored, in index order.
        expected: Expected per-entry read in MiB.
        recompute_ref_s: Reference recompute TTFT at this entry size.

    Returns:
        Per-tier counts and TTFT medians (including a ``fallback`` bucket for silent
        recompute fallbacks), and ``retrievals``: one record per request with the
        ``entry`` index, ``read_mib`` and ``write_mib`` deltas (a recompute fallback
        may store the entry again), ``ttft_s`` and ``kind``.
    """
    ttft: dict[str, list[float]] = {"dram": [], "nvme": [], "fallback": []}
    counts = {"dram": 0, "nvme": 0, "fallback": 0, "miss": 0}
    records: list[dict[str, Any]] = []
    for index, entry in enumerate(entries):
        read_before, write_before = nvfs_read_mib(), nvfs_write_mib()
        result = stream_ttft(port, entry)
        read_after, write_after = nvfs_read_mib(), nvfs_write_mib()
        delta = _delta(read_before, read_after)
        kind = classify(delta, expected, result["ttft_s"], recompute_ref_s)
        counts[kind] += 1
        if kind in ttft:
            ttft[kind].append(result["ttft_s"])
        records.append(
            {
                "entry": index,
                "read_mib": delta,
                "write_mib": _delta(write_before, write_after),
                "ttft_s": result["ttft_s"],
                "kind": kind,
            }
        )
    return {
        "counts": counts,
        "ttft_dram_p50_s": statistics.median(ttft["dram"]) if ttft["dram"] else None,
        "ttft_nvme_p50_s": statistics.median(ttft["nvme"]) if ttft["nvme"] else None,
        "ttft_fallback_p50_s": statistics.median(ttft["fallback"]) if ttft["fallback"] else None,
        "retrievals": records,
    }


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments.

    Returns:
        Parsed arguments.
    """
    parser = argparse.ArgumentParser(description="Gate 4 — capacity justification / DRAM->NVMe crossover.")
    parser.add_argument("--port", type=int, default=8102)
    parser.add_argument("--recompute-port", type=int, default=8100, help="recompute arm, for the fast/slow read-0 reference")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--entry-tokens", type=int, default=4096, help="fixed cacheable tokens per entry (multiple of 256)")
    parser.add_argument(
        "--working-sets",
        type=int,
        nargs="+",
        default=[1, 2, 4, 8, 16, 24, 32],
        help="working-set sizes (entry counts) to sweep, ascending",
    )
    return parser.parse_args()


def main() -> None:
    """Sweep the working set, find the DRAM->NVMe crossover, write evidence."""
    args = parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    per_entry = expected_mib(args.entry_tokens)
    largest = max(args.working_sets)

    print(f"building {largest} distinct entries of {args.entry_tokens} tokens (~{per_entry} MiB each)", flush=True)
    entries = [build_entry(args.port, args.entry_tokens, f"CAP-{RUN_NONCE}-{index}") for index in range(largest)]

    # Recompute reference: distinguishes a fast resident zero-read (DRAM) from a slow
    # zero-read (silent recompute fallback / failed load).
    reference = build_entry(args.recompute_port, args.entry_tokens, f"REF-{RUN_NONCE}")
    recompute_ref = statistics.median([stream_ttft(args.recompute_port, reference)["ttft_s"] for _ in range(5)])
    print(f"recompute reference TTFT ~= {recompute_ref:.4f}s (zero-read faster than {0.5 * recompute_ref:.4f}s counts as DRAM)", flush=True)

    rows: list[dict[str, Any]] = []
    crossover: int | None = None
    any_fallback = False
    for working_set in sorted(args.working_sets):
        active = entries[:working_set]
        # Store (or refresh) every entry in the working set, then read them back.
        stores = store_all(args.port, active)
        tier = retrieve_all(args.port, active, per_entry, recompute_ref)
        counts = tier["counts"]
        if crossover is None and counts["nvme"] > 0:
            crossover = working_set
        if counts["fallback"] > 0:
            any_fallback = True
        row = {
            "working_set": working_set,
            "resident_mib": working_set * per_entry,
            "dram_hits": counts["dram"],
            "nvme_hits": counts["nvme"],
            "fallback": counts["fallback"],
            "miss": counts["miss"],
            "ttft_dram_p50_s": tier["ttft_dram_p50_s"],
            "ttft_nvme_p50_s": tier["ttft_nvme_p50_s"],
            "ttft_fallback_p50_s": tier["ttft_fallback_p50_s"],
            "stores": stores,
            "retrievals": tier["retrievals"],
        }
        rows.append(row)
        print(
            f"working_set={working_set:>3} resident~{row['resident_mib']} MiB "
            f"dram={counts['dram']} nvme={counts['nvme']} fallback={counts['fallback']} miss={counts['miss']}",
            flush=True,
        )
        print(
            "  stores write_mib="
            + str([record["write_mib"] for record in stores])
            + " retrievals "
            + " ".join(
                f"{record['entry']}:{record['kind']}(r{record['read_mib']}/w{record['write_mib']})"
                for record in tier["retrievals"]
            ),
            flush=True,
        )

    if crossover is None:
        verdict = "no NVMe hits at any working set measured: DRAM never overflowed, so the NVMe tier was never the best choice here"
    else:
        resident = crossover * per_entry
        verdict = (
            f"DRAM->NVMe crossover at working_set={crossover} (~{resident} MiB resident): "
            f"below it DRAM serves and NVMe is redundant; at and above it NVMe carries the overflow"
        )
    if any_fallback:
        verdict += (
            ". WARNING: silent recompute fallbacks were observed (zero device read but recompute-slow "
            "TTFT) — the tier failed to serve some retrievals; treat the crossover as contaminated and "
            "investigate the load failures before claiming a clean capacity result"
        )
    print(f"\n{verdict}")

    identity = gpu_identity()
    envelope = identity | {
        "gate": "capacity_justification",
        "arguments": vars(args) | {"output": str(args.output)},
        "per_entry_mib": per_entry,
        "recompute_ref_s": recompute_ref,
        "crossover_working_set": crossover,
        "any_fallback": any_fallback,
        "rows": rows,
        "verdict": verdict,
        "timing": run_timing(),
    }
    args.output.write_text(json.dumps(envelope, indent=2))
    print(f"\nevidence: {args.output}")
    report_device_identity(identity)


if __name__ == "__main__":
    main()
