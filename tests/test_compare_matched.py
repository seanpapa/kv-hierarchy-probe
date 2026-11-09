# SPDX-License-Identifier: Apache-2.0

# Standard
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any


SCRIPT = Path(__file__).parents[1] / "scripts" / "tools" / "compare_matched.py"


def _evidence(
    *,
    container: str,
    arm: str = "gds",
    attribution: str | None = None,
    backend: str | None = None,
    prompt_tokens: int = 4111,
    cacheable_tokens: int = 4096,
    nvfs_read_mib: float = 1024.0,
    cgroup_read_bytes: float = 1024.0 * 1024**2,
    samples: int = 3,
    ttft_base: float = 0.16,
    filler_key_policy: str = "unique",
) -> dict[str, Any]:
    rows = [
        {
            "index": index,
            "phase": "warmup" if index == 0 else "measure",
            "prompt_tokens": prompt_tokens,
            "cacheable_tokens": cacheable_tokens,
            "ttft_s": ttft_base + index * 0.001,
            "retrieve_gib_s": 10.0,
            "nvfs_read_mib": nvfs_read_mib,
            "cgroup_read_bytes": cgroup_read_bytes,
            "correct": True,
            "tier_ok": True,
        }
        for index in range(samples + 1)
    ]
    payload = {
        "mode": "context",
        "arm": arm,
        "attribution": attribution or (arm if arm in {"gds", "posix"} else "gds"),
        "port": 8106,
        "container": container,
        "arguments": {
            "mode": "context",
            "arm": arm,
            "attribution": attribution or (arm if arm in {"gds", "posix"} else "gds"),
            "contexts": [4096],
            "samples": samples,
            "warmup": 1,
            "fillers": 6,
            "filler_tokens": 8192,
            "filler_key_policy": filler_key_policy,
        },
        "result": {"4096": {"rows": rows}},
    }
    if backend is not None:
        payload["backend"] = backend
        payload["arguments"]["backend"] = backend
    return payload


def _write(path: Path, payload: dict[str, Any]) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _run(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *arguments],
        capture_output=True,
        check=False,
        text=True,
    )


def _matched_command(path_a: Path, path_b: Path) -> tuple[str, ...]:
    return (
        "--context",
        "4096",
        "--a",
        str(path_a),
        "--b",
        str(path_b),
        "--provenance-verified",
    )


