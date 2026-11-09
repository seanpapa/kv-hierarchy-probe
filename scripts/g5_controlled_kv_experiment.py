# SPDX-License-Identifier: Apache-2.0
"""Measure recompute, POSIX, and native-GDS KV-cache recall.

The server process is started by ``g5_controlled_kv_experiment.sh``. This client
keeps payload construction, cache eviction, tier validation, telemetry, and
latency measurement identical across arms. It supports a sequential context
sweep and a distinct-prefix concurrency sweep.
"""

# Standard
import argparse
import concurrent.futures
import hashlib
import json
import math
import os
import platform
import statistics
import subprocess
import tempfile
import threading
import time
import urllib.request
from contextlib import ExitStack, nullcontext
from pathlib import Path
from typing import Any

# Wall-clock cost of a run is planning information for a shared host, so every
# run records its own. time.monotonic() measures the elapsed span; the stamps
# are UTC so bundles from different sessions line up.
# Where the POSIX arm's files live *inside the container this exec targets*.
# A reference discrete-GPU host mounts the tier at /gds. A host that mounts it
# elsewhere — a unified-memory node reading a remote block export, for example
# — sets this instead of editing a copy of the script.
POSIX_TIER_DIR = os.environ.get("PROBE_POSIX_TIER_DIR", "/gds/lmcache-controlled-posix")

#: Host path of the tier filesystem, used to resolve which block device the
#: cgroup counter must be read for. ``POSIX_TIER_DIR`` is a container path and
#: cannot answer that: this script runs on the host.
TIER_MOUNT = os.environ.get("PROBE_TIER_MOUNT", "")

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
# Portable English transport payload. Keep it byte-for-byte stable within this
# workload version: changing it changes tokenization and therefore what is measured.
SENTENCE = (
    "LMCache reuses previously computed key-value cache blocks when the same long "
    "context appears again. This deterministic sentence exists only to control "
    "the token length of this storage experiment. "
)
TAIL = "\n\nDo not summarize the context. Reply with exactly 'STAGE1_D6_OK'."
TRANSPORT_PROFILE_VERSION = "english-v1"
CHUNK_TOKENS = 256
# KV bytes per 256-token chunk, set by the model's KV geometry
# (2 * layers * kv_heads * head_dim * dtype_bytes * CHUNK_TOKENS). Qwen2.5-32B
# = 64 MiB (256 KiB/token); Llama-3.1-8B = 32 MiB (128 KiB/token). This drives
# the tier_ok gate (nvfs_delta >= expected * 0.95), so a wrong value fails every
# row. Override per model with PROBE_MIB_PER_CHUNK.
MIB_PER_CHUNK = int(os.environ.get("PROBE_MIB_PER_CHUNK", "64"))

PROMPT_PROFILES = ("transport", "retrieval")
NEEDLE_POSITIONS = {"early": 0.10, "middle": 0.50, "late": 0.90}
RETRIEVAL_HEADER = (
    "The following synthetic experiment log is used to verify long-context "
    "retrieval. Each record is independent. Read the requested record exactly.\n\n"
)


def _digest(seed: int, unique: str, index: int) -> str:
    """Return stable hexadecimal material for one synthetic record."""
    return hashlib.sha256(f"{seed}:{unique}:{index}".encode()).hexdigest()


def _verification_code(seed: int, unique: str, index: int) -> str:
    """Return a compact numeric answer that fits the seven-token output cap."""
    return f"{int(_digest(seed, unique, index)[:12], 16) % 1_000_000:06d}"


def _retrieval_record(seed: int, unique: str, index: int) -> str:
    """Build one deterministic, English synthetic experiment record."""
    material = _digest(seed, unique, index)
    workloads = ("prefill", "decode", "kv-transfer", "cache-admission")
    owners = ("Avery", "Blake", "Casey", "Devon", "Emery", "Frankie")
    return (
        f"[Record R{index:06d}]\n"
        f"Node: gpu-{int(material[0:2], 16) % 64:02d}\n"
        f"Workload: {workloads[int(material[2:4], 16) % len(workloads)]}\n"
        f"Owner: {owners[int(material[4:6], 16) % len(owners)]}\n"
        f"Batch: B{int(material[6:10], 16) % 10_000:04d}\n"
        f"Temperature: {45 + int(material[10:12], 16) % 31}."
        f"{int(material[12:14], 16) % 10} C\n"
        f"Verification code: {_verification_code(seed, unique, index)}\n\n"
    )


