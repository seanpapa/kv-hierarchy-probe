# SPDX-License-Identifier: Apache-2.0
"""summarize_runs.py turns context runs into the summary verify_against_baseline reads."""

# Standard
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "tools"))

# First Party
import summarize_runs  # noqa: E402
import verify_against_baseline  # noqa: E402

SCOPE = {"model": "reference-32b", "chunk_tokens": 256, "kv_mib_per_chunk": 64}


def _run(arm: str, ttft: list[float], device: str = "", prompt_sha: str = "a" * 64) -> dict[str, Any]:
    """Return a synthetic context run with one 4096-token cell."""
    rows = [
        {"phase": "warmup", "ttft_s": 9.0, "correct": True, "tier_ok": True, "cacheable_tokens": 4096}
    ] + [
        {"phase": "measure", "ttft_s": value, "correct": True, "tier_ok": True, "cacheable_tokens": 4096}
        for value in ttft
    ]
    run: dict[str, Any] = {
        "mode": "context",
        "attribution": "recompute" if arm == "recompute" else "gds",
        "arguments": {
            "warmup": 1, "samples": len(ttft), "fillers": 6, "filler_tokens": 4096,
            "filler_key_policy": "unique", "prompt_profile": "transport", "prompt_seed": 17,
        },
        "result": {
            "4096": {
                "prompt": {
                    "version": "english-v1", "expected_output": "OK",
                    "prompt_tokens": 4116, "prompt_sha256": prompt_sha,
                },
                "rows": rows,
            }
        },
    }
    if device:
        run["cgroup_io_device"] = device
        run["cgroup_io_device_scope"] = "whole_disk"
    return run


class SummarizeRunsTests(unittest.TestCase):
    """The summary is built from the runs and refuses contradictions."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def _write(self, name: str, run: dict[str, Any]) -> Path:
        path = self.root / name
        path.write_text(json.dumps(run), encoding="utf-8")
        return path

    def test_pools_measured_rows_with_the_client_percentile_rule(self) -> None:
        """Warm-ups are left out; p50/p95 use min(int(q*n), n-1) of the sorted samples."""
        runs = [
            ("native-gds", self._write("a.json", _run("gds", [0.1, 0.4, 0.2]))),
            ("native-gds", self._write("b.json", _run("gds", [0.3, 0.5, 0.6]))),
        ]
        summary = summarize_runs.summarize(runs, SCOPE, 64)
        self.assertEqual(summary["baseline_version"], "0.1.0")
        self.assertNotIn("schema_version", summary)
        cell = summary["result"]["native-gds/4096"]["summary"]
        self.assertEqual(cell["n"], 6)
        self.assertEqual(cell["ttft_p50_s"], 0.4)
        self.assertEqual(cell["ttft_p95_s"], 0.6)
        self.assertEqual(cell["kv_expected_mib"], 1024)
        self.assertEqual(summary["scope"]["arm_order_runs"], 2)
        self.assertEqual(summary["workload"]["prompt_identity"]["4096"]["prompt_tokens"], 4116)

    def test_a_stated_scope_the_runs_contradict_is_refused(self) -> None:
        runs = [("native-gds", self._write("a.json", _run("gds", [0.1])))]
        with self.assertRaises(summarize_runs.SummaryError):
            summarize_runs.summarize(runs, SCOPE | {"filler_tokens": 8192}, 64)

    def test_runs_with_different_prompts_are_refused(self) -> None:
        runs = [
            ("recompute", self._write("a.json", _run("recompute", [0.7]))),
            ("native-gds", self._write("b.json", _run("gds", [0.2], prompt_sha="b" * 64))),
        ]
        with self.assertRaises(summarize_runs.SummaryError):
            summarize_runs.summarize(runs, SCOPE, 64)

    def test_counter_scope_follows_the_recorded_device(self) -> None:
        filtered = summarize_runs.summarize(
            [("native-gds", self._write("a.json", _run("gds", [0.2], device="259:4")))], SCOPE, 64
        )
        unfiltered = summarize_runs.summarize(
            [("native-gds", self._write("b.json", _run("gds", [0.2])))], SCOPE, 64
        )
        self.assertTrue(filtered["scope"]["target_device_filtered"])
        self.assertFalse(unfiltered["scope"]["target_device_filtered"])

    def test_the_output_is_a_candidate_the_comparator_accepts(self) -> None:
        """Summaries of the same runs compare as identical."""
        run = self._write("a.json", _run("gds", [0.2, 0.3]))
        scope = self._write("scope.json", {"scope": SCOPE})
        output = self.root / "candidate.json"
        # main() reports the file it wrote; keep that off the test runner's output.
        with contextlib.redirect_stdout(io.StringIO()):
            status = summarize_runs.main(
                ["--scope", str(scope), "--run", "native-gds", str(run), "--output", str(output)]
            )
        self.assertEqual(status, 0)
        candidate = json.loads(output.read_text(encoding="utf-8"))
        flat = verify_against_baseline.flatten(candidate)
        self.assertEqual(verify_against_baseline.compare(flat, flat, 0.05), [])
        self.assertEqual(verify_against_baseline.compare_identity(candidate, candidate), [])


if __name__ == "__main__":
    unittest.main()
