# SPDX-License-Identifier: Apache-2.0
"""Explicit NFS reference-originals adapter; never infers a block-device filter."""

# Standard
from hashlib import sha256
import json
from pathlib import Path
from typing import Any

# Local
from .configuration import fingerprint_configuration, validate_configuration
from .ingest import IngestError
from .nfs_client import replay_client_execution
from .nfs_bundle import read_snapshot
from .nfs_observation import NfsObservationError
from .nfs_policy import COUNTS, FLAGS, ROLES
from .nfs_qualification import (
    QualificationError,
    contained,
    layout_facts,
    namespace_facts,
    read_json,
    reference_client_binding,
)

NFS_ADAPTER_NAME = "nfs-reference-adapter"
NFS_ADAPTER_VERSION = "0.1.0-experimental"

# These optional raw-format diagnostics are decoded by the stack-health reader.
# Keep their bytes/hashes in the inventory without making JSON syntax a Gate 1
# condition. No measurement JSON or similarly named file elsewhere is exempt.
_STACK_DIAGNOSTICS = frozenset(
    "candidate/client/" + prefix + name
    for prefix in ("", "baseline/")
    for name in ("shutdown.json", "reference-cleanup-state.json")
)


def _role(name: str) -> str:
    if "/proof/" in name or name.endswith(
        ("output.bin", "baseline.bin", "request.response.bin")
    ):
        return "correctness"
    if "/io/" in name:
        return (
            "source"
            if name.endswith(".source") or name.endswith("implementation.json")
            else "file_layout"
        )
    if "/serving/" in name:
        return "counters"
    if (
        "/binding-" in name
        or name.startswith("scope-")
        or name == "outer-mount-namespace.txt"
    ):
        return "namespace"
    if name.endswith(".source"):
        return "source"
    return "client"


def _inventory(root: Path) -> tuple[list[dict[str, str]], dict[str, list[str]]]:
    artifacts = []
    roles: dict[str, list[str]] = {name: [] for name in ROLES}
    paths = sorted((root / "candidate").rglob("*"))
    paths += [
        root / name
        for name in (
            "scope-before.json",
            "scope-after.json",
            "outer-mount-namespace.txt",
        )
        if (root / name).exists()
    ]
    for path in paths:
        if path.is_dir() and not path.is_symlink():
            continue
        if path.is_symlink() or not path.is_file():
            raise IngestError("reference artifact tree contains a link or special file")
        name = path.relative_to(root).as_posix()
        data = contained(root, name).read_bytes()
        if path.suffix == ".json" and name not in _STACK_DIAGNOSTICS:
            read_json(root, name)  # Malformed measurement JSON remains an error.
        role = _role(name)
        identifier = f"nfs-original-{len(artifacts):04d}"
        artifacts.append(
            {
                "id": identifier,
                "kind": "nfs_" + role,
                "sha256": sha256(data).hexdigest(),
            }
        )
        roles[role].append(identifier)
    return artifacts, roles


def _unmeasured(name: str) -> dict[str, Any]:
    return {
        "availability": {
            "status": "unsupported",
            "reason_code": name + "_not_measured",
            "detail": "Only NFS Gate 1 request evidence was collected.",
        }
    }


