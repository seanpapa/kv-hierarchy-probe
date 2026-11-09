# SPDX-License-Identifier: Apache-2.0
"""Experimental, bounded root-only NVMe capture; never a serving verdict."""

# Standard
import hashlib
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import time
from pathlib import Path

# Local
from .nvme_plan import TracePlanError, build_trace_plan

MAX_CAPTURE_BYTES = 128 * 1024 * 1024


class CaptureError(ValueError):
    """Raised when explicit capture prerequisites or arguments are unsafe."""


def build_capture_script(namespace: str, duration_seconds: int) -> str:
    """Generate the fixed tracepoint profile with explicit monotonic timestamps.

    Args:
        namespace: Literal whole namespace, not a shell expression or partition.
        duration_seconds: Capture duration after the first attached timer tick.

    Returns:
        Bpftrace source for setup/completion records and ready/end markers.

    Raises:
        CaptureError: If the namespace or duration is outside the initial scope.
    """
    if not re.fullmatch(r"nvme[0-9]+n[1-9][0-9]*", namespace) or len(namespace) >= 32:
        raise CaptureError("a literal whole NVMe namespace is required")
    if type(duration_seconds) is not int or not 1 <= duration_seconds <= 3600:
        raise CaptureError("duration must be an integer from 1 to 3600 seconds")
    return f'''// Experimental nvme-byte-array-v1; BEGIN counters are not readiness.
BEGIN
{{
  // A sentinel invocation ensures both count maps exist on an empty capture.
  @kvprobe_setup_count = count();
  @kvprobe_completion_count = count();
}}
interval:s:1
{{
  if (@ready == 0) {{
    @ready = 1;
    printf("# READY,%llu\\n", nsecs(monotonic));
  }} else {{
    @ticks = @ticks + 1;
    if (@ticks >= {duration_seconds}) {{ exit(); }}
  }}
}}
tracepoint:nvme:nvme_setup_cmd
/@ready && args.qid > 0 && args.disk == "{namespace}"/
{{
  @kvprobe_setup_count = count();
  $lba = (uint64)args.cdw10[0] | ((uint64)args.cdw10[1] << 8)
       | ((uint64)args.cdw10[2] << 16) | ((uint64)args.cdw10[3] << 24)
       | ((uint64)args.cdw10[4] << 32) | ((uint64)args.cdw10[5] << 40)
       | ((uint64)args.cdw10[6] << 48) | ((uint64)args.cdw10[7] << 56);
  $blocks = (uint64)args.cdw10[8] | ((uint64)args.cdw10[9] << 8);
  printf("S,%llu,%d,%d,%u,%u,%llu,%llu,%u\\n", nsecs(monotonic),
         args.ctrl_id, args.qid, args.cid, args.opcode, $lba, $blocks + 1, cpu);
}}
tracepoint:nvme:nvme_complete_rq
/@ready && args.qid > 0 && args.disk == "{namespace}"/
{{
  @kvprobe_completion_count = count();
  printf("C,%llu,%d,%d,%d,%u,%u\\n", nsecs(monotonic),
         args.ctrl_id, args.qid, args.cid, args.status, cpu);
}}
END
{{
  printf("# END,%llu\\n", nsecs(monotonic));
  clear(@ready);
  clear(@ticks);
}}
'''


def summarize_capture(raw_path: Path) -> dict:
    """Inspect bounded capture records without sorting or inventing identity.

    Args:
        raw_path: Finished raw stdout containing S/C records and markers.

    Returns:
        Ready/end timestamps, observed controller IDs and malformed record count.

    Raises:
        OSError: If raw stdout cannot be read.
    """
    result = {
        "ready_ns": None,
        "end_ns": None,
        "controller_ids": [],
        "malformed_records": 0,
        "setup_records": 0,
        "completion_records": 0,
        "kernel_setup_count": None,
        "kernel_completion_count": None,
        "reported_lost_events": 0,
        "cpu_missing_records": 0,
    }
    controllers = set()
    with raw_path.open(encoding="utf-8", errors="replace") as stream:
        while line := stream.readline(4097):
            if len(line) > 4096:
                raise CaptureError("capture record exceeds 4096 characters")
            line = line.strip()
            if not line:
                continue
            marker = re.fullmatch(r"# (READY|END),([0-9]+)", line)
            if marker:
                field = "ready_ns" if marker[1] == "READY" else "end_ns"
                if result[field] is not None:
                    result["malformed_records"] += 1
                result[field] = int(marker[2])
                continue
            counter = re.fullmatch(r"@kvprobe_(setup|completion)_count: ([0-9]+)", line)
            if counter:
                field = "kernel_" + counter[1] + "_count"
                if result[field] is not None or int(counter[2]) < 1:
                    result["malformed_records"] += 1
                    # Duplicates must never become a successful loss assertion.
                    result[field] = -1
                else:
                    result[field] = int(counter[2]) - 1
                continue
            lost = re.fullmatch(r"Lost ([0-9]+) events", line)
            if lost:
                result["reported_lost_events"] += int(lost[1])
                continue
            fields = line.split(",")
            if (
                len(fields) in ((8, 9) if fields[0] == "S" else (6, 7))
                and fields[0] in ("S", "C")
                and all(re.fullmatch(r"[0-9]+", item) for item in fields[1:])
            ):
                controllers.add(int(fields[2]))
                if len(fields) == (8 if fields[0] == "S" else 6):
                    result["cpu_missing_records"] += 1
                result[
                    "setup_records" if fields[0] == "S" else "completion_records"
                ] += 1
            else:
                result["malformed_records"] += 1
    result["controller_ids"] = sorted(controllers)
    return result


