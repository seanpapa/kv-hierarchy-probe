# SPDX-License-Identifier: Apache-2.0
"""Reference receipts are checked against originals, never promoted to serving proof."""

# Standard
from hashlib import sha256
import json
from pathlib import Path
import tempfile
import time
from typing import Any
import unittest

# First Party
from kvprobe.configuration import fingerprint_configuration
from kvprobe.nfs_client import replay_client_execution
from kvprobe.nfs_observation import NfsObservationError
from kvprobe.nfs_serving import capture_serving_candidate

# Local
from test_nfs_bundle import BOOT, MOUNT, proc
from test_nfs_serving import mapping
from test_nfs_scope import scoped
from test_nfs_path_binding import fixture as path_fixture
from test_nfs_path_binding import stat_bytes


def put(root: Path, name: str, data: bytes) -> None:
    """Write a synthetic raw artifact under the test bundle."""
    path = root / name
    path.parent.mkdir(exist_ok=True, parents=True)
    path.write_bytes(data)


def obj(root: Path, name: str, value: Any) -> None:
    """Write synthetic JSON, without relying on implementation serializers."""
    put(root, name, json.dumps(value).encode())


def seal(root: Path) -> None:
    """Seal a synthetic artifact inventory using the documented public format."""
    entries = [
        {"path": str(p.relative_to(root)), "sha256": sha256(p.read_bytes()).hexdigest()}
        for p in sorted(root.rglob("*"))
        if p.is_file() and p != root / "client-manifest.json"
    ]
    obj(
        root,
        "client-manifest.json",
        {
            "nfs_client_version": "0.1.0-experimental",
            "kind": "tiny_reference_client",
            "artifacts": entries,
        },
    )


def fixture(root: Path, mode: str = "local") -> None:
    """Build synthetic receipts; no package, model, GPU or NFS is executed."""
    root.mkdir(exist_ok=True)
    source = b"synthetic reference source; audit not satisfied"
    copy_source = b"synthetic explicit copy override"
    input_bytes = json.dumps({"prompt_token_ids": list(range(10, 42)) + [52]}).encode()
    config = json.dumps(
        {"num_hidden_layers": 1, "num_key_value_heads": 1, "head_dim": 64}
    ).encode()
    versions = {name: "synthetic-version" for name in ("vllm", "lmcache", "torch")}
    frozen = {
        "reference_client_version": "0.1.0-experimental",
        "mode": mode,
        "pid": 123 if mode == "baseline" else 124,
        "clock": "CLOCK_MONOTONIC",
        "boot_id": BOOT,
        "time_namespace": "time:[200]",
        "reference_copy_override": mode != "baseline",
        "source_sha256": sha256(source).hexdigest(),
        "copy_source_sha256": sha256(copy_source).hexdigest(),
        "input_sha256": sha256(input_bytes).hexdigest(),
        "model_files": {
            "config.json": {"bytes": len(config), "sha256": sha256(config).hexdigest()},
            "model.safetensors": {"bytes": 1, "sha256": "a" * 64},
        },
        "runtime": versions,
        "sampling": {"max_tokens": 2, "temperature": 0},
        "requested_engine": {
            "model": "/models/tiny",
            "enable_prefix_caching": False,
            "tensor_parallel_size": 1,
            "dtype": "bfloat16",
            "kv_cache_dtype": "auto",
            "block_size": 16,
        },
    }
    obj(root, "frozen.json", frozen)
    put(root, "reference_client.py.source", source)
    put(root, "reference_copy.py.source", copy_source)
    put(root, "model-config.json", config)
    put(root, "input.bin", input_bytes)
    put(root, "actual-vllm-config.txt", b"synthetic runtime config")
    stack = {}
    for name in versions:
        data = ("synthetic module " + name).encode()
        put(root, name + "-init.source", data)
        stack[name] = {"version": versions[name], "sha256": sha256(data).hexdigest()}
    obj(root, "stack.json", stack)
    for name in ["before.stat.bin", "after-request.stat.bin"]:
        put(root, name, stat_bytes(frozen["pid"], 1000 + frozen["pid"]))
    obj(
        root,
        "retrieve.json",
        {
            "input": json.loads(input_bytes),
            "input_sha256": frozen["input_sha256"],
            "output_token_ids": [55, 21],
            "cached_tokens": 0,
            "interval_ns": [200, 300],
        },
    )
    put(root, "output.bin", b"[55,21]")
    obj(root, "shutdown.json", {"public_engine_shutdown_returned": True})
    obj(root, "device.json", {"stdout": "synthetic-device", "exit_code": 0})
    if mode != "baseline":
        fixture(root / "baseline", "baseline")
        for name in ("store_gpu", "store_cpu", "load_cpu", "load_gpu"):
            put(root, "proof/" + name + "-0-32-call1.bin", b"K" * 8192)
    seal(root)


