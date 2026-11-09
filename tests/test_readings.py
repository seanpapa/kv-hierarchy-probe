# SPDX-License-Identifier: Apache-2.0
"""Public efficiency contract and compatibility with existing reports."""

# Standard
import hashlib
import io
import json
import re
import unittest
from contextlib import redirect_stdout
from copy import deepcopy
from pathlib import Path

# First Party
from kvprobe.cli import main
from kvprobe.ingest import ingest_files
from kvprobe.readings import compute_readings, render_readings
from kvprobe.report import EvidenceError, assess_evidence, render_markdown, render_text

ROOT = Path(__file__).resolve().parents[1]
BASELINE_HASHES = {
    "attribution-unfiltered-counter.json": [
        "667202f9374483480d40590fdf3e06a7598457de4fcc0e841288e68bf1da8ff5",
        "f6650d3751aa9b4966ebbb7a016c06ed0967a77069ed148987823d77ffc29a0e",
    ],
    "envelope-collapse-small-pool.json": [
        "81e792ee4f4c6c44cd2951fe3fe88adc6e168d98ef8cd602c78788b39d68a249",
        "238b2a75fb606b9c4e996dcdef058cac9399ce607f3c80f44a4c2055d7b323de",
    ],
    "envelope-pass-large-pool.json": [
        "67f04fb73cf82e73e7b4c7af3f1413478da5e38e61536aed9b88f705f0d86937",
        "a240eca0dbf31053a94d65fdbf068944d447e1accef4143536eb188907491d2e",
    ],
    "invalid-allocation.json": [
        "2ae6c3498ba846a841ed1e5db6e1dd1128c8111f1e7d271cb19ed16400ff8b31",
        "ec4ea93244c19f61579b95a3795e8feac758023ad70898e042f18af7fa9bd464",
    ],
    "mixed-configuration-v0.3.json": [
        "6449f4ce7c42a595f37bd0a577f7422a5229105e68a49d7f229739cdfba0432c",
        "6f5f8965e0e7f5614f1ad701255c2b7526f616bb870da520c45b86c7bbc10653",
    ],
    "partial-unsupported.json": [
        "b7c401b54cc6128b2c2d9fd4e81eeef282126a14d4b6bbc749810c2ab5f91956",
        "0e88382061b18ca7c67783de3e9119668487bab4e8934a232b22a0e9ffd40098",
    ],
    "pass-capabilities.json": [
        "eac1bde677e6d6c51d84d18770797f7f3995b1dfa3d012c4aa4a2a05bd9cacad",
        "b113775b5b67909149fb9810251f1dcacf32181237e22cb90c8041421c6b2da2",
    ],
    "pass-v0.3.json": [
        "464803722694455d0982376fa341402dc3fae92d12ca9eb11795995e056e7e23",
        "a6c21d94906173d9e8ceaaaca50ef6962a3e3f260799dcd03eeaeb19d560b553",
    ],
}


def sample() -> dict:
    return json.loads((ROOT / "examples/evidence/pass-v0.3.json").read_text())