def prompt_text(
    profile: str,
    units: int,
    unique: str,
    seed: int,
    needle_position: str,
) -> tuple[str, str, dict[str, Any]]:
    """Build deterministic text and its expected answer for one prompt profile."""
    if profile == "transport":
        text = f"{unique} " + SENTENCE * units + TAIL
        return text, "STAGE1_D6_OK", {
            "profile": profile,
            "version": TRANSPORT_PROFILE_VERSION,
        }
    if profile != "retrieval":
        raise ValueError(f"unknown prompt profile: {profile}")
    if needle_position not in NEEDLE_POSITIONS:
        raise ValueError(f"unknown needle position: {needle_position}")

    needle_index = min(
        units - 1,
        max(0, round((units - 1) * NEEDLE_POSITIONS[needle_position])),
    )
    records = "".join(_retrieval_record(seed, unique, index) for index in range(units))
    expected = _verification_code(seed, unique, needle_index)
    tail = (
        f"Question: What is the verification code for Record R{needle_index:06d}?\n"
        "Reply with only the six-digit verification code."
    )
    text = f"Run key: {unique}\n{RETRIEVAL_HEADER}{records}{tail}"
    return text, expected, {
        "profile": profile,
        "seed": seed,
        "needle_position": needle_position,
        "needle_record": f"R{needle_index:06d}",
    }



def percentile(values: list[float], quantile: float) -> float:
    """Return a linearly interpolated sample percentile.

    Args:
        values: Numeric observations.
        quantile: Quantile in the closed interval [0, 1].

    Returns:
        The interpolated percentile.

    Raises:
        ValueError: If values is empty or quantile is outside [0, 1].
    """
    if not values or not 0 <= quantile <= 1:
        raise ValueError("values must be non-empty and quantile must be in [0, 1]")
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def post_json(port: int, route: str, body: dict[str, Any]) -> dict[str, Any]:
    """POST JSON to a local vLLM endpoint and decode the response.

    Args:
        port: Local HTTP port.
        route: Endpoint path beginning with a slash.
        body: JSON request body.

    Returns:
        The decoded JSON object.

    Raises:
        urllib.error.URLError: If the request fails.
        json.JSONDecodeError: If the response is not JSON.
    """
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{route}",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=900) as response:
        return json.loads(response.read())


