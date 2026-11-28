# SPDX-License-Identifier: Apache-2.0
"""Scope declarations bind to a configuration and never establish serving proof."""

# Standard
import copy
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Any
import unittest

# First Party
from kvprobe.configuration import (
    ConfigurationError,
    fingerprint_configuration,
    validate_configuration,
    validate_registry,
)
from kvprobe.report import EvidenceError, assess_evidence
from kvprobe.scope_contract import validate_scope_bindings
from kvprobe.tier_scope import ScopeError, validate_target_scope

ROOT = Path(__file__).resolve().parents[1]
SCOPE = {
    "kind": "nfs_service",
    "id": "scope-a",
    "service_id": "service-a",
    "export_id": "export-a",
    "client_id": "client-a",
    "transport": "rdma",
    "io_mode": "direct",
}


def example() -> dict[str, Any]:
    """Load a synthetic legacy fixture through its checked-in public path."""
    return json.loads(
        (ROOT / "examples/evidence/gate1-pass-filtered-counter.json").read_text()
    )


def scoped() -> dict[str, Any]:
    """Create draft declarations only; these are not measured serving evidence."""
    document = example()
    document["schema_version"] = "0.4.0"
    entry = document["configurations"][0]
    arm = entry["parameters"]["arms"][0]
    arm["name"] = "nfs-rdma"
    arm["backend"] = "lmcache_posix"
    arm["tier_path"] = "/tier/nfs"
    arm["attribution_provider"] = "nfs_mountstats"
    arm["tier_identity"] = copy.deepcopy(SCOPE)
    entry["fingerprint"] = fingerprint_configuration(
        entry["parameters"], evidence_version="0.4.0"
    )
    for request in document["evidence"]["attribution"]["requests"]:
        request["attribution"]["provider"] = "nfs_mountstats"
        request["attribution"]["target_device_filtered"] = False
        request["attribution"]["target_scope"] = copy.deepcopy(SCOPE)
    return document


def validator(fragment: str | None = None) -> Any:
    """Resolve the draft schema locally using test-only JSON Schema libraries."""
    # Third Party
    from jsonschema import Draft202012Validator
    from referencing import Registry, Resource

    resources = []
    for path in (ROOT / "schemas").glob("evidence-v*.schema.json"):
        schema = json.loads(path.read_text())
        resources.append((schema["$id"], Resource.from_contents(schema)))
    schema = json.loads((ROOT / "schemas/evidence-v0.4.0.schema.json").read_text())
    registry = Registry().with_resources(resources)
    if fragment:
        schema = {"$ref": schema["$id"] + "#/$defs/" + fragment}
    return Draft202012Validator(schema, registry=registry)


