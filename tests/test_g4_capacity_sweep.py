# SPDX-License-Identifier: Apache-2.0
"""Gate 4 producer keeps a record of every store and every retrieval."""

# Standard
import importlib.util
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "g4_capacity_sweep.py"
SPEC = importlib.util.spec_from_file_location("g4_capacity_sweep", SCRIPT)
if SPEC is None or SPEC.loader is None:
    raise ImportError(f"cannot load {SCRIPT}")
CLIENT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CLIENT)


class FakeHost:
    """Host-wide counters that move as scripted requests are served."""

    def __init__(self, steps: list[tuple[int, int, float]]) -> None:
        """Script one ``(write MiB, read MiB, TTFT)`` per request, in order."""
        self.steps = list(steps)
        self.counters = {"Reads": 0, "Writes": 0}

    def stream_ttft(self, port: int, body: dict[str, Any]) -> dict[str, Any]:
        """Serve the next scripted request and advance the counters."""
        wrote, read, ttft = self.steps.pop(0)
        self.counters["Writes"] += wrote
        self.counters["Reads"] += read
        return {"ttft_s": ttft, "output": ""}

    def nvfs_mib(self, kind: str) -> int:
        """Return the current cumulative counter."""
        return self.counters[kind]


class CapacityRecordTest(unittest.TestCase):
    """A fallback stays traceable to the store that preceded it."""

    def test_records_tell_an_unwritten_store_from_a_failed_load(self) -> None:
        entries = [{"messages": [{"content": str(index)}]} for index in range(3)]
        host = FakeHost(
            [
                # stores: entry 0 written, entry 1 wrote nothing, entry 2 written
                (1024, 0, 0.80),
                (0, 0, 0.79),
                (1024, 0, 0.81),
                # retrievals: device hit, fallback that re-stores, fallback after a write
                (0, 1024, 0.18),
                (1024, 0, 0.82),
                (0, 0, 0.93),
            ]
        )
        with patch.object(CLIENT, "stream_ttft", side_effect=host.stream_ttft), patch.object(
            CLIENT, "nvfs_mib", side_effect=host.nvfs_mib
        ):
            stores = CLIENT.store_all(8104, entries)
            tier = CLIENT.retrieve_all(8104, entries, 1024, 0.75)

        self.assertEqual([record["write_mib"] for record in stores], [1024, 0, 1024])
        self.assertEqual(
            [(r["entry"], r["kind"], r["read_mib"], r["write_mib"]) for r in tier["retrievals"]],
            [(0, "nvme", 1024, 0), (1, "fallback", 0, 1024), (2, "fallback", 0, 0)],
        )
        self.assertEqual(tier["counts"], {"dram": 0, "nvme": 1, "fallback": 2, "miss": 0})

    def test_unavailable_counter_is_recorded_as_none(self) -> None:
        entries = [{"messages": [{"content": "0"}]}]
        with patch.object(
            CLIENT, "stream_ttft", return_value={"ttft_s": 0.8, "output": ""}
        ), patch.object(CLIENT, "nvfs_mib", return_value=-1):
            stores = CLIENT.store_all(8104, entries)
            tier = CLIENT.retrieve_all(8104, entries, 1024, 0.75)

        self.assertIsNone(stores[0]["write_mib"])
        self.assertIsNone(tier["retrievals"][0]["read_mib"])
        self.assertEqual(tier["retrievals"][0]["kind"], "miss")


if __name__ == "__main__":
    unittest.main()