def assess_capture_loss(
    summary: dict,
    stderr_path: Path,
    *,
    process_returncode: int,
    stop_reason: str | None,
) -> dict:
    """Reconcile probe counters and retained records, not hardware-global loss.

    Args:
        summary: Finished stdout inspection from summarize_capture.
        stderr_path: Retained bpftrace stderr; unknown diagnostics block zero.
        process_returncode: Actual tool exit code.
        stop_reason: Wrapper timeout, interruption or size limit, otherwise None.

    Returns:
        Observed loss lower bound or null, method, and review reasons. Zero
        requires clean ready/end, final counters and matching S/C record counts.

    Raises:
        OSError: If the retained stderr cannot be read.
        CaptureError: If a stderr record exceeds the bounded input limit.

    Notes:
        Counts cover matched probe invocations after ready. They do not prove
        correct filtering, every hardware command, or a serving tier PASS.
    """
    reasons = []
    reported = summary["reported_lost_events"]
    with stderr_path.open(encoding="utf-8", errors="replace") as stream:
        while line := stream.readline(4097):
            if len(line) > 4096:
                raise CaptureError("stderr record exceeds 4096 characters")
            lost = re.fullmatch(r"Lost ([0-9]+) events", line.strip())
            if lost:
                reported += int(lost[1])
            elif line.strip():
                reasons.append("stderr_requires_review")
    if process_returncode != 0 or stop_reason is not None:
        reasons.append("capture_not_cleanly_finished")
    start, end = summary["ready_ns"], summary["end_ns"]
    if start is None or end is None or end <= start:
        reasons.append("ready_end_missing_or_invalid")
    if summary["malformed_records"]:
        reasons.append("stdout_requires_review")
    deficit = 0
    for kind in ("setup", "completion"):
        kernel = summary["kernel_" + kind + "_count"]
        records = summary[kind + "_records"]
        if kernel is None or kernel < 0:
            reasons.append(kind + "_counter_missing_or_invalid")
        elif kernel < records:
            reasons.append(kind + "_counter_below_retained_records")
        else:
            deficit += kernel - records
    # Lost callback records and counter deficits can overlap: never add them.
    lower_bound = max(reported, deficit)
    return {
        "loss_events": lower_bound if lower_bound else (None if reasons else 0),
        "method": "probe_count_vs_records_and_bpftrace_reports",
        "scope": "matched_probe_invocations_to_retained_records",
        "reported_lost_events": reported,
        "record_deficit": deficit,
        "reasons": sorted(set(reasons)),
    }


def _normalize_trace(raw_path: Path, trace_path: Path) -> None:
    """Retain valid event/marker lines separately; raw stdout is never edited."""
    with (
        raw_path.open(encoding="utf-8", errors="replace") as raw,
        trace_path.open("x") as output,
    ):
        while line := raw.readline(4097):
            if len(line) > 4096:
                raise CaptureError("capture record exceeds 4096 characters")
            if re.fullmatch(r"# (READY|END),[0-9]+\s*", line) or re.fullmatch(
                r"(?:S(?:,[0-9]+){7,8}|C(?:,[0-9]+){5,6})\s*", line
            ):
                output.write(line)


def _trusted_path(path: Path, *, directory: bool) -> None:
    """Reject root execution paths under non-root or unsafe writable parents."""
    for item in (path, *path.parents):
        info = item.lstat()
        if stat.S_ISLNK(info.st_mode) or info.st_uid != 0:
            raise CaptureError(f"root-owned, non-symlink path required: {item}")
        sticky_directory = stat.S_ISDIR(info.st_mode) and bool(
            info.st_mode & stat.S_ISVTX
        )
        if info.st_mode & 0o022 and not sticky_directory:
            raise CaptureError(f"unsafe writable path: {item}")
    if directory and not path.is_dir():
        raise CaptureError("run directory parent must exist")


