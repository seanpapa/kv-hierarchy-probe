# SPDX-License-Identifier: Apache-2.0

"""The public reader accepts configuration-bound evidence or names a normalization path."""

# Standard
import unittest

# First party
from kvprobe.report import EvidenceError, assess_evidence


class UnsupportedSchemaVersions(unittest.TestCase):
    """Unsupported input is refused instead of upgraded with invented configuration."""

    def test_reading_v0_1_is_refused(self) -> None:
        payload = {"schema_version": "0.1.0", "run_id": "removal-check"}
        with self.assertRaises(EvidenceError) as caught:
            assess_evidence(payload)

        self.assertIn("kvprobe ingest", str(caught.exception))

    def test_an_unknown_version_names_the_supported_ones(self) -> None:
        payload = {"schema_version": "9.9.9", "run_id": "unknown-check"}
        with self.assertRaises(EvidenceError) as caught:
            assess_evidence(payload)

        self.assertNotIn("0.2.0", str(caught.exception))
        self.assertIn("0.3.0", str(caught.exception))
        self.assertIn("0.3.1", str(caught.exception))

    def test_configuration_free_v0_2_is_refused(self) -> None:
        """The public reader cannot invent a configuration for old evidence."""
        payload = {"schema_version": "0.2.0", "run_id": "unsupported-check"}
        with self.assertRaises(EvidenceError) as caught:
            assess_evidence(payload)
        self.assertIn("0.2.0", str(caught.exception))
        self.assertIn("kvprobe ingest", str(caught.exception))
