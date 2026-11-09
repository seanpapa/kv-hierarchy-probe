# SPDX-License-Identifier: Apache-2.0
"""CPU-only collector contract tests; no privileges or probe attachment."""

# Standard
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

# First Party
from kvprobe.cli import main
from kvprobe.nvme_capture import (
    CaptureError,
    assess_capture_loss,
    build_capture_script,
    capture_namespace,
    summarize_capture,
)


class NvmeCaptureTest(unittest.TestCase):
    """Check source generation and raw observations through public interfaces."""

    def test_fixed_profile_clock_and_readiness(self) -> None:
        """Require monotonic clock, namespace filters and post-attach timer marker."""
        script = build_capture_script("nvme0n1", 5)
        self.assertIn("BEGIN\n", script)
        self.assertEqual(script.count("@kvprobe_setup_count = count();"), 2)
        self.assertEqual(script.count("@kvprobe_completion_count = count();"), 2)
        self.assertNotIn("clear(@kvprobe_setup_count)", script)
        self.assertIn("interval:s:1", script)
        self.assertIn("nsecs(monotonic)", script)
        self.assertEqual(script.count('args.disk == "nvme0n1"'), 2)
        self.assertNotIn("str(args.disk)", script)
        self.assertIn("$blocks + 1", script)
        self.assertIn("@ticks >= 5", script)
        self.assertIn("# END", script)
        self.assertIn("$blocks + 1, cpu", script)
        self.assertIn("args.status, cpu", script)

    def test_cpu_records_and_legacy_missing_provenance(self) -> None:
        """Count CPU-bearing records without fabricating legacy provenance."""
        with tempfile.TemporaryDirectory() as directory:
            raw = Path(directory) / "raw.txt"
            raw.write_text("S,11,7,1,2,2,0,16,3\nC,12,7,1,2,0,4\n")
            result = summarize_capture(raw)
            self.assertEqual(result["cpu_missing_records"], 0)
            self.assertEqual(result["setup_records"], 1)
            self.assertEqual(result["completion_records"], 1)
            raw.write_text("S,11,7,1,2,2,0,16\nC,12,7,1,2,0\n")
            self.assertEqual(summarize_capture(raw)["cpu_missing_records"], 2)

    def test_reject_injection_partition_and_invalid_duration(self) -> None:
        """Reject script interpolation and unbounded durations."""
        for namespace in ("nvme0n1p1", 'nvme0n1";exit()', "/dev/nvme0n1"):
            with self.assertRaises(CaptureError):
                build_capture_script(namespace, 5)
        for duration in (0, 3601, True, 1.5):
            with self.assertRaises(CaptureError):
                build_capture_script("nvme0n1", duration)

    def test_observed_controller_not_name_inference(self) -> None:
        """Retain actual trace controller IDs and record markers."""
        with tempfile.TemporaryDirectory() as directory:
            raw = Path(directory) / "trace.csv"
            raw.write_text("# READY,10\nS,11,7,1,2,2,0,16\nC,12,7,1,2,0\n# END,20\n")
            result = summarize_capture(raw)
        self.assertEqual(result["controller_ids"], [7])
        self.assertEqual(result["ready_ns"], 10)
        self.assertEqual(result["end_ns"], 20)
        self.assertEqual(result["malformed_records"], 0)

    def test_empty_trace_does_not_invent_controller(self) -> None:
        """No observed commands means no inferred controller identity."""
        with tempfile.TemporaryDirectory() as directory:
            raw = Path(directory) / "trace.csv"
            raw.write_text("# READY,10\n# END,20\n")
            self.assertEqual(summarize_capture(raw)["controller_ids"], [])

    def test_duplicate_marker_and_unexpected_output(self) -> None:
        """Do not hide malformed stdout or repeated readiness markers."""
        with tempfile.TemporaryDirectory() as directory:
            raw = Path(directory) / "trace.csv"
            raw.write_text("# READY,10\n# READY,11\n@unexpected: 5\n")
            self.assertEqual(summarize_capture(raw)["malformed_records"], 2)

    def loss_case(
        self, raw_text: str, stderr: str = "", *, code: int = 0, stop: str | None = None
    ) -> dict:
        """Inspect synthetic output without running a privileged collector."""
        with tempfile.TemporaryDirectory() as directory:
            raw = Path(directory) / "stdout"
            err = Path(directory) / "stderr"
            raw.write_text(raw_text)
            err.write_text(stderr)
            return assess_capture_loss(
                summarize_capture(raw), err, process_returncode=code, stop_reason=stop
            )

    def test_loss_zero_requires_final_counts_not_pairing(self) -> None:
        """Counter agreement permits only scoped delivery-loss zero."""
        raw = "# READY,10\nS,11,7,1,2,2,0,16\nC,12,7,1,2,0\n# END,20\n"
        self.assertIsNone(self.loss_case(raw)["loss_events"])
        complete = raw + "@kvprobe_setup_count: 2\n@kvprobe_completion_count: 2\n"
        self.assertEqual(self.loss_case(complete)["loss_events"], 0)

    def test_dropped_pair_detected_by_probe_counts(self) -> None:
        """Perfect pairing of retained rows cannot hide an entirely missing pair."""
        raw = "# READY,10\n# END,20\n@kvprobe_setup_count: 2\n@kvprobe_completion_count: 2\n"
        loss = self.loss_case(raw)
        self.assertEqual(loss["record_deficit"], 2)
        self.assertEqual(loss["loss_events"], 2)

    def test_empty_capture_has_counter_sentinels(self) -> None:
        """Sentinel count one represents zero matched probe invocations."""
        raw = "# READY,10\n# END,20\n@kvprobe_setup_count: 1\n@kvprobe_completion_count: 1\n"
        self.assertEqual(self.loss_case(raw)["loss_events"], 0)

    def test_lost_reports_are_not_added_to_same_deficit(self) -> None:
        """Preserve a positive lower bound, never double-count overlapping signals."""
        raw = "# READY,10\n# END,20\n@kvprobe_setup_count: 2\n@kvprobe_completion_count: 2\nLost 2 events\n"
        loss = self.loss_case(raw, "Lost 1 events\n")
        self.assertEqual(loss["loss_events"], 3)
        self.assertEqual(loss["record_deficit"], 2)

    def test_warning_nonzero_exit_and_forced_stop_block_zero(self) -> None:
        """Untrusted termination or diagnostics must remain unknown."""
        raw = "# READY,10\n# END,20\n@kvprobe_setup_count: 1\n@kvprobe_completion_count: 1\n"
        for stderr, code, stop in (
            ("warning\n", 0, None),
            ("", 1, None),
            ("", 0, "size_limit"),
        ):
            self.assertIsNone(
                self.loss_case(raw, stderr, code=code, stop=stop)["loss_events"]
            )

    def test_invalid_counters_and_marker_order_block_zero(self) -> None:
        """Duplicates and absent/invalid final markers cannot establish zero."""
        raws = [
            "# READY,10\n# END,20\n@kvprobe_setup_count: 0\n@kvprobe_completion_count: 1\n",
            "# READY,10\n# END,20\n@kvprobe_setup_count: 1\n@kvprobe_setup_count: 1\n@kvprobe_completion_count: 1\n",
            "# READY,20\n# END,10\n@kvprobe_setup_count: 1\n@kvprobe_completion_count: 1\n",
            "# READY,10\n@kvprobe_setup_count: 1\n@kvprobe_completion_count: 1\n",
            "# READY,10\nS,11,7,1,2,2,0,16\n# END,20\n@kvprobe_setup_count: 1\n@kvprobe_completion_count: 1\n",
        ]
        for raw in raws:
            self.assertIsNone(self.loss_case(raw)["loss_events"])

    def test_non_root_refused_before_preflight(self) -> None:
        """No implicit privilege escalation or subprocess for non-root capture."""
        with (
            patch("kvprobe.nvme_capture.os.geteuid", return_value=1000),
            patch("kvprobe.nvme_capture.build_trace_plan") as plan,
        ):
            with self.assertRaises(CaptureError):
                capture_namespace("nvme0n1", Path("/tmp/new-capture"), 1)
            plan.assert_not_called()

    def test_cli_capture_refusal(self) -> None:
        """CLI reports explicit privilege requirement with exit two."""
        with (
            patch("kvprobe.nvme_capture.os.geteuid", return_value=1000),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(
                main(
                    [
                        "gate1",
                        "capture",
                        "--disk",
                        "nvme0n1",
                        "--run-dir",
                        "/tmp/new-capture",
                    ]
                ),
                2,
            )

    def test_cli_capture_zero_is_scoped_not_serving_pass(self) -> None:
        """Exit zero is capture bookkeeping only and must say so."""
        result = {
            "loss_events": 0,
            "capture_exit_code": 0,
            "capture_ready": True,
            "controller_id": 7,
            "loss_accounting": {"reasons": []},
        }
        output = io.StringIO()
        with (
            patch("kvprobe.cli.capture_namespace", return_value=result),
            contextlib.redirect_stdout(output),
        ):
            self.assertEqual(
                main(
                    [
                        "gate1",
                        "capture",
                        "--disk",
                        "nvme0n1",
                        "--run-dir",
                        "/tmp/unused",
                    ]
                ),
                0,
            )
        self.assertIn("not Gate 1 PASS", output.getvalue())

    def test_capture_retains_raw_artifacts_and_unknown_loss(self) -> None:
        """Simulate subprocess output; do not assert real attachment or loss safety."""
        plan = {
            "preflight_status": "eligible",
            "namespace": "nvme0n1",
            "logical_block_size": {"value": 4096},
            "bpftrace": {
                "executable": "/usr/bin/bpftrace",
                "version_output": "bpftrace v0.20.2",
            },
        }
        process = Mock(returncode=0)
        process.poll.side_effect = [None, 0, 0]
        counter_output = ""

        def launch(command: list[str], *, stdout: object, stderr: object) -> Mock:
            """Emit a simulated complete stream, never launch a privileged tool."""
            stdout.write("# READY,10\nS,11,7,1,2,2,0,16,3\nC,12,7,1,2,0,4\n# END,20\n")
            stdout.write(counter_output)
            stdout.flush()
            return process

        with (
            tempfile.TemporaryDirectory() as directory,
            patch("kvprobe.nvme_capture.os.geteuid", return_value=0),
            patch("kvprobe.nvme_capture._trusted_path"),
            patch(
                "kvprobe.nvme_capture.shutil.which", return_value="/usr/bin/bpftrace"
            ),
            patch("kvprobe.nvme_capture.build_trace_plan", return_value=plan),
            patch("kvprobe.nvme_capture.subprocess.Popen", side_effect=launch),
            patch("kvprobe.nvme_capture.time.sleep"),
        ):
            run = Path(directory) / "new-run"
            result = capture_namespace("nvme0n1", run, 1)
            self.assertTrue((run / "ready.json").exists())
            self.assertEqual(json.loads((run / "manifest.json").read_text()), result)
            self.assertEqual(result["controller_id"], 7)
            self.assertIsNone(result["loss_events"])
            self.assertEqual(result["serving_verification"], "not_assessed")
            self.assertEqual((run / "bpftrace.err").read_text(), "")
            self.assertTrue((run / "bpftrace.out").exists())
            self.assertEqual(result["collector_version"], "0.3.0-experimental")
            self.assertEqual(result["trace_contract_version"], "0.2.0")
            self.assertEqual(result["cpu_missing_records"], 0)
            counter_output = "@kvprobe_setup_count: 2\n@kvprobe_completion_count: 2\n"
            process.poll.side_effect = [None, 0, 0]
            counted = Path(directory) / "counted-run"
            counted_result = capture_namespace("nvme0n1", counted, 1)
            self.assertEqual(counted_result["loss_events"], 0)
            self.assertNotIn("@kvprobe", (counted / "trace.csv").read_text())
            self.assertIn("@kvprobe", (counted / "bpftrace.out").read_text())

    def test_analyze_cannot_overwrite_raw_stdout(self) -> None:
        """Raw transport output remains immutable through analyzer CLI options."""
        with tempfile.TemporaryDirectory() as directory:
            raw = Path(directory) / "bpftrace.out"
            raw.write_text("raw retained")
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(
                    main(["gate1", "analyze", directory, "--output", str(raw)]), 2
                )
            self.assertEqual(raw.read_text(), "raw retained")


if __name__ == "__main__":
    unittest.main()
