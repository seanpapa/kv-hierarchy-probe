# SPDX-License-Identifier: Apache-2.0
"""Offline namespace observations, separate from serving evidence verdicts."""

# Standard
import csv
import hashlib
import json
import re
from collections.abc import Iterator
from itertools import zip_longest
from pathlib import Path
from typing import Any

# Local
from .nvme_capture import assess_capture_loss, summarize_capture

MAX_PHASES = 256
MAX_PENDING = 65536
MAX_LINE_BYTES = 4096
MAX_COMMAND_HISTORY = 65536


class TraceError(ValueError):
    """Raised when a trace input cannot be interpreted safely."""


def _integer(value: Any, name: str, minimum: int = 0) -> int:
    """Validate an integer without silently accepting booleans or fractions."""
    if isinstance(value, bool):
        raise TraceError(f"{name} must be an integer >= {minimum}")
    if isinstance(value, str) and re.fullmatch(r"[0-9]+", value):
        value = int(value)
    if not isinstance(value, int) or value < minimum:
        raise TraceError(f"{name} must be an integer >= {minimum}")
    return value


def _lines(path: Path) -> Iterator[str]:
    """Yield bounded input lines, rejecting truncation-prone giant records."""
    with path.open(encoding="utf-8") as stream:
        while True:
            line = stream.readline(MAX_LINE_BYTES + 1)
            if not line:
                return
            if len(line.encode("utf-8")) > MAX_LINE_BYTES:
                raise TraceError(f"{path.name}: record exceeds {MAX_LINE_BYTES} bytes")
            yield line


def _digest(path: Path) -> str:
    """Hash an input without loading it into memory."""
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            result.update(chunk)
    return result.hexdigest()


def _manifest(path: Path) -> dict:
    """Validate the normalized observation contract, not hardware readiness."""
    if path.stat().st_size > 65536:
        raise TraceError("manifest.json exceeds 65536 bytes")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise TraceError("manifest.json must be an object")
    if data.get("trace_contract_version") != "0.2.0":
        raise TraceError(
            "unsupported trace_contract_version; expected CPU-bearing 0.2.0 "
            "from kvprobe gate1 capture"
        )
    if data.get("clock") != "CLOCK_MONOTONIC":
        raise TraceError("clock must be CLOCK_MONOTONIC; clocks are not converted")
    if not isinstance(data.get("namespace"), str) or not re.fullmatch(
        r"nvme[0-9]+n[0-9]+", data["namespace"]
    ):
        raise TraceError("namespace must be a whole NVMe namespace, not a partition")
    for name in ("controller_id", "capture_start_ns", "capture_end_ns"):
        data[name] = _integer(data.get(name), name)
    data["logical_block_size"] = _integer(
        data.get("logical_block_size"), "logical_block_size", 1
    )
    if data["capture_end_ns"] <= data["capture_start_ns"]:
        raise TraceError("capture_end_ns must exceed capture_start_ns")
    if not isinstance(data.get("capture_ready"), bool):
        raise TraceError("capture_ready must be a boolean")
    for name in ("capture_exit_code", "loss_events"):
        if data.get(name) is not None:
            data[name] = _integer(data[name], name)
    return data


def _phases(path: Path, manifest: dict) -> list[dict]:
    """Read bounded half-open phase intervals with optional expected read bytes."""
    reader = csv.DictReader(_lines(path))
    required = {"request_id", "phase", "start_ns", "end_ns"}
    if reader.fieldnames is None or not required <= set(reader.fieldnames):
        raise TraceError("phases.csv requires request_id,phase,start_ns,end_ns")
    phases = []
    for row in reader:
        if len(phases) >= MAX_PHASES:
            raise TraceError(f"phases.csv exceeds {MAX_PHASES} intervals")
        if None in row or any(value is None for value in row.values()):
            raise TraceError("phases.csv: malformed row")
        if not row["request_id"] or not row["phase"]:
            raise TraceError("request_id and phase must be nonempty")
        start = _integer(row["start_ns"], "phase start_ns")
        end = _integer(row["end_ns"], "phase end_ns")
        if (
            not manifest["capture_start_ns"]
            <= start
            < end
            <= manifest["capture_end_ns"]
        ):
            raise TraceError("phase must be inside the recorded capture interval")
        expected = row.get("expected_read_bytes", "")
        phases.append(
            {
                "request_id": row["request_id"],
                "phase": row["phase"],
                "start_ns": start,
                "end_ns": end,
                "expected_read_bytes": _integer(expected, "expected_read_bytes")
                if expected
                else None,
                **_counts(),
            }
        )
    if not phases:
        raise TraceError("phases.csv must contain at least one interval")
    return phases


