# SPDX-License-Identifier: Apache-2.0

"""Every measurement script must record how long its run took.

Wall-clock cost is planning information on a shared host: it is what tells an
operator whether a gate fits in the window they have. The scripts are copied to
a host one file at a time, so each carries its own timing rather than importing
a helper — these tests are what keeps the four copies consistent.
"""

# Standard
import re
import subprocess
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).parents[1] / "scripts"
MEASUREMENT = (
    "g2_context_sweep.py",
    "g3_admission_cost.py",
    "g4_capacity_sweep.py",
    "g5_controlled_kv_experiment.py",
)
STAMP = 'time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())'


class RunTiming(unittest.TestCase):
    def test_every_measurement_script_starts_a_clock(self) -> None:
        for name in MEASUREMENT:
            with self.subTest(script=name):
                body = (SCRIPTS / name).read_text(encoding="utf-8")
                self.assertIn("_STARTED_MONO = time.monotonic()", body)
                self.assertIn(f"_STARTED_AT = {STAMP}", body)

    def test_every_measurement_script_writes_the_three_fields(self) -> None:
        for name in MEASUREMENT:
            with self.subTest(script=name):
                body = (SCRIPTS / name).read_text(encoding="utf-8")
                for field in ("started_at", "ended_at", "elapsed_s"):
                    self.assertIn(f'"{field}"', body)

    def test_stamp_is_iso_8601_utc_to_the_second(self) -> None:
        rendered = subprocess.run(
            ["python3", "-c", f"import time; print({STAMP})"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        self.assertRegex(rendered, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

    def test_g2_keeps_its_summary_a_list(self) -> None:
        """Timing must not change the shape the baseline comparator reads."""
        body = (SCRIPTS / "g2_context_sweep.py").read_text(encoding="utf-8")
        self.assertIn('json.dump(results, open(f"{RUN}/sweep-summary.json"', body)
        self.assertIn("run-timing.json", body)


class ScriptsStillStart(unittest.TestCase):
    """Import-time failures are the realistic risk of an unexecuted change.

    The timing clock runs at module level, so a mistake there breaks the script
    before it reaches a host. ``--help`` executes the imports and the argument
    parser and exits, which is as far as these can be driven without a GPU.
    """

    def test_clients_reach_their_argument_parser(self) -> None:
        for name in ("g3_admission_cost.py", "g4_capacity_sweep.py",
                     "g5_controlled_kv_experiment.py"):
            with self.subTest(script=name):
                result = subprocess.run(
                    ["python3", str(SCRIPTS / name), "--help"],
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(result.returncode, 0, result.stderr[-400:])
                self.assertIn("usage", result.stdout.lower())


if __name__ == "__main__":
    unittest.main()
