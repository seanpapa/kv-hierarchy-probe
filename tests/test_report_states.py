# SPDX-License-Identifier: Apache-2.0
"""Configuration-bound evidence separates failure, unsupported, and invalid runs."""

# Standard
import copy
import json
import subprocess
import sys
import unittest
from pathlib import Path
from typing import Any

# First Party
from kvprobe.report import EvidenceError, assess_evidence


ROOT = Path(__file__).parents[1]
EXAMPLES = ROOT / "examples" / "evidence"
PORTABILITY = ROOT / "tests" / "fixtures" / "portability"


def _load(name: str) -> dict[str, Any]:
    """Load one evidence example.

    Args:
        name: Example filename.

    Returns:
        Parsed evidence object.
    """
    return json.loads((EXAMPLES / name).read_text(encoding="utf-8"))


class EvidenceVerdictTests(unittest.TestCase):
    """Verify public evidence verdicts and refusal behavior."""

    def test_all_pass_allows_performance_claims(self) -> None:
        """Only five passing gates expose numeric performance."""
        assessment = assess_evidence(_load("pass-capabilities.json"))

        self.assertEqual(assessment["assessment_version"], "0.6.1")
        self.assertEqual(assessment["overall"], "PASS")
        self.assertEqual(assessment["performance_claims"], "allowed")
        self.assertEqual(
            [gate["status"] for gate in assessment["gates"]], ["PASS"] * 5
        )
        self.assertIn("performance", assessment)

    def test_unsupported_evidence_is_partial_not_failed(self) -> None:
        """Missing platform capability must remain distinct from a measured failure."""
        assessment = assess_evidence(_load("partial-unsupported.json"))

        self.assertEqual(assessment["overall"], "PARTIAL")
        self.assertEqual(assessment["gates"][0]["status"], "SKIP_UNSUPPORTED")
        self.assertEqual(assessment["gates"][4]["status"], "SKIP_UNSUPPORTED")
        self.assertEqual(assessment["performance_claims"], "suppressed")
        self.assertNotIn("performance", assessment)

    def test_measured_failure_takes_precedence_over_unsupported_gate(self) -> None:
        """A partial run with a measured failure must have an overall failure."""
        payload = copy.deepcopy(_load("partial-unsupported.json"))
        payload["evidence"]["capacity"]["target_tier_served"] = False

        assessment = assess_evidence(payload)

        self.assertEqual(assessment["gates"][0]["status"], "SKIP_UNSUPPORTED")
        self.assertEqual(assessment["gates"][3]["status"], "FAIL")
        self.assertEqual(assessment["overall"], "FAIL")

    def test_fatal_observation_invalidates_a_well_formed_run(self) -> None:
        """A captured allocation failure is evidence, not malformed JSON."""
        assessment = assess_evidence(_load("invalid-allocation.json"))

        self.assertEqual(assessment["overall"], "INVALID")
        self.assertEqual(
            [gate["status"] for gate in assessment["gates"]], ["INVALID"] * 5
        )
        self.assertEqual(assessment["performance_claims"], "suppressed")

    def test_direct_attribution_requires_expected_byte_match(self) -> None:
        """Positive bytes outside the declared tolerance must fail attribution."""
        payload = copy.deepcopy(_load("pass-capabilities.json"))
        request = payload["evidence"]["attribution"]["requests"][0]
        request["observed_bytes"] = request["expected_bytes"] * 2

        assessment = assess_evidence(payload)

        self.assertEqual(assessment["gates"][0]["status"], "FAIL")
        self.assertEqual(assessment["overall"], "FAIL")
        self.assertNotIn("performance", assessment)

    def test_unknown_artifact_reference_is_malformed(self) -> None:
        """Attribution cannot cite evidence absent from provenance."""
        payload = copy.deepcopy(_load("pass-capabilities.json"))
        attribution = payload["evidence"]["attribution"]["requests"][0][
            "attribution"
        ]
        attribution["artifact_refs"] = ["missing-artifact"]

        with self.assertRaises(EvidenceError):
            assess_evidence(payload)

    def test_cli_uses_existing_exit_code_for_partial_and_invalid(self) -> None:
        """Valid non-passing reports return one while malformed input returns two."""
        for name in (
            "partial-unsupported.json",
            "invalid-allocation.json",
        ):
            with self.subTest(name=name):
                result = subprocess.run(
                    [sys.executable, "-m", "kvprobe", "report", str(EXAMPLES / name)],
                    cwd=ROOT,
                    capture_output=True,
                    check=False,
                    text=True,
                )
                self.assertEqual(result.returncode, 1, result.stderr)

    def test_examples_preserve_captured_portability_outcomes(self) -> None:
        """Reporter examples must stay tied to the sanitized baseline cases."""
        cases = (
            (
                "discrete_native_gds_pass.json",
                "pass-capabilities.json",
                "overall",
                "PASS",
            ),
            (
                "aggregate_cgroup_attribution.json",
                "partial-unsupported.json",
                "gate_1",
                "SKIP_UNSUPPORTED",
            ),
            (
                "allocation_failure.json",
                "invalid-allocation.json",
                "overall",
                "INVALID",
            ),
        )
        for fixture_name, example_name, scope, expected in cases:
            with self.subTest(fixture=fixture_name):
                fixture = json.loads(
                    (PORTABILITY / fixture_name).read_text(encoding="utf-8")
                )
                self.assertEqual(fixture["baseline"]["target_gate_status"], expected)
                assessment = assess_evidence(_load(example_name))
                actual = (
                    assessment["gates"][0]["status"]
                    if scope == "gate_1"
                    else assessment["overall"]
                )
                self.assertEqual(actual, expected)

    def test_v0_1_is_refused_with_a_migration_path(self) -> None:
        """A removed version must name its replacement, not just be rejected."""
        with self.assertRaises(EvidenceError) as caught:
            assess_evidence({"schema_version": "0.1.0", "run_id": "legacy"})

        message = str(caught.exception)
        self.assertIn("unsupported", message)
        self.assertIn("kvprobe ingest", message)


if __name__ == "__main__":
    unittest.main()
