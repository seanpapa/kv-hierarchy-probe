# SPDX-License-Identifier: Apache-2.0
"""The bundled English-v1 reference remains sanitized and comparator-readable."""

# Standard
import json
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
REFERENCE = ROOT / "examples" / "baselines" / "transport-english-v1-20260925.json"
COMPARATOR = ROOT / "scripts" / "tools" / "verify_against_baseline.py"


class EnglishReferenceBaselineTest(unittest.TestCase):
    """Lock the public reference's identity, coverage, and executable format."""

    def test_reference_covers_all_four_arms_and_five_contexts(self) -> None:
        """The reference must contain exactly the reviewed 20 comparison cells."""
        evidence = json.loads(REFERENCE.read_text(encoding="utf-8"))
        self.assertEqual(evidence["workload"]["profile"], "transport")
        self.assertEqual(evidence["workload"]["version"], "english-v1")
        self.assertEqual(evidence["workload"]["seed"], 17)
        self.assertEqual(
            set(evidence["workload"]["prompt_identity"]),
            {"256", "512", "1024", "2048", "4096"},
        )
        expected = {
            f"{arm}/{context}"
            for arm in ("recompute", "posix", "native-gds", "nixl-gds-mt")
            for context in (256, 512, 1024, 2048, 4096)
        }
        self.assertEqual(set(evidence["result"]), expected)

    def test_reference_is_accepted_by_the_comparator(self) -> None:
        """The documented command must consume the bundled format without conversion."""
        result = subprocess.run(
            [
                "python3",
                str(COMPARATOR),
                "--baseline",
                str(REFERENCE),
                "--candidate",
                str(REFERENCE),
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("compared 20 label(s)", result.stdout)
        self.assertIn("PASS", result.stdout)

    def test_reference_rejects_a_different_workload_identity(self) -> None:
        """Similar metrics cannot hide a different prompt profile or seed."""
        candidate = json.loads(REFERENCE.read_text(encoding="utf-8"))
        candidate["workload"]["seed"] = 18
        with tempfile.TemporaryDirectory() as directory:
            candidate_path = Path(directory) / "candidate.json"
            candidate_path.write_text(json.dumps(candidate), encoding="utf-8")
            result = subprocess.run(
                [
                    "python3",
                    str(COMPARATOR),
                    "--baseline",
                    str(REFERENCE),
                    "--candidate",
                    str(candidate_path),
                ],
                capture_output=True,
                text=True,
            )

        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("workload: candidate does not match reference", result.stdout)


if __name__ == "__main__":
    unittest.main()
