# SPDX-License-Identifier: Apache-2.0
"""Synthetic C3 originals; no model, GPU, NFS server or hardware result."""

# Standard
from hashlib import sha256
import json
from pathlib import Path
from typing import Any

# First Party
from kvprobe.configuration import fingerprint_configuration
from kvprobe.nfs_policy import COUNTS, FLAGS, ROLES

# Local
from test_nfs_client import nfs_fixture, obj, put, seal
from test_nfs_scope import scoped

ROOT = Path(__file__).resolve().parents[1]


def normalized() -> dict[str, Any]:
    """Construct a complete normalized policy fixture with original-role references."""
    value = scoped()
    cfg = value["configurations"][0]
    cfg["parameters"]["kv_geometry"]["bytes_per_chunk"] = 8192
    cfg["parameters"]["kv_geometry"]["chunk_tokens"] = 32
    cfg["fingerprint"] = fingerprint_configuration(
        cfg["parameters"], evidence_version="0.4.0"
    )
    value["provenance"] = {
        "normalized_by": {"adapter": "synthetic-contract-fixture", "version": "test"},
        "artifacts": [
            {
                "id": role,
                "kind": "nfs_" + role,
                "sha256": sha256(("SYNTHETIC " + role).encode()).hexdigest(),
            }
            for role in ROLES
        ],
    }
    roles = {role: [role] for role in ROLES}
    measurement = {
        **{key: 0 for key in COUNTS},
        **{key: True for key in FLAGS},
        "capture_status": "complete",
        "capture_reason": "synthetic_complete",
        "configuration_fingerprint": cfg["fingerprint"],
        "workload": "serving_retrieval",
        "clock": "CLOCK_MONOTONIC",
        "interval_ns": [1, 2, 3, 4],
        "counter": "NFSIOS_SERVERREADBYTES",
        "counter_before": 10000,
        "counter_after": 18192,
        "rpc_before": 5,
        "rpc_after": 6,
        "expected_payload_bytes": 8192,
        "identity_binding": "matched",
        "source_binding": "matched",
        "payload_mapping": "identity",
        "namespace_binding": "bounded",
        "quiet_activity": False,
        "gap_activity": False,
        "artifact_roles": roles,
    }
    request = {
        "id": "request-a",
        "correct": True,
        "source": "target_tier",
        "expected_bytes": 8192,
        "observed_bytes": 8192,
        "attribution": {
            "provider": "nfs_mountstats",
            "granularity": "per_request",
            "confidence": "corroborating",
            "target_device_filtered": False,
            "target_scope": cfg["parameters"]["arms"][0]["tier_identity"],
            "artifact_refs": list(ROLES),
        },
        "nfs_measurement": measurement,
    }
    value["evidence"]["attribution"] = {
        "availability": {"status": "supported"},
        "tolerance_fraction": 0,
        "requests": [request],
    }
    value["bindings"] = {
        "attribution_requests": [
            {"request_id": "request-a", "configuration_id": cfg["id"]}
        ],
        "break_even": None,
        "admission": None,
        "capacity": None,
        "envelope_levels": [],
    }
    for name in ("break_even", "admission", "capacity", "envelope"):
        value["evidence"][name] = {
            "availability": {
                "status": "unsupported",
                "reason_code": "not_measured",
                "detail": "Synthetic Gate 1 only.",
            }
        }
    return value


