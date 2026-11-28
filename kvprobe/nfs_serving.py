# SPDX-License-Identifier: Apache-2.0
"""Experimental serving-callback capture; replay remains observation only."""

# Standard
from collections.abc import Callable
from hashlib import sha256
import json
from pathlib import Path
import re
import time
from typing import Any

# Local
from .configuration import fingerprint_configuration
from .nfs_bundle import capture_snapshot, read_artifact, read_snapshot
from .nfs_observation import NfsObservationError, counter_delta


def _write(root: Path, name: str, data: bytes) -> dict[str, str]:
    with (root / name).open("xb") as stream:
        stream.write(data)
    return {"path": name, "sha256": sha256(data).hexdigest()}


def _json(root: Path, name: str, value: Any) -> dict[str, str]:
    return _write(
        root, name, (json.dumps(value, allow_nan=False, indent=2) + "\n").encode()
    )


def _decode(data: bytes) -> dict[str, Any]:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result = {}
        for key, value in items:
            if key in result:
                raise NfsObservationError("duplicate JSON key")
            result[key] = value
        return result

    def constant(value: str) -> None:
        raise NfsObservationError(f"non-finite JSON value: {value}")

    try:
        value = json.loads(data, object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, UnicodeError) as exc:
        raise NfsObservationError(f"invalid capture JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise NfsObservationError("capture JSON must be an object")
    return value


def _exact(value: Any, keys: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise NfsObservationError(f"malformed {label}")
    return value


def _positive(value: Any, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise NfsObservationError(f"{name} must be a positive integer")
    return value


def _request(parameters: dict[str, Any], request: Any) -> tuple[dict[str, Any], int]:
    request = _exact(
        request,
        {"id", "arm", "cacheable_tokens", "input_sha256"},
        "request specification",
    )
    if not isinstance(request["id"], str) or not re.fullmatch(
        r"[A-Za-z0-9_-]+", request["id"]
    ):
        raise NfsObservationError("unsafe request ID")
    digest = request["input_sha256"]
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise NfsObservationError("input SHA-256 is required")
    arms = [arm for arm in parameters["arms"] if arm["name"] == request["arm"]]
    if len(arms) != 1 or arms[0]["attribution_provider"] != "nfs_mountstats":
        raise NfsObservationError("request must select one configured NFS arm")
    geometry = parameters["kv_geometry"]
    tokens = _positive(request["cacheable_tokens"], "cacheable tokens")
    chunks, remainder = divmod(tokens, geometry["chunk_tokens"])
    if remainder:
        raise NfsObservationError("only complete identity-mapped chunks are defined")
    return arms[0]["tier_identity"], chunks * geometry["bytes_per_chunk"]


def _phase(
    root: Path,
    mount: str,
    label: str,
    operation: Callable[[], bytes | None],
    expected_mount: dict[str, Any],
) -> dict[str, Any]:
    before = capture_snapshot(root, mount, label + "-before")
    context = read_snapshot(root, before)
    if any(
        context[key] != expected_mount[key]
        for key in ("identity", "mount_namespace", "clock_domain")
    ):
        raise NfsObservationError("phase preflight changed; callback was not run")
    started = time.monotonic_ns()
    output = None
    error = None
    try:
        output = operation()
        if output is not None and not isinstance(output, bytes):
            raise TypeError("callback must return bytes or None")
    except Exception as exc:
        error = type(exc).__name__ + ": " + str(exc)
        if not isinstance(output, bytes):
            output = None
    ended = time.monotonic_ns()
    ref = None if output is None else _write(root, label + ".response.bin", output)
    after = capture_snapshot(root, mount, label + "-after")
    return {
        "before": before,
        "after": after,
        "interval_ns": [started, ended],
        "response": ref,
        "error": error,
    }


def capture_serving_candidate(
    bundle_dir: Path,
    configuration: dict[str, Any],
    request_spec: dict[str, Any],
    input_bytes: bytes,
    expected_output: bytes | None,
    mountpoint: str,
    expected_mount: dict[str, Any],
    operation: Callable[[], bytes],
    *,
    quiet_seconds: float = 1.0,
) -> Path:
    """Capture a controlled callback and adjacent quiet intervals as raw evidence.

    Args:
        bundle_dir: Existing empty directory outside the measured mount.
        configuration: Versioned experiment parameters with a named NFS arm.
        request_spec: ID, arm, cacheable tokens and input SHA-256. Geometry is
            independent of observed counters and requires complete chunks.
        input_bytes: Exact submitted input bytes; hash must match the frozen spec.
        expected_output: Independently obtained baseline bytes, retained before
            execution; None records missing correctness, never a successful check.
        mountpoint: Exact NFS/RDMA mountpoint.
        expected_mount: Operator's prior mapping with identity, mount_namespace
            and clock_domain fields. Verified against all snapshots, not inferred.
        operation: Synchronous serving-client callback returning comparable output.
            This API cannot establish the actual client/model/build/device binding.
        quiet_seconds: Positive idle interval on either side, at most 60 seconds.

    Returns:
        Retained manifest path, including failed-callback receipts. There is no
        serving verdict, normalized evidence or live serving acceptance here.

    Raises:
        NfsObservationError: Invalid parameters, unsafe output, nonempty directory
            or malformed mount/identity/request data.
        ConfigurationError: Invalid configuration parameters.
        OSError: Inaccessible artifacts/proc data. Partial originals are retained.
    """
    root = Path(bundle_dir).resolve(strict=True)
    if (
        not root.is_dir()
        or any(root.iterdir())
        or root.is_relative_to(Path(mountpoint).resolve())
    ):
        raise NfsObservationError(
            "capture needs an empty directory outside the measured mount"
        )
    if (
        isinstance(quiet_seconds, bool)
        or not isinstance(quiet_seconds, (int, float))
        or not 0 < quiet_seconds <= 60
    ):
        raise NfsObservationError("quiet_seconds must be positive and at most 60")
    fingerprint = fingerprint_configuration(configuration, evidence_version="0.4.0")
    scope, expected_kv = _request(configuration, request_spec)
    _exact(
        expected_mount,
        {"identity", "mount_namespace", "clock_domain"},
        "expected mount mapping",
    )
    if (
        not isinstance(input_bytes, bytes)
        or sha256(input_bytes).hexdigest() != request_spec["input_sha256"]
    ):
        raise NfsObservationError("input bytes contradict frozen request hash")
    if expected_output is not None and not isinstance(expected_output, bytes):
        raise NfsObservationError("baseline must be bytes or unavailable")
    configuration_ref = _json(root, "configuration.json", configuration)
    request_ref = _json(root, "request.json", request_spec)
    input_ref = _write(root, "input.bin", input_bytes)
    baseline_ref = (
        None
        if expected_output is None
        else _write(root, "baseline.bin", expected_output)
    )
    mapping = _json(
        root,
        "scope-mapping.json",
        {"scope": scope, "mountpoint": mountpoint, **expected_mount},
    )
    preflight = capture_snapshot(root, mountpoint, "preflight")
    initial = read_snapshot(root, preflight)
    if any(
        initial[key] != expected_mount[key]
        for key in ("identity", "mount_namespace", "clock_domain")
    ):
        raise NfsObservationError(
            "preflight differs from prior scope mapping; callback was not run"
        )
    phases = {}
    for name, action in [
        ("quiet_before", lambda: time.sleep(quiet_seconds)),
        ("request", operation),
        ("quiet_after", lambda: time.sleep(quiet_seconds)),
    ]:
        phases[name] = _phase(root, mountpoint, name, action, expected_mount)
    manifest = {
        "nfs_serving_capture_version": "0.1.0-experimental",
        "kind": "serving_callback_capture",
        "configuration_fingerprint": fingerprint,
        "configuration": configuration_ref,
        "request": request_ref,
        "input": input_ref,
        "baseline": baseline_ref,
        "mapping": mapping,
        "expected_kv_bytes": expected_kv,
        "producer_sha256": sha256(Path(__file__).read_bytes()).hexdigest(),
        "preflight": preflight,
        "phases": phases,
    }
    _json(root, "serving-manifest.json", manifest)
    return root / "serving-manifest.json"


def replay_serving_candidate(bundle_dir: Path) -> dict[str, Any]:
    """Recompute a candidate from retained raw bytes without a serving verdict.

    Args:
        bundle_dir: Extracted candidate capture directory; legacy file-I/O bundles
            cannot be upgraded by changing a workload string.

    Returns:
        Geometry, correctness comparison, phase payload/RPC deltas and review
        reasons. Diagnostics flag adverse mount-scoped deltas, not request-ID
        attribution or a verdict. Always observation_only/not_assessed/suppressed.
        Quiet controls
        are observations, not proof of exclusive use or established serving-client
        provenance.

    Raises:
        NfsObservationError: Wrong contract, missing/tampered data, inconsistent
            mapping/configuration, reset, clock/identity drift or invalid intervals.
        ConfigurationError: Malformed captured configuration.
        OSError: Inaccessible originals. No replay-host proc data is consulted.
    """
    root = Path(bundle_dir).resolve(strict=True)
    manifest_path = root / "serving-manifest.json"
    if not manifest_path.resolve(strict=True).is_relative_to(root):
        raise NfsObservationError("capture manifest escapes bundle")
    manifest = _decode(manifest_path.read_bytes())
    _exact(
        manifest,
        {
            "nfs_serving_capture_version",
            "kind",
            "configuration_fingerprint",
            "configuration",
            "request",
            "input",
            "baseline",
            "mapping",
            "expected_kv_bytes",
            "producer_sha256",
            "phases",
            "preflight",
        },
        "serving capture manifest",
    )
    if (
        manifest["nfs_serving_capture_version"] != "0.1.0-experimental"
        or manifest["kind"] != "serving_callback_capture"
    ):
        raise NfsObservationError("unsupported serving capture contract")
    if not isinstance(manifest["producer_sha256"], str) or not re.fullmatch(
        r"[0-9a-f]{64}", manifest["producer_sha256"]
    ):
        raise NfsObservationError("malformed producer source receipt")
    configuration = _decode(read_artifact(root, manifest["configuration"]))
    fingerprint = fingerprint_configuration(configuration, evidence_version="0.4.0")
    if fingerprint != manifest["configuration_fingerprint"]:
        raise NfsObservationError("configuration fingerprint mismatch")
    request = _decode(read_artifact(root, manifest["request"]))
    scope, expected = _request(configuration, request)
    if _positive(manifest["expected_kv_bytes"], "expected KV bytes") != expected:
        raise NfsObservationError("expected bytes contradict independent geometry")
    if (
        sha256(read_artifact(root, manifest["input"])).hexdigest()
        != request["input_sha256"]
    ):
        raise NfsObservationError("submitted input hash mismatch")
    mapping = _decode(read_artifact(root, manifest["mapping"]))
    _exact(
        mapping,
        {"scope", "mountpoint", "identity", "mount_namespace", "clock_domain"},
        "scope mapping",
    )
    if mapping["scope"] != scope:
        raise NfsObservationError("mapping contradicts configured scope")
    phases = _exact(
        manifest["phases"],
        {"quiet_before", "request", "quiet_after"},
        "phase inventory",
    )
    initial = read_snapshot(root, manifest["preflight"])
    if initial["mountpoint"] != mapping["mountpoint"] or any(
        initial[key] != mapping[key]
        for key in ("identity", "mount_namespace", "clock_domain")
    ):
        raise NfsObservationError("preflight contradicts configured mapping")
    observations = {}
    gaps = {}
    previous_end = initial["capture_interval_ns"][1]
    previous_counters = initial["counters"]
    errors = []
    for name in ("quiet_before", "request", "quiet_after"):
        phase = _exact(
            phases[name],
            {"before", "after", "interval_ns", "response", "error"},
            "phase",
        )
        before, after = [read_snapshot(root, phase[key]) for key in ("before", "after")]
        for snapshot in (before, after):
            if snapshot["mountpoint"] != mapping["mountpoint"] or any(
                snapshot[key] != mapping[key]
                for key in ("identity", "mount_namespace", "clock_domain")
            ):
                raise NfsObservationError("snapshot differs from prior scope mapping")
        interval = phase["interval_ns"]
        if (
            not isinstance(interval, list)
            or len(interval) != 2
            or any(type(x) is not int or x < 0 for x in interval)
        ):
            raise NfsObservationError("invalid request timeline")
        if (
            not before["capture_interval_ns"][1]
            <= interval[0]
            < interval[1]
            <= after["capture_interval_ns"][0]
        ):
            raise NfsObservationError("request is outside captured bounds")
        if previous_end is not None and previous_end > before["capture_interval_ns"][0]:
            raise NfsObservationError("phase intervals overlap or reorder")
        if previous_counters is not None:
            gaps[name] = counter_delta(previous_counters, before["counters"])
        previous_end = after["capture_interval_ns"][1]
        previous_counters = after["counters"]
        observations[name] = counter_delta(before["counters"], after["counters"])
        if phase["error"] is not None:
            if not isinstance(phase["error"], str) or not phase["error"]:
                raise NfsObservationError("malformed callback error")
            errors.append(name)
        if name != "request" and phase["response"] is not None:
            raise NfsObservationError("idle phase cannot contain serving output")
    response = phases["request"]["response"]
    actual = None if response is None else read_artifact(root, response)
    baseline = (
        None
        if manifest["baseline"] is None
        else read_artifact(root, manifest["baseline"])
    )
    correct = (
        None
        if actual is None or baseline is None or "request" in errors
        else actual == baseline
    )
    reasons = [
        "serving_client_binding_missing",
        "competing_io_review_missing",
        "baseline_provenance_not_independently_qualified",
    ]
    if "nosharecache" not in initial["counters"].options:
        reasons.append("counter_sharing_unqualified")
    if any(any(delta.values()) for delta in gaps.values()):
        reasons.append("interphase_activity")
    if any(
        any(observations[name].values()) for name in ("quiet_before", "quiet_after")
    ):
        reasons.append("quiet_phase_activity")
    intervals = [*observations.values(), *gaps.values()]
    if any(
        delta["read_rpc_transmissions"] > delta["read_rpc_operations"]
        for delta in intervals
    ):
        reasons.append("rpc_retransmission")
    if any(
        delta["read_rpc_timeouts"] or delta["read_rpc_errors"] for delta in intervals
    ):
        reasons.append("rpc_timeout_or_error")
    request_delta = observations["request"]
    conflicting_counter = (
        "normal_read_bytes" if scope["io_mode"] == "direct" else "direct_read_bytes"
    )
    if request_delta[conflicting_counter]:
        reasons.append("io_mode_mismatch")
    if errors:
        reasons.append("callback_error")
    if correct is None:
        reasons.append("correctness_unavailable")
    return {
        "status": "observation_only",
        "serving_verification": "not_assessed",
        "performance_claims": "suppressed",
        "request_id": request["id"],
        "configuration_fingerprint": fingerprint,
        "target_scope": scope,
        "expected_kv_bytes": expected,
        "correct_output_comparison": correct,
        "phase_deltas": observations,
        "between_phase_deltas": gaps,
        "callback_errors": errors,
        "review_reasons": reasons,
    }
