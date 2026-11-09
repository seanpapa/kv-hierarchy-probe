# SPDX-License-Identifier: Apache-2.0
"""Public phase writer contract, independent of a serving stack."""

# Standard
import csv
import io
import tempfile
import unittest
from pathlib import Path

# First Party
from kvprobe.phases import PhaseWriter


class PhaseWriterTests(unittest.TestCase):
    def test_normal_and_error_intervals(self) -> None:
        """Keep boundaries and failures without exposing exception messages."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "phases.csv"
            ticks = iter([10, 20, 30, 40])
            with PhaseWriter(path, clock=lambda: next(ticks)) as writer:
                with writer.record("r1", "retrieve", expected_read_bytes=64):
                    pass
                with self.assertRaisesRegex(RuntimeError, "secret"):
                    with writer.record("r2", "retrieve"):
                        raise RuntimeError("secret")
            rows = list(csv.DictReader(io.StringIO(path.read_text())))
            self.assertEqual(len(rows), 2)
            self.assertEqual((rows[0]["start_ns"], rows[0]["end_ns"]), ("10", "20"))
            self.assertEqual(rows[0]["expected_read_bytes"], "64")
            self.assertEqual(rows[0]["outcome"], "completed")
            self.assertEqual(rows[1]["outcome"], "error")
            self.assertEqual(rows[1]["error_type"], "RuntimeError")
            self.assertNotIn("secret", path.read_text())

    def test_existing_output_and_symlink_are_refused(self) -> None:
        """Exclusive creation preserves existing artifacts and link targets."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "existing"
            path.write_text("retain")
            link = Path(directory) / "link"
            link.symlink_to(path)
            for candidate in (path, link):
                with self.assertRaises(FileExistsError):
                    PhaseWriter(candidate)
            self.assertEqual(path.read_text(), "retain")

    def test_duplicates_nesting_and_invalid_bytes(self) -> None:
        """Reject ambiguous serial intervals before starting their actions."""
        with tempfile.TemporaryDirectory() as directory:
            ticks = iter([1, 2])
            with PhaseWriter(
                Path(directory) / "p", clock=lambda: next(ticks)
            ) as writer:
                for value in (-1, True, 1.5):
                    with self.assertRaises(ValueError):
                        with writer.record("r", "p", expected_read_bytes=value):
                            self.fail("invalid action entered")
                with writer.record("r", "p"):
                    with self.assertRaises(ValueError):
                        with writer.record("r2", "p"):
                            self.fail("nested action entered")
                with self.assertRaises(ValueError):
                    with writer.record("r", "p"):
                        self.fail("duplicate action entered")

    def test_invalid_clock_is_not_fabricated(self) -> None:
        """Zero or backward duration never becomes a valid exported interval."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "p"
            with PhaseWriter(path, clock=lambda: 1) as writer:
                with self.assertRaises(ValueError):
                    with writer.record("r", "p"):
                        pass
                with self.assertRaises(RuntimeError):
                    with writer.record("r2", "p"):
                        raise RuntimeError("original")
            self.assertEqual(list(csv.DictReader(io.StringIO(path.read_text()))), [])