def build_nfs_document(
    run_dir: Path, configuration: dict[str, Any], run_id: str
) -> dict[str, Any]:
    """Normalize one retained tiny reference run, with no live access.

    Args:
        run_dir: Full extracted run with candidate/client and scope originals.
            No archive extraction, sudo, process query or workload is performed.
        configuration: Frozen explicit 0.4.0 configuration from this same run.
        run_id: Nonempty identifier for the normalized document.

    Returns:
        Evidence 0.4.0 for Gate 1 only. Missing originals stay UNPROVEN candidates;
        capture contradictions remain INVALID observations. Stack exit is omitted
        from measurement policy; raw operational diagnostics remain in the bundle.

    Raises:
        IngestError: Unsupported input shape, malformed data, or caller/config mismatch.
        ConfigurationError: Invalid explicit configuration.
        OSError: Root or configuration input unavailable.
    """
    validate_configuration(configuration, evidence_version="0.4.0")
    if not isinstance(run_id, str) or not run_id.strip():
        raise IngestError("NFS run_id must be nonempty text")
    root = run_dir.resolve(strict=True)
    if not root.is_dir() or not (root / "candidate").is_dir():
        raise IngestError("nfs-reference requires a full extracted reference run")
    arms = configuration["arms"]
    if len(arms) != 1 or arms[0]["attribution_provider"] != "nfs_mountstats":
        raise IngestError("NFS reference checkpoint requires exactly one NFS arm")
    if configuration["measurement"]["concurrency"] != [1]:
        raise IngestError("NFS reference checkpoint is serial only")
    fingerprint = fingerprint_configuration(configuration, evidence_version="0.4.0")
    try:
        artifacts, roles = _inventory(root)
    except (QualificationError, ValueError) as exc:
        raise IngestError(str(exc)) from exc
    client = root / "candidate/client"
    config_path = client / "serving/configuration.json"
    if (
        config_path.exists()
        and read_json(client / "serving", "configuration.json") != configuration
    ):
        raise IngestError(
            "supplied configuration differs from the captured NFS configuration"
        )
    measurement: dict[str, Any] = {
        **{key: None for key in COUNTS},
        **{key: None for key in FLAGS},
        "capture_status": "missing",
        "capture_reason": "required_original_unavailable",
        "configuration_fingerprint": fingerprint,
        "workload": "serving_retrieval",
        "clock": "CLOCK_MONOTONIC",
        "interval_ns": None,
        "counter": "NFSIOS_SERVERREADBYTES",
        "identity_binding": "missing",
        "source_binding": "missing",
        "payload_mapping": "unknown",
        "namespace_binding": "unknown",
        "artifact_roles": roles,
    }
    correct, observed, request_id = None, None, "reference-retrieval"
    try:
        parent_manifest = root / "candidate/process-manifest.json"
        replay_root = (
            root / "candidate"
            if parent_manifest.exists() or parent_manifest.is_symlink()
            else client
        )
        replay = replay_client_execution(replay_root)
        if (
            replay["measurement_evidence"]["status"] != "available"
            or replay.get("mode") != "nfs"
        ):
            raise FileNotFoundError("completed NFS request originals unavailable")
        observations = replay["nfs_observations"]
        manifest = read_json(client / "serving", "serving-manifest.json")
        phase = manifest["phases"]["request"]
        before = read_snapshot(client / "serving", phase["before"])
        after = read_snapshot(client / "serving", phase["after"])
        b, a = before["counters"], after["counters"]
        delta = observations["phase_deltas"]["request"]
        correct = replay["baseline_output_equal"] and replay["kv_originals_equal"]
        observed = delta["server_read_payload_bytes"]
        request_id = observations["request_id"]
        mapping = read_json(client / "serving", "scope-mapping.json")

        def identity(value: dict[str, Any]) -> str:
            return sha256(
                json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()

        configured = identity(mapping["identity"])
        first, last = identity(before["identity"]), identity(after["identity"])
        binding = (
            "changed"
            if first != last
            else "wrong_target"
            if first != configured
            else "matched"
        )
        measurement.update(
            {
                "capture_status": "invalid"
                if observations["callback_errors"]
                else "complete",
                "capture_reason": "measurement_callback_error"
                if observations["callback_errors"]
                else "captured_request",
                "identity_binding": binding,
                "client_binding": True,
                "interval_ns": [
                    before["capture_interval_ns"][1],
                    *phase["interval_ns"],
                    after["capture_interval_ns"][0],
                ],
                "counter_before": b.byte_counts[4],
                "counter_after": a.byte_counts[4],
                "rpc_before": b.read_rpc_counts[0],
                "rpc_after": a.read_rpc_counts[0],
                "retransmissions": max(
                    0, delta["read_rpc_transmissions"] - delta["read_rpc_operations"]
                ),
                "timeouts": delta["read_rpc_timeouts"],
                "errors": delta["read_rpc_errors"],
                "dedicated_mount": "nosharecache" in b.options
                and "nosharecache" in a.options,
                "serialized": True,  # Reference contract + single config concurrency.
                "quiet_activity": any(
                    v != 0
                    for name, d in observations["phase_deltas"].items()
                    if name != "request"
                    for v in d.values()
                ),
                "gap_activity": any(
                    v != 0
                    for d in observations["between_phase_deltas"].values()
                    for v in d.values()
                ),
                "io_mode_match": "io_mode_mismatch"
                not in observations["review_reasons"],
            }
        )
        if any(
            x["path_binding_status"] != "matched"
            for x in replay["path_bindings"].values()
        ):
            measurement["identity_binding"] = "missing"
        measurement["client_binding"] = reference_client_binding(client, configuration)
        facts = layout_facts(client, replay)
        kv_correct = facts.pop("kv_correct")
        correct = correct and kv_correct
        measurement.update(facts)
        measurement["namespace_binding"] = namespace_facts(root, client, replay)
    except FileNotFoundError:
        measurement["capture_status"] = "missing"
        measurement["capture_reason"] = "required_original_unavailable"
    except NfsObservationError as exc:
        text = str(exc).lower()
        unavailable = (
            "must identify rdma" in text
            or "only nfs statvers" in text
            or text.startswith("expected one ")
            or text.startswith("missing client original")
            or ("counters" in text and "malformed" in text)
        )
        if unavailable:
            measurement["capture_status"] = "missing"
            measurement["capture_reason"] = "counter_or_original_unavailable"
        elif any(
            word in text
            for word in ("unsupported", "duplicate", "non-finite", "must be an object")
        ):
            raise IngestError(
                "malformed or unsupported NFS reference original"
            ) from exc
        else:
            measurement["capture_status"] = "invalid"
            measurement["capture_reason"] = "raw_capture_inconsistent"
    except QualificationError as exc:
        if "malformed" in str(exc) or "duplicate" in str(exc):
            raise IngestError("malformed NFS qualification original") from exc
        measurement["capture_status"] = "invalid"
        measurement["capture_reason"] = "qualification_original_inconsistent"
    except (KeyError, TypeError, IndexError, ValueError, UnicodeError) as exc:
        raise IngestError("malformed NFS original structure") from exc
    expected = configuration["kv_geometry"]["bytes_per_chunk"]
    request = {
        "id": request_id,
        "correct": correct,
        "source": "target_tier",
        "expected_bytes": expected,
        "observed_bytes": observed,
        "attribution": {
            "provider": "nfs_mountstats",
            "granularity": "per_request",
            "confidence": "corroborating",
            "target_device_filtered": False,
            "target_scope": arms[0]["tier_identity"],
            "artifact_refs": [a["id"] for a in artifacts],
        },
        "nfs_measurement": measurement,
    }
    return {
        "schema_version": "0.4.0",
        "run_id": run_id,
        "provenance": {
            "normalized_by": {
                "adapter": NFS_ADAPTER_NAME,
                "version": NFS_ADAPTER_VERSION,
            },
            "artifacts": artifacts,
        },
        "validity": {"fatal_observations": []},
        "configurations": [
            {
                "id": "nfs-reference",
                "fingerprint": fingerprint,
                "parameters": configuration,
            }
        ],
        "bindings": {
            "attribution_requests": [
                {"request_id": request_id, "configuration_id": "nfs-reference"}
            ],
            "break_even": None,
            "admission": None,
            "capacity": None,
            "envelope_levels": [],
        },
        "evidence": {
            "attribution": {
                "availability": {"status": "supported"},
                "tolerance_fraction": 0,
                "requests": [request],
            },
            **{
                name: _unmeasured(name)
                for name in ("break_even", "admission", "capacity", "envelope")
            },
        },
    }
