# SPDX-License-Identifier: Apache-2.0
"""Delivery, timestamp and command-causality regression contracts."""

# Standard
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

# First Party
from kvprobe.nvme_trace import TraceError, analyze_trace


class NvmeTraceOrderTests(unittest.TestCase):
    """Test ordering through the public analyzer without repairing input."""

    def setUp(self) -> None:
        """Create a synthetic capture and one retrieval interval."""
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.run_dir = Path(directory.name)
        manifest = {
            "trace_contract_version": "0.2.0",
            "namespace": "nvme0n1",
            "controller_id": 0,
            "logical_block_size": 4096,
            "clock": "CLOCK_MONOTONIC",
            "capture_start_ns": 0,
            "capture_end_ns": 1000,
            "capture_ready": True,
            "capture_exit_code": 0,
            "loss_events": 0,
        }
        (self.run_dir / "manifest.json").write_text(json.dumps(manifest))
        (self.run_dir / "phases.csv").write_text(
            "request_id,phase,start_ns,end_ns,expected_read_bytes\n"
            "r1,retrieve,100,900,8192\n"
        )
        (self.run_dir / "bpftrace.err").write_text("")

    def observe(self, records: str) -> dict:
        """Analyze preserved records twice and verify their input digest."""
        path = self.run_dir / "trace.csv"
        path.write_text(records)
        original = path.read_bytes()
        result = analyze_trace(self.run_dir)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(
            result["input_sha256"]["trace.csv"], hashlib.sha256(original).hexdigest()
        )
        self.assertEqual(analyze_trace(self.run_dir), result)
        self.assertEqual(result["serving_verification"], "not_assessed")
        return result

    def test_independent_timestamp_inversion_remains_incomplete(self) -> None:
        """Even a one-nanosecond inversion cannot be waived by exact bytes."""
        for delta in (1, 20, 120):
            with self.subTest(delta=delta):
                result = self.observe(
                    f"S,300,0,1,7,2,100,1,0\nS,{300 - delta},0,1,8,2,200,1,0\n"
                    "C,400,0,1,7,0,0\nC,410,0,1,8,0,0\n"
                )
                self.assertEqual(
                    result["quality"],
                    {"status": "incomplete", "reasons": ["event_order_not_monotonic"]},
                )
                self.assertEqual(
                    result["phases"][0]["successful_read_to_expected_ratio"], 1
                )

    def test_equal_timestamp_preserves_record_order(self) -> None:
        """Equal timestamps allow an already ordered setup/completion pair."""
        result = self.observe("S,200,0,1,7,2,100,2,0\nC,200,0,1,7,0,0\n")
        self.assertEqual(result["quality"]["status"], "usable")
        self.assertEqual(result["host_command_lifetime_ns"]["max"], 0)

    def test_completion_delivered_first_is_not_repaired(self) -> None:
        """Sorting must not fabricate a pair from a completion delivered first."""
        result = self.observe("C,300,0,1,7,0,0\nS,200,0,1,7,2,100,2,0\n")
        self.assertEqual(result["phases"][0]["successful_completed_read_bytes"], 0)
        self.assertIn("event_order_not_monotonic", result["quality"]["reasons"])

    def test_negative_command_lifetime_is_not_success(self) -> None:
        """A completion timestamp before its setup cannot contribute success."""
        result = self.observe("S,200,0,1,7,2,100,2,0\nC,199,0,1,7,0,0\n")
        self.assertIn("negative_command_lifetime", result["quality"]["reasons"])
        self.assertEqual(result["host_command_lifetime_ns"]["count"], 0)
        self.assertEqual(result["phases"][0]["successful_completed_read_bytes"], 0)

    def test_timestamp_overlap_on_reused_id_cannot_be_certified(self) -> None:
        """Delivered ID reuse with overlapping timestamps remains incomplete."""
        result = self.observe(
            "S,200,0,1,7,2,100,1,0\nC,300,0,1,7,0,0\nS,250,0,1,7,2,200,1,0\nC,350,0,1,7,0,0\n"
        )
        self.assertIn("event_order_not_monotonic", result["quality"]["reasons"])
        self.assertEqual(result["quality"]["status"], "incomplete")

    def test_same_id_on_distinct_queues_is_independent(self) -> None:
        """Queue identity separates equal command IDs."""
        result = self.observe(
            "S,200,0,1,7,2,100,1,0\nS,210,0,2,7,2,200,1,0\nC,220,0,2,7,0,0\nC,230,0,1,7,0,0\n"
        )
        self.assertEqual(result["quality"]["status"], "usable")
        self.assertEqual(result["host_command_lifetime_ns"]["count"], 2)

    def test_outside_phase_inversion_blocks_whole_capture(self) -> None:
        """An exact retrieval does not cancel an inversion outside its window."""
        result = self.observe(
            "S,20,0,1,8,1,100,1,0\nS,19,0,1,9,1,200,1,0\n"
            "C,30,0,1,8,0,0\nC,40,0,1,9,0,0\n"
            "S,200,0,1,7,2,100,2,0\nC,300,0,1,7,0,0\n"
        )
        self.assertEqual(result["phases"][0]["successful_read_to_expected_ratio"], 1)
        self.assertEqual(result["quality"]["status"], "incomplete")

    def test_equal_timestamp_does_not_resolve_duplicate_setup(self) -> None:
        """Timestamp ties do not disambiguate two pending uses of one ID."""
        result = self.observe(
            "S,200,0,1,7,2,100,1,0\nS,200,0,1,7,2,200,1,0\nC,300,0,1,7,0,0\n"
        )
        self.assertIn("ambiguous_command_id_reuse", result["quality"]["reasons"])
        self.assertEqual(result["phases"][0]["successful_completed_read_bytes"], 0)

    def test_order_diagnostics_distinguish_adjacent_and_high_water(self) -> None:
        """A partial timestamp recovery may still lag behind the high water."""
        result = self.observe(
            "# retained comment\nS,300,0,1,7,2,100,1,0\n"
            "S,200,0,1,8,2,200,1,0\nC,250,0,1,8,0,0\nC,400,0,1,7,0,0\n"
        )
        ordering = result["ordering"]
        self.assertEqual(ordering["adjacent_inversions"], 1)
        self.assertEqual(ordering["max_adjacent_backward_ns"], 100)
        self.assertEqual(ordering["max_high_water_lag_ns"], 100)
        self.assertEqual(ordering["examples"][0]["line_number"], 3)
        self.assertEqual(ordering["cpu_provenance"], "recorded")
        self.assertFalse(ordering["reordered"])

    def test_order_examples_are_bounded_without_losing_count(self) -> None:
        """Only the first sixteen examples are stored, not the whole trace."""
        result = self.observe(
            "".join(f"S,{300 - index},0,1,{index},2,100,1,0\n" for index in range(25))
        )
        ordering = result["ordering"]
        self.assertEqual(ordering["adjacent_inversions"], 24)
        self.assertEqual(len(ordering["examples"]), 16)
        self.assertTrue(ordering["examples_truncated"])
        self.assertEqual(ordering["max_high_water_lag_ns"], 24)

    def test_ordered_empty_and_equal_timestamp_diagnostics(self) -> None:
        """No inversions or lag are invented for empty or tied records."""
        for records in ("", "S,200,0,1,7,2,100,2,0\nC,200,0,1,7,0,0\n"):
            with self.subTest(records=records):
                result = self.observe(records)
                self.assertEqual(result["trace_observation_version"], "0.2.0")
                self.assertEqual(result["ordering"]["adjacent_inversions"], 0)
                self.assertEqual(result["ordering"]["max_high_water_lag_ns"], 0)
                self.assertEqual(result["ordering"]["examples"], [])

    def enable_cpu_contract(self) -> None:
        """Select the explicitly versioned CPU-bearing synthetic contract."""
        path = self.run_dir / "manifest.json"
        manifest = json.loads(path.read_text())
        manifest["trace_contract_version"] = "0.2.0"
        path.write_text(json.dumps(manifest))

    def test_cross_cpu_inversion_remains_incomplete(self) -> None:
        """Different CPUs identify delivery context, not a quality waiver."""
        self.enable_cpu_contract()
        result = self.observe(
            "S,300,0,1,7,2,100,1,3\nS,200,0,1,8,2,200,1,4\n"
            "C,400,0,1,7,0,3\nC,410,0,1,8,0,4\n"
        )
        self.assertEqual(result["ordering"]["adjacent_cross_cpu_inversions"], 1)
        self.assertEqual(result["ordering"]["per_cpu_inversions"], 0)
        self.assertEqual(result["ordering"]["observed_cpu_ids"], [3, 4])
        self.assertEqual(result["quality"]["status"], "incomplete")

    def test_per_cpu_inversion_across_other_cpu_record(self) -> None:
        """Per-CPU order must be checked beyond adjacent raw records."""
        self.enable_cpu_contract()
        result = self.observe(
            "S,300,0,1,7,2,100,1,3\nS,100,0,1,8,2,200,1,4\n"
            "C,200,0,1,7,0,3\nC,410,0,1,8,0,4\n"
        )
        self.assertEqual(result["ordering"]["per_cpu_inversions"], 1)
        self.assertEqual(result["quality"]["status"], "incomplete")

    def test_same_cpu_inversion_is_separate_diagnostic(self) -> None:
        """A same-CPU backward step is not labelled cross-CPU delivery."""
        self.enable_cpu_contract()
        result = self.observe("S,300,0,1,7,2,100,2,3\nC,200,0,1,7,0,3\n")
        self.assertEqual(result["ordering"]["adjacent_same_cpu_inversions"], 1)
        self.assertEqual(result["ordering"]["adjacent_cross_cpu_inversions"], 0)

    def test_cpu_contract_rejects_missing_extra_or_unbounded_cpu(self) -> None:
        """Record width is strict by version, and CPU state is bounded."""
        self.enable_cpu_contract()
        for records in (
            "S,200,0,1,7,2,100,2\n",
            "S,200,0,1,7,2,100,2,3,4\n",
            "S,200,0,1,7,2,100,2,65536\n",
            "".join(f"S,200,0,1,{cpu},2,100,1,{cpu}\n" for cpu in range(4097)),
        ):
            with self.subTest(records=records[:60]), self.assertRaises(TraceError):
                self.observe(records)