class CompareMatchedTest(unittest.TestCase):
    """Exercise the comparator through its public command-line interface."""

    def setUp(self) -> None:
        """Create an isolated directory for evidence files."""
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary_directory.name)

    def tearDown(self) -> None:
        """Remove the isolated evidence directory."""
        self.temporary_directory.cleanup()

    def test_valid_matched_evidence_passes(self) -> None:
        path_a = _write(
            self.directory / "native.json", _evidence(container="kv-native")
        )
        path_b = _write(
            self.directory / "nixl.json", _evidence(container="kv-nixl")
        )

        result = _run(*_matched_command(path_a, path_b))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("MATCH GATE: PASS", result.stdout)
        self.assertIn("DESCRIPTIVE VERDICT", result.stdout)
        self.assertIn("not a statistical equivalence", result.stdout)

    def test_relative_change_uses_first_arm_as_baseline(self) -> None:
        path_a = _write(
            self.directory / "native.json",
            _evidence(container="kv-native", ttft_base=0.20),
        )
        path_b = _write(
            self.directory / "nixl.json",
            _evidence(container="kv-nixl", ttft_base=0.18),
        )

        result = _run(*_matched_command(path_a, path_b))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("NIXL-GDS_MT p50 TTFT is 9.9% lower than native-GDS", result.stdout)

    def test_posix_evidence_cannot_be_mislabeled_as_nixl(self) -> None:
        path_a = _write(
            self.directory / "native.json", _evidence(container="kv-native")
        )
        path_b = _write(
            self.directory / "posix.json",
            _evidence(container="kv-posix", arm="posix", nvfs_read_mib=0.0),
        )

        result = _run(*_matched_command(path_a, path_b))

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("arm must be 'gds'", result.stderr)

    def test_prompt_shape_mismatch_fails(self) -> None:
        path_a = _write(
            self.directory / "native.json", _evidence(container="kv-native")
        )
        path_b = _write(
            self.directory / "nixl.json",
            _evidence(
                container="kv-nixl", prompt_tokens=8207, cacheable_tokens=8192
            ),
        )

        result = _run(*_matched_command(path_a, path_b))

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("prompt_tokens differs", result.stderr)

    def test_missing_native_device_bytes_fails(self) -> None:
        path_a = _write(
            self.directory / "native.json", _evidence(container="kv-native")
        )
        path_b = _write(
            self.directory / "nixl.json",
            _evidence(container="kv-nixl", nvfs_read_mib=0.0),
        )

        result = _run(*_matched_command(path_a, path_b))

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not match expected 1024.0", result.stderr)

    def test_distinct_nixl_mode_identities_can_be_compared(self) -> None:
        path_a = _write(
            self.directory / "static.json",
            _evidence(
                container="kv-static",
                arm="nixl_static_gds_mt",
                backend="lmcache_nixl_static_gds_mt",
            ),
        )
        path_b = _write(
            self.directory / "dynamic.json",
            _evidence(
                container="kv-dynamic",
                arm="nixl_dynamic_gds_mt",
                backend="lmcache_nixl_dynamic_gds_mt",
            ),
        )

        result = _run(
            *_matched_command(path_a, path_b),
            "--a-arm",
            "nixl_static_gds_mt",
            "--b-arm",
            "nixl_dynamic_gds_mt",
            "--a-backend",
            "lmcache_nixl_static_gds_mt",
            "--b-backend",
            "lmcache_nixl_dynamic_gds_mt",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("provider=gds", result.stdout)

    def test_expected_backend_must_be_recorded(self) -> None:
        path_a = _write(
            self.directory / "native.json", _evidence(container="kv-native")
        )
        path_b = _write(
            self.directory / "nixl.json", _evidence(container="kv-nixl")
        )

        result = _run(
            *_matched_command(path_a, path_b),
            "--b-backend",
            "lmcache_nixl_static_gds_mt",
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("backend must be recorded", result.stderr)

    def test_posix_provider_uses_cgroup_bytes(self) -> None:
        path_a = _write(
            self.directory / "native-posix.json",
            _evidence(
                container="kv-native-posix",
                arm="native_posix",
                attribution="posix",
                backend="lmcache_native_posix",
            ),
        )
        path_b = _write(
            self.directory / "dynamic-posix.json",
            _evidence(
                container="kv-dynamic-posix",
                arm="nixl_dynamic_posix",
                attribution="posix",
                backend="lmcache_nixl_dynamic_posix",
                nvfs_read_mib=0.0,
            ),
        )

        result = _run(
            *_matched_command(path_a, path_b),
            "--provider",
            "posix",
            "--a-arm",
            "native_posix",
            "--b-arm",
            "nixl_dynamic_posix",
            "--a-backend",
            "lmcache_native_posix",
            "--b-backend",
            "lmcache_nixl_dynamic_posix",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("provider=posix", result.stdout)

    def test_schedule_mismatch_fails(self) -> None:
        path_a = _write(
            self.directory / "native.json", _evidence(container="kv-native")
        )
        path_b = _write(
            self.directory / "nixl.json",
            _evidence(container="kv-nixl", samples=4),
        )

        result = _run(*_matched_command(path_a, path_b))

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("schedule differs", result.stderr)

    def test_filler_key_policy_mismatch_fails(self) -> None:
        path_a = _write(
            self.directory / "static.json",
            _evidence(container="kv-static", filler_key_policy="unique"),
        )
        path_b = _write(
            self.directory / "dynamic.json",
            _evidence(container="kv-dynamic", filler_key_policy="reuse"),
        )

        result = _run(*_matched_command(path_a, path_b))

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("schedule differs", result.stderr)

    def test_provenance_attestation_is_required(self) -> None:
        path_a = _write(
            self.directory / "native.json", _evidence(container="kv-native")
        )
        path_b = _write(
            self.directory / "nixl.json", _evidence(container="kv-nixl")
        )

        result = _run(
            "--context", "4096", "--a", str(path_a), "--b", str(path_b)
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("--provenance-verified", result.stderr)

    def test_same_container_identity_fails(self) -> None:
        path_a = _write(
            self.directory / "native.json", _evidence(container="kv-shared")
        )
        path_b = _write(
            self.directory / "nixl.json", _evidence(container="kv-shared")
        )

        result = _run(*_matched_command(path_a, path_b))

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("distinct container identities", result.stderr)


if __name__ == "__main__":
    unittest.main()