def capture_namespace(disk: str, run_dir: Path, duration_seconds: int) -> dict:
    """Capture a local PCIe namespace with explicit root authority and limits.

    Args:
        disk: Whole namespace accepted by the read-only preflight.
        run_dir: New absolute directory under trusted root-owned parents.
        duration_seconds: One to 3600 seconds after readiness.

    Returns:
        Capture manifest with scoped record-delivery loss accounting. This is
        experimental until Linux counter-output acceptance is completed.

    Raises:
        CaptureError: If root, preflight, version or path checks fail.
        OSError: If capture artifacts or subprocess cannot be created.

    Notes:
        Does not generate I/O, export request phases, mutate caches, or set PASS.
        All output is private/root-owned; retrieval requires explicit sudo.
    """
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        raise CaptureError(
            "capture requires explicit root execution; no sudo is invoked"
        )
    if not run_dir.is_absolute() or run_dir.exists() or run_dir.is_symlink():
        raise CaptureError("run-dir must be a new absolute directory")
    script = build_capture_script(disk.removeprefix("/dev/"), duration_seconds)
    _trusted_path(run_dir.parent, directory=True)
    executable = shutil.which("bpftrace")
    if executable is None:
        raise CaptureError("bpftrace is not available")
    _trusted_path(Path(executable), directory=False)
    try:
        plan = build_trace_plan(disk)
    except TracePlanError as exc:
        raise CaptureError(str(exc)) from exc
    if plan["preflight_status"] != "eligible":
        raise CaptureError(f"preflight not eligible: {plan['reasons']}")
    tool = Path(plan["bpftrace"]["executable"])
    if str(tool) != executable:
        raise CaptureError("bpftrace executable changed during preflight")
    _trusted_path(tool, directory=False)
    if plan["bpftrace"].get("version_output", "").strip() != "bpftrace v0.20.2":
        raise CaptureError("initial collector requires bpftrace v0.20.2")
    run_dir.mkdir(mode=0o700)
    (run_dir / "preflight.json").write_text(json.dumps(plan, indent=2) + "\n")
    (run_dir / "capture.bt").write_text(script)
    command = [str(tool), "-q", "-B", "line", str(run_dir / "capture.bt")]
    stop_reason = None
    ready_seen = False
    deadline = time.monotonic() + duration_seconds + 60
    with (
        (run_dir / "bpftrace.out").open("x") as output,
        (run_dir / "bpftrace.err").open("x") as errors,
    ):
        process = subprocess.Popen(command, stdout=output, stderr=errors)
        try:
            with (run_dir / "bpftrace.out").open() as markers:
                while process.poll() is None:
                    if not ready_seen:
                        line = markers.readline(4097)
                        if re.fullmatch(r"# READY,[0-9]+\n", line):
                            ready_seen = True
                            (run_dir / "ready.json").write_text(
                                json.dumps(
                                    {
                                        "clock": "CLOCK_MONOTONIC",
                                        "ready_ns": int(line.split(",")[1]),
                                        "serving_verification": "not_assessed",
                                    }
                                )
                                + "\n"
                            )
                    total = sum(
                        (run_dir / name).stat().st_size
                        for name in ("bpftrace.out", "bpftrace.err")
                    )
                    if total >= MAX_CAPTURE_BYTES or time.monotonic() >= deadline:
                        stop_reason = (
                            "size_limit" if total >= MAX_CAPTURE_BYTES else "timeout"
                        )
                        break
                    time.sleep(0.05)
        except KeyboardInterrupt:
            stop_reason = "interrupted"
        finally:
            if process.poll() is None:
                process.send_signal(signal.SIGINT)
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)
    summary = summarize_capture(run_dir / "bpftrace.out")
    _normalize_trace(run_dir / "bpftrace.out", run_dir / "trace.csv")
    loss = assess_capture_loss(
        summary,
        run_dir / "bpftrace.err",
        process_returncode=process.returncode,
        stop_reason=stop_reason,
    )
    raw_digest = hashlib.sha256()
    with (run_dir / "bpftrace.out").open("rb") as raw:
        for chunk in iter(lambda: raw.read(65536), b""):
            raw_digest.update(chunk)
    controllers = summary["controller_ids"]
    manifest = {
        "trace_contract_version": "0.2.0",
        "collector_version": "0.3.0-experimental",
        "namespace": plan["namespace"],
        "clock": "CLOCK_MONOTONIC",
        "controller_id": controllers[0] if len(controllers) == 1 else None,
        "observed_controller_ids": controllers,
        "logical_block_size": plan["logical_block_size"]["value"],
        "capture_start_ns": summary["ready_ns"],
        "capture_end_ns": summary["end_ns"],
        "capture_ready": summary["ready_ns"] is not None,
        "capture_exit_code": process.returncode
        if process.returncode >= 0
        and stop_reason is None
        and not summary["malformed_records"]
        and not summary["cpu_missing_records"]
        else 1,
        "process_returncode": process.returncode,
        "loss_events": loss["loss_events"],
        "loss_accounting": loss,
        "probe_record_counts": {
            key: summary[key]
            for key in (
                "kernel_setup_count",
                "kernel_completion_count",
                "setup_records",
                "completion_records",
            )
        },
        "raw_stdout_sha256": raw_digest.hexdigest(),
        "stop_reason": stop_reason,
        "malformed_records": summary["malformed_records"],
        "cpu_missing_records": summary["cpu_missing_records"],
        "serving_verification": "not_assessed",
        "limitations": [
            "Linux acceptance pending; loss covers matched probe record delivery only.",
            "Namespace observations are not request-ID attribution.",
            "No phase producer or serving verdict is included.",
        ],
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest
