# SPDX-License-Identifier: Apache-2.0
"""Public CLI checks for the opt-in synthetic resource acceptance tool."""

# Standard
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class ResourceToolTests(unittest.TestCase):
    """Check diagnostic correctness and preservation, not machine-specific speed."""

    def setUp(self) -> None:
        """Prepare a fresh output path and explicit source-tree import path."""
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.output = Path(directory.name) / "results"
        self.root = Path(__file__).resolve().parents[1]
        self.script = self.root / "scripts/tools/benchmark_gate1_trace.py"

    def invoke(self, *extra: str) -> subprocess.CompletedProcess:
        """Invoke the supported CLI on a small fixture, bounded to 30 seconds.

        Args:
            extra: Additional command-line options.

        Returns:
            Captured child status and stdout/stderr.

        Raises:
            subprocess.TimeoutExpired: If the diagnostic exceeds 30 seconds.
        """
        return subprocess.run(
            [
                sys.executable,
                str(self.script),
                "--output-dir",
                str(self.output),
                "--commands",
                "100",
                *extra,
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            env={
                **os.environ,
                "PYTHONPATH": str(self.root),
                "PYTHONDONTWRITEBYTECODE": "1",
            },
        )

    def test_complete_and_missing_completion_checks(self) -> None:
        """Both cases disclose no serving verdict and retain independent hashes."""
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads((self.output / "report.json").read_text())
        self.assertEqual(report["serving_verification"], "not_assessed")
        for name, events, quality in (
            ("complete", 200, "usable"),
            ("missing-completion", 199, "incomplete"),
        ):
            self.assertEqual(report["cases"][name]["events"], events)
            self.assertTrue(all(report["cases"][name]["checks"].values()))
            observation = json.loads(
                (self.output / name / "observation.json").read_text()
            )
            self.assertEqual(observation["quality"]["status"], quality)
            self.assertEqual(observation["serving_verification"], "not_assessed")

    def test_existing_output_is_preserved(self) -> None:
        """An existing directory is refused without changing its contents."""
        self.output.mkdir()
        marker = self.output / "retain.txt"
        marker.write_text("retain")
        self.assertEqual(self.invoke().returncode, 2)
        self.assertEqual(marker.read_text(), "retain")
        self.assertEqual(list(self.output.iterdir()), [marker])

    def test_invalid_or_excessive_limits_are_refused(self) -> None:
        """Generation and resource budgets cannot silently become unbounded."""
        for extra in (
            ("--commands", "0"),
            ("--commands", "500001"),
            ("--max-seconds", "nan"),
            ("--max-rss-mib", "inf"),
        ):
            with self.subTest(extra=extra):
                self.assertEqual(self.invoke(*extra).returncode, 2)
                self.assertFalse(self.output.exists())

    def test_explicit_small_budget_fails_acceptance(self) -> None:
        """A budget miss is retained as diagnostic failure, not a tool PASS."""
        result = self.invoke("--max-rss-mib", "0.001")
        self.assertEqual(result.returncode, 1, result.stderr)
        report = json.loads((self.output / "report.json").read_text())
        self.assertFalse(report["accepted"])
        self.assertEqual(report["serving_verification"], "not_assessed")


if __name__ == "__main__":
    unittest.main()
