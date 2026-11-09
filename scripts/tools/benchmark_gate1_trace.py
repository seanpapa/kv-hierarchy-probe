# SPDX-License-Identifier: Apache-2.0
"""Synthetic Gate 1 resource acceptance; no device I/O or serving verdict."""

# Standard
import argparse
import hashlib
import json
import os
import platform
import resource
import subprocess
import sys
import time
from pathlib import Path

# First Party
from kvprobe.nvme_capture import assess_capture_loss, summarize_capture
from kvprobe.nvme_trace import analyze_trace


def make_fixture(directory: Path, commands: int, missing_completion: bool) -> None:
    """Write synthetic CPU-bearing input, with optional final completion omission.

    Args:
        directory: Existing empty case directory.
        commands: Number of submitted read commands; command IDs are reused.
        missing_completion: Whether to omit the last completion in both streams.

    Returns:
        None. Files are exclusively created, with internally matching assertions.

    Raises:
        OSError: If a file exists or cannot be written.
    """
    end = commands * 4 + 100
    raw_path = directory / "bpftrace.out"
    digest = hashlib.sha256()
    with (
        raw_path.open("x", encoding="utf-8") as raw,
        (directory / "trace.csv").open("x", encoding="utf-8") as trace,
    ):
        marker = "# READY,0\n"
        raw.write(marker)
        digest.update(marker.encode())
        for index in range(commands):
            timestamp = index * 4 + 10
            cid = index % 32768
            cpu = index % 4
            records = f"S,{timestamp},0,1,{cid},2,{index},1,{cpu}\n"
            if not (missing_completion and index == commands - 1):
                records += f"C,{timestamp + 1},0,1,{cid},0,{cpu}\n"
            raw.write(records)
            trace.write(records)
            digest.update(records.encode())
        tail = (
            f"# END,{end}\n@kvprobe_setup_count: {commands + 1}\n"
            f"@kvprobe_completion_count: {commands + 1 - int(missing_completion)}\n"
        )
        raw.write(tail)
        digest.update(tail.encode())
    (directory / "bpftrace.err").write_text("", encoding="utf-8")
    loss = assess_capture_loss(
        summarize_capture(raw_path),
        directory / "bpftrace.err",
        process_returncode=0,
        stop_reason=None,
    )
    manifest = {
        "trace_contract_version": "0.2.0",
        "namespace": "nvme0n1",
        "controller_id": 0,
        "logical_block_size": 4096,
        "clock": "CLOCK_MONOTONIC",
        "capture_start_ns": 0,
        "capture_end_ns": end,
        "capture_ready": True,
        "capture_exit_code": 0,
        "process_returncode": 0,
        "stop_reason": None,
        "loss_events": 0,
        "raw_stdout_sha256": digest.hexdigest(),
        "loss_accounting": loss,
    }
    with (directory / "manifest.json").open("x", encoding="utf-8") as stream:
        json.dump(manifest, stream)
    completed = commands - int(missing_completion)
    with (directory / "phases.csv").open("x", encoding="utf-8") as stream:
        stream.write("request_id,phase,start_ns,end_ns,expected_read_bytes\n")
        stream.write(f"synthetic,retrieve,1,{end - 1},{completed * 4096}\n")


