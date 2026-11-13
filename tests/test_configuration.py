# SPDX-License-Identifier: Apache-2.0
"""Configuration fingerprints prevent unlike measurements from being pooled."""

# Standard
import copy
import json
import subprocess
import sys
import unittest
from pathlib import Path
from typing import Any

# First Party
from kvprobe.configuration import (
    ConfigurationError,
    fingerprint_configuration,
    validate_configuration,
    validate_registry,
)
from kvprobe.report import EvidenceError, assess_evidence, render_markdown


ROOT = Path(__file__).parents[1]
EXAMPLES = ROOT / "examples"
EVIDENCE = EXAMPLES / "evidence"
PORTABILITY = ROOT / "tests" / "fixtures" / "portability"


def _load(path: Path) -> dict[str, Any]:
    """Load one JSON object from disk."""
    return json.loads(path.read_text(encoding="utf-8"))


class ConfigurationFingerprintTest(unittest.TestCase):
    """Verify canonical hashing, binding coverage, and mismatch suppression."""

    def test_fingerprint_is_stable_across_key_order(self) -> None:
        """JSON object insertion order must not change the configuration identity."""
        parameters = _load(EXAMPLES / "reference-configuration.json")
        reordered = {key: parameters[key] for key in reversed(parameters)}

        self.assertEqual(
            fingerprint_configuration(parameters),
            fingerprint_configuration(reordered),
        )

    def test_capacity_change_changes_fingerprint(self) -> None:
        """A changed upper-tier capacity must create a different configuration."""
        parameters = _load(EXAMPLES / "reference-configuration.json")
        changed = copy.deepcopy(parameters)
        changed["cache"]["upper_tier_capacity_bytes"] *= 2

        self.assertNotEqual(
            fingerprint_configuration(parameters),
            fingerprint_configuration(changed),
        )

    def test_a_different_device_is_a_different_configuration(self) -> None:
        """Two devices on one host must not share a configuration identity.

        Storage counters do not identify which accelerator issued I/O, even
        when cgroup accounting is filtered to one block device. On a
        multi-device host two accelerators can sit behind different root
        complexes and reach the tier at different cost; without this, those
        runs would merge under one fingerprint.
        """
        parameters = _load(EXAMPLES / "reference-configuration.json")
        other_device = copy.deepcopy(parameters)
        other_device["runtime"]["device_id"] = "00000000:65:00.0"

        self.assertNotEqual(
            fingerprint_configuration(parameters),
            fingerprint_configuration(other_device),
        )

    def test_configuration_without_a_device_is_refused(self) -> None:
        """A configuration that does not name its device cannot be validated."""
        parameters = _load(EXAMPLES / "reference-configuration.json")
        incomplete = copy.deepcopy(parameters)
        del incomplete["runtime"]["device_id"]

        with self.assertRaises(ConfigurationError) as caught:
            validate_configuration(incomplete)

        self.assertIn("device_id", str(caught.exception))

    def test_device_identity_is_not_bound_to_one_vendor_format(self) -> None:
        """Any stable identifier names a device, not one tool's output format.

        The fingerprint needs a value that differs between devices and stays
        stable across the campaign. Requiring a particular vendor's address
        format would put platforms that do not print it outside the contract.
        """
        parameters = _load(EXAMPLES / "reference-configuration.json")
        fingerprints = set()
        for value in (
            "00000000:01:00.0",  # nvidia-smi prints an eight-digit domain
            "0000:a1:00.0",  # lspci and other tools print four
            "GPU-3f2a9c11",  # an opaque stable identifier
            "reference-unified-aarch64-0",  # a platform without PCI addressing
        ):
            with self.subTest(value=value):
                accepted = copy.deepcopy(parameters)
                accepted["runtime"]["device_id"] = value

                validate_configuration(accepted)
                fingerprints.add(fingerprint_configuration(accepted))

        self.assertEqual(len(fingerprints), 4)

    def test_device_identity_must_be_recorded_verbatim(self) -> None:
        """Invisible drift in the identity must not change the fingerprint."""
        parameters = _load(EXAMPLES / "reference-configuration.json")
        for value in ("", "   ", " 0000:a1:00.0", "0000:a1:00.0\n", "a\tb", "x" * 201):
            with self.subTest(value=value):
                bad = copy.deepcopy(parameters)
                bad["runtime"]["device_id"] = value

                with self.assertRaises(ConfigurationError):
                    validate_configuration(bad)

    def test_build_identity_is_not_bound_to_a_digest_format(self) -> None:
        """Naming the serving software must not require disclosing the build.

        The fingerprint needs a value that differs when the image differs and
        stays stable across the campaign. Requiring a content digest would put
        an operator who cannot publish a private build outside the contract,
        and a fabricated digest reads as authentic where a label reads as what
        it is.
        """
        parameters = _load(EXAMPLES / "reference-configuration.json")
        fingerprints = set()
        for value in (
            "sha256:" + "d" * 64,  # the preferred form, prefixed
            "e" * 64,  # the same form, bare
            "reference-build-discrete-01",  # an opaque stable label
            "vllm-0.16.0+lmcache-0.4.1",  # a version pair the site can resolve
        ):
            with self.subTest(value=value):
                accepted = copy.deepcopy(parameters)
                accepted["runtime"]["image_digest"] = value

                validate_configuration(accepted)
                fingerprints.add(fingerprint_configuration(accepted))

        self.assertEqual(len(fingerprints), 4)

    def test_build_identity_must_be_recorded_verbatim(self) -> None:
        """Invisible drift in the build identity must not change the fingerprint."""
        parameters = _load(EXAMPLES / "reference-configuration.json")
        for value in ("", "   ", " build-01", "build-01\n", "a\tb", "x" * 201):
            with self.subTest(value=value):
                bad = copy.deepcopy(parameters)
                bad["runtime"]["image_digest"] = value

                with self.assertRaises(ConfigurationError):
                    validate_configuration(bad)

    def test_device_index_is_optional_and_never_the_identity(self) -> None:
        """Enumeration order is unstable, so the index alone cannot identify."""
        parameters = _load(EXAMPLES / "reference-configuration.json")
        without_index = copy.deepcopy(parameters)
        del without_index["runtime"]["gpu_index"]

        validate_configuration(without_index)

        renumbered = copy.deepcopy(parameters)
        renumbered["runtime"]["gpu_index"] = parameters["runtime"]["gpu_index"] + 1
        self.assertNotEqual(
            fingerprint_configuration(parameters),
            fingerprint_configuration(renumbered),
        )
        self.assertNotEqual(
            fingerprint_configuration(parameters),
            fingerprint_configuration(without_index),
        )

    def test_registry_rejects_stale_fingerprint(self) -> None:
        """A fingerprint cannot be reused after parameters change."""
        parameters = _load(EXAMPLES / "reference-configuration.json")
        entry = {
            "id": "config-a",
            "fingerprint": "0" * 64,
            "parameters": parameters,
        }

        with self.assertRaises(ConfigurationError):
            validate_registry([entry])

    def test_cli_prints_the_canonical_fingerprint(self) -> None:
        """Operators can compute the exact fingerprint before normalization."""
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "kvprobe",
                "fingerprint",
                str(EXAMPLES / "reference-configuration.json"),
            ],
            cwd=ROOT,
            capture_output=True,
            check=False,
            text=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.strip(),
            "ef82b81a722e489f1c9222bc7decafba23c27cad5ade1e3de76bcb1981274390",
        )

    def test_single_configuration_allows_v0_3_report(self) -> None:
        """Complete bindings to one verified configuration preserve a passing report."""
        assessment = assess_evidence(_load(EVIDENCE / "pass-v0.3.json"))

        self.assertEqual(assessment["assessment_version"], "0.6.2")
        self.assertEqual(assessment["overall"], "PASS")
        self.assertEqual(assessment["configuration"]["id"], "config-a")
        self.assertEqual(assessment["performance_claims"], "allowed")
        self.assertIn(
            assessment["configuration"]["fingerprint"],
            render_markdown(assessment),
        )

    def test_mixed_configuration_is_invalid(self) -> None:
        """A curve assembled from two configurations must suppress every ratio."""
        assessment = assess_evidence(
            _load(EVIDENCE / "mixed-configuration-v0.3.json")
        )

        self.assertEqual(assessment["overall"], "INVALID")
        self.assertEqual(assessment["performance_claims"], "suppressed")
        self.assertEqual(
            assessment["configuration_mismatch"]["bound_configuration_ids"],
            ["config-a", "config-b"],
        )
        self.assertNotIn("performance", assessment)

    def test_mixed_case_matches_the_sanitized_baseline(self) -> None:
        """The v0.3 verdict must close the captured mixed-configuration gap."""
        fixture = _load(PORTABILITY / "mixed_configuration_invalid.json")
        assessment = assess_evidence(
            _load(EVIDENCE / "mixed-configuration-v0.3.json")
        )

        self.assertEqual(
            assessment["overall"], fixture["baseline"]["target_gate_status"]
        )

    def test_incomplete_request_bindings_are_malformed(self) -> None:
        """Every measured request must be tied to configuration identity."""
        payload = _load(EVIDENCE / "pass-v0.3.json")
        payload["bindings"]["attribution_requests"] = []

        with self.assertRaises(EvidenceError):
            assess_evidence(payload)

    def test_binding_objects_reject_unversioned_fields(self) -> None:
        """An ignored binding field must not silently change comparison semantics."""
        payload = _load(EVIDENCE / "pass-v0.3.json")
        payload["bindings"]["attribution_requests"][0]["host_hint"] = "private"

        with self.assertRaises(EvidenceError):
            assess_evidence(payload)


if __name__ == "__main__":
    unittest.main()
