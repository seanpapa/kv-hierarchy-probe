# SPDX-License-Identifier: Apache-2.0
"""Opt-in serial producer boundaries and legacy output compatibility."""

# Standard
import argparse
import csv
import importlib.util
import io
import itertools
import json
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

# First Party
from kvprobe.phases import PhaseWriter

SCRIPT = Path(__file__).parents[1] / "scripts" / "g5_controlled_kv_experiment.py"
SPEC = importlib.util.spec_from_file_location("phase_export_client", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
CLIENT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CLIENT)


class PhaseExportClientTests(unittest.TestCase):
    def test_serial_lifecycle_and_sample_schema(self) -> None:
        """Exported phases preserve the same serial results and classify samples."""
        args = argparse.Namespace(
            port=1, attribution="gds", arm="gds", container="c", contexts=[256],
            fillers=0, filler_tokens=256, prompt_profile="transport", prompt_seed=17,
            needle_position="early", filler_key_policy="unique", warmup=1, samples=1,
        )
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root = Path(directory)
            writers = {
                view: stack.enter_context(PhaseWriter(root / view))
                for view in ("lifecycle", "request", "envelope")
            }
            stack.enter_context(patch.object(
                CLIENT, "build_prompt_case", return_value=({}, 256, "OK", {})
            ))
            stack.enter_context(patch.object(CLIENT.time, "sleep"))
            stack.enter_context(patch.object(CLIENT, "evict"))
            stack.enter_context(patch.object(
                CLIENT, "stream_completion",
                side_effect=lambda *a: {"output": "OK", "ttft_s": 1, "total_s": 2},
            ))
            for name, value in (
                ("docker_log_lines", 0), ("nvfs_read_mib", 0),
                ("cgroup_read_bytes", 0), ("gpu_telemetry", {}), ("retrieve_line", ""),
            ):
                stack.enter_context(patch.object(CLIENT, name, return_value=value))
            legacy = CLIENT.sequential_sweep(args)
            exported = CLIENT.sequential_sweep(args, writers)
            self.assertEqual(legacy, exported)
            lifecycle = list(csv.DictReader(io.StringIO((root / "lifecycle").read_text())))
            self.assertEqual([r["phase"] for r in lifecycle], [
                "target-prime", "store-settle", "eviction-pressure", "eviction-pressure",
            ])
            for view in ("request", "envelope"):
                rows = list(csv.DictReader(io.StringIO((root / view).read_text())))
                self.assertEqual([r["request_id"] for r in rows], [
                    "ctx-256-warmup-0", "ctx-256-measure-1",
                ])

    def test_request_boundaries_exclude_snapshots_and_logs(self) -> None:
        """The HTTP interval is nested only across files, not mixed with counters."""
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            ticks = itertools.count(100, 10)
            events = []

            def action(name: str, result: object) -> object:
                events.append((name, next(ticks)))
                return result

            path = Path(directory)
            request = stack.enter_context(PhaseWriter(path / "r", clock=lambda: next(ticks)))
            envelope = stack.enter_context(PhaseWriter(path / "e", clock=lambda: next(ticks)))
            for name, result in (
                ("docker_log_lines", 0), ("nvfs_read_mib", 0),
                ("cgroup_read_bytes", 0), ("gpu_telemetry", {}),
                ("retrieve_line", ""),
            ):
                stack.enter_context(patch.object(
                    CLIENT, name, side_effect=lambda *a, n=name, r=result: action(n, r)
                ))
            stack.enter_context(patch.object(
                CLIENT, "stream_completion",
                side_effect=lambda *a: action("http", {"output": "OK", "ttft_s": 1}),
            ))
            with envelope.record("r1", "retrieve-envelope"):
                row = CLIENT.measure_one(
                    1, "gds", "c", {}, 64, "OK",
                    phase_writer=request, request_id="r1",
                )
            r = next(csv.DictReader(io.StringIO((path / "r").read_text())))
            e = next(csv.DictReader(io.StringIO((path / "e").read_text())))
            start, end = int(r["start_ns"]), int(r["end_ns"])
            self.assertLess(int(e["start_ns"]), start)
            self.assertGreater(int(e["end_ns"]), end)
            for name, stamp in events:
                self.assertEqual(start < stamp < end, name == "http")
            self.assertEqual(r["expected_read_bytes"], str(64 * 1024**2))
            self.assertTrue(row["correct"])
            self.assertNotIn("request_id", row)

    def test_http_error_exports_both_views_and_reraises(self) -> None:
        """Failure is retained, never converted into a completed KV request."""
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            path = Path(directory)
            ticks = itertools.count(1)
            request = stack.enter_context(PhaseWriter(path / "r", clock=lambda: next(ticks)))
            envelope = stack.enter_context(PhaseWriter(path / "e", clock=lambda: next(ticks)))
            for name, value in (
                ("docker_log_lines", 0), ("nvfs_read_mib", 0),
                ("cgroup_read_bytes", 0), ("gpu_telemetry", {}),
            ):
                stack.enter_context(patch.object(CLIENT, name, return_value=value))
            stack.enter_context(patch.object(
                CLIENT, "stream_completion", side_effect=RuntimeError("private prompt")
            ))
            with self.assertRaises(RuntimeError):
                with envelope.record("r1", "retrieve-envelope"):
                    CLIENT.measure_one(
                        1, "gds", "c", {}, 64, "OK",
                        phase_writer=request, request_id="r1",
                    )
            for view in ("r", "e"):
                text = (path / view).read_text()
                row = next(csv.DictReader(io.StringIO(text)))
                self.assertEqual(row["outcome"], "error")
                self.assertNotIn("private prompt", text)

    def test_legacy_main_omits_new_argument_and_keeps_serial_call(self) -> None:
        """No option adds no output field or producer argument."""
        with tempfile.TemporaryDirectory() as directory:
            args = argparse.Namespace(
                mode="context", arm="gds", attribution="gds", backend=None,
                port=1, container="c", output=Path(directory) / "out.json",
                phase_export_dir=None,
            )
            with patch.object(CLIENT, "parse_args", return_value=args), patch.object(
                CLIENT, "sequential_sweep", return_value={}
            ) as sweep, patch.object(CLIENT, "gpu_identity", return_value={}):
                CLIENT.main()
            sweep.assert_called_once_with(args)
            data = json.loads(args.output.read_text())
            self.assertNotIn("phase_export_dir", data["arguments"])
            self.assertEqual(data["result"], {})

    def test_existing_export_directory_refuses_before_workload(self) -> None:
        """No previously retained phase artifact can be overwritten."""
        with tempfile.TemporaryDirectory() as directory:
            args = argparse.Namespace(
                output=Path(directory) / "out.json", phase_export_dir=Path(directory),
            )
            with patch.object(CLIENT, "parse_args", return_value=args), patch.object(
                CLIENT, "sequential_sweep"
            ) as sweep, self.assertRaises(FileExistsError):
                CLIENT.main()
            sweep.assert_not_called()

    def test_opt_in_main_creates_separate_views_and_provenance(self) -> None:
        """The option exports companions without changing request result fields."""
        with tempfile.TemporaryDirectory() as directory:
            args = argparse.Namespace(
                mode="context", arm="gds", attribution="gds", backend=None,
                port=1, container="c", output=Path(directory) / "out.json",
                phase_export_dir=Path(directory) / "phases",
            )
            with patch.object(CLIENT, "parse_args", return_value=args), patch.object(
                CLIENT, "sequential_sweep", return_value={"unchanged": True}
            ) as sweep, patch.object(CLIENT, "gpu_identity", return_value={}):
                CLIENT.main()
            self.assertEqual(set(sweep.call_args.args[1]), {"lifecycle", "request", "envelope"})
            self.assertEqual(set(p.name for p in args.phase_export_dir.iterdir()), {
                "lifecycle.csv", "request.csv", "envelope.csv", "producer.json",
            })
            metadata = json.loads((args.phase_export_dir / "producer.json").read_text())
            self.assertEqual(metadata["capture_coverage"], "not_assessed")
            self.assertEqual(metadata["serving_verification"], "not_assessed")
            self.assertEqual(len(metadata["producer_sha256"]), 64)
            self.assertEqual(json.loads(args.output.read_text())["result"], {"unchanged": True})

    def test_nonlinux_and_recompute_export_are_refused(self) -> None:
        """Unsupported producer scopes fail before any workload invocation."""
        for system, arm in (("Darwin", "gds"), ("Linux", "recompute")):
            with self.subTest(system=system, arm=arm), patch.object(
                CLIENT.platform, "system", return_value=system
            ), patch.object(sys, "argv", [
                "client", "context", "--arm", arm, "--output", "out.json",
                "--phase-export-dir", "new-phases",
            ]), self.assertRaises(SystemExit):
                CLIENT.parse_args()

    def test_concurrency_export_is_refused(self) -> None:
        """Serial interval claims are not silently applied to concurrent batches."""
        with patch.object(sys, "argv", [
            "client", "concurrency", "--arm", "gds", "--output", "out.json",
            "--phase-export-dir", "new-phases",
        ]), self.assertRaises(SystemExit):
            CLIENT.parse_args()
