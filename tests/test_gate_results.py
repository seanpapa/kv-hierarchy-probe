# SPDX-License-Identifier: Apache-2.0
"""A gate passes on a valid measurement; what it found is a separate result.

Assessment 0.5.0: retrieval slower than recompute, an admission that never
repays, a tier that was never needed and an envelope that ends are findings
of a valid measurement, not failures. A gate fails or is unproven only when
its measurement cannot be trusted.
"""

# Standard
import copy
import json
import unittest
from pathlib import Path
from typing import Any

# First Party
from kvprobe.report import EvidenceError, assess_evidence

EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "evidence"


def _pass() -> dict[str, Any]:
    """Return a copy of the hand-written all-pass example."""
    return copy.deepcopy(
        json.loads((EXAMPLES / "pass-all-attempts.json").read_text(encoding="utf-8"))
    )


def _successful_samples() -> dict[str, Any]:
    """Return a copy of the example whose Gate 3 keeps successful samples only."""
    return copy.deepcopy(
        json.loads(
            (EXAMPLES / "admission-successful-samples-only.json").read_text(
                encoding="utf-8"
            )
        )
    )


def _without_capacity_counts() -> dict[str, Any]:
    """Return the all-pass example with only the two Gate 4 booleans."""
    payload = _pass()
    for name in ("classification_counts", "overflow_classification_counts"):
        del payload["evidence"]["capacity"][name]
    return payload


def _gate(assessment: dict[str, Any], gate_id: int) -> dict[str, Any]:
    """Return one gate from an assessment."""
    return next(gate for gate in assessment["gates"] if gate["id"] == gate_id)