class ScopeContractTests(unittest.TestCase):
    """Public API tests for the draft scope and legacy rejection boundary."""

    def test_legacy_fingerprint_is_byte_for_byte_unchanged(self) -> None:
        """Version selection adds no bytes to an existing canonical configuration."""
        parameters = example()["configurations"][0]["parameters"]
        expected = sha256(
            json.dumps(
                parameters,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode()
        ).hexdigest()
        for version in ("0.3.0", "0.3.1", "0.3.2", "0.4.0"):
            self.assertEqual(
                fingerprint_configuration(parameters, evidence_version=version),
                expected,
            )
        self.assertEqual(fingerprint_configuration(parameters), expected)

    def test_nfs_requires_explicit_contract(self) -> None:
        """The legacy default cannot accept a service-scoped arm."""
        parameters = scoped()["configurations"][0]["parameters"]
        with self.assertRaises(ConfigurationError):
            fingerprint_configuration(parameters)
        for version in ("0.3.0", "0.3.1", "0.3.2", "9.9.9"):
            with self.assertRaises(ConfigurationError):
                validate_configuration(parameters, evidence_version=version)
        self.assertIs(
            validate_configuration(parameters, evidence_version="0.4.0"), parameters
        )

    def test_zero_upper_tier_is_explicit_draft_only(self) -> None:
        """Disabled upper-tier reuse uses zero, never a fabricated capacity."""
        parameters = scoped()["configurations"][0]["parameters"]
        parameters["cache"]["upper_tier_capacity_bytes"] = 0
        self.assertIs(
            validate_configuration(parameters, evidence_version="0.4.0"), parameters
        )
        self.assertFalse(
            list(validator("configurationParameters").iter_errors(parameters))
        )
        for value in (-1, True, None):
            parameters["cache"]["upper_tier_capacity_bytes"] = value
            with self.assertRaises(ConfigurationError):
                validate_configuration(parameters, evidence_version="0.4.0")
            self.assertTrue(
                list(validator("configurationParameters").iter_errors(parameters))
            )
        legacy = example()["configurations"][0]["parameters"]
        legacy["cache"]["upper_tier_capacity_bytes"] = 0
        with self.assertRaises(ConfigurationError):
            validate_configuration(legacy)

    def test_every_identity_component_changes_the_fingerprint(self) -> None:
        """Service, export, client, scope and I/O mode all define the experiment."""
        parameters = scoped()["configurations"][0]["parameters"]
        original = fingerprint_configuration(parameters, evidence_version="0.4.0")
        for key, value in [
            ("id", "scope-b"),
            ("service_id", "service-b"),
            ("export_id", "export-b"),
            ("client_id", "client-b"),
            ("io_mode", "buffered"),
        ]:
            changed = copy.deepcopy(parameters)
            changed["arms"][0]["tier_identity"][key] = value
            self.assertNotEqual(
                fingerprint_configuration(changed, evidence_version="0.4.0"), original
            )

    def test_identity_is_not_optional_or_valid_for_another_provider(self) -> None:
        """An arbitrary provider cannot carry an NFS identity."""
        parameters = scoped()["configurations"][0]["parameters"]
        for mutate in ("missing", "other_provider"):
            changed = copy.deepcopy(parameters)
            if mutate == "missing":
                del changed["arms"][0]["tier_identity"]
            else:
                changed["arms"][0]["attribution_provider"] = "cgroup_blk"
            with self.assertRaises(ConfigurationError):
                validate_configuration(changed, evidence_version="0.4.0")

    def test_binding_accepts_only_matching_declarations(self) -> None:
        """A successful binding check returns fingerprints, not a gate verdict."""
        document = scoped()
        before = copy.deepcopy(document)
        result = validate_scope_bindings(document)
        self.assertEqual(
            result,
            validate_registry(document["configurations"], evidence_version="0.4.0"),
        )
        self.assertEqual(document, before)
        self.assertNotIn("gates", result)
        for key, value in [
            ("id", "scope-b"),
            ("export_id", "export-b"),
            ("client_id", "client-b"),
            ("service_id", "service-b"),
            ("io_mode", "buffered"),
        ]:
            changed = copy.deepcopy(document)
            changed["evidence"]["attribution"]["requests"][0]["attribution"][
                "target_scope"
            ][key] = value
            with self.assertRaises(ScopeError):
                validate_scope_bindings(changed)

    def test_scope_or_provider_shortcuts_are_refused(self) -> None:
        """A name or device-filter flag cannot substitute for a complete scope."""
        for value in (True, 0, None, "false"):
            document = scoped()
            document["evidence"]["attribution"]["requests"][0]["attribution"][
                "target_device_filtered"
            ] = value
            with self.assertRaises(ScopeError):
                validate_scope_bindings(document)
        document = scoped()
        document["evidence"]["attribution"]["requests"][0]["attribution"][
            "provider"
        ] = "cgroup_blk"
        with self.assertRaises(ScopeError):
            validate_scope_bindings(document)

    def test_empty_duplicate_missing_or_unknown_bindings_are_refused(self) -> None:
        """Every request must resolve exactly once to a known configuration."""
        document = scoped()
        refs = document["bindings"]["attribution_requests"]
        for replacement in (
            [],
            refs + refs,
            [{"request_id": refs[0]["request_id"], "configuration_id": "missing"}],
            [{"request_id": [], "configuration_id": refs[0]["configuration_id"]}],
        ):
            changed = copy.deepcopy(document)
            changed["bindings"]["attribution_requests"] = replacement
            with self.assertRaises(ScopeError):
                validate_scope_bindings(changed)
        document["evidence"]["attribution"]["requests"] *= 2
        with self.assertRaises(ScopeError):
            validate_scope_bindings(document)

    def test_changed_configuration_receipt_is_refused(self) -> None:
        """Scope binding validates canonical fingerprints before accepting labels."""
        document = scoped()
        document["configurations"][0]["parameters"]["arms"][0]["tier_identity"][
            "client_id"
        ] = "other-client"
        with self.assertRaises(ScopeError):
            validate_scope_bindings(document)

    def test_ambiguous_arm_scope_is_refused(self) -> None:
        """Two named arms cannot ambiguously match the same request scope."""
        document = scoped()
        parameters = document["configurations"][0]["parameters"]
        other = copy.deepcopy(parameters["arms"][0])
        other["name"] += "-other"
        parameters["arms"].append(other)
        document["configurations"][0]["fingerprint"] = fingerprint_configuration(
            parameters, evidence_version="0.4.0"
        )
        with self.assertRaises(ScopeError):
            validate_scope_bindings(document)

    def test_scope_declaration_alone_cannot_produce_report_pass(self) -> None:
        """A validated declaration cannot produce a serving PASS before integration."""
        document = scoped()
        validate_scope_bindings(document)
        with self.assertRaisesRegex(EvidenceError, "Gate 1 only|nfs_measurement"):
            assess_evidence(document)
        document["schema_version"] = "0.3.2"
        with self.assertRaises(ScopeError):
            validate_scope_bindings(document)

    def test_legacy_scope_mixing_is_refused_for_every_old_version(self) -> None:
        """The device flag never masks an NFS declaration under an old version."""
        for version in ("0.3.0", "0.3.1", "0.3.2"):
            for filtered in (True, False):
                for addition in ("scope", "provider", "configuration"):
                    with self.subTest(
                        version=version, filtered=filtered, addition=addition
                    ):
                        document = example()
                        document["schema_version"] = version
                        record = document["evidence"]["attribution"]["requests"][0][
                            "attribution"
                        ]
                        record["target_device_filtered"] = filtered
                        if addition == "scope":
                            record["target_scope"] = copy.deepcopy(SCOPE)
                        elif addition == "provider":
                            record["provider"] = "nfs_mountstats"
                        else:
                            document["configurations"][0]["parameters"]["arms"][0][
                                "attribution_provider"
                            ] = "nfs_mountstats"
                        with self.assertRaises(EvidenceError):
                            assess_evidence(document)

    def test_legacy_fatal_cannot_bypass_scope_guard(self) -> None:
        """The rejection runs before the legacy fatal-observation shortcut."""
        document = example()
        document["validity"]["fatal_observations"] = [
            {"code": "synthetic-error", "scope": "measurement"}
        ]
        document["evidence"]["attribution"]["requests"][0]["attribution"][
            "target_scope"
        ] = copy.deepcopy(SCOPE)
        with self.assertRaisesRegex(EvidenceError, "legacy scope mixing"):
            assess_evidence(document)

    def test_legacy_corroboration_cannot_carry_the_new_provider(self) -> None:
        """Per-run observations cannot hide a new NFS provider under old evidence."""
        document = example()
        document["evidence"]["attribution"]["corroborating_observations"] = [
            {
                "provider": "nfs_mountstats",
                "granularity": "per_run",
                "observed_bytes": 8192,
                "artifact_refs": [],
            }
        ]
        with self.assertRaises(EvidenceError):
            assess_evidence(document)

    def test_malformed_version_is_a_configuration_error(self) -> None:
        """An explicit version selector is a string, never a coerced value."""
        parameters = example()["configurations"][0]["parameters"]
        for value in ([], {}, True, 4, None):
            with self.assertRaises(ConfigurationError):
                validate_configuration(parameters, evidence_version=value)

    def test_unsupported_attribution_has_no_request_bindings(self) -> None:
        """An unmeasured attribution block cannot contain measured scope records."""
        document = scoped()
        document["evidence"]["attribution"] = {
            "availability": {"status": "unsupported", "reason": "not measured"}
        }
        document["bindings"]["attribution_requests"] = []
        self.assertTrue(validate_scope_bindings(document))
        document["evidence"]["attribution"]["requests"] = []
        with self.assertRaises(ScopeError):
            validate_scope_bindings(document)

    def test_cli_refuses_legacy_scope_mixing_with_exit_two(self) -> None:
        """The actual report CLI exposes an input error, not a false serving claim."""
        document = example()
        document["evidence"]["attribution"]["requests"][0]["attribution"][
            "target_scope"
        ] = copy.deepcopy(SCOPE)
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "input.json"
            path.write_text(json.dumps(document))
            result = subprocess.run(
                [sys.executable, "-m", "kvprobe", "report", str(path)],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertNotIn("PASS", result.stdout)
        self.assertIn("legacy scope mixing is refused", result.stderr)


class ScopeSchemaTests(unittest.TestCase):
    """Schema parity for new fields; schema validation is a test-only dependency."""

    def test_draft_schema_and_generic_fixture_are_valid(self) -> None:
        """The complete draft references frozen legacy definitions offline."""
        v = validator()
        v.check_schema(v.schema)
        # Scope declarations alone are no longer complete report evidence.
        from nfs_c3_fixtures import normalized
        self.assertEqual(list(v.iter_errors(normalized())), [])
        self.assertTrue(list(v.iter_errors(scoped())))

    def test_scope_fragment_and_manual_identity_validation_agree(self) -> None:
        """Malformed types, IDs, transports, modes and keys have the same boundary."""
        values: list[Any] = [None, [], "nfs_service", {}, copy.deepcopy(SCOPE)]
        for key in SCOPE:
            changed = copy.deepcopy(SCOPE)
            del changed[key]
            values.append(changed)
            for bad in (None, [], {}, True, 1, "", " ", "x\n", "x/y", "x" * 129):
                changed = copy.deepcopy(SCOPE)
                changed[key] = bad
                values.append(changed)
        for key, value in [
            ("transport", "tcp"),
            ("kind", "block_device"),
            ("io_mode", "automatic"),
            ("extra", "x"),
        ]:
            changed = copy.deepcopy(SCOPE)
            changed[key] = value
            values.append(changed)
        v = validator("targetScope")
        for value in values:
            with self.subTest(value=value):
                try:
                    validate_target_scope(value)
                    accepted = True
                except ScopeError:
                    accepted = False
                self.assertEqual(accepted, not list(v.iter_errors(value)))

    def test_configuration_new_field_schema_and_api_agree(self) -> None:
        """Configuration and schema reject the same new identity errors."""
        parameters = scoped()["configurations"][0]["parameters"]
        candidates = [copy.deepcopy(parameters)]
        for action in (
            "missing",
            "wrong_provider",
            "wrong_transport",
            "extra_scope_key",
        ):
            changed = copy.deepcopy(parameters)
            arm = changed["arms"][0]
            if action == "missing":
                del arm["tier_identity"]
            elif action == "wrong_provider":
                arm["attribution_provider"] = "cgroup_blk"
            elif action == "wrong_transport":
                arm["tier_identity"]["transport"] = "tcp"
            else:
                arm["tier_identity"]["extra"] = "x"
            candidates.append(changed)
        v = validator("configurationParameters")
        for value in candidates:
            try:
                validate_configuration(value, evidence_version="0.4.0")
                accepted = True
            except ConfigurationError:
                accepted = False
            self.assertEqual(accepted, not list(v.iter_errors(value)))

    def test_draft_schema_cannot_label_nfs_as_device_filtered(self) -> None:
        """The draft request requires both the scope and the false device flag."""
        for action in ("missing", "device_true", "wrong_provider"):
            document = scoped()
            record = document["evidence"]["attribution"]["requests"][0]["attribution"]
            if action == "missing":
                del record["target_scope"]
            elif action == "device_true":
                record["target_device_filtered"] = True
            else:
                record["provider"] = "cgroup_blk"
            self.assertTrue(list(validator().iter_errors(document)))
            with self.assertRaises(ScopeError):
                validate_scope_bindings(document)