def stream_completion(port: int, body: dict[str, Any]) -> dict[str, Any]:
    """Run one streamed completion and measure TTFT and total latency.

    Args:
        port: Local vLLM HTTP port.
        body: Chat-completion request without the ``stream`` field.

    Returns:
        A dictionary containing TTFT, total latency, and output text.

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
    return {
        "ttft_s": first,
        "total_s": time.perf_counter() - started,
        "output": "".join(output),
    }


def token_count(port: int, text: str) -> int:
    """Return the exact tokenizer count reported by vLLM.

    Args:
        port: Local vLLM HTTP port.
        text: Text to tokenize.

    Returns:
        The exact token count.
    """
    return int(post_json(port, "/tokenize", {"model": MODEL, "prompt": text})["count"])


def build_prompt_case(
    port: int,
    target: int,
    unique: str = "",
    profile: str = "transport",
    seed: int = 17,
    needle_position: str = "early",
) -> tuple[dict[str, Any], int, str, dict[str, Any]]:
    """Build a prompt case whose cacheable prefix is aligned to ``target`` tokens.

    Args:
        port: Local vLLM HTTP port used for tokenization.
        target: Desired cacheable token count, divisible by 256.
        unique: Per-payload prefix used to prevent cross-request cache hits.
        profile: ``transport`` for the portable English movement workload or
            ``retrieval`` for deterministic English records with a queried needle.
        seed: Stable synthetic-data seed for the retrieval profile.
        needle_position: Requested record location: early, middle, or late.

    Returns:
        Request body, actual prompt-token count, expected output, and metadata.

    Raises:
        ValueError: If target is not a positive multiple of 256.
        RuntimeError: If a suitable payload cannot be built.
    """
    if target <= 0 or target % CHUNK_TOKENS:
        raise ValueError("target must be a positive multiple of 256")
    low, high = 1, 8000
    best: tuple[int, str, str, dict[str, Any]] | None = None
    while low <= high:
        middle = (low + high) // 2
        text, expected, metadata = prompt_text(
            profile, middle, unique, seed, needle_position
        )
        count = token_count(port, text)
        if count < target:
            low = middle + 1
        else:
            best = (count, text, expected, metadata)
            high = middle - 1
    if best is None:
        raise RuntimeError(f"could not construct a {target}-token payload")
    count, text, expected, metadata = best
    cacheable = count // CHUNK_TOKENS * CHUNK_TOKENS
    if cacheable != target:
        raise RuntimeError(
            f"prompt profile {profile} jumped from below {target} to "
            f"{cacheable} cacheable tokens; reduce its sizing unit"
        )
    metadata = metadata | {
        "expected_output": expected,
        "prompt_tokens": count,
        "cacheable_tokens": cacheable,
        "prompt_sha256": hashlib.sha256(text.encode()).hexdigest(),
    }
    return (
        {
            "model": MODEL,
            "messages": [{"role": "user", "content": text}],
            "max_tokens": 7,
            "temperature": 0,
        },
        count,
        expected,
        metadata,
    )


def build_payload(
    port: int,
    target: int,
    unique: str = "",
    profile: str = "transport",
    seed: int = 17,
    needle_position: str = "early",
) -> tuple[dict[str, Any], int]:
    """Build a payload while preserving the legacy two-value return shape."""
    body, count, _, _ = build_prompt_case(
        port, target, unique, profile, seed, needle_position
    )
    return body, count


def run_text(command: list[str]) -> str:
    """Run a read-only command and return standard output.

    Args:
        command: Command and arguments.

    Returns:
        Standard output with surrounding whitespace removed.
    """
    return subprocess.run(command, check=False, capture_output=True, text=True).stdout.strip()


def docker_log_lines(container: str) -> int:
    """Return the current number of log lines for a container.

    Args:
        container: Docker container name.

    Returns:
        Current log-line count, or zero for the recompute arm.
    """
    if not container:
        return 0
    return len(docker_logs(container))


def docker_logs(container: str) -> list[str]:
    """Return combined stdout/stderr logs for a Docker container.

    Args:
        container: Docker container name.

    Returns:
        Container log lines in emission order.
    """
    process = subprocess.run(
        ["docker", "logs", container],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    return process.stdout.splitlines()


def retrieve_line(container: str, start_line: int) -> str:
    """Return the last LMCache retrieve line emitted after a marker.

    Args:
        container: Docker container name.
        start_line: Zero-based log-line marker.

    Returns:
        The last matching log line, or an empty string.
    """
    if not container:
        return ""
    lines = docker_logs(container)[start_line:]
    return next((line for line in reversed(lines) if "retrieved" in line.lower()), "")


def nvfs_read_mib() -> int:
    """Return the host native-GDS cumulative read counter in MiB.

    Returns:
        The cumulative ``readMiB`` value, or -1 if unavailable.
    """
    try:
        for line in Path("/proc/driver/nvidia-fs/stats").read_text().splitlines():
            if line.startswith("Reads"):
                for token in line.split():
                    if token.startswith("readMiB="):
                        return int(token.split("=", 1)[1])
    except OSError:
        pass
    return -1


def tier_io_device(path: str) -> tuple[str, str] | None:
    """Return the ``io.stat`` key for the device behind a path, and its scope.

    ``io.stat`` accounts I/O against whole disks. A path on a partition carries
    the partition's ``st_dev``, which appears in no ``io.stat`` line, so matching
    on it alone would find nothing and read zero bytes — a silent phantom-tier
    reading, which is the failure this counter exists to detect.

    Args:
        path: Host path on the filesystem backing the tier.

    Returns:
        ``(key, scope)`` where ``key`` is ``"<major>:<minor>"`` as ``io.stat``
        writes it. ``scope`` is ``whole_disk`` when the path's own device is the
        disk, or ``widened_to_parent_disk`` when it is a partition and the
        counter therefore also covers that disk's other partitions. None when
        the device cannot be resolved; the caller must not read a total then.
    """
    try:
        st = os.stat(path)
    except OSError:
        return None
    major, minor = os.major(st.st_dev), os.minor(st.st_dev)
    block = Path(f"/sys/dev/block/{major}:{minor}")
    if not block.exists():
        return None
    if not (block / "partition").exists():
        return f"{major}:{minor}", "whole_disk"
    try:
        parent = (block.resolve().parent / "dev").read_text().strip()
    except OSError:
        return None
    return parent, "widened_to_parent_disk"


#: Resolved once per run: the ``io.stat`` key the cgroup counter is read for,
#: and how wide that key is. An empty device means the counter is unavailable,
#: which withholds the observation rather than reporting zero bytes.
_TIER_IO = tier_io_device(TIER_MOUNT) if TIER_MOUNT else None
TIER_IO_DEVICE = _TIER_IO[0] if _TIER_IO else ""
TIER_IO_SCOPE = _TIER_IO[1] if _TIER_IO else "unresolved"


def cgroup_read_bytes(container: str, device: str) -> int:
    """Return cgroup block-I/O read bytes for one device, for a container.

    Summing every device would count the OS disk and any other volume the
    container touched as tier traffic, which is why this selects a single
    ``io.stat`` line.

    Two absences are different and must not collapse into one reading.
    ``io.stat`` lists a device only after the cgroup has moved bytes on it — a
    container that has not touched the tier yet has an empty file — so a
    readable file without the device is a measured **zero**. An unreadable
    cgroup, or an unresolved device, is **unknown** and returns -1, which the
    callers treat as no measurement. The arm has to be able to report zero:
    zero device bytes is the phantom-tier signature this counter exists to
    expose, and a counter that can never read zero can never expose it.

    Args:
        container: Docker container name.
        device: ``io.stat`` key from :func:`tier_io_device`.

    Returns:
        ``rbytes`` for that device, 0 when the cgroup has moved none, or -1 when
        the reading is unavailable.
    """
    if not container or not device:
        return -1
    pid = run_text(["docker", "inspect", "--format", "{{.State.Pid}}", container])
    if not pid.isdigit():
        return -1
    try:
        relative = Path(f"/proc/{pid}/cgroup").read_text().splitlines()[0].split("::", 1)[1]
        lines = (Path("/sys/fs/cgroup") / relative.lstrip("/") / "io.stat").read_text().splitlines()
    except (OSError, IndexError):
        return -1
    for line in lines:
        fields = line.split()
        if not fields or fields[0] != device:
            continue
        for token in fields[1:]:
            if token.startswith("rbytes="):
                return int(token.split("=", 1)[1])
        return 0
    return 0


def gpu_telemetry() -> dict[str, Any]:
    """Capture the measured GPU's temperature, clocks, power, and memory usage.

    Returns:
        A dictionary of telemetry values as reported by ``nvidia-smi``.
    """
    fields = [
        "temperature.gpu",
        "clocks.sm",
        "clocks.mem",
        "power.draw",
        "memory.used",
    ]
    output = run_text(
        [
            "nvidia-smi",
            "-i",
            str(GPU_INDEX),
            f"--query-gpu={','.join(fields)}",
            "--format=csv,noheader,nounits",
        ]
    )
    values = [item.strip() for item in output.split(",")]
    return dict(zip(fields, values, strict=False))


def evict_posix_file_pages(attribution: str, container: str) -> None:
    """Evict only the POSIX arm's experiment files from Linux page cache.

    Args:
        attribution: Counter and cache-control class. Only ``posix`` performs
            file-page eviction.
        container: Container that has the POSIX tier mounted. The directory
            inside it comes from ``PROBE_POSIX_TIER_DIR`` (default
            ``/gds/lmcache-controlled-posix``).

    Raises:
        subprocess.CalledProcessError: If targeted cache eviction fails.

    Notes:
        This deliberately avoids the host-wide ``drop_caches`` control, which
        would disturb unrelated workloads on the shared server.
    """
    if attribution != "posix":
        return
    subprocess.run(["sync"], check=True)
    # The directory is passed as an argument rather than interpolated into the
    # program text, so a path with quotes or spaces cannot break or rewrite it.
    program = """
