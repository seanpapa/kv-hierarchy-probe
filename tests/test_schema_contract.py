# SPDX-License-Identifier: Apache-2.0

"""Hold the bundled examples to the versioned JSON Schema.

The reporter validates by hand and carries no runtime dependency, which is what
lets the portable core run anywhere. The cost is that ``schemas/`` can drift
from what the tool and the examples actually do. This module closes that gap
where it is cheapest to close: every bundled example must satisfy the schema its
own ``schema_version`` names.

``jsonschema`` is a test-only dependency (``pip install -e '.[test]'``). Without
it, or with a release too old to provide ``Draft202012Validator`` and
``referencing`` (jsonschema 4.18+), these checks skip rather than fail, so the
zero-dependency test run stays green and CI is the place the contract is
enforced.
"""

# Standard
import json
import unittest
from pathlib import Path


def _jsonschema_usable() -> bool:
    """Return True only if the installed jsonschema can run these checks.

    A bare ``find_spec`` is not enough: an older distribution-packaged
    jsonschema is importable yet lacks ``Draft202012Validator`` and the
    ``referencing`` package, which turned a skip into an ImportError.
    """
    try:
        # Third Party
        from jsonschema import Draft202012Validator  # noqa: F401
        from referencing import Registry, Resource  # noqa: F401
    except ImportError:
        return False
    return True


_HAS_JSONSCHEMA = _jsonschema_usable()

ROOT = Path(__file__).resolve().parent.parent
SCHEMAS = ROOT / "schemas"
EXAMPLES = ROOT / "examples"


def _registry():
    """Return a registry resolving every bundled schema offline.

    The v0.3 schema reuses v0.2 definitions through a relative ``$ref``, which
    resolves against its ``$id``. Registering each document under its own
    ``$id`` keeps that resolution local instead of fetching over the network.
    """
    # Third Party
    from referencing import Registry, Resource

    resources = []
    for path in sorted(SCHEMAS.glob("evidence-v*.schema.json")):
        document = json.loads(path.read_text(encoding="utf-8"))
        resources.append((document["$id"], Resource.from_contents(document)))
    return Registry().with_resources(resources)


def _validator(schema_version: str):
    """Return a draft 2020-12 validator for one evidence schema version."""
    # Third Party
    from jsonschema import Draft202012Validator

    schema = json.loads(
        (SCHEMAS / f"evidence-v{schema_version}.schema.json").read_text(
            encoding="utf-8"
        )
    )
    return Draft202012Validator(schema, registry=_registry())


@unittest.skipUnless(_HAS_JSONSCHEMA, "jsonschema is a test-only dependency")
class SchemaContractTest(unittest.TestCase):
    """The examples a reader is pointed at must match the published contract."""

    def test_bundled_examples_satisfy_their_declared_schema(self) -> None:
        """Every example validates against the version it declares."""
        documents = sorted((EXAMPLES / "evidence").glob("*.json"))
        self.assertTrue(documents, "no bundled evidence examples were found")
        validators: dict[str, object] = {}
        for path in documents:
            document = json.loads(path.read_text(encoding="utf-8"))
            version = document.get("schema_version")
            with self.subTest(example=path.name, schema_version=version):
                self.assertIn(
                    version,
                    {"0.3.0", "0.3.1"},
                    "example declares a schema version with no schema document",
                )
                if version not in validators:
                    validators[version] = _validator(version)
                errors = sorted(
                    validators[version].iter_errors(document),
                    key=lambda error: list(error.absolute_path),
                )
                self.assertEqual(
                    [],
                    [
                        f"{'/'.join(str(part) for part in error.absolute_path)}: "
                        f"{error.message}"
                        for error in errors
                    ],
                )

    def test_reference_configuration_satisfies_the_parameter_schema(self) -> None:
        """The configuration example operators copy must be schema-valid too."""
        # Third Party
        from jsonschema import Draft202012Validator

        schema = json.loads(
            (SCHEMAS / "evidence-v0.3.0.schema.json").read_text(encoding="utf-8")
        )
        validator = Draft202012Validator(
            {
                "$schema": schema["$schema"],
                "$id": schema["$id"],
                "$ref": "#/$defs/configurationParameters",
                "$defs": schema["$defs"],
            },
            registry=_registry(),
        )
        parameters = json.loads(
            (EXAMPLES / "reference-configuration.json").read_text(encoding="utf-8")
        )

        self.assertEqual([], [error.message for error in validator.iter_errors(parameters)])

    def test_a_device_identity_is_required_by_the_schema(self) -> None:
        """The schema, not only the hand validator, requires the device."""
        validator = _validator("0.3.0")
        document = json.loads(
            (EXAMPLES / "evidence" / "pass-v0.3.json").read_text(encoding="utf-8")
        )
        del document["configurations"][0]["parameters"]["runtime"]["device_id"]

        self.assertTrue(list(validator.iter_errors(document)))

    def test_the_schema_accepts_a_device_identity_from_any_platform(self) -> None:
        """No vendor address format is baked into the published contract."""
        validator = _validator("0.3.0")
        for value in ("00000000:01:00.0", "0000:a1:00.0", "gb10-0", "node2-accel-a"):
            with self.subTest(device_id=value):
                document = json.loads(
                    (EXAMPLES / "evidence" / "pass-v0.3.json").read_text(
                        encoding="utf-8"
                    )
                )
                runtime = document["configurations"][0]["parameters"]["runtime"]
                runtime["device_id"] = value
                # The fingerprint is checked by the reporter, not the schema, so
                # a changed identity is still schema-valid here.
                self.assertEqual([], list(validator.iter_errors(document)))

    def test_only_v0_3_1_carries_the_envelope_input_size(self) -> None:
        """The input size is a 0.3.1 addition; 0.3.0 stays closed to it."""
        document = json.loads(
            (EXAMPLES / "evidence" / "pass-v0.3.json").read_text(encoding="utf-8")
        )
        document["evidence"]["envelope"]["request_cacheable_tokens"] = 2048
        self.assertTrue(list(_validator("0.3.0").iter_errors(document)))

        document["schema_version"] = "0.3.1"
        validator = _validator("0.3.1")
        self.assertEqual([], [error.message for error in validator.iter_errors(document)])
        for value in (0, 2048.5, "2048"):
            with self.subTest(value=value):
                document["evidence"]["envelope"]["request_cacheable_tokens"] = value
                self.assertTrue(list(validator.iter_errors(document)))


if __name__ == "__main__":
    unittest.main()
