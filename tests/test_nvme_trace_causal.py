# SPDX-License-Identifier: Apache-2.0
"""Public opt-in causal consistency contract; synthetic input, no hardware."""

# Standard
import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path

# First Party
from kvprobe.cli import main
from kvprobe.nvme_capture import assess_capture_loss, summarize_capture
from kvprobe.nvme_trace import TraceError, analyze_trace


class CausalTraceTests(unittest.TestCase):
    """Exercise policy selection, strict compatibility and fail-closed rules."""

    def setUp(self) -> None:
        """Prepare a CPU-bearing capture with one phase and recorded assertions."""
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.run = Path(directory.name)
        self.manifest = {
            "trace_contract_version": "0.2.0",
            "namespace": "nvme0n1",
            "controller_id": 0,
            "logical_block_size": 4096,
            "clock": "CLOCK_MONOTONIC",
            "capture_start_ns": 0,
            "capture_end_ns": 1000,
            "capture_ready": True,
            "capture_exit_code": 0,
            "process_returncode": 0,
            "stop_reason": None,
            "loss_events": 0,
        }
        (self.run / "phases.csv").write_text(
            "request_id,phase,start_ns,end_ns,expected_read_bytes\n"
            "r1,retrieve,100,900,8192\n"
        )
        (self.run / "bpftrace.err").write_text("")

    def write_capture(self, records: str) -> None:
        """Persist matching raw/normalized records and scoped counter assertions."""
        lines = records.splitlines()
        setups = sum(line.startswith("S,") for line in lines)
        completions = sum(line.startswith("C,") for line in lines)
        raw = self.run / "bpftrace.out"
        raw.write_text(
            "# READY,0\n" + records + "# END,1000\n"
            f"@kvprobe_setup_count: {setups + 1}\n"
            f"@kvprobe_completion_count: {completions + 1}\n"
        )
        (self.run / "trace.csv").write_text(records)
        self.manifest["raw_stdout_sha256"] = hashlib.sha256(
            raw.read_bytes()
        ).hexdigest()
        self.manifest["loss_accounting"] = assess_capture_loss(
            summarize_capture(raw),
            self.run / "bpftrace.err",
            process_returncode=0,
            stop_reason=None,
        )
        self.save_manifest()

    def save_manifest(self) -> None:
        """Persist modified assertions without altering raw observations."""
        (self.run / "manifest.json").write_text(json.dumps(self.manifest))

    def observe(self) -> dict:
        """Assert deterministic analysis leaves every input byte unchanged."""
        before = {path.name: path.read_bytes() for path in self.run.iterdir()}
        result = analyze_trace(self.run, ordering_policy="causal")
        self.assertEqual(result, analyze_trace(self.run, ordering_policy="causal"))
        self.assertEqual(
            before, {path.name: path.read_bytes() for path in self.run.iterdir()}
        )
        self.assertEqual(result["serving_verification"], "not_assessed")
        self.assertFalse(result["ordering"]["reordered"])
        self.assertEqual(result["trace_observation_version"], "0.3.0")
        return result

    def test_independent_cross_cpu_inversion_is_diagnostic(self) -> None:
        """Only explicit causal selection accepts consistent independent commands."""
        self.write_capture(
            "S,300,0,1,7,2,100,1,3\nS,200,0,1,8,2,200,1,4\n"
            "C,400,0,1,7,0,3\nC,410,0,1,8,0,4\n"
        )
        strict = analyze_trace(self.run)
        result = self.observe()
        self.assertEqual(strict["quality"]["status"], "incomplete")
        self.assertEqual(result["quality"], {"status": "usable", "reasons": []})
        for field in ("phases", "totals", "pairing", "input_sha256"):
            self.assertEqual(strict[field], result[field])
        self.assertEqual(result["ordering"]["adjacent_cross_cpu_inversions"], 1)

    def test_same_cpu_and_subsequence_inversions_block(self) -> None:
        """Per-CPU order is checked across intervening records from other CPUs."""
        for records in (
            "S,300,0,1,7,2,100,2,3\nC,200,0,1,7,0,3\n",
            "S,300,0,1,7,2,100,1,3\nS,100,0,1,8,2,200,1,4\n"
            "C,200,0,1,7,0,3\nC,410,0,1,8,0,4\n",
        ):
            with self.subTest(records=records):
                self.write_capture(records)
                self.assertIn(
                    "per_cpu_event_order_not_monotonic",
                    self.observe()["quality"]["reasons"],
                )

    def test_completion_first_and_negative_lifetime_block(self) -> None:
        """Cross-CPU identity cannot excuse broken command causality."""
        for records, reason in (
            ("C,300,0,1,7,0,3\nS,200,0,1,7,2,100,2,4\n", "unmatched_completion"),
            ("S,300,0,1,7,2,100,2,3\nC,200,0,1,7,0,4\n", "negative_command_lifetime"),
        ):
            with self.subTest(reason=reason):
                self.write_capture(records)
                result = self.observe()
                self.assertIn(reason, result["quality"]["reasons"])
                self.assertEqual(result["totals"]["successful_completed_read_bytes"], 0)

    def test_reuse_timestamp_overlap_blocks(self) -> None:
        """History must catch temporal overlap even after a key was completed."""
        self.write_capture(
            "S,200,0,1,7,2,100,1,3\nC,300,0,1,7,0,3\n"
            "S,250,0,1,7,2,200,1,4\nC,350,0,1,7,0,4\n"
        )
        self.assertIn(
            "command_id_reuse_timestamp_overlap", self.observe()["quality"]["reasons"]
        )

    def test_reuse_history_survives_intervening_command(self) -> None:
        """A non-adjacent overlapping reuse cannot hide behind another key."""
        self.write_capture(
            "S,200,0,1,7,2,100,1,3\nC,300,0,1,7,0,3\n"
            "S,220,0,1,8,2,300,1,5\nS,250,0,1,7,2,200,1,4\n"
            "C,350,0,1,7,0,4\nC,400,0,1,8,0,5\n"
        )
        result = self.observe()
        self.assertEqual(result["quality"]["reasons"], ["command_id_reuse_timestamp_overlap"])

    def test_equal_timestamp_reuse_and_distinct_queue_are_valid(self) -> None:
        """Ties retain delivered completion-before-reuse; queues separate keys."""
        self.write_capture(
            "S,200,0,1,7,2,100,1,3\nC,200,0,1,7,0,4\n"
            "S,200,0,1,7,2,200,1,3\nC,200,0,1,7,0,4\n"
        )
        self.assertEqual(self.observe()["quality"]["status"], "usable")
        self.write_capture(
            "S,300,0,1,7,2,100,1,3\nS,200,0,2,7,2,200,1,4\n"
            "C,400,0,1,7,0,3\nC,410,0,2,7,0,4\n"
        )
        self.assertEqual(self.observe()["quality"]["status"], "usable")

    def test_pending_collision_and_failed_status_block(self) -> None:
        """Causal policy does not weaken existing pairing/status checks."""
        for records, reason in (
            (
                "S,200,0,1,7,2,100,1,3\nS,210,0,1,7,2,200,1,4\nC,300,0,1,7,0,3\n",
                "ambiguous_command_id_reuse",
            ),
            ("S,200,0,1,7,2,100,2,3\nC,300,0,1,7,1,4\n", "nonzero_completion_status"),
        ):
            with self.subTest(reason=reason):
                self.write_capture(records)
                self.assertIn(reason, self.observe()["quality"]["reasons"])

    def test_raw_and_loss_assertions_are_cross_checked(self) -> None:
        """Edited normalized events and forged loss assertions cannot pass."""
        self.write_capture("S,200,0,1,7,2,100,2,3\nC,300,0,1,7,0,4\n")
        (self.run / "trace.csv").write_text("S,200,0,1,7,2,999,2,3\nC,300,0,1,7,0,4\n")
        self.assertIn("causal_raw_trace_mismatch", self.observe()["quality"]["reasons"])
        self.manifest["loss_accounting"] = {}
        self.save_manifest()
        self.assertIn(
            "causal_scoped_loss_unverified", self.observe()["quality"]["reasons"]
        )

    def test_missing_raw_and_unknown_loss_remain_incomplete(self) -> None:
        """No raw evidence or unknown manifest loss must not be waived."""
        self.write_capture("S,200,0,1,7,2,100,2,3\nC,300,0,1,7,0,4\n")
        (self.run / "bpftrace.out").unlink()
        self.manifest["loss_events"] = None
        self.save_manifest()
        result = self.observe()
        self.assertIn("causal_raw_evidence_missing", result["quality"]["reasons"])
        self.assertIn("loss_unknown", result["quality"]["reasons"])

    def test_legacy_or_unknown_policy_is_refused(self) -> None:
        """Opt-in cannot fabricate CPU provenance for historical records."""
        self.write_capture("")
        self.manifest["trace_contract_version"] = "0.1.0"
        self.save_manifest()
        with self.assertRaises(TraceError):
            self.observe()
        with self.assertRaises(TraceError):
            analyze_trace(self.run, ordering_policy="sort")

    def test_capture_assertion_failures_are_not_waived(self) -> None:
        """Opt-in still rejects missing readiness, loss, interruption and clock."""
        for field, value, reason in (
            ("capture_ready", False, "capture_not_ready"),
            ("loss_events", 1, "events_lost"),
            ("process_returncode", 1, "causal_scoped_loss_unverified"),
            ("stop_reason", "timeout", "causal_scoped_loss_unverified"),
            ("raw_stdout_sha256", "wrong", "causal_raw_digest_mismatch"),
        ):
            with self.subTest(field=field):
                self.setUp()
                self.write_capture("S,200,0,1,7,2,100,2,3\nC,300,0,1,7,0,4\n")
                self.manifest[field] = value
                self.save_manifest()
                self.assertIn(reason, self.observe()["quality"]["reasons"])
        self.manifest["clock"] = "CLOCK_BOOTTIME"
        self.save_manifest()
        with self.assertRaises(TraceError):
            self.observe()

    def test_phase_boundary_and_overlap_remain_incomplete(self) -> None:
        """Ordering policy does not weaken interval quality checks."""
        self.write_capture("S,200,0,1,7,2,100,2,3\nC,900,0,1,7,0,4\n")
        self.assertIn("phase_boundary_crossing", self.observe()["quality"]["reasons"])
        (self.run / "phases.csv").write_text(
            "request_id,phase,start_ns,end_ns\nr1,a,100,900\nr2,b,200,800\n"
        )
        self.assertIn("overlapping_phases", self.observe()["quality"]["reasons"])

    def test_history_limit_does_not_evict_validation_state(self) -> None:
        """Completed keys remain tracked; exhaustion raises instead of certifying."""
        self.write_capture(
            "".join(
                f"S,200,0,{1 + index // 65536},{index % 65536},2,100,1,3\n"
                f"C,200,0,{1 + index // 65536},{index % 65536},0,4\n"
                for index in range(65537)
            )
        )
        with self.assertRaisesRegex(TraceError, "command history limit"):
            self.observe()

    def test_cli_explicit_selection_and_separate_output(self) -> None:
        """CLI strict remains default; causal output names its policy and version."""
        self.write_capture(
            "S,300,0,1,7,2,100,1,3\nS,200,0,1,8,2,200,1,4\n"
            "C,400,0,1,7,0,3\nC,410,0,1,8,0,4\n"
        )
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["gate1", "analyze", str(self.run)]), 1)
            path = self.run / "causal-observation.json"
            self.assertEqual(
                main(
                    [
                        "gate1",
                        "analyze",
                        str(self.run),
                        "--ordering-policy",
                        "causal",
                        "--output",
                        str(path),
                    ]
                ),
                0,
            )
        result = json.loads(path.read_text())
        self.assertEqual(result["ordering"]["policy"], "retained_delivery_causal_v1")
        self.assertEqual(result["serving_verification"], "not_assessed")
        original = path.read_bytes()
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(
                main(
                    [
                        "gate1",
                        "analyze",
                        str(self.run),
                        "--ordering-policy",
                        "causal",
                        "--output",
                        str(path),
                    ]
                ),
                2,
            )
        self.assertEqual(path.read_bytes(), original)