def measure_case(directory: Path, commands: int, missing_completion: bool) -> dict:
    """Generate and analyze one case in a fresh process; return measured checks.

    Args:
        directory: New case directory, created by this function.
        commands: Number of synthetic setup records.
        missing_completion: Whether the final command is deliberately unmatched.

    Returns:
        Analysis time, process peak RSS (including generation/imports), checks and
        observation. Quality is checked independently of scoped record delivery.

    Raises:
        OSError: If artifacts cannot be created.
        ValueError: If the analyzer rejects generated input.
    """
    directory.mkdir()
    make_fixture(directory, commands, missing_completion)
    started = time.perf_counter()
    observation = analyze_trace(directory, ordering_policy="causal")
    seconds = time.perf_counter() - started
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    peak_bytes = peak if sys.platform == "darwin" else peak * 1024
    completed = commands - int(missing_completion)
    checks = {
        "quality": observation["quality"]["status"]
        == ("incomplete" if missing_completion else "usable"),
        "successful_read_bytes": observation["totals"][
            "successful_completed_read_bytes"
        ]
        == completed * 4096,
        "unmatched_setups": observation["pairing"]["unmatched_setups"]
        == int(missing_completion),
        "missing_reason": ("unmatched_setup" in observation["quality"]["reasons"])
        == missing_completion,
        "not_serving_pass": observation["serving_verification"] == "not_assessed",
        "not_reordered": observation["ordering"]["reordered"] is False,
    }
    with (directory / "observation.json").open("x", encoding="utf-8") as stream:
        json.dump(observation, stream, indent=2)
    return {
        "commands": commands,
        "events": commands * 2 - int(missing_completion),
        "analyzer_seconds": seconds,
        "process_peak_rss_bytes": peak_bytes,
        "checks": checks,
        "input_sha256": observation["input_sha256"],
        "analyzer_sha256": observation["ordering"]["analyzer_sha256"],
    }


def main() -> int:
    """Run bounded synthetic cases and retain a JSON resource acceptance report.

    Returns:
        0 for all checks within selected budgets; 1 for acceptance failure.
        Argparse errors return 2. This never grants a Gate 1 serving PASS.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--commands", type=int, default=500000)
    parser.add_argument("--max-seconds", type=float, default=120)
    parser.add_argument("--max-rss-mib", type=float, default=256)
    parser.add_argument(
        "--worker", choices=("complete", "missing-completion"), help=argparse.SUPPRESS
    )
    args = parser.parse_args()
    if not 1 <= args.commands <= 500000 or not (
        0 < args.max_seconds <= 120 and 0 < args.max_rss_mib <= 256
    ):
        parser.error("commands must be 1..500000; budgets must be >0 and <=120s/256MiB")
    if args.worker:
        print(
            json.dumps(
                measure_case(
                    args.output_dir, args.commands, args.worker == "missing-completion"
                )
            )
        )
        return 0
    if args.output_dir.exists():
        parser.error("output directory must not exist; existing evidence is preserved")
    args.output_dir.mkdir(parents=True)
    report = {
        "workload": "synthetic_no_device_io",
        "serving_verification": "not_assessed",
        "benchmark_source_sha256": hashlib.sha256(
            Path(__file__).read_bytes()
        ).hexdigest(),
        "environment": {"python": sys.version, "platform": platform.platform()},
        "budgets": {
            "analyzer_seconds": args.max_seconds,
            "process_peak_rss_bytes": int(args.max_rss_mib * 1024 * 1024),
            "worker_timeout_seconds": 180,
        },
        "scope": "one phase, four CPUs, 32768 reused command keys, sequential pairs",
        "cases": {},
    }
    for case in ("complete", "missing-completion"):
        try:
            worker = subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "--output-dir",
                    str(args.output_dir / case),
                    "--commands",
                    str(args.commands),
                    "--worker",
                    case,
                ],
                capture_output=True,
                text=True,
                timeout=180,
                check=False,
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            )
            if worker.returncode:
                result = {
                    "error": "worker_failed",
                    "returncode": worker.returncode,
                    "stderr": worker.stderr[-4096:],
                    "accepted": False,
                }
            else:
                result = json.loads(worker.stdout)
                result["accepted"] = (
                    all(result["checks"].values())
                    and result["analyzer_seconds"] <= args.max_seconds
                    and result["process_peak_rss_bytes"]
                    <= report["budgets"]["process_peak_rss_bytes"]
                )
        except subprocess.TimeoutExpired:
            result = {"error": "worker_timeout", "accepted": False}
        report["cases"][case] = result
    report["accepted"] = all(case["accepted"] for case in report["cases"].values())
    with (args.output_dir / "report.json").open("x", encoding="utf-8") as stream:
        json.dump(report, stream, indent=2)
    print(json.dumps(report, indent=2))
    return 0 if report["accepted"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