def _counts() -> dict:
    """Create distinct submission and successful completion counters."""
    return {
        "submitted_read_bytes": 0,
        "submitted_write_bytes": 0,
        "successful_completed_read_bytes": 0,
        "successful_completed_write_bytes": 0,
        "submitted_commands": 0,
        "paired_commands": 0,
        "failed_commands": 0,
        "boundary_crossing_commands": 0,
    }


def _causal_capture_reasons(run_dir: Path, manifest: dict) -> set[str]:
    """Cross-check retained raw records and scoped loss for opt-in analysis."""
    raw = run_dir / "bpftrace.out"
    stderr = run_dir / "bpftrace.err"
    if not raw.exists() or not stderr.exists():
        return {"causal_raw_evidence_missing"}
    reasons: set[str] = set()
    if _digest(raw) != manifest.get("raw_stdout_sha256"):
        reasons.add("causal_raw_digest_mismatch")
    summary = summarize_capture(raw)
    loss = assess_capture_loss(
        summary,
        stderr,
        process_returncode=manifest.get("process_returncode"),
        stop_reason=manifest.get("stop_reason"),
    )
    if loss["loss_events"] != 0 or loss != manifest.get("loss_accounting"):
        reasons.add("causal_scoped_loss_unverified")
    if summary["malformed_records"] or summary["cpu_missing_records"]:
        reasons.add("causal_raw_records_invalid")
    if (
        summary["ready_ns"] != manifest["capture_start_ns"]
        or summary["end_ns"] != manifest["capture_end_ns"]
        or summary["controller_ids"] != [manifest["controller_id"]]
    ):
        reasons.add("causal_capture_identity_mismatch")
    raw_records = (
        line.strip() for line in _lines(raw) if line.startswith(("S,", "C,"))
    )
    trace_records = (
        line.strip()
        for line in _lines(run_dir / "trace.csv")
        if line.strip() and not line.startswith("#")
    )
    if any(a != b for a, b in zip_longest(raw_records, trace_records)):
        reasons.add("causal_raw_trace_mismatch")
    return reasons