def raw_bundle(
    root: Path,
    *,
    layout_source: bytes | None = None,
    io_mode: str = "direct",
    payload: int = 8192,
) -> dict[str, Any]:
    """Join synthetic client/counter/path originals with a file/namespace supplement."""
    client = root / "candidate/client"
    client.mkdir(parents=True)
    nfs_fixture(client, io_mode=io_mode, payload=payload)
    frozen = json.loads((client / "frozen.json").read_text())
    payload = b"K" * 8192
    (client / "io").mkdir()
    source = (
        (ROOT / "tests/fixtures/nfs/layout-source.txt").read_bytes()
        if layout_source is None
        else layout_source
    )
    helper = b"synthetic trusted I/O recorder fixture; not an execution attestation"
    put(client, "io/disk.source", source)
    put(client, "io_receipts.py.source", helper)
    inventory = {
        "lmcache.inprocess.v1.storage_backend.local_disk_backend": {
            "path": "disk.source",
            "loaded_path": "/opt/synthetic/disk.py",
            "sha256": sha256(source).hexdigest(),
        }
    }
    obj(client, "io/implementation.json", inventory)

    def canonical(value: Any) -> bytes:
        return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()

    frozen["io_implementation_sha256"] = sha256(canonical(inventory)).hexdigest()
    frozen["io_receipts_source_sha256"] = sha256(helper).hexdigest()
    frozen["reference_cleanup_override"] = False
    obj(client, "frozen.json", frozen)
    configuration = json.loads((client / "serving/configuration.json").read_text())
    configuration["arms"] = configuration["arms"][:1]
    configuration["measurement"] = {
        "contexts": [33],
        "concurrency": [1],
        "samples": 1,
        "warmup": 0,
    }
    configuration["cache"]["upper_tier_capacity_bytes"] = 0
    configuration["runtime"]["device_id"] = "00000000:01:00.0"
    configuration["runtime"]["gpu_index"] = 0
    obj(
        client,
        "device.json",
        {
            "exit_code": 0,
            "stdout": (
                "0, synthetic-gpu, 00000000:01:00.0, "
                "synthetic-driver, synthetic-device\n"
            ),
        },
    )
    for stage in ("pre-request", "after-request"):
        obj(
            client,
            stage + ".gpu.json",
            {"exit_code": 0, "stdout": str(frozen["pid"]) + ", synthetic-gpu\n"},
        )
    identity = {
        "packages": json.loads((client / "stack.json").read_text()),
        "driver": frozen["source_sha256"],
        "copy": frozen["copy_source_sha256"],
        "cleanup": None,
        "cleanup_enabled": False,
        "io_receipts": frozen["io_receipts_source_sha256"],
        "io_implementation": frozen["io_implementation_sha256"],
    }
    configuration["runtime"]["image_digest"] = (
        "reference-source-sha256:" + sha256(canonical(identity)).hexdigest()
    )
    fp = fingerprint_configuration(configuration, evidence_version="0.4.0")
    for name, manifest_name in [
        ("serving", "serving-manifest.json"),
        ("binding-before", "path-binding.json"),
        ("binding-after", "path-binding.json"),
    ]:
        obj(client, name + "/configuration.json", configuration)
        p = client / name / manifest_name
        manifest = json.loads(p.read_text())
        manifest["configuration"]["sha256"] = sha256(
            (client / name / "configuration.json").read_bytes()
        ).hexdigest()
        manifest["configuration_fingerprint"] = fp
        obj(client, name + "/" + manifest_name, manifest)
    obj(
        client,
        "actual-runtime.json",
        {
            "prefix_cache_enabled": False,
            "block_size": 16,
            "kv_cache_dtype": "auto",
            "tensor_parallel_size": 1,
            "kv_connector": "LMCacheConnectorV1Dynamic",
            "lmcache": {
                "chunk_size": 32,
                "local_cpu": False,
                "local_disk": configuration["arms"][0]["tier_path"],
                "use_layerwise": False,
                "save_unfull_chunk": False,
                "enable_scheduler_bypass_lookup": False,
                "store_location": "LocalDiskBackend",
                "retrieve_locations": ["LocalDiskBackend"],
                "use_odirect": io_mode == "direct",
            },
        },
    )
    path = configuration["arms"][0]["tier_path"] + "/key-a.pt"
    manifest = json.loads((client / "serving/serving-manifest.json").read_text())
    phases = manifest["phases"]
    start, end = json.loads((client / "retrieve.json").read_text())["interval_ns"]
    first = json.loads(
        (client / "serving" / phases["quiet_before"]["before"]["path"]).read_text()
    )["capture_interval_ns"][0]
    last = json.loads(
        (client / "serving" / phases["quiet_after"]["after"]["path"]).read_text()
    )["capture_interval_ns"][1]
    digest = sha256(payload).hexdigest()
    for index, operation, phase, bounds in [
        (1, "write", "store", [first - 1000, first - 500]),
        (2, "read", "retrieval", [start, end]),
    ]:
        put(client, f"io/{index:04d}-{operation}-buffer.bin", payload)
        obj(
            client,
            f"io/{index:04d}-call.json",
            {
                "operation": operation,
                "phase": phase,
                "path": path,
                "pid": frozen["pid"],
                "thread_id": 1,
                "key": "key-a" if operation == "read" else None,
                "requested_bytes": 8192,
                "syscall_return_bytes": None,
                "offset": 0,
                "offset_basis": "synthetic recognized source",
                "use_odirect": io_mode == "direct",
                "disk_block_bytes": 4096,
                "buffer_sha256": digest,
                "call_outcome": "returned",
                "interval_ns": bounds,
            },
        )
    for label, bounds in [
        ("before-request", [first - 400, first - 100]),
        ("after-request", [last + 100, last + 400]),
    ]:
        artifact = label + "-file.bin"
        put(client, "io/" + artifact, payload)
        obj(
            client,
            "io/" + label + "-files.json",
            {
                "clock": "CLOCK_MONOTONIC",
                "interval_ns": bounds,
                "files": [
                    {
                        "path": path,
                        "artifact": artifact,
                        "bytes": 8192,
                        "sha256": digest,
                        "inode": 777,
                        "device": "0:55",
                        "mtime_ns": 1,
                        "ctime_ns": 1,
                    }
                ],
            },
        )
    binding = json.loads((client / "binding-before/path-binding.json").read_text())
    namespace = binding["epochs"]["before"]["roles"]["serving"]["namespace"]
    for name, bounds in [
        ("scope-before.json", [first - 10000, first - 9000]),
        ("scope-after.json", [last + 9000, last + 10000]),
    ]:
        obj(
            root,
            name,
            {
                "scope_inventory_version": "0.1.0-experimental",
                "mount_namespace": namespace,
                "owner_pid": 1000,
                "clock": "CLOCK_MONOTONIC",
                "boot_id": frozen["boot_id"],
                "time_namespace": frozen["time_namespace"],
                "interval_ns": bounds,
                "unresolved_processes": [],
                "members": [
                    {
                        "pid": 1000,
                        "stat": "1000 (owner) S 1 0 0",
                        "cmdline": "synthetic owner",
                    },
                    {
                        "pid": 1001,
                        "stat": "1001 (collector) S 1000 0 0",
                        "cmdline": "synthetic collector",
                    },
                ],
                "mountinfo": (
                    "1 1 0:1 / / rw - ext4 /dev/synthetic rw\n"
                    "10 1 0:55 / /tier rw - nfs4 storage.invalid:/export rw\n"
                ),
            },
        )
    (root / "outer-mount-namespace.txt").write_text("mnt:[999]\n")
    seal(client)
    return configuration