def mutate(root: Path, name: str, key: str, value: Any) -> None:
    """Change synthetic original content and refresh its outer hash receipt."""
    data = json.loads((root / name).read_bytes())
    data[key] = value
    obj(root, name, data)
    seal(root)


class ClientExecutionTests(unittest.TestCase):
    """Tests target the public reader, including rehashed semantic contradictions."""

    def test_baseline_records_consistency_without_cache_verdict(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fixture(root, "baseline")
            result = replay_client_execution(root)
        self.assertEqual(result["status"], "observation_only")
        self.assertIsNone(result["baseline_output_equal"])
        self.assertEqual(result["serving_verification"], "not_assessed")

    def test_local_control_binds_baseline_and_full_kv_originals(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fixture(root)
            result = replay_client_execution(root)
        self.assertTrue(result["baseline_output_equal"])
        self.assertTrue(result["kv_originals_equal"])
        self.assertEqual(result["performance_claims"], "suppressed")
        self.assertIn("competing_io_review_required", result["review_reasons"])
        self.assertNotIn("gates", result)

    def test_wrong_output_is_observed_not_promoted(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fixture(root)
            mutate(root, "retrieve.json", "output_token_ids", [7])
            put(root, "output.bin", b"[7]")
            seal(root)
            result = replay_client_execution(root)
        self.assertFalse(result["baseline_output_equal"])
        self.assertEqual(result["status"], "observation_only")

    def test_changed_kv_original_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fixture(root)
            put(root, "proof/load_gpu-0-32-call1.bin", b"V" * 8192)
            seal(root)
            result = replay_client_execution(root)
        self.assertFalse(result["kv_originals_equal"])

    def test_corrupted_hash_missing_artifact_and_short_kv_are_refused(self) -> None:
        for action in ("tamper", "missing", "short"):
            with self.subTest(action=action), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                fixture(root)
                path = root / "proof/load_gpu-0-32-call1.bin"
                if action == "missing":
                    path.unlink()
                    seal(root)
                else:
                    path.write_bytes(b"bad")
                    if action == "short":
                        seal(root)
                with self.assertRaises((NfsObservationError, OSError)):
                    replay_client_execution(root)

    def test_process_change_is_refused_even_with_new_artifact_hash(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fixture(root)
            put(root, "after-request.stat.bin", stat_bytes(124, 9999))
            seal(root)
            with self.assertRaisesRegex(NfsObservationError, "process changed"):
                replay_client_execution(root)

    def test_input_source_model_and_stack_identity_contradictions_are_refused(
        self,
    ) -> None:
        for name, key, value in [
            ("frozen.json", "input_sha256", "a" * 64),
            ("frozen.json", "source_sha256", "b" * 64),
            ("model-config.json", "head_dim", 128),
            ("stack.json", "torch", {"version": "other", "sha256": "c" * 64}),
        ]:
            with (
                self.subTest(name=name, key=key),
                tempfile.TemporaryDirectory() as temp,
            ):
                root = Path(temp)
                fixture(root)
                mutate(root, name, key, value)
                with self.assertRaises(NfsObservationError):
                    replay_client_execution(root)

    def test_baseline_input_sampling_and_mode_must_match(self) -> None:
        for key, value in [
            ("sampling", {"temperature": 1}),
            ("runtime", {"vllm": "other"}),
            ("mode", "local"),
        ]:
            with self.subTest(key=key), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                fixture(root)
                mutate(root / "baseline", "frozen.json", key, value)
                seal(root)
                with self.assertRaises(NfsObservationError):
                    replay_client_execution(root)

    def test_baseline_gpu_cache_hit_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fixture(root)
            mutate(root / "baseline", "retrieve.json", "cached_tokens", 32)
            seal(root)
            with self.assertRaisesRegex(NfsObservationError, "cached tokens"):
                replay_client_execution(root)

    def test_duplicate_and_escaping_manifest_paths_are_refused(self) -> None:
        for action in ("duplicate", "escape"):
            with self.subTest(action=action), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                fixture(root)
                data = json.loads((root / "client-manifest.json").read_text())
                if action == "duplicate":
                    data["artifacts"].append(data["artifacts"][0])
                else:
                    data["artifacts"][0]["path"] = "../outside"
                obj(root, "client-manifest.json", data)
                with self.assertRaises(NfsObservationError):
                    replay_client_execution(root)

    def test_duplicate_json_keys_and_unknown_version_are_refused(self) -> None:
        for content in (b'{"mode":1,"mode":2}', b'{"nfs_client_version":"99"}'):
            with self.subTest(content=content), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                fixture(root)
                put(root, "client-manifest.json", content)
                with self.assertRaises(NfsObservationError):
                    replay_client_execution(root)

    def test_actual_runtime_contradiction_is_not_overridden_by_requested_settings(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fixture(root, "baseline")
            obj(
                root,
                "actual-runtime.json",
                {
                    "prefix_cache_enabled": True,
                    "block_size": 16,
                    "tensor_parallel_size": 1,
                    "kv_cache_dtype": "auto",
                    "kv_connector": None,
                    "lmcache": None,
                },
            )
            seal(root)
            with self.assertRaisesRegex(NfsObservationError, "actual runtime"):
                replay_client_execution(root)

    def test_missing_actual_runtime_stays_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fixture(root, "baseline")
            result = replay_client_execution(root)
        self.assertEqual(result["actual_runtime_receipt"], "unavailable")

    def test_optional_cleanup_health_does_not_gate_measurement(self) -> None:
        """Cleanup state is operational metadata; source identity still matters."""
        for cleaned in (True, False):
            with self.subTest(cleaned=cleaned), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                fixture(root)
                data = b"synthetic reference cleanup source"
                put(root, "offload_cleanup_reference.py.source", data)
                value = json.loads((root / "frozen.json").read_bytes())
                value["reference_cleanup_override"] = True
                value["cleanup_source_sha256"] = sha256(data).hexdigest()
                obj(root, "frozen.json", value)
                obj(
                    root,
                    "reference-cleanup-state.json",
                    {
                        "offload_control_services": [
                            {"thread_alive": not cleaned, "socket_closed": cleaned}
                        ]
                    },
                )
                seal(root)
                result = replay_client_execution(root)
                self.assertEqual(result["measurement_evidence"]["status"], "available")
                self.assertTrue(result["baseline_output_equal"])
                self.assertEqual(
                    result["stack_health"]["experimental_cleanup_complete"], cleaned
                )
                put(root, "offload_cleanup_reference.py.source", b"tampered")
                seal(root)
                with self.assertRaisesRegex(NfsObservationError, "source mismatch"):
                    replay_client_execution(root)

    def test_shutdown_receipt_is_not_a_measurement_requirement(self) -> None:
        """Missing, false or malformed shutdown metadata never replaces KV evidence."""
        for variant in ("missing", "false", "malformed"):
            with self.subTest(variant=variant), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                fixture(root)
                if variant == "missing":
                    (root / "shutdown.json").unlink()
                elif variant == "false":
                    obj(
                        root,
                        "shutdown.json",
                        {"public_engine_shutdown_returned": False},
                    )
                else:
                    put(root, "shutdown.json", b"not-json")
                seal(root)
                result = replay_client_execution(root)
                self.assertEqual(result["measurement_evidence"]["status"], "available")
                self.assertTrue(result["baseline_output_equal"])
                self.assertTrue(result["kv_originals_equal"])
                self.assertNotIn(
                    "outer_process_exit_review_required", result["review_reasons"]
                )

    def test_baseline_teardown_is_not_a_correctness_prerequisite(self) -> None:
        """A complete recompute response stays a baseline without shutdown metadata."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fixture(root)
            (root / "baseline/shutdown.json").unlink()
            seal(root / "baseline")
            seal(root)
            result = replay_client_execution(root)
        self.assertTrue(result["baseline_output_equal"])
        self.assertEqual(result["measurement_evidence"]["status"], "available")

    def test_standalone_checkpoint_needs_no_stack_shutdown(self) -> None:
        """A persistent serving stack can provide a finished request checkpoint."""
        # First Party
        from kvprobe.nfs_client_process import checkpoint_client_workload

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fixture(root)
            (root / "client-manifest.json").unlink()
            (root / "shutdown.json").unlink()
            checkpoint_client_workload(root)
            result = replay_client_execution(root)
        self.assertEqual(result["measurement_evidence"]["status"], "available")
        self.assertEqual(result["stack_health"]["status"], "unknown")
        self.assertTrue(result["baseline_output_equal"])

    def test_nfs_label_does_not_upgrade_a_local_capture(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fixture(root)
            mutate(root, "frozen.json", "mode", "nfs")
            with self.assertRaises((NfsObservationError, OSError)):
                replay_client_execution(root)


def nfs_fixture(root: Path) -> None:
    """Join independent synthetic client, C1 and C2-A original formats."""
    fixture(root)
    mutate(root, "frozen.json", "mode", "nfs")
    configuration = scoped()["configurations"][0]["parameters"]
    configuration["model"]["root"] = "/models/tiny"
    configuration["kv_geometry"] = {
        "chunk_tokens": 32,
        "bytes_per_chunk": 8192,
        "dtype": "bfloat16",
    }
    (root / "serving").mkdir()
    input_bytes = (root / "input.bin").read_bytes()
    state = {"payload": 0, "ops": 0}

    def operation() -> bytes:
        start = time.monotonic_ns()
        state["payload"] = 8192
        state["ops"] = 1
        end = time.monotonic_ns()
        value = json.loads((root / "retrieve.json").read_text())
        value["interval_ns"] = [start, end]
        obj(root, "retrieve.json", value)
        return (root / "output.bin").read_bytes()

    with proc(state):
        capture_serving_candidate(
            root / "serving",
            configuration,
            {
                "id": "retrieve-1",
                "arm": "nfs-rdma",
                "cacheable_tokens": 32,
                "input_sha256": sha256(input_bytes).hexdigest(),
            },
            input_bytes,
            (root / "baseline/output.bin").read_bytes(),
            MOUNT,
            mapping(),
            operation,
            quiet_seconds=0.0001,
        )
    serving = json.loads((root / "serving/serving-manifest.json").read_text())
    bounds = serving["phases"]["request"]["interval_ns"]
    for name, interval in [
        ("binding-before", [bounds[0] - 2000, bounds[0] - 100]),
        ("binding-after", [bounds[1] + 100, bounds[1] + 2000]),
    ]:
        directory = root / name
        directory.mkdir()
        value = path_fixture(directory)
        obj(directory, "configuration.json", configuration)
        value["configuration"] = {
            "path": "configuration.json",
            "sha256": sha256(
                (directory / "configuration.json").read_bytes()
            ).hexdigest(),
        }
        value["configuration_fingerprint"] = fingerprint_configuration(
            configuration, evidence_version="0.4.0"
        )
        value["serving_pid"] = 124
        value["capture_interval_ns"] = interval
        for epoch, offset in [("before", 10), ("after", 30)]:
            item = value["epochs"][epoch]
            item["interval_ns"] = [interval[0] + offset, interval[0] + offset + 10]
            ref = item["roles"]["serving"]["stat"]
            put(directory, ref["path"], stat_bytes(124, 1124))
            ref["sha256"] = sha256((directory / ref["path"]).read_bytes()).hexdigest()
        obj(directory, "path-binding.json", value)
    seal(root)


class NfsClientJoinTests(unittest.TestCase):
    """Synthetic joins exercise actual shared time/input/process constraints."""

    def test_nfs_receipts_join_without_promoting_isolation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            nfs_fixture(root)
            result = replay_client_execution(root)
        self.assertEqual(result["nfs_observations"]["expected_kv_bytes"], 8192)
        self.assertTrue(result["baseline_output_equal"])
        for value in result["path_bindings"].values():
            self.assertEqual(value["path_binding_status"], "matched")
        self.assertEqual(result["serving_verification"], "not_assessed")

    def test_nfs_facts_are_invariant_under_teardown_health(self) -> None:
        """Cleanup errors cannot downgrade or improve request/counter evidence."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            nfs_fixture(root)
            before = replay_client_execution(root)
            obj(root, "shutdown.json", {"public_engine_shutdown_returned": False})
            seal(root)
            after = replay_client_execution(root)
        self.assertEqual(
            {k: v for k, v in before.items() if k != "stack_health"},
            {k: v for k, v in after.items() if k != "stack_health"},
        )
        self.assertEqual(after["stack_health"]["status"], "error")
        self.assertEqual(
            after["nfs_observations"]["phase_deltas"]["request"][
                "server_read_payload_bytes"
            ],
            8192,
        )
        self.assertEqual(after["serving_verification"], "not_assessed")

    def test_join_refuses_changed_process_lifetime(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            nfs_fixture(root)
            directory = root / "binding-after"
            value = json.loads((directory / "path-binding.json").read_text())
            for epoch in ("before", "after"):
                ref = value["epochs"][epoch]["roles"]["serving"]["stat"]
                put(directory, ref["path"], stat_bytes(124, 2224))
                ref["sha256"] = sha256(
                    (directory / ref["path"]).read_bytes()
                ).hexdigest()
            obj(directory, "path-binding.json", value)
            seal(root)
            with self.assertRaisesRegex(NfsObservationError, "lifetime"):
                replay_client_execution(root)

    def test_join_refuses_unbracketed_actual_generation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            nfs_fixture(root)
            mutate(root, "retrieve.json", "interval_ns", [0, 1])
            with self.assertRaisesRegex(NfsObservationError, "outside callback"):
                replay_client_execution(root)


if __name__ == "__main__":
    unittest.main()