def analyze_trace(run_dir: Path, *, ordering_policy: str = "strict") -> dict:
    """Analyze normalized NVMe events without granting a serving tier PASS.

    Args:
        run_dir: Directory containing manifest.json, phases.csv and trace.csv.
            bpftrace.err is optional but its absence makes quality incomplete.
        ordering_policy: "strict" (default) requires global monotonic timestamps.
            "causal" explicitly opts into CPU-bearing input and raw/loss checks,
            per-CPU order and command-ID history validation, without sorting.

    Returns:
        Versioned observations and quality reasons. Submitted bytes include
        failed/unmatched setups; successful bytes count only paired status-zero
        completions. Phase ownership is by setup time, never hardware request ID.

    Raises:
        TraceError: If input contracts or resource limits are violated.
        OSError: If required inputs cannot be read.
        ValueError: If JSON or text encoding is malformed.

    Notes:
        Usable quality is consistency with recorded capture assertions, not
        independent certification of their truth or the serving stack.
    """
    manifest = _manifest(run_dir / "manifest.json")
    if ordering_policy not in ("strict", "causal"):
        raise TraceError("ordering_policy must be strict or causal")
    causal = ordering_policy == "causal"
    if causal and manifest["trace_contract_version"] != "0.2.0":
        raise TraceError("causal policy requires CPU-bearing input contract 0.2.0")
    phases = _phases(run_dir / "phases.csv", manifest)
    reasons: set[str] = set()
    if causal:
        reasons.update(_causal_capture_reasons(run_dir, manifest))
    if not manifest["capture_ready"]:
        reasons.add("capture_not_ready")
    if manifest.get("capture_exit_code") != 0:
        reasons.add("capture_exit_unknown_or_nonzero")
    if manifest.get("loss_events") is None:
        reasons.add("loss_unknown")
    elif manifest["loss_events"] > 0:
        reasons.add("events_lost")
    stderr = run_dir / "bpftrace.err"
    if not stderr.exists():
        reasons.add("capture_stderr_missing")
    else:
        for line in _lines(stderr):
            # Conservative: do not guess zero from an unrecognized diagnostic.
            if line.strip():
                reasons.add("capture_stderr_requires_review")
    overlaps = sum(
        a["start_ns"] < b["end_ns"] and b["start_ns"] < a["end_ns"]
        for i, a in enumerate(phases)
        for b in phases[i + 1 :]
    )
    if overlaps:
        reasons.add("overlapping_phases")
    totals = _counts()
    outside = _counts()
    pending: dict[tuple[int, int, int], tuple | None] = {}
    unmatched_completions = collisions = events = 0
    lifetime_count = lifetime_total = 0
    lifetime_min = lifetime_max = None
    last_timestamp = -1
    high_water_timestamp = -1
    inversion_count = max_adjacent_backward_ns = max_high_water_lag_ns = 0
    inversion_examples: list[dict] = []
    has_cpu = manifest["trace_contract_version"] == "0.2.0"
    cpu_last: dict[int, int] = {}
    previous_cpu = None
    previous_key = None
    command_history: dict[tuple[int, int, int], int | None] = {}
    same_cpu_inversions = cross_cpu_inversions = per_cpu_inversions = 0
    for line_number, line in enumerate(_lines(run_dir / "trace.csv"), 1):
        if not line.strip() or line.startswith("#"):
            continue
        fields = line.strip().split(",")
        kind = fields[0]
        expected_fields = (8 if kind == "S" else 6) + int(has_cpu)
        if kind not in ("S", "C") or len(fields) != expected_fields:
            raise TraceError(
                f"trace.csv:{line_number}: invalid record width for contract"
            )
        values = [_integer(x, f"trace.csv:{line_number} field") for x in fields[1:]]
        current_cpu = values.pop() if has_cpu else None
        if current_cpu is not None:
            if current_cpu > 65535:
                raise TraceError("CPU ID exceeds 65535")
            if current_cpu not in cpu_last and len(cpu_last) >= 4096:
                raise TraceError("observed CPU limit 4096 exceeded")
            timestamp_cpu = cpu_last.get(current_cpu)
            if timestamp_cpu is not None and values[0] < timestamp_cpu:
                per_cpu_inversions += 1
                if causal:
                    reasons.add("per_cpu_event_order_not_monotonic")
            cpu_last[current_cpu] = values[0]
        timestamp, controller, queue, cid = values[:4]
        key = (controller, queue, cid)
        if causal and key not in command_history:
            if len(command_history) >= MAX_COMMAND_HISTORY:
                raise TraceError(
                    f"command history limit {MAX_COMMAND_HISTORY} exceeded"
                )
            command_history[key] = None
        if controller != manifest["controller_id"] or queue == 0:
            raise TraceError("trace event outside declared controller/I/O queue scope")
        if not manifest["capture_start_ns"] <= timestamp <= manifest["capture_end_ns"]:
            reasons.add("event_outside_capture")
        if timestamp < last_timestamp:
            if not causal:
                reasons.add("event_order_not_monotonic")
            elif current_cpu == previous_cpu or key == previous_key:
                reasons.add("causal_adjacent_order_violation")
            inversion_count += 1
            if has_cpu:
                if current_cpu == previous_cpu:
                    same_cpu_inversions += 1
                else:
                    cross_cpu_inversions += 1
            backward_ns = last_timestamp - timestamp
            max_adjacent_backward_ns = max(max_adjacent_backward_ns, backward_ns)
            if len(inversion_examples) < 16:
                inversion_examples.append(
                    {
                        "line_number": line_number,
                        "kind": kind,
                        "timestamp_ns": timestamp,
                        "previous_timestamp_ns": last_timestamp,
                        "backward_ns": backward_ns,
                        "controller_id": controller,
                        "queue_id": queue,
                        "command_id": cid,
                        "cpu_id": current_cpu,
                        "previous_cpu_id": previous_cpu,
                    }
                )
        max_high_water_lag_ns = max(
            max_high_water_lag_ns, high_water_timestamp - timestamp
        )
        high_water_timestamp = max(high_water_timestamp, timestamp)
        last_timestamp = timestamp
        previous_cpu = current_cpu
        previous_key = key
        events += 1
        if kind == "S":
            if causal:
                last_completion = command_history[key]
                if last_completion is not None and timestamp < last_completion:
                    reasons.add("command_id_reuse_timestamp_overlap")
            opcode, _slba, blocks = values[4:]
            if blocks < 1 or blocks > 65536 or opcode > 255 or cid > 65535:
                raise TraceError("invalid opcode, command ID or decoded block count")
            owners = [
                i
                for i, phase in enumerate(phases)
                if phase["start_ns"] <= timestamp < phase["end_ns"]
            ]
            targets = [totals] + ([phases[i] for i in owners] if owners else [outside])
            for target in targets:
                target["submitted_commands"] += 1
                if opcode in (1, 2):
                    direction = "write" if opcode == 1 else "read"
                    target[f"submitted_{direction}_bytes"] += (
                        blocks * manifest["logical_block_size"]
                    )
            if key in pending:
                collisions += 1
                pending[key] = None
                reasons.add("ambiguous_command_id_reuse")
            else:
                if len(pending) >= MAX_PENDING:
                    raise TraceError(f"pending command limit {MAX_PENDING} exceeded")
                pending[key] = (timestamp, opcode, blocks, owners)
        else:
            if causal:
                command_history[key] = timestamp
            if key not in pending:
                unmatched_completions += 1
                reasons.add("unmatched_completion")
                continue
            setup = pending.pop(key)
            if setup is None:
                continue
            start, opcode, blocks, owners = setup
            if timestamp < start:
                reasons.add("negative_command_lifetime")
                continue
            status = values[4]
            targets = [totals] + ([phases[i] for i in owners] if owners else [outside])
            for target in targets:
                target["paired_commands"] += 1
                if status:
                    target["failed_commands"] += 1
                elif opcode in (1, 2):
                    direction = "write" if opcode == 1 else "read"
                    target[f"successful_completed_{direction}_bytes"] += (
                        blocks * manifest["logical_block_size"]
                    )
            if status:
                reasons.add("nonzero_completion_status")
            crossing = [
                phase
                for phase in phases
                if start < phase["end_ns"]
                and timestamp >= phase["start_ns"]
                and (start < phase["start_ns"] or timestamp >= phase["end_ns"])
            ]
            if crossing:
                totals["boundary_crossing_commands"] += 1
                if not owners:
                    outside["boundary_crossing_commands"] += 1
                for phase in crossing:
                    phase["boundary_crossing_commands"] += 1
                reasons.add("phase_boundary_crossing")
            lifetime = timestamp - start
            lifetime_count += 1
            lifetime_total += lifetime
            lifetime_min = (
                lifetime if lifetime_min is None else min(lifetime_min, lifetime)
            )
            lifetime_max = (
                lifetime if lifetime_max is None else max(lifetime_max, lifetime)
            )
    if pending:
        reasons.add("unmatched_setup")
    for phase in phases:
        expected = phase["expected_read_bytes"]
        phase["successful_read_to_expected_ratio"] = (
            phase["successful_completed_read_bytes"] / expected if expected else None
        )
    paths = ["manifest.json", "phases.csv", "trace.csv"]
    if stderr.exists():
        paths.append("bpftrace.err")
    if (run_dir / "bpftrace.out").exists():
        paths.append("bpftrace.out")
    return {
        "trace_observation_version": "0.3.0" if causal else "0.2.0",
        "quality": {
            "status": "incomplete" if reasons else "usable",
            "reasons": sorted(reasons),
        },
        "scope": {
            "namespace": manifest["namespace"],
            "controller_id": manifest["controller_id"],
            "logical_block_size": manifest["logical_block_size"],
            "clock": manifest["clock"],
        },
        "events": events,
        "ordering": {
            "policy": "retained_delivery_causal_v1"
            if causal
            else "retained_delivery_strict_timestamp",
            "adjacent_inversions": inversion_count,
            "max_adjacent_backward_ns": max_adjacent_backward_ns,
            "max_high_water_lag_ns": max_high_water_lag_ns,
            "examples": inversion_examples,
            "example_limit": 16,
            "examples_truncated": inversion_count > len(inversion_examples),
            "cpu_provenance": "recorded" if has_cpu else "not_recorded",
            "observed_cpu_ids": sorted(cpu_last),
            "adjacent_same_cpu_inversions": same_cpu_inversions if has_cpu else None,
            "adjacent_cross_cpu_inversions": cross_cpu_inversions if has_cpu else None,
            "per_cpu_inversions": per_cpu_inversions if has_cpu else None,
            "reordered": False,
            **(
                {
                    "history_scope": "all_command_keys_in_capture",
                    "history_keys": len(command_history),
                    "history_key_limit": MAX_COMMAND_HISTORY,
                    "validation_scope": "recorded_capture_assertions_and_retained_records",
                    "analyzer_sha256": _digest(Path(__file__)),
                }
                if causal
                else {}
            ),
        },
        "totals": totals,
        "outside_phases": outside,
        "phases": phases,
        "pairing": {
            "unmatched_setups": len(pending),
            "unmatched_completions": unmatched_completions,
            "ambiguous_id_reuses": collisions,
            "overlapping_phase_pairs": overlaps,
        },
        "host_command_lifetime_ns": {
            "count": lifetime_count,
            "total": lifetime_total,
            "min": lifetime_min,
            "max": lifetime_max,
        },
        "input_sha256": {name: _digest(run_dir / name) for name in paths},
        "serving_verification": "not_assessed",
        "limitations": [
            "Namespace observations are not a request-ID hardware trace "
            "or a serving tier PASS.",
            "Quality relies on recorded capture assertions; "
            "hardware clock/filter/loss validation remains required.",
            "Phase bytes use setup time; overlapping phase totals must not be summed.",
            "Expected bytes are supplied inputs, not observed model/runtime geometry.",
            "Trace-on latency is diagnostic; "
            "do not pool with trace-off performance results.",
        ],
    }


def render_trace(observation: dict) -> str:
    """Render an offline observation summary, not an evidence verdict.

    Args:
        observation: Result returned by analyze_trace.

    Returns:
        English summary with quality reasons and an explicit claim boundary.
    """
    lines = [
        "Gate 1 — offline NVMe observations (experimental)",
        f"Quality: {observation['quality']['status']}",
        f"Namespace: {observation['scope']['namespace']}",
    ]
    if observation.get("ordering", {}).get("policy") == "retained_delivery_causal_v1":
        lines.append(
            "Ordering: causal v1 (experimental opt-in; no chronological reconstruction)"
        )
    for phase in observation["phases"]:
        lines.append(
            f"  {phase['request_id']} / {phase['phase']}: "
            f"successful read={phase['successful_completed_read_bytes']} B, "
            f"write={phase['successful_completed_write_bytes']} B"
        )
    lines.extend(f"  Review: {reason}" for reason in observation["quality"]["reasons"])
    lines.append(
        "Serving verification: not assessed. No Gate 1 PASS or confidence upgrade."
    )
    return "\n".join(lines)
