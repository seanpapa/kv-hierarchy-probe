# SPDX-License-Identifier: Apache-2.0
"""The controlled launcher runs on a host without GDS: POSIX preflight and Gate 5.

The launcher is executed end to end against stub ``docker``, ``nvidia-smi``,
``curl``, ``findmnt`` and ``gdscheck`` commands and a stub client, so the tests
see exactly which arms it starts and what it asks the client to measure.
"""

# Standard
import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

# First Party
from kvprobe.ingest import build_document
from kvprobe.report import assess_evidence

LAUNCHER = Path(__file__).resolve().parents[1] / "scripts" / "g5_controlled_kv_experiment.sh"
MODEL = "/models/hub/snapshots/rev1"

STUBS = {
    # No container exists before a start (the ownership query fails, so there is nothing to
    # remove); after it, `inspect` describes it and `logs` shows an LMCache start.
    "docker": (
        'echo "docker $*" >> "$CALLS"\n'
        'case "$1" in\n'
        '  inspect) [[ "$2" == --format ]] && exit 1; echo "[]" ;;\n'
        "  logs) echo LMCacheEngine ;;\n"
        "  image) echo sha256:stub ;;\n"
        "esac\n"
    ),
    "nvidia-smi": 'echo "nvidia-smi $*" >> "$CALLS"\necho 0\n',
    "curl": f'echo \'{{"data": [{{"root": "{MODEL}"}}]}}\'\n',
    "findmnt": 'echo "/gds-host /dev/stub ext4 rw"\n',
}


