# SPDX-License-Identifier: Apache-2.0
"""Pin what docs/PROFILES.md says is enforced about measurement profiles today.

PROFILES.md states a boundary the schema cannot express: evidence v0.3 has no
``measurement_profile`` property, yet ``kvprobe report`` validates by hand and
so accepts the property and ignores it. That combination is a claim about
behaviour, not a design intent, and nothing pinned it. If the reporter later
starts rejecting the property, or starts deriving a profile-specific claim
scope from it, PROFILES.md becomes wrong and these tests say so.
"""
# Standard
import copy
import json
import unittest
from pathlib import Path
from typing import Any

# First Party
from kvprobe.report import assess_evidence

ROOT = Path(__file__).parents[1]
EXAMPLES = ROOT / "examples" / "evidence"
SCHEMAS = ROOT / "schemas"


def _load(name: str) -> dict[str, Any]:
    """Load one evidence example.

    Args:
        name: Example filename.

    Returns:
        Parsed evidence object.
    """
    return json.loads((EXAMPLES / name).read_text(encoding="utf-8"))


class SchemaHasNoProfileTest(unittest.TestCase):
    """The v0.3 schema must keep rejecting an unknown top-level property."""

    def test_schema_omits_measurement_profile_and_is_closed(self) -> None:
        """A document with ``measurement_profile`` must violate the schema."""
        schema = json.loads(
            (SCHEMAS / "evidence-v0.3.0.schema.json").read_text(encoding="utf-8")
        )
        self.assertNotIn("measurement_profile", schema["properties"])
        self.assertFalse(schema["additionalProperties"])


class ReporterIgnoresProfileTest(unittest.TestCase):
    """``report`` must accept the property and derive nothing from it."""

    def test_profile_does_not_change_the_assessment(self) -> None:
        """The verdict must be identical with and without the property."""
        baseline = assess_evidence(_load("pass-v0.3.json"))
        tagged = copy.deepcopy(_load("pass-v0.3.json"))
        tagged["measurement_profile"] = "disaggregated-path"
        self.assertEqual(assess_evidence(tagged), baseline)

    def test_profile_is_not_carried_into_the_assessment(self) -> None:
        """No profile value may reach the output and become a claim scope."""
        tagged = copy.deepcopy(_load("pass-v0.3.json"))
        tagged["measurement_profile"] = "disaggregated-path"
        assessment = assess_evidence(tagged)
        self.assertNotIn("measurement_profile", assessment)
        self.assertNotIn(
            "disaggregated-path", json.dumps(assessment, sort_keys=True)
        )


if __name__ == "__main__":
    unittest.main()