class GateResultTests(unittest.TestCase):
    """Verdicts judge the measurement; results describe the tier."""

    def test_slow_retrieval_is_a_result_not_a_failure(self) -> None:
        """Retrieval slower than recompute passes Gate 2 and says so."""
        payload = _pass()
        payload["evidence"]["break_even"]["retrieval_ttft_s"] = [0.9, 0.9, 0.9]
        payload["evidence"]["admission"]["realized_reuse_saving_s"] = -0.6
        assessment = assess_evidence(payload)
        self.assertEqual(_gate(assessment, 2)["status"], "PASS")
        self.assertIn("offloading efficiency is low", _gate(assessment, 2)["result"])
        self.assertEqual(_gate(assessment, 3)["status"], "PASS")
        self.assertIn("NOT_ECONOMICAL", _gate(assessment, 3)["result"])
        self.assertEqual(assessment["overall"], "PASS")

    def test_too_few_reuses_is_not_economical(self) -> None:
        """Expected reuses below the repayment point give a negative finding."""
        payload = _pass()
        payload["evidence"]["admission"]["expected_reuses"] = 0.1
        gate = _gate(assess_evidence(payload), 3)
        self.assertEqual(gate["status"], "PASS")
        self.assertIn("needs more reuses than the workload expects", gate["result"])

    def test_admission_reason_limits_the_write_counter_scope(self) -> None:
        """Gate 3 must not turn a host-wide write signal into device attribution."""
        gate = _gate(assess_evidence(_pass()), 3)
        self.assertIn("host-wide nvidia-fs activity", gate["reason"])
        self.assertIn("not target-device exclusivity", gate["reason"])
        self.assertNotIn("device-confirmed", gate["reason"])

    def test_admission_uses_all_attempts_including_fallback(self) -> None:
        """A classified fallback stays in repayment and does not fail the gate."""
        payload = _pass()
        payload["evidence"]["admission"].update(
            {
                "store_cost_s": 0.3,
                "expected_reuses": 2,
                "realized_reuse_saving_s": 0.1,
                "calculation_basis": "all_attempts",
                "attempt_counts": {
                    "attempted": 4,
                    "target_tier": 3,
                    "fallback": 1,
                    "store_unattributed": 0,
                    "unattributed": 0,
                },
            }
        )
        gate = _gate(assess_evidence(payload), 3)
        self.assertEqual(gate["status"], "PASS")
        self.assertIn("all 4 attempts included", gate["result"])
        self.assertIn("NOT_ECONOMICAL", gate["result"])

    def test_admission_unattributed_attempt_is_unproven(self) -> None:
        """An unclassified attempt blocks validity rather than disappearing."""
        payload = _pass()
        payload["evidence"]["admission"].update(
            {
                "realized_reuse_saving_s": 0.1,
                "calculation_basis": "all_attempts",
                "attempt_counts": {
                    "attempted": 4,
                    "target_tier": 3,
                    "fallback": 0,
                    "store_unattributed": 0,
                    "unattributed": 1,
                },
            }
        )
        gate = _gate(assess_evidence(payload), 3)
        self.assertEqual(gate["status"], "UNPROVEN")
        self.assertIn("could not be classified", gate["reason"])
        self.assertEqual(gate["result"], "")

    def test_admission_attempt_counts_must_add_up(self) -> None:
        payload = _pass()
        payload["evidence"]["admission"].update(
            {
                "realized_reuse_saving_s": 0.1,
                "calculation_basis": "all_attempts",
                "attempt_counts": {
                    "attempted": 4,
                    "target_tier": 2,
                    "fallback": 1,
                    "store_unattributed": 0,
                    "unattributed": 0,
                },
            }
        )
        with self.assertRaisesRegex(EvidenceError, "attempt_counts do not add up"):
            assess_evidence(payload)

    def test_break_even_needs_tier_verified_requests(self) -> None:
        """Timings from requests that failed Gate 1 do not measure the tier."""
        payload = _pass()
        request = payload["evidence"]["attribution"]["requests"][0]
        request["observed_bytes"] = 0
        assessment = assess_evidence(payload)
        self.assertEqual(_gate(assessment, 1)["status"], "FAIL")
        self.assertEqual(_gate(assessment, 2)["status"], "UNPROVEN")
        self.assertEqual(_gate(assessment, 3)["status"], "UNPROVEN")
        self.assertEqual(_gate(assessment, 2)["result"], "")

    def test_no_overflow_is_a_result(self) -> None:
        """A sweep that never filled the upper tier is valid; necessity is not shown."""
        payload = _pass()
        payload["evidence"]["capacity"]["upper_tier_overflow_observed"] = False
        payload["evidence"]["capacity"]["target_tier_served"] = False
        payload["evidence"]["capacity"]["overflow_classification_counts"] = {
            "retrievals_expected": 0,
            "upper_tier_served": 0,
            "target_tier_served": 0,
            "fallback": 0,
            "miss": 0,
        }
        gate = _gate(assess_evidence(payload), 4)
        self.assertEqual(gate["status"], "PASS")
        self.assertIn("necessity is not shown", gate["result"])

    def test_overflow_without_tier_service_fails(self) -> None:
        """Entries that spilled but were never served mean the tier did not serve."""
        payload = _pass()
        payload["evidence"]["capacity"]["target_tier_served"] = False
        payload["evidence"]["capacity"]["classification_counts"] = {
            "retrievals_expected": 8,
            "upper_tier_served": 4,
            "target_tier_served": 0,
            "fallback": 4,
            "miss": 0,
        }
        payload["evidence"]["capacity"]["overflow_classification_counts"] = {
            "retrievals_expected": 4,
            "upper_tier_served": 0,
            "target_tier_served": 0,
            "fallback": 4,
            "miss": 0,
        }
        self.assertEqual(_gate(assess_evidence(payload), 4)["status"], "FAIL")

    def test_capacity_counts_must_cover_every_retrieval(self) -> None:
        """A completeness claim is refused when retained totals do not add up."""
        payload = _pass()
        payload["evidence"]["capacity"]["classification_counts"]["miss"] = 1
        with self.assertRaisesRegex(EvidenceError, "classified 9 retrievals"):
            assess_evidence(payload)

    def test_envelope_boundary_is_a_result(self) -> None:
        """A level whose batches were not credited ends the envelope, not the sweep."""
        payload = _pass()
        levels = payload["evidence"]["envelope"]["levels"]
        levels.append({"concurrency": 4, "requests": 8, "target_tier_served": 6})
        payload["bindings"]["envelope_levels"].append(
            {"level_index": len(levels) - 1, "configuration_id": "config-a"}
        )
        assessment = assess_evidence(payload)
        gate = _gate(assessment, 5)
        self.assertEqual(gate["status"], "PASS")
        self.assertIn("2 of 8 requests were in batches that did not", gate["result"])
        self.assertNotIn("left", gate["result"])
        self.assertEqual(assessment["validated_envelope"]["max_concurrency"], 2)

    def test_envelope_result_names_the_measured_input_size(self) -> None:
        """An envelope holds for its input size, so the result states it."""
        payload = _pass()
        payload["schema_version"] = "0.3.1"
        payload["evidence"]["envelope"]["request_cacheable_tokens"] = 2048
        gate = _gate(assess_evidence(payload), 5)
        self.assertIn("through concurrency 2 at 2,048 tokens/request", gate["result"])

    def test_envelope_result_omits_an_unrecorded_input_size(self) -> None:
        gate = _gate(assess_evidence(_pass()), 5)
        self.assertNotIn("tokens/request", gate["result"])

    def test_envelope_input_size_must_be_a_positive_integer(self) -> None:
        for value in (0, -1, 2048.0, True, "2048"):
            with self.subTest(value=value):
                payload = _pass()
                payload["schema_version"] = "0.3.1"
                payload["evidence"]["envelope"]["request_cacheable_tokens"] = value
                with self.assertRaisesRegex(
                    EvidenceError, "request_cacheable_tokens must be a positive integer"
                ):
                    assess_evidence(payload)

    def test_envelope_input_size_belongs_to_its_version_only(self) -> None:
        """A 0.3.0 document cannot carry a 0.3.1 field, as its schema says."""
        payload = _pass()
        self.assertEqual(payload["schema_version"], "0.3.0")
        payload["evidence"]["envelope"]["request_cacheable_tokens"] = 2048
        with self.assertRaisesRegex(EvidenceError, "not part of evidence 0.3.0"):
            assess_evidence(payload)
        for version in ("0.3.1", "0.3.2"):
            with self.subTest(version=version):
                payload["schema_version"] = version
                self.assertIn("2,048 tokens/request", _gate(assess_evidence(payload), 5)["result"])

    def test_lowest_level_not_served_fails(self) -> None:
        """No fully served level means the sweep never measured the tier."""
        payload = _pass()
        for level in payload["evidence"]["envelope"]["levels"]:
            level["target_tier_served"] = 0
        self.assertEqual(_gate(assess_evidence(payload), 5)["status"], "FAIL")

    def test_level_without_requests_is_unproven(self) -> None:
        """A declared level with nothing measured leaves the envelope unproven."""
        payload = _pass()
        payload["evidence"]["envelope"]["levels"][-1]["requests"] = 0
        payload["evidence"]["envelope"]["levels"][-1]["target_tier_served"] = 0
        self.assertEqual(_gate(assess_evidence(payload), 5)["status"], "UNPROVEN")

    def test_numbers_appear_only_when_every_gate_passed(self) -> None:
        """A partial run states its finding without the values behind it."""
        assessment = assess_evidence(
            json.loads(
                (EXAMPLES / "gate1-pass-filtered-counter.json").read_text(
                    encoding="utf-8"
                )
            )
        )
        result = _gate(assessment, 2)["result"]
        self.assertIn("retrieval beats recompute", result)
        self.assertNotIn(" s vs ", result)
        self.assertIn(" s vs ", _gate(assess_evidence(_pass()), 2)["result"])

    def test_capacity_result_states_what_fell_back_beyond_the_upper_tier(self) -> None:
        """A tier that served part of the overflow is reported with the counts."""
        payload = _pass()
        counts = {
            "retrievals_expected": 15,
            "upper_tier_served": 3,
            "target_tier_served": 4,
            "fallback": 8,
            "miss": 0,
        }
        payload["evidence"]["capacity"]["classification_counts"] = dict(counts)
        payload["evidence"]["capacity"]["overflow_classification_counts"] = dict(counts)
        gate = _gate(assess_evidence(payload), 4)
        self.assertEqual(gate["status"], "PASS")
        self.assertIn("served only 4 of 15", gate["result"])
        self.assertIn("8 fell back", gate["result"])

    def test_capacity_result_does_not_claim_the_records_locate_the_failure(
        self,
    ) -> None:
        """Observed write activity is not shown as an entry on the target device."""
        payload = _pass()
        counts = {
            "retrievals_expected": 15,
            "upper_tier_served": 3,
            "target_tier_served": 4,
            "fallback": 8,
            "miss": 0,
        }
        payload["evidence"]["capacity"]["classification_counts"] = dict(counts)
        payload["evidence"]["capacity"]["overflow_classification_counts"] = dict(counts)
        result = _gate(assess_evidence(payload), 4)["result"]
        self.assertIn("no observed write", result)
        self.assertIn("retrieval not established", result)
        self.assertIn("does not show that an entry reached the target device", result)
        self.assertNotIn("show which failed", result)

    def test_successful_sample_admission_is_unproven(self) -> None:
        """A store cost from successful samples only opens no performance claim."""
        assessment = assess_evidence(_successful_samples())
        gate = _gate(assessment, 3)
        self.assertEqual(gate["status"], "UNPROVEN")
        self.assertIn("successful samples only", gate["reason"])
        self.assertIn("calculation_basis all_attempts", gate["reason"])
        self.assertEqual(gate["result"], "")
        self.assertEqual(
            [_gate(assessment, gate_id)["status"] for gate_id in (1, 2, 4, 5)],
            ["PASS"] * 4,
        )
        self.assertEqual(assessment["overall"], "UNPROVEN")
        self.assertEqual(assessment["performance_claims"], "suppressed")
        self.assertNotIn("performance", assessment)
        self.assertNotIn(" s vs ", _gate(assessment, 2)["result"])

    def test_successful_sample_admission_is_unproven_however_many_samples(
        self,
    ) -> None:
        """No sample count makes a successful-sample block the realized decision."""
        for counts in (
            {"requested": 10, "valid": 10, "dropped": 0},
            {"requested": 10, "valid": 3, "dropped": 7},
        ):
            with self.subTest(counts=counts):
                payload = _successful_samples()
                payload["evidence"]["admission"]["sample_counts"] = counts
                self.assertEqual(
                    _gate(assess_evidence(payload), 3)["status"], "UNPROVEN"
                )

    def test_successful_sample_reason_states_its_sample_count(self) -> None:
        """Most stores dropped: the reason says how few the cost rests on."""
        payload = _successful_samples()
        payload["evidence"]["admission"]["sample_counts"] = {
            "requested": 10,
            "valid": 3,
            "dropped": 7,
        }
        reason = _gate(assess_evidence(payload), 3)["reason"]
        self.assertIn("from 3 of 10 samples, 7 dropped", reason)
        self.assertIn("low-confidence", reason)

    def test_gate2_shortfall_is_named_before_the_admission_basis(self) -> None:
        """Without an established saving, Gate 3 says so whatever its basis."""
        payload = _successful_samples()
        payload["evidence"]["attribution"]["requests"][0]["observed_bytes"] = 0
        gate = _gate(assess_evidence(payload), 3)
        self.assertEqual(gate["status"], "UNPROVEN")
        self.assertIn("saving per reuse is not established", gate["reason"])

    def test_overflow_without_counts_is_unproven(self) -> None:
        """Two booleans cannot show how much of the overflow the tier served."""
        assessment = assess_evidence(_without_capacity_counts())
        gate = _gate(assessment, 4)
        self.assertEqual(gate["status"], "UNPROVEN")
        self.assertIn("at least one retrieval", gate["reason"])
        self.assertIn("overflow_classification_counts", gate["reason"])
        self.assertEqual(gate["result"], "")
        self.assertEqual(assessment["overall"], "UNPROVEN")
        self.assertEqual(assessment["performance_claims"], "suppressed")
        self.assertNotIn("performance", assessment)

    def test_no_overflow_without_counts_is_still_a_result(self) -> None:
        """A sweep that never overflowed claims nothing the counts would back."""
        payload = _without_capacity_counts()
        payload["evidence"]["capacity"]["upper_tier_overflow_observed"] = False
        payload["evidence"]["capacity"]["target_tier_served"] = False
        assessment = assess_evidence(payload)
        gate = _gate(assessment, 4)
        self.assertEqual(gate["status"], "PASS")
        self.assertIn("necessity is not shown", gate["result"])
        self.assertEqual(assessment["overall"], "PASS")

    def test_overflow_without_counts_or_tier_service_still_fails(self) -> None:
        """No retrieval served by the tier is a failure with or without counts."""
        payload = _without_capacity_counts()
        payload["evidence"]["capacity"]["target_tier_served"] = False
        assessment = assess_evidence(payload)
        self.assertEqual(_gate(assessment, 4)["status"], "FAIL")
        self.assertEqual(assessment["overall"], "FAIL")

    def test_every_attempt_and_every_count_keep_a_lossy_run_passing(self) -> None:
        """Retained failures are findings: the measurement is valid and says so."""
        payload = _pass()
        payload["evidence"]["admission"].update(
            {
                "realized_reuse_saving_s": 0.2,
                "attempt_counts": {
                    "attempted": 10,
                    "target_tier": 5,
                    "fallback": 0,
                    "store_unattributed": 5,
                    "unattributed": 0,
                },
            }
        )
        counts = {
            "retrievals_expected": 15,
            "upper_tier_served": 3,
            "target_tier_served": 4,
            "fallback": 8,
            "miss": 0,
        }
        payload["evidence"]["capacity"]["classification_counts"] = dict(counts)
        payload["evidence"]["capacity"]["overflow_classification_counts"] = dict(counts)
        assessment = assess_evidence(payload)
        self.assertEqual(assessment["overall"], "PASS")
        self.assertEqual(assessment["performance_claims"], "allowed")
        self.assertIn(
            "all 10 attempts included: 5 target-tier, 0 fallback, "
            "5 store-unattributed",
            _gate(assessment, 3)["result"],
        )
        self.assertIn("served only 4 of 15", _gate(assessment, 4)["result"])
        self.assertIn("did not serve what overflowed", _gate(assessment, 4)["result"])

    def test_admission_sample_counts_must_add_up(self) -> None:
        payload = _successful_samples()
        payload["evidence"]["admission"]["sample_counts"] = {
            "requested": 10,
            "valid": 3,
            "dropped": 6,
        }
        with self.assertRaises(EvidenceError):
            assess_evidence(payload)


if __name__ == "__main__":
    unittest.main()