import os
import sys
from pathlib import Path
for path in Path(sys.argv[1]).rglob('*'):
    if not path.is_file():
        continue
    fd = os.open(path, os.O_RDONLY)
    try:
        os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
    finally:
        os.close(fd)
"""
    subprocess.run(
        ["docker", "exec", container, "python3", "-c", program, POSIX_TIER_DIR],
        check=True,
    )


def evict(
    port: int,
    attribution: str,
    container: str,
    fillers: list[dict[str, Any]],
    iteration: int,
    filler_key_policy: str,
) -> None:
    """Apply identical compute pressure and clear page cache before a sample.

    Args:
        port: Local vLLM HTTP port.
        attribution: Counter and cache-control class.
        container: Live Docker container name.
        fillers: Base filler payloads.
        iteration: Sample index used to make every filler unique.
        filler_key_policy: ``unique`` creates new filler keys per sample;
            ``reuse`` bounds the L2 working set by reusing the same keys.
    """
    for index, base in enumerate(fillers):
        payload = json.loads(json.dumps(base))
        if filler_key_policy == "unique":
            payload["messages"][0]["content"] = (
                f"EVICT-{iteration}-{index} " + payload["messages"][0]["content"]
            )
        stream_completion(port, payload)
    evict_posix_file_pages(attribution, container)


def expected_mib(cacheable_tokens: int) -> int:
    """Convert aligned Qwen2.5-32B cacheable tokens to KV MiB.

    Args:
        cacheable_tokens: Token count aligned to 256.

    Returns:
        Expected KV size in MiB.
    """
    return cacheable_tokens // CHUNK_TOKENS * MIB_PER_CHUNK


def measure_one(
    port: int,
    attribution: str,
    container: str,
    payload: dict[str, Any],
    expected: int,
    expected_output: str,
    *,
    phase_writer: Any = None,
    request_id: str = "",
) -> dict[str, Any]:
    """Measure one request with tier counters and correctness checks.

    Args:
        port: Local vLLM HTTP port.
        attribution: One of ``recompute``, ``posix``, or ``gds``.
        container: Docker container name, empty for recompute.
        payload: Chat completion request body.
        expected: Expected disk read in MiB for cache arms.
        expected_output: Canary or retrieved value required in the completion.
        phase_writer: Optional serial PhaseWriter for the HTTP-only interval.
        request_id: Unique interval label when exporting phases.

    Returns:
        Per-request latency, telemetry, counter deltas, and tier verdict.
    """
    marker = docker_log_lines(container)
    nvfs_before = nvfs_read_mib()
    cgroup_before = cgroup_read_bytes(container, TIER_IO_DEVICE)
    telemetry_before = gpu_telemetry()
    with (
        phase_writer.record(
            request_id, "target-retrieve", expected_read_bytes=expected * 1024**2
        )
        if phase_writer is not None
        else nullcontext()
    ):
        result = stream_completion(port, payload)
    telemetry_after = gpu_telemetry()
    nvfs_after = nvfs_read_mib()
    cgroup_after = cgroup_read_bytes(container, TIER_IO_DEVICE)
    line = retrieve_line(container, marker)
    retrieve_gib_s = None
    if "throughput:" in line:
        try:
            retrieve_gib_s = float(line.split("throughput:", 1)[1].split("GB/s", 1)[0])
        except ValueError:
            pass
    nvfs_delta = nvfs_after - nvfs_before if min(nvfs_before, nvfs_after) >= 0 else None
    cgroup_delta = (
        cgroup_after - cgroup_before if min(cgroup_before, cgroup_after) >= 0 else None
    )
    if attribution == "gds":
        tier_ok = nvfs_delta is not None and nvfs_delta >= expected * 0.95
    elif attribution == "posix":
        tier_ok = cgroup_delta is not None and cgroup_delta >= expected * 1024**2 * 0.90
    else:
        tier_ok = not line
    result.update(
        {
            "correct": expected_output in result.pop("output"),
            "retrieve_gib_s": retrieve_gib_s,
            "nvfs_read_mib": nvfs_delta,
            "cgroup_read_bytes": cgroup_delta,
            "tier_ok": tier_ok,
            "telemetry_before": telemetry_before,
            "telemetry_after": telemetry_after,
        }
    )
    return result


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize valid measurement rows.

    Args:
        rows: Per-request records including a ``phase`` key.

    Returns:
        Count, latency percentiles, throughput, and validation totals.
    """
    measured = [row for row in rows if row["phase"] == "measure"]
    ttft = [float(row["ttft_s"]) for row in measured]
    total = [float(row["total_s"]) for row in measured]
    return {
        "n": len(measured),
        "ttft_p50_s": percentile(ttft, 0.50),
        "ttft_p95_s": percentile(ttft, 0.95),
        "ttft_p99_s": percentile(ttft, 0.99),
        "total_p50_s": percentile(total, 0.50),
        "correct": sum(bool(row["correct"]) for row in measured),
        "tier_ok": sum(bool(row["tier_ok"]) for row in measured),
    }


