# SPDX-License-Identifier: Apache-2.0
"""Experimental raw NFS file-I/O capture/replay, separate from normalized evidence."""

# Standard
from collections.abc import Callable, Sequence
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import re
import time
from typing import Any

# Local
from .nfs_observation import (
    NfsObservationError,
    counter_delta,
    parse_mountinfo,
    parse_mountstats,
)

_VERSION = "0.1.0-experimental"
_ID = re.compile(r"[A-Za-z0-9_-]+")
_HASH = re.compile(r"[0-9a-f]{64}")


def _integer(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise NfsObservationError(f"{label} must be a non-negative integer")
    return value


def _pair(value: Any) -> tuple[int, int]:
    if not isinstance(value, list) or len(value) != 2:
        raise NfsObservationError("capture interval must contain two timestamps")
    a, b = (_integer(v, "timestamp") for v in value)
    if a > b:
        raise NfsObservationError("capture clock moved backwards")
    return a, b


def _object(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise NfsObservationError("expected a JSON object")
    return value


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise NfsObservationError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _constant(value: str) -> None:
    raise NfsObservationError(f"non-finite JSON number: {value}")


def _decode(data: bytes) -> dict[str, Any]:
    try:
        return _object(
            json.loads(data, object_pairs_hook=_unique, parse_constant=_constant)
        )
    except (UnicodeError, ValueError) as exc:
        raise NfsObservationError(f"unreadable JSON: {exc}") from exc


def _path(root: Path, name: Any) -> Path:
    if not isinstance(name, str) or not name or "\\" in name:
        raise NfsObservationError("artifact path must be a relative POSIX path")
    relative = PurePosixPath(name)
    if relative.is_absolute() or ".." in relative.parts:
        raise NfsObservationError("artifact path escapes the bundle")
    path = root / name
    try:
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(root.resolve()) or not resolved.is_file():
            raise NfsObservationError("artifact escapes the bundle or is not a file")
    except (OSError, RuntimeError) as exc:
        raise NfsObservationError(f"artifact unavailable: {name}") from exc
    return resolved


def _read(root: Path, ref: Any) -> bytes:
    ref = _object(ref)
    if (
        set(ref) != {"path", "sha256"}
        or not isinstance(ref["sha256"], str)
        or not _HASH.fullmatch(ref["sha256"])
    ):
        raise NfsObservationError("malformed artifact reference")
    data = _path(root, ref["path"]).read_bytes()
    if sha256(data).hexdigest() != ref["sha256"]:
        raise NfsObservationError("artifact hash mismatch")
    return data


def _write(root: Path, name: str, data: bytes) -> dict[str, str]:
    with (root / name).open("xb") as stream:
        stream.write(data)
    return {"path": name, "sha256": sha256(data).hexdigest()}


def _write_json(root: Path, name: str, value: dict[str, Any]) -> dict[str, str]:
    return _write(
        root, name, (json.dumps(value, allow_nan=False, indent=2) + "\n").encode()
    )


def _raw(root: Path, refs: Any, mountpoint: str) -> tuple[Any, dict[str, str]]:
    refs = _object(refs)
    if set(refs) != {"mountstats", "mountinfo"}:
        raise NfsObservationError("both raw proc snapshots are required")
    try:
        counters = parse_mountstats(
            _read(root, refs["mountstats"]).decode(), mountpoint
        )
        identity = parse_mountinfo(_read(root, refs["mountinfo"]).decode(), mountpoint)
    except UnicodeError as exc:
        raise NfsObservationError("proc artifact is not UTF-8") from exc
    if (
        counters.fstype != identity["fstype"]
        or counters.export != identity["mount_source"]
    ):
        raise NfsObservationError("mountstats and mountinfo disagree")
    return counters, identity


def _window(before: Any, operation: Any, after: Any) -> tuple[int, int]:
    a, b, c = _pair(before), _pair(operation), _pair(after)
    if not a[1] <= b[0] < b[1] <= c[0]:
        raise NfsObservationError("operation is outside its snapshot interval")
    return a[0], c[1]


def _snapshot(root: Path, ref: Any) -> dict[str, Any]:
    value = _decode(_read(root, ref))
    required = {
        "nfs_capture_version",
        "kind",
        "clock",
        "clock_domain",
        "mount_namespace",
        "capture_interval_ns",
        "mountpoint",
        "raw",
    }
    if (
        set(value) != required
        or value["nfs_capture_version"] != _VERSION
        or value["kind"] != "nfs_snapshot"
    ):
        raise NfsObservationError("unsupported snapshot contract")
    domain = _object(value["clock_domain"])
    if (
        value["clock"] != "CLOCK_MONOTONIC"
        or set(domain) != {"boot_id", "time_namespace"}
        or not isinstance(domain["boot_id"], str)
        or not re.fullmatch(
            r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", domain["boot_id"]
        )
        or not isinstance(domain["time_namespace"], str)
        or not re.fullmatch(r"time:\[[0-9]+\]", domain["time_namespace"])
        or not isinstance(value["mount_namespace"], str)
        or not re.fullmatch(r"mnt:\[[0-9]+\]", value["mount_namespace"])
    ):
        raise NfsObservationError("unsupported clock or namespace provenance")
    if not isinstance(value["mountpoint"], str) or not value["mountpoint"].startswith(
        "/"
    ):
        raise NfsObservationError("mountpoint must be absolute")
    _pair(value["capture_interval_ns"])
    counters, identity = _raw(root, value["raw"], value["mountpoint"])
    return {**value, "counters": counters, "identity": identity}


def _new_operation(root: Path, ref: Any) -> dict[str, Any]:
    op = _decode(_read(root, ref))
    required = {
        "nfs_capture_version",
        "kind",
        "request_id",
        "operation_kind",
        "before",
        "after",
        "operation_interval_ns",
        "result",
        "error",
    }
    if (
        set(op) != required
        or op["nfs_capture_version"] != _VERSION
        or op["kind"] != "nfs_operation"
        or not isinstance(op["request_id"], str)
        or not _ID.fullmatch(op["request_id"])
        or op["operation_kind"] not in {"file_read", "idle"}
    ):
        raise NfsObservationError("unsupported operation contract")
    before, after = _snapshot(root, op["before"]), _snapshot(root, op["after"])
    if (
        before["clock_domain"] != after["clock_domain"]
        or before["mount_namespace"] != after["mount_namespace"]
        or before["identity"] != after["identity"]
    ):
        raise NfsObservationError("clock domain, namespace or mount identity changed")
    window = _window(
        before["capture_interval_ns"],
        op["operation_interval_ns"],
        after["capture_interval_ns"],
    )
    delta = counter_delta(before["counters"], after["counters"])
    if op["error"] is not None:
        raise NfsObservationError("operation failed; incomplete observations retained")
    result = _object(op["result"])
    if set(result) != {
        "expected_bytes",
        "received_bytes",
        "expected_sha256",
        "received_sha256",
    }:
        raise NfsObservationError("malformed byte-comparison receipt")
    expected = _integer(result["expected_bytes"], "expected bytes")
    if op["operation_kind"] == "idle":
        if expected != 0 or any(
            result[k] is not None
            for k in ("received_bytes", "expected_sha256", "received_sha256")
        ):
            raise NfsObservationError("idle record contains read results")
        correct = None
    else:
        received = _integer(result["received_bytes"], "received bytes")
        if not expected or any(
            not isinstance(result[k], str) or not _HASH.fullmatch(result[k])
            for k in ("expected_sha256", "received_sha256")
        ):
            raise NfsObservationError("file read lacks comparison hashes")
        correct = (
            expected == received
            and result["expected_sha256"] == result["received_sha256"]
        )
    return {
        "request_id": op["request_id"],
        "capture_interval_ns": list(window),
        "counter_delta": delta,
        "reported_correct": correct,
        "expected_bytes_recorded": expected,
        "metadata_status": "recorded_per_snapshot",
        "scope": {
            "mountpoint": before["mountpoint"],
            "identity": before["identity"],
            "mount_namespace": before["mount_namespace"],
            "clock_domain": before["clock_domain"],
            "options": list(before["counters"].options),
        },
    }


def _legacy_operation(root: Path, op: Any) -> dict[str, Any]:
    op = _object(op)
    request_id = op.get("request_id")
    if not isinstance(request_id, str) or not _ID.fullmatch(request_id):
        raise NfsObservationError("invalid legacy request ID")
    individual = _decode(_path(root, request_id + ".json").read_bytes())
    if individual != op:
        raise NfsObservationError("legacy summary and individual record disagree")
    mount = _object(op.get("mount"))
    point = mount.get("mountpoint")
    if not isinstance(point, str) or not point.startswith("/"):
        raise NfsObservationError("invalid legacy mountpoint")
    intervals = op.get("counter_capture_intervals_ns")
    if not isinstance(intervals, list) or len(intervals) != 2:
        raise NfsObservationError("missing legacy snapshot intervals")
    window = _window(intervals[0], op.get("operation_interval_ns"), intervals[1])
    if list(window) != op.get("capture_interval_ns"):
        raise NfsObservationError("legacy envelope contradicts capture intervals")
    refs = _object(op.get("artifacts"))
    if set(refs) != {"before", "after"}:
        raise NfsObservationError("legacy operation needs before and after artifacts")
    a, identity_a = _raw(root, refs["before"], point)
    b, identity_b = _raw(root, refs["after"], point)
    if identity_a != identity_b or any(
        mount.get(k) != identity_a[k]
        for k in ("mount_id", "major_minor", "root", "fstype")
    ):
        raise NfsObservationError("legacy mount identity changed or disagrees")
    if (
        mount.get("export") != a.export
        or mount.get("options") != list(a.options)
        or mount.get("transport") != list(a.transports)
    ):
        raise NfsObservationError("legacy scope receipt contradicts raw observations")
    delta = counter_delta(a, b)
    reported = _object(op.get("counter_delta"))
    if (
        set(reported) != set(delta)
        or any(type(v) is not int for v in reported.values())
        or reported != delta
    ):
        raise NfsObservationError("legacy reported delta contradicts raw counters")
    if op.get("correct") is not None and not isinstance(op["correct"], bool):
        raise NfsObservationError("legacy correctness is malformed")
    return {
        "request_id": request_id,
        "capture_interval_ns": list(window),
        "counter_delta": delta,
        "reported_correct": op.get("correct"),
        "expected_bytes_recorded": _integer(op.get("expected_bytes"), "expected bytes"),
        "metadata_status": "legacy_snapshot_namespace_and_clock_domain_unavailable",
        "scope": {
            "mountpoint": point,
            "identity": identity_a,
            "mount_namespace": None,
            "clock_domain": None,
            "options": list(a.options),
        },
    }


def _validate_sequence(operations: list[dict[str, Any]]) -> None:
    ids = [op["request_id"] for op in operations]
    if len(set(ids)) != len(ids):
        raise NfsObservationError("duplicate operation ID")
    for a, b in zip(operations, operations[1:], strict=False):
        if a["capture_interval_ns"][1] > b["capture_interval_ns"][0]:
            raise NfsObservationError(
                "operation intervals overlap or arrive out of order"
            )
        if a["scope"] != b["scope"]:
            raise NfsObservationError("scope changed between operations")


def _new_operations(root: Path, entries: Any) -> list[dict[str, Any]]:
    if not isinstance(entries, list) or not entries:
        raise NfsObservationError("bundle has no operations")
    operations = [_new_operation(root, ref) for ref in entries]
    paths = [ref["path"] for ref in entries]
    if len(set(paths)) != len(paths) or set(paths) != {
        p.name for p in root.glob("*.operation.json")
    }:
        raise NfsObservationError("manifest must cover every operation exactly once")
    _validate_sequence(operations)
    return operations


def replay_bundle(bundle_dir: Path) -> dict[str, Any]:
    """Recompute a completed raw NFS diagnostic from retained files only.

    Args:
        bundle_dir: Extracted run directory; archives are not extracted by this API.

    Returns:
        Observation-only results with provenance gaps. No serving verdict is made.

    Raises:
        NfsObservationError: Missing/tampered files, unsupported contract, identity,
            clock, counter or receipt inconsistency. Invalid new manifests never
            fall back to legacy parsing.
        OSError: The bundle directory cannot be accessed.
    """
    root = Path(bundle_dir).resolve(strict=True)
    if not root.is_dir():
        raise NfsObservationError("bundle root must be a directory")
    if (root / "manifest.json").exists() or (root / "manifest.json").is_symlink():
        manifest = _decode(_path(root, "manifest.json").read_bytes())
        if (
            set(manifest) != {"nfs_capture_version", "kind", "workload", "operations"}
            or manifest["nfs_capture_version"] != _VERSION
            or manifest["kind"] != "nfs_observation_bundle"
            or manifest["workload"] != "isolated_file_io"
        ):
            raise NfsObservationError("unsupported bundle contract or workload")
        operations = _new_operations(root, manifest["operations"])
        kind = "nfs_capture"
        gaps = [
            "comparison_hashes_are_collector_receipts_not_independent_serving_correctness",
            "configuration_and_serving_request_not_recorded",
            "exclusive_use_not_established",
        ]
    else:
        try:
            artifacts = (root / "artifacts").resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise NfsObservationError("legacy artifacts directory unavailable") from exc
        if not artifacts.is_relative_to(root) or not artifacts.is_dir():
            raise NfsObservationError(
                "legacy artifacts directory escapes bundle or is not a directory"
            )
        root = artifacts
        summary = _decode(_path(root, "summary.json").read_bytes())
        if (
            summary.get("diagnostic_version") != "0.1.0-experimental"
            or summary.get("workload") != "isolated_file_io_not_kv"
            or summary.get("status") != "diagnostic_completed"
            or summary.get("clock") != "CLOCK_MONOTONIC"
        ):
            raise NfsObservationError("unsupported or incomplete legacy diagnostic")
        entries = summary.get("requests")
        if not isinstance(entries, list) or not entries:
            raise NfsObservationError("legacy diagnostic has no intervals")
        operations = [_legacy_operation(root, op) for op in entries]
        if {op["request_id"] + ".json" for op in operations} != {
            path.name for path in root.glob("*.json") if path.name != "summary.json"
        }:
            raise NfsObservationError("legacy summary must cover every operation")
        kind = "legacy_n1_diagnostic"
        gaps = [
            "per_snapshot_namespace_and_clock_domain_not_recorded",
            "correctness_is_a_collector_boolean_not_independent_serving_correctness",
            "configuration_and_serving_request_not_recorded",
            "exclusive_use_not_established",
        ]
    _validate_sequence(operations)
    return {
        "input_kind": kind,
        "status": "observation_only",
        "operations": operations,
        "provenance_gaps": gaps,
        "serving_verification": "not_assessed",
        "performance_claims": "suppressed",
    }


def read_artifact(bundle_dir: Path, reference: Any) -> bytes:
    """Read a hash-verified artifact confined to an extracted bundle directory.

    Args:
        bundle_dir: Existing extracted bundle directory.
        reference: Exact relative path and SHA-256 mapping.

    Returns:
        Retained artifact bytes; no archive or external path is read.

    Raises:
        NfsObservationError: Malformed/escaping reference or hash mismatch.
        OSError: An input directory or artifact is inaccessible.
    """
    return _read(Path(bundle_dir).resolve(strict=True), reference)


def read_snapshot(bundle_dir: Path, reference: Any) -> dict[str, Any]:
    """Reparse one captured snapshot with its recorded namespace and clock domain.

    Args:
        bundle_dir: Existing extracted directory.
        reference: Hash-bound snapshot manifest reference.

    Returns:
        Validated metadata, parsed MountCounters and mountinfo identity. These
        are raw observations, not an attribution or serving verdict.

    Raises:
        NfsObservationError: Invalid contract, raw data, provenance or reference.
        OSError: An input directory or artifact is inaccessible.
    """
    return _snapshot(Path(bundle_dir).resolve(strict=True), reference)


def capture_snapshot(bundle_dir: Path, mountpoint: str, label: str) -> dict[str, str]:
    """Retain raw proc files and per-snapshot namespace/clock metadata on Linux.

    Args:
        bundle_dir: Existing writable directory outside the measured mount.
        mountpoint: Exact NFS/RDMA mountpoint.
        label: Unique alphanumeric/underscore/hyphen artifact prefix.

    Returns:
        Reference to a newly written snapshot manifest.

    Raises:
        NfsObservationError: Unsafe output, changing namespace or malformed proc data.
        OSError: Missing Linux proc files or existing/unwritable output files.
    """
    root = Path(bundle_dir).resolve(strict=True)
    if not _ID.fullmatch(label) or root.is_relative_to(Path(mountpoint).resolve()):
        raise NfsObservationError("unsafe snapshot output")

    def context() -> tuple[str, dict[str, str]]:
        return os.readlink("/proc/self/ns/mnt"), {
            "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
            "time_namespace": os.readlink("/proc/self/ns/time"),
        }

    started = time.monotonic_ns()
    namespace, domain = context()
    raw = {
        name: Path(f"/proc/self/{name}").read_bytes()
        for name in ("mountstats", "mountinfo")
    }
    final_namespace, final_domain = context()
    ended = time.monotonic_ns()
    refs = {
        name: _write(root, f"{label}.{name}.txt", data) for name, data in raw.items()
    }
    metadata = {
        "nfs_capture_version": "0.1.0-experimental",
        "kind": "nfs_snapshot",
        "clock": "CLOCK_MONOTONIC",
        "clock_domain": domain,
        "mount_namespace": namespace,
        "capture_interval_ns": [started, ended],
        "mountpoint": mountpoint,
        "raw": refs,
    }
    if (final_namespace, final_domain) != (namespace, domain):
        metadata["capture_error"] = "namespace_or_clock_domain_changed"
        metadata["context_after"] = {
            "mount_namespace": final_namespace,
            "clock_domain": final_domain,
        }
        _write_json(root, label + ".snapshot.json", metadata)
        raise NfsObservationError("namespace or clock domain changed during capture")
    ref = _write_json(root, label + ".snapshot.json", metadata)
    _snapshot(root, ref)
    return ref


def capture_operation(
    bundle_dir: Path,
    mountpoint: str,
    request_id: str,
    operation: Callable[[], bytes | None],
    expected: bytes | None,
) -> dict[str, str]:
    """Capture a synchronous file read or idle interval, never a serving claim.

    Args:
        bundle_dir: Existing diagnostic directory.
        mountpoint: Exact NFS/RDMA mountpoint.
        request_id: Unique artifact-safe interval ID.
        operation: Controlled callable returning actual bytes, or None for idle.
        expected: Expected file bytes; None for an idle operation.

    Returns:
        Reference to the new operation record with before/after snapshot manifests.

    Raises:
        NfsObservationError: Failed operation, inconsistent capture or unsafe identity.
        OSError: Missing proc files or an existing/unwritable output artifact.
    """
    if not isinstance(request_id, str) or not _ID.fullmatch(request_id):
        raise NfsObservationError("invalid request ID")
    root = Path(bundle_dir).resolve(strict=True)
    before = capture_snapshot(root, mountpoint, request_id + "-before")
    started = time.monotonic_ns()
    received = None
    error = None
    try:
        received = operation()
        if (expected is None and received is not None) or (
            expected is not None and not isinstance(received, bytes)
        ):
            raise NfsObservationError("operation returned an unexpected type")
    except Exception as exc:
        error = type(exc).__name__ + ": " + str(exc)
    ended = time.monotonic_ns()
    after = capture_snapshot(root, mountpoint, request_id + "-after")
    record = {
        "nfs_capture_version": "0.1.0-experimental",
        "kind": "nfs_operation",
        "request_id": request_id,
        "operation_kind": "idle" if expected is None else "file_read",
        "before": before,
        "after": after,
        "operation_interval_ns": [started, ended],
        "result": {
            "expected_bytes": len(expected) if expected is not None else 0,
            "received_bytes": len(received) if isinstance(received, bytes) else None,
            "expected_sha256": sha256(expected).hexdigest()
            if expected is not None
            else None,
            "received_sha256": sha256(received).hexdigest()
            if isinstance(received, bytes)
            else None,
        },
        "error": error,
    }
    ref = _write_json(root, request_id + ".operation.json", record)
    _new_operation(root, ref)
    return ref


def seal_bundle(bundle_dir: Path, operation_refs: Sequence[dict[str, str]]) -> Path:
    """Write one completed file-I/O manifest and replay it for consistency.

    Args:
        bundle_dir: Directory containing all captured operation artifacts.
        operation_refs: Ordered references covering every operation exactly once.

    Returns:
        New manifest path. The bundle remains observation-only.

    Raises:
        NfsObservationError: Empty/incomplete, failed or inconsistent observations.
        OSError: Existing manifest or unwritable output.
            Existing files are never replaced.
    """
    root = Path(bundle_dir).resolve(strict=True)
    _new_operations(root, list(operation_refs))
    manifest = {
        "nfs_capture_version": "0.1.0-experimental",
        "kind": "nfs_observation_bundle",
        "workload": "isolated_file_io",
        "operations": list(operation_refs),
    }
    _write_json(root, "manifest.json", manifest)
    replay_bundle(root)
    return root / "manifest.json"
