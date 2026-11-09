# SPDX-License-Identifier: Apache-2.0

"""A run from a stack without reference scripts must still expose a phantom tier.

The fixtures under ``fixtures/external/block-tier`` are context runs shaped the
way docs/EXTERNAL-RUNS.md asks another serving stack to record them — here a
generic disk tier that stores fixed-size blocks and is observed through a
GDS byte counter. They are synthetic: they fix the contract, not a measurement,
and they describe no particular framework.
"""

# Standard
from pathlib import Path
import unittest

# First Party
from kvprobe.ingest import IngestError, ingest_files
from kvprobe.report import assess_evidence
from test_schema_contract import _HAS_JSONSCHEMA, _validator

FIXTURES = Path(__file__).parent / "fixtures" / "external" / "block-tier"
CONFIGURATION = FIXTURES / "configuration.json"


def _gate_1(run_name: str) -> dict[str, object]:
    """Ingest one fixture run and return its Gate 1 verdict."""
    document = ingest_files([FIXTURES / run_name], CONFIGURATION, run_id=run_name)
    assessment = assess_evidence(document)
    return next(gate for gate in assessment["gates"] if gate["id"] == 1)


class ExternalBlockTierRuns(unittest.TestCase):
    def test_served_run_matches_bytes_but_stays_corroborating(self) -> None:
        """Host-wide nvidia-fs bytes agree, but an unfiltered counter leaves Gate 1 unproven."""
        gate = _gate_1("served.json")
        self.assertEqual(gate["status"], "UNPROVEN")
        self.assertIn("3/3 target-tier requests match expected bytes", gate["reason"])
        self.assertIn("0/3 moved no bytes from the tier", gate["reason"])

    def test_empty_tier_with_counter_derived_gate_is_refused(self) -> None:
        """No bytes and a failed tier gate: the adapter refuses to guess."""
        with self.assertRaisesRegex(IngestError, "phantom tier"):
            ingest_files(
                [FIXTURES / "phantom-no-writes.json"], CONFIGURATION, run_id="phantom"
            )

    def test_framework_reported_hit_with_no_bytes_is_named(self) -> None:
        """A framework-claimed hit that read nothing is counted, not passed."""
        gate = _gate_1("phantom-framework-hit.json")
        self.assertEqual(gate["status"], "FAIL")
        self.assertIn("0/3 target-tier requests match expected bytes", gate["reason"])
        self.assertIn("3/3 moved no bytes from the tier", gate["reason"])

    def test_partial_read_matches_neither_expectation_nor_zero(self) -> None:
        gate = _gate_1("partial.json")
        self.assertEqual(gate["status"], "FAIL")
        self.assertIn("0/3 target-tier requests match expected bytes", gate["reason"])
        self.assertIn("0/3 moved no bytes from the tier", gate["reason"])

    def test_warmup_that_missed_the_tier_is_noted_not_judged(self) -> None:
        """A first reuse recomputed during warm-up shows in the reason only."""
        gate = _gate_1("second-use-admission.json")
        self.assertEqual(gate["status"], "UNPROVEN")
        self.assertIn("3/3 target-tier requests match expected bytes", gate["reason"])
        self.assertIn("1/1 warm-up requests moved no bytes", gate["reason"])

    def test_served_warmup_adds_no_note(self) -> None:
        document = ingest_files([FIXTURES / "served.json"], CONFIGURATION, run_id="served")
        warmups = document["evidence"]["attribution"]["warmup_observations"]
        self.assertEqual((warmups["requests"], warmups["moved_no_bytes"]), (1, 0))
        self.assertNotIn("warm-up", _gate_1("served.json")["reason"])


@unittest.skipUnless(_HAS_JSONSCHEMA, "jsonschema is a test-only dependency")
class ExternalRunSchema(unittest.TestCase):
    def test_ingested_external_runs_satisfy_the_schema(self) -> None:
        validator = _validator("0.3.1")
        for name in ("served.json", "second-use-admission.json", "partial.json"):
            with self.subTest(run=name):
                document = ingest_files([FIXTURES / name], CONFIGURATION, run_id=name)
                errors = [error.message for error in validator.iter_errors(document)]
                self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