class EfficiencyReadings(unittest.TestCase):
    def test_hand_calculated_values_and_purity(self) -> None:
        payload = sample()
        original = deepcopy(payload)
        readings = compute_readings(payload)
        metrics = readings["efficiency"]
        self.assertEqual(readings["grade"], "ATTRIBUTED")
        self.assertEqual(metrics["read_amplification"]["value"]["median"], 1)
        self.assertAlmostEqual(
            metrics["effective_read_bandwidth"]["value"], 0.25 / 0.17
        )
        self.assertAlmostEqual(metrics["saving_per_hit"]["value"], 0.13)
        self.assertAlmostEqual(metrics["speedup"]["value"], 0.3 / 0.17)
        self.assertAlmostEqual(metrics["repay_reuses"]["value"], 0.05 / 0.13)
        self.assertAlmostEqual(metrics["margin"]["value"], 5.2)
        self.assertEqual(
            [x["ratio"] for x in metrics["tier_served_ratio"]["value"]], [1, 1]
        )
        self.assertEqual(payload, original)

    def test_library_rendering_without_readings_is_unchanged(self) -> None:
        for filename, hashes in BASELINE_HASHES.items():
            with self.subTest(filename=filename):
                payload = json.loads(
                    (ROOT / "examples/evidence" / filename).read_text()
                )
                assessment = assess_evidence(payload)
                compute_readings(payload)
                for render, expected in zip((render_text, render_markdown), hashes):
                    self.assertEqual(
                        hashlib.sha256(render(assessment).encode()).hexdigest(),
                        expected,
                    )

    def test_unsupported_evidence_cannot_produce_readings(self) -> None:
        """Efficiency output must not bypass the supported evidence contract."""
        payload = sample()
        payload["schema_version"] = "0.2.0"
        payload.pop("bindings")
        payload.pop("configurations")
        with self.assertRaisesRegex(EvidenceError, "kvprobe ingest"):
            compute_readings(payload)

    def test_zero_bytes_and_zero_ttft_do_not_divide(self) -> None:
        payload = sample()
        payload["evidence"]["attribution"]["requests"][0]["expected_bytes"] = 0
        payload["evidence"]["break_even"]["retrieval_ttft_s"] = [0]
        metrics = compute_readings(payload)["efficiency"]
        for name in ("read_amplification", "effective_read_bandwidth", "speedup"):
            self.assertEqual(metrics[name]["status"], "not_measured")

    def test_nonpositive_savings_never_repay(self) -> None:
        for retrieval in (0.3, 0.4):
            payload = sample()
            payload["evidence"]["break_even"]["retrieval_ttft_s"] = [retrieval]
            readings = compute_readings(payload)
            repay = readings["efficiency"]["repay_reuses"]
            if readings["claims_allowed"]:
                self.assertEqual(repay["value"], "never")
            else:
                # Retrieval slower than recompute fails gate 2, and a failed
                # gate withholds the performance family. The computation is
                # still exercised: a withheld metric keeps its sample count.
                self.assertEqual(repay["status"], "withheld")
                self.assertEqual(repay["n"], 1)

    def test_free_store_has_zero_repay_and_no_undefined_margin(self) -> None:
        payload = sample()
        payload["evidence"]["admission"]["store_cost_s"] = 0
        metrics = compute_readings(payload)["efficiency"]
        self.assertEqual(metrics["repay_reuses"]["value"], 0)
        self.assertEqual(metrics["margin"]["status"], "not_measured")

    def test_repayment_uses_realized_all_attempt_saving(self) -> None:
        """Fallback cost changes repayment even when Gate 2 hits are fast."""
        payload = sample()
        payload["evidence"]["admission"].update(
            {
                "store_cost_s": 0.3,
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
        metric = compute_readings(payload)["efficiency"]["repay_reuses"]
        self.assertAlmostEqual(metric["value"], 3.0)
        self.assertEqual(metric["n"], 4)

    def test_unsupported_sections(self) -> None:
        payload = sample()
        for name in ("attribution", "break_even", "admission", "envelope"):
            payload["evidence"][name] = {
                "availability": {
                    "status": "unsupported",
                    "reason_code": "not_collected",
                    "detail": "No measurement.",
                }
            }
        payload["bindings"].update(
            attribution_requests=[], break_even=None, admission=None, envelope_levels=[]
        )
        self.assertTrue(
            all(
                m["status"] == "not_measured"
                for m in compute_readings(payload)["efficiency"].values()
            )
        )

    def test_bandwidth_caveat_only_with_a_measured_value(self) -> None:
        # Local
        from kvprobe.readings import render_readings

        measured = compute_readings(sample())
        self.assertEqual(
            measured["efficiency"]["effective_read_bandwidth"]["status"], "ok"
        )
        self.assertIn("TTFT-bounded lower bound", render_readings(measured, True))
        payload = sample()
        payload["evidence"]["break_even"] = {
            "availability": {
                "status": "unsupported",
                "reason_code": "not_collected",
                "detail": "No measurement.",
            }
        }
        payload["bindings"]["break_even"] = None
        unmeasured = compute_readings(payload)
        self.assertEqual(
            unmeasured["efficiency"]["effective_read_bandwidth"]["status"],
            "not_measured",
        )
        self.assertNotIn("TTFT-bounded lower bound", render_readings(unmeasured, True))

    def test_empty_envelope_level(self) -> None:
        payload = sample()
        payload["evidence"]["envelope"]["levels"][0].update(
            requests=0, target_tier_served=0
        )
        self.assertIsNone(
            compute_readings(payload)["efficiency"]["tier_served_ratio"]["value"][0][
                "ratio"
            ]
        )

    def test_external_bytes_and_off_tier(self) -> None:
        folder = ROOT / "tests/fixtures/external/block-tier"
        for filename, ratio in (("served.json", 1), ("partial.json", 0.5)):
            payload = ingest_files(
                [folder / filename], folder / "configuration.json", run_id="external"
            )
            readings = compute_readings(payload)
            self.assertEqual(readings["grade"], "CORROBORATED")
            self.assertEqual(
                readings["efficiency"]["read_amplification"]["value"]["median"], ratio
            )
            self.assertEqual(readings["off_tier"], ratio < 1)

    def test_weakest_grade_and_mixed_sizes(self) -> None:
        payload = sample()
        request = deepcopy(payload["evidence"]["attribution"]["requests"][0])
        request.update(
            id="request-2", expected_bytes=134217728, observed_bytes=134217728
        )
        request["attribution"].update(
            confidence="corroborating", target_device_filtered=False
        )
        payload["evidence"]["attribution"]["requests"].append(request)
        payload["bindings"]["attribution_requests"].append(
            {"request_id": "request-2", "configuration_id": "config-a"}
        )
        readings = compute_readings(payload)
        self.assertEqual(readings["grade"], "CORROBORATED")
        self.assertEqual(
            readings["efficiency"]["effective_read_bandwidth"]["status"], "not_measured"
        )

    def test_invalid_measurements_never_combine(self) -> None:
        payload = sample()
        configuration = deepcopy(payload["configurations"][0])
        configuration["id"] = "config-b"
        payload["configurations"].append(configuration)
        payload["bindings"]["admission"] = "config-b"
        self.assertEqual(assess_evidence(payload)["overall"], "INVALID")
        self.assertTrue(
            all(
                m["status"] == "not_measured"
                for m in compute_readings(payload)["efficiency"].values()
            )
        )

    def test_cli_shows_readings_by_default_and_exit_status_is_unchanged(self) -> None:
        for filename in ("pass-v0.3.json", "attribution-unfiltered-counter.json"):
            path = ROOT / "examples/evidence" / filename
            for fmt in ("text", "markdown", "json"):
                claim_only = io.StringIO()
                with redirect_stdout(claim_only):
                    expected_exit = main(
                        ["report", str(path), "--format", fmt, "--no-readings"]
                    )
                for extra in ([], ["--readings"]):
                    output = io.StringIO()
                    with redirect_stdout(output):
                        actual_exit = main(
                            ["report", str(path), "--format", fmt, *extra]
                        )
                    self.assertEqual(actual_exit, expected_exit)
                    if fmt == "json":
                        result = json.loads(output.getvalue())
                        result.pop("readings")
                        self.assertEqual(result, json.loads(claim_only.getvalue()))
                        continue
                    self.assertNotIn("READINGS", claim_only.getvalue())
                    # The caveat qualifies a bandwidth value, so it is shown only
                    # when one was measured.
                    self.assertEqual(
                        "TTFT-bounded lower bound" in output.getvalue(),
                        filename == "pass-v0.3.json",
                    )
                    if expected_exit:
                        # A failed run reports integrity observations and names
                        # the verdict; it does not offer a performance number.
                        self.assertIn("READINGS (integrity only)", output.getvalue())
                        self.assertIn(
                            "performance readings withheld", output.getvalue()
                        )
                        self.assertNotIn("Performance numbers", output.getvalue())
                    else:
                        self.assertIn("READINGS (efficiency)", output.getvalue())

    def test_a_failed_gate_withholds_every_performance_reading(self) -> None:
        """The core rule reaches the readings: no gate, no performance number.

        A caveat beside a speedup does not survive being quoted, so a failed run
        must not carry one at all — in the rendered report or in the document a
        downstream reader parses.
        """
        payload = sample()
        payload["evidence"]["attribution"]["requests"][0]["correct"] = False
        readings = compute_readings(payload)

        self.assertFalse(readings["claims_allowed"])
        self.assertEqual(readings["verdict"], "FAIL")
        for name in ("saving_per_hit", "speedup", "repay_reuses", "margin"):
            with self.subTest(metric=name):
                self.assertEqual(readings["efficiency"][name]["status"], "withheld")
                self.assertIsNone(readings["efficiency"][name]["value"])
        # Integrity observations stay: they explain the failure rather than
        # claiming the tier performed.
        self.assertEqual(
            readings["efficiency"]["read_amplification"]["status"], "ok"
        )
        rendered = render_readings(readings, claims_allowed=False)
        self.assertIn("READINGS (integrity only)", rendered)
        self.assertIn("FAIL", rendered)
        self.assertNotIn("not measured by this run  n=1", rendered)

    def test_withheld_is_not_reported_as_unmeasured(self) -> None:
        """A withheld quantity was measured; saying otherwise misreports it."""
        payload = sample()
        payload["evidence"]["attribution"]["requests"][0]["correct"] = False
        readings = compute_readings(payload)

        speedup = readings["efficiency"]["speedup"]
        self.assertEqual(speedup["status"], "withheld")
        self.assertGreater(speedup["n"], 0)
        self.assertIn("gates did not pass", speedup["withheld_reason"])
        self.assertIn(
            "withheld — gates did not pass",
            render_readings(readings, claims_allowed=False),
        )

    def test_a_passing_run_still_reports_every_reading(self) -> None:
        """Withholding must not leak into the path the gates authorize."""
        readings = compute_readings(sample())

        self.assertTrue(readings["claims_allowed"])
        self.assertEqual(readings["verdict"], "PASS")
        for metric in readings["efficiency"].values():
            self.assertNotEqual(metric["status"], "withheld")
        rendered = render_readings(readings, claims_allowed=True)
        self.assertIn("READINGS (efficiency)", rendered)
        self.assertNotIn("withheld", rendered)

    def test_no_readings_keeps_the_claim_only_report(self) -> None:
        path = ROOT / "examples/evidence/attribution-unfiltered-counter.json"
        assessment = assess_evidence(json.loads(path.read_text()))
        output = io.StringIO()
        with redirect_stdout(output):
            main(["report", str(path), "--no-readings"])
        self.assertEqual(output.getvalue().rstrip("\n"), render_text(assessment))
        self.assertIn("Performance numbers omitted", output.getvalue())

    def test_readings_never_evaluate(self) -> None:
        """Readings state numbers and evidence grades, not whether they are good."""
        evaluative = re.compile(
            r"\b(excellent|superior|outstanding|impressive|great|good|bad|poor|"
            r"best|worst|better|worse|faster|slower|efficient|inefficient|strong|"
            r"weak|healthy|optimal|recommend\w*|should|ideal)\b",
            re.IGNORECASE,
        )
        for path in sorted((ROOT / "examples/evidence").glob("*.json")):
            payload = json.loads(path.read_text())
            assessment = assess_evidence(payload)
            readings = compute_readings(payload)
            for render in (render_text, render_markdown):
                with self.subTest(example=path.name, render=render.__name__):
                    text = render(assessment, readings)
                    section = text[text.index("READINGS") :]
                    self.assertIsNone(evaluative.search(section), section)
