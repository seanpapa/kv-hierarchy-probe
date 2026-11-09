# SPDX-License-Identifier: Apache-2.0
"""Synthetic trace contract tests; no hardware, root or serving stack needed."""

# Standard
import json
import tempfile
import unittest
from pathlib import Path

# First Party
from kvprobe.cli import main
from kvprobe.nvme_trace import TraceError, analyze_trace


class NvmeTraceTest(unittest.TestCase):
    """Exercise the public offline analyzer and CLI contract."""

    def setUp(self) -> None:
        """Prepare a synthetic capture with a known clock and namespace."""
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.run_dir = Path(self.directory.name)
        self.manifest = {
            "trace_contract_version": "0.2.0",
            "namespace": "nvme0n1",
            "controller_id": 0,
            "logical_block_size": 4096,
            "clock": "CLOCK_MONOTONIC",
            "capture_start_ns": 0,
            "capture_end_ns": 100,
            "capture_ready": True,
            "capture_exit_code": 0,
            "loss_events": 0,
        }
        self.write_manifest()
        (self.run_dir / "phases.csv").write_text(
            "request_id,phase,start_ns,end_ns,expected_read_bytes\n"
            "r1,retrieve,10,50,8192\n",
            encoding="utf-8",
        )
        (self.run_dir / "trace.csv").write_text(
            "S,20,0,1,7,2,100,2,0\nC,30,0,1,7,0,0\n", encoding="utf-8"
        )
        (self.run_dir / "bpftrace.err").write_text("", encoding="utf-8")

    def write_manifest(self) -> None:
        """Persist operator-supplied synthetic capture assertions."""
        (self.run_dir / "manifest.json").write_text(
            json.dumps(self.manifest), encoding="utf-8"
        )

    def trace(self, content: str) -> dict:
        """Replace events and analyze through the public API."""
        (self.run_dir / "trace.csv").write_text(content, encoding="utf-8")
        return analyze_trace(self.run_dir)

    def test_successful_read_is_not_serving_pass(self) -> None:
        """Exact expected bytes must not create a serving verdict."""
        result = analyze_trace(self.run_dir)
        self.assertEqual(result["quality"]["status"], "usable")
        self.assertEqual(result["phases"][0]["successful_completed_read_bytes"], 8192)
        self.assertEqual(result["phases"][0]["successful_read_to_expected_ratio"], 1)
        self.assertEqual(result["serving_verification"], "not_assessed")
        self.assertNotIn("confidence", result)
        self.assertEqual(result["host_command_lifetime_ns"]["max"], 10)

    def test_zero_read_requires_complete_capture_assertions(self) -> None:
        """Zero events are usable only under complete capture assertions."""
        self.assertEqual(self.trace("")["quality"]["status"], "usable")
        self.manifest["loss_events"] = None
        self.write_manifest()
        self.assertIn("loss_unknown", analyze_trace(self.run_dir)["quality"]["reasons"])

    def test_loss_stderr_and_missing_stderr_are_not_zero(self) -> None:
        """Missing diagnostics and diagnostics requiring review remain visible."""
        (self.run_dir / "bpftrace.err").write_text("Lost 2 events\n", encoding="utf-8")
        self.assertEqual(analyze_trace(self.run_dir)["quality"]["status"], "incomplete")
        (self.run_dir / "bpftrace.err").unlink()
        self.assertIn(
            "capture_stderr_missing", analyze_trace(self.run_dir)["quality"]["reasons"]
        )

    def test_incomplete_capture_assertions(self) -> None:
        """Unknown exit, lost events and missing readiness invalidate quality."""
        self.manifest.update(capture_ready=False, capture_exit_code=None, loss_events=3)
        self.write_manifest()
        reasons = analyze_trace(self.run_dir)["quality"]["reasons"]
        for reason in (
            "capture_not_ready",
            "capture_exit_unknown_or_nonzero",
            "events_lost",
        ):
            self.assertIn(reason, reasons)

    def test_failed_io_keeps_submission_but_not_success_bytes(self) -> None:
        """Failed commands retain submission bytes but not successful bytes."""
        result = self.trace("S,20,0,1,7,1,100,2,0\nC,30,0,1,7,4,0\n")
        self.assertEqual(result["totals"]["submitted_write_bytes"], 8192)
        self.assertEqual(result["totals"]["successful_completed_write_bytes"], 0)
        self.assertEqual(result["totals"]["failed_commands"], 1)

    def test_unmatched_setup_and_completion(self) -> None:
        """Unpaired events must not become successful transfer evidence."""
        result = self.trace("S,20,0,1,7,2,100,2,0\nC,30,0,1,8,0,0\n")
        self.assertEqual(result["pairing"]["unmatched_setups"], 1)
        self.assertEqual(result["pairing"]["unmatched_completions"], 1)
        self.assertEqual(result["quality"]["status"], "incomplete")

    def test_safe_id_reuse_and_ambiguous_reuse(self) -> None:
        """Completed IDs may be reused, whereas overlapping reuse is ambiguous."""
        result = self.trace(
            "S,20,0,1,7,2,100,2,0\nC,21,0,1,7,0,0\nS,22,0,1,7,2,200,2,0\nC,23,0,1,7,0,0\n"
        )
        self.assertEqual(result["totals"]["successful_completed_read_bytes"], 16384)
        result = self.trace("S,20,0,1,7,2,100,2,0\nS,21,0,1,7,2,200,2,0\nC,23,0,1,7,0,0\n")
        self.assertEqual(result["totals"]["successful_completed_read_bytes"], 0)
        self.assertEqual(result["pairing"]["ambiguous_id_reuses"], 1)

    def test_half_open_phase_and_crossing(self) -> None:
        """End-boundary setup is outside, and crossing completions are flagged."""
        result = self.trace(
            "S,20,0,1,7,2,100,2,0\nC,50,0,1,7,0,0\nS,50,0,1,7,2,100,2,0\nC,51,0,1,7,0,0\n"
        )
        self.assertEqual(result["phases"][0]["submitted_read_bytes"], 8192)
        self.assertEqual(result["outside_phases"]["submitted_read_bytes"], 8192)
        self.assertEqual(result["phases"][0]["boundary_crossing_commands"], 1)

    def test_overlapping_phases_are_flagged_not_unique_requests(self) -> None:
        """Overlapping labels must not duplicate global bytes or imply ownership."""
        with (self.run_dir / "phases.csv").open("a", encoding="utf-8") as stream:
            stream.write("r2,retrieve,15,40,8192\n")
        result = analyze_trace(self.run_dir)
        self.assertEqual(result["pairing"]["overlapping_phase_pairs"], 1)
        self.assertEqual(result["totals"]["submitted_read_bytes"], 8192)
        self.assertEqual(result["quality"]["status"], "incomplete")

    def test_command_entering_phase_stays_outside_but_is_flagged(self) -> None:
        """A command crossing into a phase keeps its original setup owner."""
        result = self.trace("S,5,0,1,7,2,100,2,0\nC,20,0,1,7,0,0\n")
        self.assertEqual(
            result["outside_phases"]["successful_completed_read_bytes"], 8192
        )
        self.assertEqual(result["phases"][0]["successful_completed_read_bytes"], 0)
        self.assertEqual(result["totals"]["boundary_crossing_commands"], 1)
        self.assertEqual(result["quality"]["status"], "incomplete")

    def test_pending_resource_limit_is_explicit(self) -> None:
        """Exceeding the documented pending limit fails rather than truncating."""
        with (self.run_dir / "trace.csv").open("w", encoding="utf-8") as stream:
            for cid in range(65536):
                stream.write(f"S,20,0,1,{cid},2,100,2,0\n")
            stream.write("S,20,0,2,0,2,100,2,0\n")
        with self.assertRaisesRegex(TraceError, "pending command limit"):
            analyze_trace(self.run_dir)

    def test_nonmonotonic_order_is_not_certified(self) -> None:
        """Out-of-order events are incomplete, not silently reordered."""
        result = self.trace("C,30,0,1,7,0,0\nS,20,0,1,7,2,100,2,0\n")
        self.assertIn("event_order_not_monotonic", result["quality"]["reasons"])

    def test_flush_does_not_use_rw_nlb_as_payload(self) -> None:
        """A non-read/write opcode does not inflate payload bytes."""
        result = self.trace("S,20,0,1,7,0,0,1,0\nC,30,0,1,7,0,0\n")
        self.assertEqual(result["totals"]["submitted_read_bytes"], 0)
        self.assertEqual(result["totals"]["submitted_write_bytes"], 0)

    def test_cpu_free_contract_is_refused_under_default_policy(self) -> None:
        """Strict analysis cannot invent CPU provenance for an old capture."""
        self.manifest["trace_contract_version"] = "0.1.0"
        self.write_manifest()
        with self.assertRaisesRegex(TraceError, "CPU-bearing 0.2.0"):
            analyze_trace(self.run_dir)

    def test_malformed_scope_lbs_clock_and_version_are_refused(self) -> None:
        """Invalid scope and geometry are refused without inferred defaults."""
        for key, value in (
            ("logical_block_size", 0),
            ("logical_block_size", True),
            ("clock", "CLOCK_REALTIME"),
            ("namespace", "nvme0n1p1"),
            ("trace_contract_version", "9"),
        ):
            with self.subTest(key=key, value=value):
                original = self.manifest[key]
                self.manifest[key] = value
                self.write_manifest()
                with self.assertRaises(TraceError):
                    analyze_trace(self.run_dir)
                self.manifest[key] = original
        self.write_manifest()
        for content in ("S,20\n", "S,20,1,1,7,2,100,2,0\n", "S,20,0,0,7,2,100,2,0\n"):
            with self.subTest(content=content), self.assertRaises(TraceError):
                self.trace(content)

    def test_bounded_records_and_phases(self) -> None:
        """Oversized records and excessive phase intervals fail explicitly."""
        with self.assertRaises(TraceError):
            self.trace("#" + "x" * 4096)
        (self.run_dir / "phases.csv").write_text(
            "request_id,phase,start_ns,end_ns\n" + "r,retrieve,10,50\n" * 257,
            encoding="utf-8",
        )
        with self.assertRaises(TraceError):
            analyze_trace(self.run_dir)

    def test_cli_exit_and_raw_input_protection(self) -> None:
        """CLI statuses describe quality, and output cannot overwrite raw input."""
        output = self.run_dir / "observation.json"
        self.assertEqual(
            main(["gate1", "analyze", str(self.run_dir), "--output", str(output)]), 0
        )
        self.assertEqual(
            json.loads(output.read_text())["serving_verification"], "not_assessed"
        )
        raw = self.run_dir / "trace.csv"
        before = raw.read_bytes()
        self.assertEqual(
            main(["gate1", "analyze", str(self.run_dir), "--output", str(raw)]), 2
        )
        self.assertEqual(raw.read_bytes(), before)
        (self.run_dir / "bpftrace.err").unlink()
        self.assertEqual(main(["gate1", "analyze", str(self.run_dir)]), 1)
        (self.run_dir / "manifest.json").unlink()
        self.assertEqual(main(["gate1", "analyze", str(self.run_dir)]), 2)