def _executable(path: Path, body: str) -> None:
    """Write a bash script and make it executable."""
    path.write_text("#!/usr/bin/env bash\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


class Host:
    """A temporary host: stub commands, a model directory, a mount and an evidence dir."""

    def __init__(self, directory: Path, *, gdscheck: bool, model: str = MODEL) -> None:
        self.calls = directory / "calls"
        self.client_calls = directory / "client-calls"
        bin_dir = directory / "bin"
        bin_dir.mkdir()
        for name, body in STUBS.items():
            _executable(bin_dir / name, body)
        self.gdscheck = directory / "gdscheck"
        if gdscheck:
            _executable(
                self.gdscheck,
                'echo "gdscheck $*" >> "$CALLS"\n'
                "echo ' NVMe : Supported'\n"
                "echo ' properties.use_compat_mode : false'\n",
            )
        client = directory / "client.py"
        client.write_text(
            "import json, os, sys\n"
            "with open(os.environ['CLIENT_CALLS'], 'a') as out:\n"
            "    out.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        )
        models = directory / "models"
        (models / model.removeprefix("/models/")).mkdir(parents=True)
        mount = directory / "mount"
        mount.mkdir()
        self.evidence = directory / "evidence"
        self.env = dict(
            os.environ,
            PATH=f"{bin_dir}:{os.environ['PATH']}",
            CALLS=str(self.calls),
            CLIENT_CALLS=str(self.client_calls),
            CLIENT=str(client),
            GDSCHECK=str(self.gdscheck),
            IMAGE="stub-image",
            GDS_MOUNT=str(mount),
            MODEL_HOST_DIR=str(models),
            MODEL=MODEL,
            EVIDENCE=str(self.evidence),
        )

    def run(self, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
        """Run the launcher with the given mode and arguments."""
        return subprocess.run(
            ["bash", str(LAUNCHER), *args],
            capture_output=True,
            text=True,
            env=self.env | env,
            check=False,
        )

    def command_calls(self) -> str:
        return self.calls.read_text() if self.calls.exists() else ""

    def client_runs(self) -> list[list[str]]:
        if not self.client_calls.exists():
            return []
        return [json.loads(line) for line in self.client_calls.read_text().splitlines()]


def _option(argv: list[str], name: str) -> str:
    """Return the value given for one client option."""
    return argv[argv.index(name) + 1]


class Preflight(unittest.TestCase):
    """`preflight [ARM]` checks GDS only for the GDS arm."""

    def test_posix_preflight_needs_no_gdscheck(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            host = Host(Path(directory), gdscheck=False)
            result = host.run("preflight", "posix")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("GDS check skipped: the posix arm does not use cuFile", result.stdout)
            self.assertNotIn("gdscheck", host.command_calls())
            self.assertIn("sha256:stub", result.stdout)

    def test_recompute_preflight_needs_no_gdscheck(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = Host(Path(directory), gdscheck=False).run("preflight", "recompute")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("GDS check skipped", result.stdout)

    def test_default_and_gds_preflight_name_the_missing_gdscheck(self) -> None:
        for args in (("preflight",), ("preflight", "gds")):
            with self.subTest(args=args), tempfile.TemporaryDirectory() as directory:
                host = Host(Path(directory), gdscheck=False)
                result = host.run(*args)
                self.assertEqual(result.returncode, 1)
                self.assertIn(f"gdscheck not found at {host.gdscheck}", result.stderr)
                self.assertIn("preflight posix", result.stderr)

    def test_default_and_gds_preflight_run_gdscheck(self) -> None:
        for args in (("preflight",), ("preflight", "gds")):
            with self.subTest(args=args), tempfile.TemporaryDirectory() as directory:
                host = Host(Path(directory), gdscheck=True)
                result = host.run(*args)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("gdscheck -p", host.command_calls())
                self.assertIn("use_compat_mode : false", result.stdout)
                self.assertNotIn("GDS check skipped", result.stdout)

    def test_unknown_arm_is_a_usage_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = Host(Path(directory), gdscheck=True).run("preflight", "nvme")
            self.assertEqual(result.returncode, 2)
            self.assertIn("usage: bash", result.stderr)

    def test_nested_model_path_is_found(self) -> None:
        """MODEL is resolved relative to /models, not by its basename."""
        with tempfile.TemporaryDirectory() as directory:
            host = Host(Path(directory), gdscheck=False)
            self.assertNotEqual(Path(MODEL).name, MODEL.removeprefix("/models/"))
            result = host.run("preflight", "posix")
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_missing_model_directory_is_named(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            host = Host(Path(directory), gdscheck=False)
            result = host.run("preflight", "posix", MODEL="/models/absent")
            self.assertEqual(result.returncode, 1)
            self.assertIn("model directory not found on the host", result.stderr)
            self.assertIn("/models/absent", result.stderr)

    def test_model_outside_models_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            host = Host(Path(directory), gdscheck=False)
            for model in ("/data/m", "/models/", "Qwen/Qwen2.5-7B-Instruct"):
                with self.subTest(model=model):
                    result = host.run("preflight", "posix", MODEL=model)
                    self.assertEqual(result.returncode, 1)
                    self.assertIn("MODEL must be a path under /models", result.stderr)


class ConcurrencyModes(unittest.TestCase):
    """Gate 5 sweeps: one tier arm, or all three."""

    def test_concurrency_posix_starts_only_the_posix_arm(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            host = Host(Path(directory), gdscheck=False)
            result = host.run("concurrency-posix", FILLERS="16", FILLER_TOKENS="4096")
            self.assertEqual(result.returncode, 0, result.stderr)
            runs = host.client_runs()
            self.assertEqual(len(runs), 1)
            argv = runs[0]
            self.assertEqual(argv[0], "concurrency")
            self.assertEqual(_option(argv, "--arm"), "posix")
            self.assertEqual(_option(argv, "--backend"), "lmcache_native_posix")
            self.assertEqual(_option(argv, "--container"), "kv-controlled")
            self.assertEqual(
                _option(argv, "--output"), str(host.evidence / "concurrency-posix.json")
            )
            self.assertEqual(argv[argv.index("--concurrency") + 1 : argv.index("--concurrency") + 5],
                             ["1", "2", "4", "8"])
            self.assertEqual(_option(argv, "--fillers"), "16")
            self.assertEqual(_option(argv, "--filler-tokens"), "4096")
            calls = host.command_calls()
            self.assertIn("LMCACHE_LOCAL_DISK=/gds/lmcache-controlled-posix", calls)
            self.assertNotIn("nvidia-fs", calls)
            self.assertNotIn("lmcache-controlled-gds", calls)
            self.assertNotIn("gdscheck", calls)
            self.assertTrue((host.evidence / "concurrency-posix-container.log").exists())

    def test_concurrency_gds_is_unchanged(self) -> None:
        """The refactor keeps the GDS sweep's client call (it still needs nvidia-fs)."""
        with tempfile.TemporaryDirectory() as directory:
            host = Host(Path(directory), gdscheck=True)
            result = host.run("concurrency-gds")
            # The stub host has no /dev/nvidia-fs*, so the GDS arm refuses to start.
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("no nvidia-fs devices", result.stderr)
            self.assertEqual(host.client_runs(), [])

    def test_concurrency_runs_three_arms_in_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            host = Host(Path(directory), gdscheck=True)
            result = host.run("concurrency")
            # recompute and posix complete; gds stops on the missing nvidia-fs devices.
            self.assertNotEqual(result.returncode, 0)
            runs = host.client_runs()
            self.assertEqual([_option(argv, "--arm") for argv in runs], ["recompute", "posix"])
            self.assertEqual(_option(runs[0], "--container"), "")
            self.assertEqual(_option(runs[0], "--backend"), "disabled")
            self.assertEqual(
                _option(runs[0], "--output"), str(host.evidence / "concurrency-recompute.json")
            )
            self.assertEqual(_option(runs[1], "--container"), "kv-controlled")

    def test_unknown_mode_lists_concurrency_posix(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = Host(Path(directory), gdscheck=False).run("concurrency-nvme")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("concurrency-posix", result.stderr)


def _posix_configuration() -> dict[str, object]:
    """A configuration with only a recompute arm and a POSIX tier arm."""
    return {
        "model": {"root": "/models/m", "revision": "unrecorded", "served_name": "m"},
        "kv_geometry": {"chunk_tokens": 256, "bytes_per_chunk": 14680064, "dtype": "bfloat16"},
        "cache": {"upper_tier_capacity_bytes": 3221225472},
        "runtime": {"image_digest": "sha256:" + "a" * 64, "device_id": "00000000:01:00.0"},
        "measurement": {"contexts": [4096], "concurrency": [1, 2], "samples": 2, "warmup": 2},
        "arms": [
            {"name": "recompute", "backend": "disabled", "tier_path": None,
             "attribution_provider": "serving_log", "staging_capacity_bytes": 0,
             "eviction_policy": "none", "eviction_parameters": {}},
            {"name": "posix", "backend": "lmcache_native_posix",
             "tier_path": "/gds/lmcache-controlled-posix", "attribution_provider": "cgroup_blk",
             "staging_capacity_bytes": 0, "eviction_policy": "targeted",
             "eviction_parameters": {"fillers": 16, "filler_tokens": 4096}},
        ],
    }


class PosixEnvelopeIngest(unittest.TestCase):
    """What concurrency-posix writes is judged as Gate 5 without a GDS arm declared."""

    def _run(self, gates: dict[int, list[bool]]) -> dict[str, object]:
        result = {
            str(level): {
                "batches": [
                    {"phase": "measure", "tier_ok": ok,
                     "rows": [{"correct": True, "prompt": {"cacheable_tokens": 2048}}] * level}
                    for ok in oks
                ]
            }
            for level, oks in gates.items()
        }
        return {
            "mode": "concurrency",
            "arm": "posix",
            "backend": "lmcache_native_posix",
            "arguments": {"arm": "posix", "concurrency": [1, 2], "warmup": 2, "batches": 2,
                          "fillers": 16, "filler_tokens": 4096},
            "result": result,
        }

    def test_posix_sweep_builds_an_envelope(self) -> None:
        document = build_document(
            [("concurrency-posix", self._run({1: [True, True], 2: [True, False]}), "b" * 64)],
            _posix_configuration(),
            run_id="t",
        )
        envelope = document["evidence"]["envelope"]
        self.assertEqual(envelope["availability"]["status"], "supported")
        self.assertEqual(
            [(level["concurrency"], level["target_tier_served"]) for level in envelope["levels"]],
            [(1, 2), (2, 2)],
        )
        assessment = assess_evidence(document)
        gate5 = next(gate for gate in assessment["gates"] if gate["id"] == 5)
        self.assertEqual(gate5["status"], "PASS")
        self.assertIn("validated envelope is concurrency 1", gate5["result"])

    def test_posix_sweep_against_a_gds_only_configuration_is_refused(self) -> None:
        configuration = _posix_configuration()
        configuration["arms"] = [arm for arm in configuration["arms"] if arm["name"] != "posix"]
        with self.assertRaisesRegex(Exception, "'posix' is not declared"):
            build_document(
                [("concurrency-posix", self._run({1: [True, True]}), "b" * 64)],
                configuration,
                run_id="t",
            )


if __name__ == "__main__":
    unittest.main()
