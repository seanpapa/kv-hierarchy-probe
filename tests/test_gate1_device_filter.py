# SPDX-License-Identifier: Apache-2.0
"""Gate 1 passes on per-request evidence from a counter filtered to the tier's device.

The runs are built from the synthetic block-tier fixture by swapping its counter
for the per-device cgroup counter. Nothing here is a measurement: the tests fix
which recorded evidence passes, which stays unproven, and which fails.
"""

# Standard
import copy
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

# First Party
from kvprobe.ingest import ingest_files
from kvprobe.report import assess_evidence

FIXTURES = Path(__file__).parent / "fixtures" / "external" / "block-tier"
MEASURED = Path(__file__).parents[1] / "examples" / "evidence" / "gate1-pass-filtered-counter.json"
MIB = 1024 * 1024


def _cgroup_run(
    scale: float = 1.0, device: str = "259:7", scope: str | None = "whole_disk"
) -> dict[str, Any]:
    """Return the served fixture observed through the per-device cgroup counter.

    Args:
        scale: Factor applied to every row's observed bytes.
        device: Recorded ``cgroup_io_device``; empty to omit it.
        scope: Recorded ``cgroup_io_device_scope``; None to omit it.

    Returns:
        A context run in the external run shape.
    """
    run = json.loads((FIXTURES / "served.json").read_text(encoding="utf-8"))
    for row in run["result"]["4096"]["rows"]:
        row["cgroup_read_bytes"] = int(row.pop("nvfs_read_mib") * MIB * scale)
    if device:
        run["cgroup_io_device"] = device
    if scope is not None:
        run["cgroup_io_device_scope"] = scope
    return run


def _gate_1(run: dict[str, Any]) -> dict[str, Any]:
    """Ingest ``run`` with a cgroup-provider configuration and return Gate 1."""
    configuration = json.loads(
        (FIXTURES / "configuration.json").read_text(encoding="utf-8")
    )
    configuration = copy.deepcopy(configuration)
    configuration["arms"][0]["attribution_provider"] = "cgroup_blk"
    with tempfile.TemporaryDirectory() as directory:
        run_path = Path(directory) / "run.json"
        configuration_path = Path(directory) / "configuration.json"
        run_path.write_text(json.dumps(run), encoding="utf-8")
        configuration_path.write_text(json.dumps(configuration), encoding="utf-8")
        document = ingest_files([run_path], configuration_path, run_id="cgroup")
    assessment = assess_evidence(document)
    return next(gate for gate in assessment["gates"] if gate["id"] == 1)


class Gate1DeviceFilterTests(unittest.TestCase):
    """The per-device counter makes Gate 1 reachable without weakening it."""

    def test_whole_disk_counter_passes(self) -> None:
        """Expected bytes on the tier's own disk, per request, pass Gate 1."""
        gate = _gate_1(_cgroup_run())
        self.assertEqual(gate["status"], "PASS")
        self.assertIn("3/3 target-tier requests have per-request", gate["reason"])
        self.assertIn("(corroborated)", gate["reason"])

    def test_partition_widened_to_its_disk_passes(self) -> None:
        """A tier on a partition is counted at its disk; small noise stays in tolerance."""
        gate = _gate_1(_cgroup_run(scale=1.02, scope="widened_to_parent_disk"))
        self.assertEqual(gate["status"], "PASS")

    def test_run_without_a_recorded_device_is_unproven(self) -> None:
        """A run from the summing reader records no device and cannot pass."""
        gate = _gate_1(_cgroup_run(device="", scope=None))
        self.assertEqual(gate["status"], "UNPROVEN")
        self.assertIn("evidence is insufficient, not a measured failure", gate["reason"])

    def test_unknown_scope_is_unproven(self) -> None:
        """Only scopes the reader is known to write count as filtered."""
        gate = _gate_1(_cgroup_run(scope="unresolved"))
        self.assertEqual(gate["status"], "UNPROVEN")

    def test_filtered_counter_reading_too_much_fails(self) -> None:
        """Beyond tolerance on the tier's own device is a measured disagreement."""
        gate = _gate_1(_cgroup_run(scale=1.5))
        self.assertEqual(gate["status"], "FAIL")

    def test_filtered_counter_reading_too_little_fails(self) -> None:
        """Half the expected bytes fails, as it would on any counter."""
        gate = _gate_1(_cgroup_run(scale=0.5))
        self.assertEqual(gate["status"], "FAIL")

    def test_bundled_measured_example_passes_gates_1_and_2(self) -> None:
        """The bundled re-measurement document keeps its verdicts under the current contract."""
        assessment = assess_evidence(json.loads(MEASURED.read_text(encoding="utf-8")))
        gates = {gate["id"]: gate for gate in assessment["gates"]}
        self.assertEqual(gates[1]["status"], "PASS")
        self.assertIn("10/10 target-tier requests have per-request", gates[1]["reason"])
        self.assertIn("(corroborated)", gates[1]["reason"])
        self.assertEqual(gates[2]["status"], "PASS")
        self.assertEqual(assessment["overall"], "PARTIAL")


if __name__ == "__main__":
    unittest.main()