def sequential_sweep(
    args: argparse.Namespace, phase_writers: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Run a context-length sweep on one live arm.

    Args:
        args: Parsed command-line arguments.
        phase_writers: Optional lifecycle/request/envelope exporters. No PASS inferred.

    Returns:
        Raw rows and summaries for every context.
    """
    fillers = [
        build_payload(
            args.port,
            args.filler_tokens,
            f"FILLER-{index}",
            args.prompt_profile,
            args.prompt_seed,
            args.needle_position,
        )[0]
        for index in range(args.fillers)
    ]
    output: dict[str, Any] = {}
    for context in args.contexts:
        payload, actual, expected_output, prompt_metadata = build_prompt_case(
            args.port,
            context,
            f"TARGET-{context}",
            args.prompt_profile,
            args.prompt_seed,
            args.needle_position,
        )
        cacheable = actual // CHUNK_TOKENS * CHUNK_TOKENS
        if args.attribution != "recompute":
            with (
                phase_writers["lifecycle"].record(f"ctx-{context}", "target-prime")
                if phase_writers else nullcontext()
            ):
                stream_completion(args.port, payload)
            with (
                phase_writers["lifecycle"].record(f"ctx-{context}", "store-settle")
                if phase_writers else nullcontext()
            ):
                time.sleep(2)
        rows: list[dict[str, Any]] = []
        for index in range(args.warmup + args.samples):
            sample_phase = "warmup" if index < args.warmup else "measure"
            request_id = f"ctx-{context}-{sample_phase}-{index}"
            with (
                phase_writers["lifecycle"].record(request_id, "eviction-pressure")
                if phase_writers else nullcontext()
            ):
                evict(
                    args.port, args.attribution, args.container, fillers,
                    context * 1000 + index, args.filler_key_policy,
                )
            with (
                phase_writers["envelope"].record(
                    request_id, "retrieve-envelope",
                    expected_read_bytes=expected_mib(cacheable) * 1024**2,
                )
                if phase_writers else nullcontext()
            ):
                optional = (
                    {"phase_writer": phase_writers["request"], "request_id": request_id}
                    if phase_writers else {}
                )
                row = measure_one(
                    args.port, args.attribution, args.container, payload,
                    expected_mib(cacheable), expected_output, **optional,
                )
            row.update(
                {
                    "index": index,
                    "phase": "warmup" if index < args.warmup else "measure",
                    "context_target": context,
                    "prompt_tokens": actual,
                    "cacheable_tokens": cacheable,
                }
            )
            rows.append(row)
            print(
                f"{args.arm} ctx={context} i={index} ttft={row['ttft_s']:.4f}s "
                f"tier={'OK' if row['tier_ok'] else 'FAIL'}",
                flush=True,
            )
        output[str(context)] = {
            "prompt": prompt_metadata,
            "summary": summarize(rows),
            "rows": rows,
        }
    return output


def concurrent_batch(
    port: int,
    attribution: str,
    container: str,
    payloads: list[dict[str, Any]],
    expected_outputs: list[str],
    prompt_metadata: list[dict[str, Any]],
    expected_total_mib: int,
) -> dict[str, Any]:
    """Run one synchronized batch of distinct-prefix requests.

    Args:
        port: Local vLLM HTTP port.
        attribution: Counter and cache-control class.
        container: Docker container name.
        payloads: Distinct-prefix request bodies.
        expected_outputs: Per-request canary or retrieved values.
        prompt_metadata: Per-request reproducibility metadata.
        expected_total_mib: Expected aggregate disk read.

    Returns:
        Per-request results, batch duration, and aggregate tier validation.
    """
    barrier = threading.Barrier(len(payloads))

    def worker(payload: dict[str, Any]) -> dict[str, Any]:
        barrier.wait()
        return stream_completion(port, payload)

    marker = docker_log_lines(container)
    nvfs_before = nvfs_read_mib()
    cgroup_before = cgroup_read_bytes(container, TIER_IO_DEVICE)
    telemetry_before = gpu_telemetry()
    started = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(payloads)) as executor:
        rows = list(executor.map(worker, payloads))
    duration = time.perf_counter() - started
    telemetry_after = gpu_telemetry()
    nvfs_after = nvfs_read_mib()
    cgroup_after = cgroup_read_bytes(container, TIER_IO_DEVICE)
    log_tail = docker_logs(container)[marker:] if container else []
    retrieved = sum("retrieved" in line.lower() for line in log_tail)
    nvfs_delta = nvfs_after - nvfs_before if min(nvfs_before, nvfs_after) >= 0 else None
    cgroup_delta = (
        cgroup_after - cgroup_before if min(cgroup_before, cgroup_after) >= 0 else None
    )
    if attribution == "gds":
        tier_ok = nvfs_delta is not None and nvfs_delta >= expected_total_mib * 0.95
    elif attribution == "posix":
        tier_ok = (
            cgroup_delta is not None
            and cgroup_delta >= expected_total_mib * 1024**2 * 0.90
        )
    else:
        tier_ok = retrieved == 0
    for row, expected_output, metadata in zip(
        rows, expected_outputs, prompt_metadata, strict=True
    ):
        row["correct"] = expected_output in row.pop("output")
        row["prompt"] = metadata
    return {
        "rows": rows,
        "batch_s": duration,
        "requests_per_s": len(rows) / duration,
        "cacheable_tokens_per_s": len(rows) * expected_total_mib / len(rows) * 4 / duration,
        "nvfs_read_mib": nvfs_delta,
        "cgroup_read_bytes": cgroup_delta,
        "retrieve_log_lines": retrieved,
        "tier_ok": tier_ok,
        "telemetry_before": telemetry_before,
        "telemetry_after": telemetry_after,
    }


def concurrency_sweep(args: argparse.Namespace) -> dict[str, Any]:
    """Run concurrency 1/2/4/8 with distinct cache keys.

    Args:
        args: Parsed command-line arguments.

    Returns:
        Raw batch records and aggregate percentiles per concurrency.
    """
    max_concurrency = max(args.concurrency)
    payloads: list[dict[str, Any]] = []
    expected_outputs: list[str] = []
    prompt_metadata: list[dict[str, Any]] = []
    actual = 0
    for index in range(max_concurrency):
        payload, actual, expected_output, metadata = build_prompt_case(
            args.port,
            args.concurrent_context,
            f"CONCURRENT-{index}",
            args.prompt_profile,
            args.prompt_seed,
            args.needle_position,
        )
        payloads.append(payload)
        expected_outputs.append(expected_output)
        prompt_metadata.append(metadata)
        if args.attribution != "recompute":
            stream_completion(args.port, payload)
            time.sleep(2)
    cacheable = actual // CHUNK_TOKENS * CHUNK_TOKENS
    fillers = [
        build_payload(
            args.port,
            args.filler_tokens,
            f"CFILLER-{index}",
            args.prompt_profile,
            args.prompt_seed,
            args.needle_position,
        )[0]
        for index in range(args.fillers)
    ]
    output: dict[str, Any] = {}
    for concurrency in args.concurrency:
        batches: list[dict[str, Any]] = []
        for batch_index in range(args.warmup + args.batches):
            evict(
                args.port,
                args.attribution,
                args.container,
                fillers,
                concurrency * 1000 + batch_index,
                args.filler_key_policy,
            )
            batch = concurrent_batch(
                args.port,
                args.attribution,
                args.container,
                payloads[:concurrency],
                expected_outputs[:concurrency],
                prompt_metadata[:concurrency],
                concurrency * expected_mib(cacheable),
            )
            batch["phase"] = "warmup" if batch_index < args.warmup else "measure"
            batch["batch_index"] = batch_index
            batches.append(batch)
            print(
                f"{args.arm} c={concurrency} batch={batch_index} "
                f"rps={batch['requests_per_s']:.2f} tier={'OK' if batch['tier_ok'] else 'FAIL'}",
                flush=True,
            )
        measured = [batch for batch in batches if batch["phase"] == "measure"]
        ttft = [float(row["ttft_s"]) for batch in measured for row in batch["rows"]]
        output[str(concurrency)] = {
            "summary": {
                "batches": len(measured),
                "requests": len(ttft),
                "ttft_p50_s": percentile(ttft, 0.50),
                "ttft_p95_s": percentile(ttft, 0.95),
                "ttft_p99_s": percentile(ttft, 0.99),
                "requests_per_s": statistics.fmean(
                    float(batch["requests_per_s"]) for batch in measured
                ),
                "cacheable_tokens_per_s": statistics.fmean(
                    float(batch["cacheable_tokens_per_s"]) for batch in measured
                ),
                "tier_ok_batches": sum(bool(batch["tier_ok"]) for batch in measured),
            },
            "batches": batches,
        }
    return output


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments.

    Returns:
        Parsed arguments.
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["context", "concurrency"])
    parser.add_argument(
        "--arm",
        required=True,
        help=(
            "Opaque experiment-arm identity. Legacy values recompute/posix/gds "
            "also select their matching attribution class when --attribution "
            "is omitted."
        ),
    )
    parser.add_argument(
        "--attribution",
        choices=["recompute", "posix", "gds"],
        help=(
            "Counter and cache-control class. Required when --arm is not one "
            "of the three legacy values."
        ),
    )
    parser.add_argument(
        "--backend",
        help=(
            "Reference backend identity recorded in the raw envelope, for "
            "example lmcache_nixl_dynamic_gds_mt."
        ),
    )
    parser.add_argument("--port", type=int, default=8104)
    parser.add_argument("--container", default="")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--phase-export-dir", type=Path,
        help="Experimental Linux serial CSV export to a new directory; not Gate 1 PASS.",
    )
    parser.add_argument("--contexts", type=int, nargs="+", default=[256, 512, 1024, 2048, 4096])
    parser.add_argument("--samples", type=int, default=10)
    parser.add_argument("--warmup", type=int, default=2)
    parser.add_argument("--fillers", type=int, default=6)
    parser.add_argument("--filler-tokens", type=int, default=8192)
    parser.add_argument(
        "--prompt-profile",
        choices=PROMPT_PROFILES,
        default="transport",
        help=(
            "transport uses the portable English movement workload; retrieval uses "
            "deterministic English experiment records and validates a queried value"
        ),
    )
    parser.add_argument(
        "--prompt-seed",
        type=int,
        default=17,
        help="deterministic synthetic-record seed for the retrieval profile",
    )
    parser.add_argument(
        "--needle-position",
        choices=tuple(NEEDLE_POSITIONS),
        default="early",
        help="location of the queried record in a retrieval prompt",
    )
    parser.add_argument(
        "--filler-key-policy",
        choices=["unique", "reuse"],
        default="unique",
        help=(
            "Use unique filler keys per sample (legacy default) or reuse a "
            "bounded filler set for backends without L2 eviction."
        ),
    )
    parser.add_argument("--concurrency", type=int, nargs="+", default=[1, 2, 4, 8])
    parser.add_argument("--concurrent-context", type=int, default=2048)
    parser.add_argument("--batches", type=int, default=10)
    args = parser.parse_args()
    if args.phase_export_dir is not None:
        if args.mode != "context":
            parser.error("--phase-export-dir requires serial context mode")
        if platform.system() != "Linux":
            parser.error("--phase-export-dir requires a Linux monotonic clock")
        if args.attribution == "recompute" or args.arm == "recompute":
            parser.error("phase export currently requires a cache arm")
    if args.attribution is None:
        if args.arm not in {"recompute", "posix", "gds"}:
            parser.error(
                "--attribution is required when --arm is not "
                "recompute, posix, or gds"
            )
        args.attribution = args.arm
    return args


def main() -> None:
    """Run the requested experiment and write checksummable JSON evidence."""
    args = parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with ExitStack() as stack:
        writers = None
        if args.phase_export_dir is not None:
            # First Party (opt-in: standalone legacy deployments need no package).
            from kvprobe.phases import PhaseWriter

            args.phase_export_dir.mkdir(exist_ok=False)
            writers = {
                view: stack.enter_context(PhaseWriter(args.phase_export_dir / f"{view}.csv"))
                for view in ("lifecycle", "request", "envelope")
            }
            metadata = {
                "phase_export_version": "0.1.0-experimental",
                "clock": "CLOCK_MONOTONIC",
                "producer_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "evidence_path": str(args.output.resolve()),
                "capture_coverage": "not_assessed",
                "serving_verification": "not_assessed",
                "expected_bytes_source": "review_required_Qwen_runtime_constants",
            }
            with (args.phase_export_dir / "producer.json").open("x") as handle:
                json.dump(metadata, handle, indent=2)
        if args.mode == "context":
            result = sequential_sweep(args, writers) if writers else sequential_sweep(args)
        else:
            result = concurrency_sweep(args)
    arguments = vars(args) | {"output": str(args.output)}
    if args.phase_export_dir is None:
        arguments.pop("phase_export_dir")
    else:
        arguments["phase_export_dir"] = str(args.phase_export_dir)
    if args.backend is None:
        arguments.pop("backend")
    envelope = {
        "mode": args.mode,
        "arm": args.arm,
        "attribution": args.attribution,
        "port": args.port,
        "container": args.container,
        "arguments": arguments,
        "timing": run_timing(),
        "result": result,
    }
    if args.backend is not None:
        envelope["backend"] = args.backend
    # Which device the cgroup counter was read for, so a reading of zero can be
    # told apart from a counter pointed at the wrong device. Without this, a
    # misconfigured tier mount would be indistinguishable from a phantom tier.
    # Every cache arm records it: the per-device cgroup counter also counts
    # GPU-direct (cuFile) reads, so a GDS or NIXL arm can be judged by it too.
    if args.attribution != "recompute":
        envelope["cgroup_io_device"] = TIER_IO_DEVICE
        envelope["cgroup_io_device_scope"] = TIER_IO_SCOPE
    identity = gpu_identity()
    envelope |= identity
    with tempfile.NamedTemporaryFile("w", dir=args.output.parent, delete=False) as handle:
        json.dump(envelope, handle, indent=2)
        temporary = Path(handle.name)
    os.replace(temporary, args.output)
    print(f"\nevidence: {args.output}")
    report_device_identity(identity)


if __name__ == "__main__":
    main()
