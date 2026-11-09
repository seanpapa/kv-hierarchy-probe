# SPDX-License-Identifier: Apache-2.0

"""Every run must tell the operator the ``device_id`` to write down.

The configuration is written by hand and ``kvprobe ingest`` refuses one that
names a device the run did not use, so the value has to travel from the run to
the operator without being retyped from memory. The scripts are copied to a host
one file at a time, so each carries its own copy of the notice — these tests are
what keeps the copies consistent, and what keeps the unavailable case from going
out silently.
"""

# Standard
import subprocess
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).parents[1] / "scripts"
PRODUCERS = (
    "g3_admission_cost.py",
    "g4_capacity_sweep.py",
    "g5_controlled_kv_experiment.py",
)


def _source(name: str) -> str:
    """Return a measurement script's source.

    Args:
        name: File name under ``scripts/``.

    Returns:
        The file's text.
    """
    return (SCRIPTS / name).read_text(encoding="utf-8")


class DeviceIdentityNotice(unittest.TestCase):
    """The value ingest will check must be printed where the operator sees it."""

    def test_every_producer_reports_the_device(self) -> None:
        for name in PRODUCERS:
            with self.subTest(script=name):
                source = _source(name)
                self.assertIn("def report_device_identity(", source)
                self.assertIn("report_device_identity(identity)", source)

    def test_the_notice_recommends_the_pci_address(self) -> None:
        """The recommendation belongs where the operator is about to act on it."""
        for name in PRODUCERS:
            with self.subTest(script=name):
                self.assertIn("PCI address", _source(name))

    def test_an_unnamed_device_is_announced_not_omitted(self) -> None:
        """Silence would hide that the configuration's claim goes unchecked."""
        for name in PRODUCERS:
            with self.subTest(script=name):
                source = _source(name)
                self.assertIn("NOT RECORDED", source)
                self.assertIn("unverified", source)

    def test_the_notice_runs_for_both_outcomes(self) -> None:
        """Both branches must execute: an operator sees one of them every run."""
        for name in PRODUCERS:
            with self.subTest(script=name):
                program = (
                    "import importlib.util, sys;"
                    f"spec = importlib.util.spec_from_file_location('m', r'{SCRIPTS / name}');"
                    "m = importlib.util.module_from_spec(spec);"
                    "sys.modules['m'] = m; spec.loader.exec_module(m);"
                    "m.report_device_identity({});"
                    "m.report_device_identity("
                    "{'device_id': '00000000:65:00.0', 'gpu_index': 0})"
                )
                result = subprocess.run(
                    [sys.executable, "-c", program],
                    capture_output=True,
                    check=False,
                    text=True,
                )

                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("NOT RECORDED", result.stdout)
                self.assertIn("00000000:65:00.0", result.stdout)


if __name__ == "__main__":
    unittest.main()
