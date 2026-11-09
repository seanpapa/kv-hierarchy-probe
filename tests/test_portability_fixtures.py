# SPDX-License-Identifier: Apache-2.0
"""Portable fixtures preserve failure shapes without deployment identifiers."""

# Standard
import json
import subprocess
import unittest
from pathlib import Path
from typing import Any


ROOT = Path(__file__).parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "portability"
#: Archive suffixes. Raw run material travels as archives, so none may be tracked.
ARCHIVE_SUFFIXES = (".tar", ".gz", ".tgz", ".zip", ".7z", ".xz", ".zst")
EXPECTED_CASES = {
    "aggregate-cgroup-attribution": "SKIP_UNSUPPORTED",
    "allocation-failure": "INVALID",
    "discrete-native-gds-pass": "PASS",
    "mixed-configuration-invalid": "INVALID",
    "unified-memory-telemetry-na": "unsupported",
}


def _load_fixtures() -> list[dict[str, Any]]:
    """Load every portability fixture.

    Returns:
        Parsed fixture objects sorted by filename.
    """
    return [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(FIXTURES.glob("*.json"))
    ]


class PortabilityFixtureTest(unittest.TestCase):
    """Lock the captured portability cases and repository sanitation boundary."""

    def test_no_raw_bundles_are_tracked(self) -> None:
        """Raw deployment bundles must stay outside the public repository.

        Raw run material travels as archives, and archives can carry container
        inspection output, logs and provenance that name a deployment. The check
        covers every tracked path rather than one directory, because a bundle
        committed anywhere is published.
        """
        tracked = subprocess.run(
            ["git", "ls-files"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        if tracked.returncode != 0:
            self.skipTest("not a git checkout; tracked files cannot be listed")
        archives = sorted(
            name
            for name in tracked.stdout.splitlines()
            if name.lower().endswith(ARCHIVE_SUFFIXES)
        )

        self.assertEqual(archives, [])

    def test_expected_failure_shapes_are_preserved(self) -> None:
        """Every captured case must retain its intended future classification."""
        fixtures = _load_fixtures()
        actual: dict[str, str] = {}
        for fixture in fixtures:
            baseline = fixture["baseline"]
            result = baseline.get(
                "target_gate_status", baseline.get("target_capability_status")
            )
            actual[fixture["case_id"]] = result

        self.assertEqual(actual, EXPECTED_CASES)

    def test_fixtures_use_generic_host_profiles(self) -> None:
        """Fixtures must describe capabilities rather than named deployments."""
        for fixture in _load_fixtures():
            with self.subTest(case_id=fixture["case_id"]):
                profile = fixture["host_profile"]
                self.assertIn(profile["architecture"], {"aarch64", "x86_64"})
                self.assertIn(
                    profile["memory_architecture"], {"discrete", "unified"}
                )
                self.assertNotIn("hostname", profile)
                self.assertNotIn("username", profile)


if __name__ == "__main__":
    unittest.main()
