# SPDX-License-Identifier: Apache-2.0
"""Replay retained tiny reference-client receipts; no automatic serving verdict."""

# Standard
from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Any

# Local
from .nfs_bundle import read_artifact
from .nfs_client_process import replay_client_process
from .nfs_observation import NfsObservationError
from .nfs_path_binding import replay_path_binding
from .nfs_serving import replay_serving_candidate


def _decode(data: bytes) -> dict[str, Any]:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result = {}
        for key, value in items:
            if key in result:
                raise NfsObservationError("duplicate client JSON key")
            result[key] = value
        return result

    def constant(value: str) -> None:
        raise NfsObservationError(f"non-finite client JSON: {value}")

    try:
        value = json.loads(data, object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, UnicodeError) as exc:
        raise NfsObservationError(str(exc)) from exc
    if not isinstance(value, dict):
        raise NfsObservationError("client JSON must be an object")
    return value


def _originals(
    root: Path, manifest_name: str = "client-manifest.json"
) -> dict[str, bytes]:
    manifest = root / manifest_name
    if not manifest.resolve(strict=True).is_relative_to(root):
        raise NfsObservationError("client manifest escapes bundle")
    value = _decode(manifest.read_bytes())
    if set(value) != {"nfs_client_version", "kind", "artifacts"} or not isinstance(
        value["artifacts"], list
    ):
        raise NfsObservationError("unsupported reference client contract")
    if manifest_name == "workload-manifest.json":
        if value["nfs_client_version"] != "0.2.0-experimental":
            raise NfsObservationError("unsupported workload checkpoint version")
        if value["kind"] != "tiny_reference_workload":
            raise NfsObservationError("unsupported workload checkpoint kind")
    else:
        if value["nfs_client_version"] != "0.1.0-experimental":
            raise NfsObservationError("unsupported complete client version")
        if value["kind"] != "tiny_reference_client":
            raise NfsObservationError("unsupported complete client kind")
    files = {}
    for ref in value["artifacts"]:
        if not isinstance(ref, dict) or set(ref) != {"path", "sha256"}:
            raise NfsObservationError("malformed client artifact")
        path = ref["path"]
        if (
            not isinstance(path, str)
            or path in files
            or Path(path).as_posix() != path
            or any(part in {".", ".."} for part in path.split("/"))
        ):
            raise NfsObservationError("ambiguous client artifact path")
        files[path] = read_artifact(root, ref)
    return files


def _required(files: dict[str, bytes], name: str) -> bytes:
    if name not in files:
        raise NfsObservationError(f"missing client original: {name}")
    return files[name]


def _tokens(value: Any) -> list[int]:
    if (
        not isinstance(value, list)
        or not value
        or any(type(token) is not int or token < 0 for token in value)
    ):
        raise NfsObservationError("token IDs must be nonempty nonnegative integers")
    return value


def _process(data: bytes) -> tuple[int, int]:
    try:
        opening, closing = data.index(b"("), data.rindex(b")")
        return int(data[:opening]), int(data[closing + 1 :].split()[19])
    except (ValueError, IndexError) as exc:
        raise NfsObservationError("invalid client proc stat") from exc


def _call(files: dict[str, bytes]) -> dict[str, Any]:
    frozen = _decode(_required(files, "frozen.json"))
    if frozen.get("reference_client_version") != "0.1.0-experimental":
        raise NfsObservationError("unsupported frozen client version")
    mode = frozen.get("mode")
    if mode not in {"baseline", "local", "nfs"}:
        raise NfsObservationError("unsupported reference execution mode")
    if (
        frozen.get("clock") != "CLOCK_MONOTONIC"
        or type(frozen.get("pid")) is not int
        or frozen["pid"] <= 0
        or not isinstance(frozen.get("boot_id"), str)
        or not re.fullmatch(r"[0-9a-f-]{36}", frozen["boot_id"])
        or not isinstance(frozen.get("time_namespace"), str)
        or not re.fullmatch(r"time:\[[0-9]+\]", frozen["time_namespace"])
    ):
        raise NfsObservationError("invalid execution clock/process identity")
    before = _process(_required(files, "before.stat.bin"))
    after = _process(_required(files, "after-request.stat.bin"))
    if before != after or before[0] != frozen["pid"]:
        raise NfsObservationError("client process changed across execution")
    for key, name in [
        ("source_sha256", "reference_client.py.source"),
        ("copy_source_sha256", "reference_copy.py.source"),
        ("input_sha256", "input.bin"),
    ]:
        if sha256(_required(files, name)).hexdigest() != frozen.get(key):
            raise NfsObservationError(f"frozen {key} mismatch")
    if "reference_cleanup_override" in frozen or "cleanup_source_sha256" in frozen:
        enabled = frozen.get("reference_cleanup_override")
        if type(enabled) is not bool or (mode == "baseline" and enabled):
            raise NfsObservationError("invalid reference cleanup mode")
        digest = frozen.get("cleanup_source_sha256")
        if enabled or digest is not None:
            if (
                sha256(
                    _required(files, "offload_cleanup_reference.py.source")
                ).hexdigest()
                != digest
            ):
                raise NfsObservationError("reference cleanup source mismatch")
    model = frozen.get("model_files")
    config_bytes = _required(files, "model-config.json")
    if (
        not isinstance(model, dict)
        or not isinstance(model.get("config.json"), dict)
        or model["config.json"]
        != {"bytes": len(config_bytes), "sha256": sha256(config_bytes).hexdigest()}
        or not any(name.endswith(".safetensors") for name in model)
    ):
        raise NfsObservationError("model file inventory/config mismatch")
    for entry in model.values():
        if (
            not isinstance(entry, dict)
            or set(entry) != {"bytes", "sha256"}
            or type(entry["bytes"]) is not int
            or entry["bytes"] <= 0
            or not isinstance(entry["sha256"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"])
        ):
            raise NfsObservationError("invalid model file digest")
    config = _decode(config_bytes)
    if any(
        type(config.get(name)) is not int or config.get(name) != expected
        for name, expected in [
            ("num_hidden_layers", 1),
            ("num_key_value_heads", 1),
            ("head_dim", 64),
        ]
    ):
        raise NfsObservationError("unsupported tiny model geometry")
    settings = frozen.get("requested_engine")
    if (
        not isinstance(settings, dict)
        or settings.get("enable_prefix_caching") is not False
        or settings.get("tensor_parallel_size") != 1
        or settings.get("dtype") != "bfloat16"
        or settings.get("kv_cache_dtype") != "auto"
        or settings.get("block_size") != 16
        or (mode == "baseline" and "kv_transfer_config" in settings)
        or frozen.get("reference_copy_override") is not (mode != "baseline")
    ):
        raise NfsObservationError("unsupported reference runtime/cache configuration")
    _required(files, "actual-vllm-config.txt")
    runtime_receipt = files.get("actual-runtime.json")
    if runtime_receipt is not None:
        actual_runtime = _decode(runtime_receipt)
        if (
            actual_runtime.get("prefix_cache_enabled") is not False
            or actual_runtime.get("block_size") != 16
            or actual_runtime.get("tensor_parallel_size") != 1
            or actual_runtime.get("kv_cache_dtype") != "auto"
            or actual_runtime.get("kv_connector")
            != (None if mode == "baseline" else "LMCacheConnectorV1Dynamic")
        ):
            raise NfsObservationError("actual runtime/cache contradicts reference mode")
        cache = actual_runtime.get("lmcache")
        if mode == "baseline":
            if cache is not None:
                raise NfsObservationError("recompute baseline has an LMCache engine")
        elif (
            not isinstance(cache, dict)
            or cache.get("chunk_size") != 32
            or cache.get("local_cpu") is not False
            or cache.get("use_layerwise") is not False
            or cache.get("save_unfull_chunk") is not False
            or cache.get("enable_scheduler_bypass_lookup") is not False
            or cache.get("store_location") != "LocalDiskBackend"
            or cache.get("retrieve_locations") != ["LocalDiskBackend"]
            or cache.get("use_odirect") is not True
            or not isinstance(cache.get("local_disk"), str)
        ):
            raise NfsObservationError("actual LMCache configuration contradicts mode")
    stack = _decode(_required(files, "stack.json"))
    runtime = frozen.get("runtime")
    if not isinstance(runtime, dict) or set(runtime) != {"vllm", "lmcache", "torch"}:
        raise NfsObservationError("incomplete package versions")
    for name in runtime:
        if not isinstance(runtime[name], str) or not runtime[name]:
            raise NfsObservationError("empty package version")
        if stack.get(name) != {
            "version": runtime[name],
            "sha256": sha256(_required(files, f"{name}-init.source")).hexdigest(),
        }:
            raise NfsObservationError("stack source/version mismatch")
    request = _decode(_required(files, "input.bin"))
    ids = _tokens(request.get("prompt_token_ids"))
    if set(request) != {"prompt_token_ids"} or ids != list(range(10, 42)) + [52]:
        raise NfsObservationError("unsupported reference input")
    response = _decode(_required(files, "retrieve.json"))
    interval = response.get("interval_ns")
    if (
        response.get("input") != request
        or response.get("input_sha256") != frozen["input_sha256"]
        or not isinstance(interval, list)
        or len(interval) != 2
        or any(type(x) is not int or x < 0 for x in interval)
        or interval[0] >= interval[1]
    ):
        raise NfsObservationError("response contradicts submitted input or clock")
    tokens = _tokens(response.get("output_token_ids"))
    if _tokens(json.loads(_required(files, "output.bin"))) != tokens:
        raise NfsObservationError("comparable output contradicts actual response")
    device = _decode(_required(files, "device.json"))
    if device.get("exit_code") != 0 or not device.get("stdout"):
        raise NfsObservationError("device inventory unavailable")
    return {"frozen": frozen, "response": response, "process_identity": before}


def _replay_capture(
    bundle_dir: Path,
    *,
    manifest_name: str = "client-manifest.json",
) -> dict[str, Any]:
    """Cross-check a retained tiny reference-client execution without live access.

    Args:
        bundle_dir: Reference producer output, including hashes, actual input and
            output, model digests, process metadata, runtime/source receipts and
            request originals. Cache modes include a separately run baseline;
            stack shutdown is not a measurement prerequisite.

    Returns:
        Raw consistency checks and optional NFS/path observations. Even matching
        receipts are observation_only: a source audit, independently qualified
        runtime/cache provenance and competing-I/O review are
        still required. Digests are integrity checks, not authenticity attestations;
        model weights and full installed binaries are not embedded or re-executed.

    Raises:
        NfsObservationError: Missing/tampered/escaping originals, unsupported
            reference scope, mismatched baseline or inconsistent execution binding.
        ValueError: Invalid nested path/configuration/capture contracts.
        OSError: Inaccessible originals. No replay-host process is consulted.
    """
    root = Path(bundle_dir).resolve(strict=True)
    files = _originals(root, manifest_name)
    actual = _call(files)
    frozen = actual["frozen"]
    mode = frozen["mode"]
    baseline_match = None
    kv_match = None
    nfs = None
    bindings = None
    if mode != "baseline":
        baseline_files = _measurement_originals(root / "baseline")
        # The outer receipt must bind the exact nested receipt and originals.
        for name, data in baseline_files.items():
            if files.get("baseline/" + name) != data:
                raise NfsObservationError(
                    "baseline originals differ from outer receipt"
                )
        baseline = _call(baseline_files)
        if baseline["frozen"]["mode"] != "baseline":
            raise NfsObservationError("cache run cannot act as recompute baseline")
        if actual["process_identity"] == baseline["process_identity"]:
            raise NfsObservationError("baseline must use a separate engine process")
        for key in ("model_files", "runtime", "sampling", "input_sha256"):
            if frozen.get(key) != baseline["frozen"].get(key):
                raise NfsObservationError(f"baseline {key} mismatch")
        for name in ("stack.json", "reference_client.py.source", "input.bin"):
            if files[name] != baseline_files[name]:
                raise NfsObservationError(f"baseline original mismatch: {name}")
        if baseline["response"].get("cached_tokens") != 0:
            raise NfsObservationError("recompute baseline reports cached tokens")
        baseline_match = files["output.bin"] == baseline_files["output.bin"]
        proofs = [
            _required(files, "proof/" + name + "-0-32-call1.bin")
            for name in ("store_gpu", "store_cpu", "load_cpu", "load_gpu")
        ]
        if any(len(data) != 8192 for data in proofs):
            raise NfsObservationError("reference KV originals must each be 8192 bytes")
        kv_match = all(data == proofs[0] for data in proofs)
    if mode == "nfs":
        nfs = replay_serving_candidate(root / "serving")
        manifest = _decode(files["serving/serving-manifest.json"])
        configuration = _decode(files["serving/configuration.json"])
        geometry = configuration["kv_geometry"]
        if geometry != {
            "chunk_tokens": 32,
            "bytes_per_chunk": 8192,
            "dtype": "bfloat16",
        }:
            raise NfsObservationError("NFS geometry contradicts actual tiny model")
        if configuration["model"]["root"] != frozen["requested_engine"].get("model"):
            raise NfsObservationError("NFS configuration names a different model")
        mapping = _decode(files["serving/scope-mapping.json"])
        if mapping["clock_domain"] != {
            "boot_id": frozen["boot_id"],
            "time_namespace": frozen["time_namespace"],
        }:
            raise NfsObservationError("NFS observation and client clock differ")
        request = manifest["phases"]["request"]["interval_ns"]
        interval = actual["response"]["interval_ns"]
        if not request[0] <= interval[0] < interval[1] <= request[1]:
            raise NfsObservationError("actual generation is outside callback interval")
        for name, data in [
            ("input.bin", files["input.bin"]),
            ("baseline.bin", files["baseline/output.bin"]),
            ("request.response.bin", files["output.bin"]),
        ]:
            if files.get("serving/" + name) != data:
                raise NfsObservationError("serving capture contradicts client bytes")
        bindings = {}
        for name in ("binding-before", "binding-after"):
            record = _decode(files[name + "/path-binding.json"])
            if (
                record["serving_pid"] != frozen["pid"]
                or record["configuration_fingerprint"]
                != nfs["configuration_fingerprint"]
                or any(
                    record["epochs"][epoch]["boot_id"] != frozen["boot_id"]
                    for epoch in ("before", "after")
                )
            ):
                raise NfsObservationError(
                    "path receipt names a different process/config"
                )
            for epoch in ("before", "after"):
                stat_ref = record["epochs"][epoch]["roles"]["serving"]["stat"]
                if (
                    stat_ref is None
                    or _process(_required(files, name + "/" + stat_ref["path"]))
                    != actual["process_identity"]
                ):
                    raise NfsObservationError("path receipt process lifetime differs")
            bounds = record["capture_interval_ns"]
            if (name == "binding-before" and bounds[1] > request[0]) or (
                name == "binding-after" and bounds[0] < request[1]
            ):
                raise NfsObservationError("path receipts do not bracket request")
            bindings[name] = replay_path_binding(root / name)
    return {
        "status": "observation_only",
        "serving_verification": "not_assessed",
        "performance_claims": "suppressed",
        "mode": mode,
        "actual_input_binding": "consistent",
        "actual_runtime_receipt": "consistent"
        if "actual-runtime.json" in files
        else "unavailable",
        "baseline_output_equal": baseline_match,
        "kv_originals_equal": kv_match,
        "nfs_observations": nfs,
        "path_bindings": bindings,
        "review_reasons": [
            "reference_source_audit_required",
            "runtime_cache_provenance_review_required",
            "competing_io_review_required",
        ],
    }


def _measurement_originals(root: Path) -> dict[str, bytes]:
    checkpoint = root / "workload-manifest.json"
    if checkpoint.exists() or checkpoint.is_symlink():
        return _originals(root, "workload-manifest.json")
    return _originals(root)


def _stack_health(
    files: dict[str, bytes], process: dict[str, Any] | None = None
) -> dict[str, Any]:
    warnings = []
    shutdown = None
    cleanup = None
    if "shutdown.json" in files:
        try:
            value = _decode(files["shutdown.json"])
            if (
                set(value) != {"public_engine_shutdown_returned"}
                or type(value["public_engine_shutdown_returned"]) is not bool
            ):
                raise ValueError("invalid shutdown receipt")
            shutdown = value["public_engine_shutdown_returned"]
            if not shutdown:
                warnings.append("stack_shutdown_error")
        except ValueError:
            warnings.append("stack_shutdown_metadata_unreadable")
    if "reference-cleanup-state.json" in files:
        try:
            value = _decode(files["reference-cleanup-state.json"])
            states = value.get("offload_control_services")
            if (
                not isinstance(states, list)
                or not states
                or any(
                    not isinstance(state, dict)
                    or set(state) != {"thread_alive", "socket_closed"}
                    or any(type(v) is not bool for v in state.values())
                    for state in states
                )
            ):
                raise ValueError("invalid optional cleanup metadata")
            cleanup = all(
                not state["thread_alive"] and state["socket_closed"] for state in states
            )
            if not cleanup:
                warnings.append("experimental_cleanup_incomplete")
        except ValueError:
            warnings.append("experimental_cleanup_metadata_unreadable")
    state = None if process is None else process["process_status"]
    if state in {"failed", "timed_out", "launch_failed"}:
        warnings.append("stack_process_" + state)
    return {
        "status": "error"
        if warnings
        else "normal"
        if state == "completed"
        else "reported"
        if shutdown is not None or cleanup is not None
        else "unknown",
        "process_status": state,
        "process_returncode": None
        if process is None
        else process["process_returncode"],
        "public_shutdown_returned": shutdown,
        "experimental_cleanup_complete": cleanup,
        "warnings": warnings,
    }


def replay_client_execution(bundle_dir: Path) -> dict[str, Any]:
    """Read measurement originals separately from serving-stack lifecycle health.

    Args:
        bundle_dir: A legacy client directory, a pre-teardown workload checkpoint,
            or a parent-supervised attempt. A workload checkpoint can be read while
            the serving stack stays alive; request artifacts must be quiescent.

    Returns:
        Versioned raw observations with independent measurement_evidence and
        stack_health sections. A complete, correctly bound workload remains
        available after a post-measurement abort/timeout or missing shutdown.
        Early failure/missing workload remains unavailable; malformed or tampered
        measurement originals are refused. Availability is not a Gate 1 PASS:
        correctness, tier/counter scope, bytes and isolation still need assessment.
        Actual wait and optional cleanup facts never become measurement criteria.

    Raises:
        NfsObservationError: Unsupported, inconsistent, missing or tampered evidence.
        ValueError: Invalid nested raw/configuration contracts.
        OSError: Inaccessible originals. No process is launched by this reader.
    """
    root = Path(bundle_dir).resolve(strict=True)
    process_path = root / "process-manifest.json"
    if not (process_path.exists() or process_path.is_symlink()):
        checkpoint = root / "workload-manifest.json"
        name = (
            "workload-manifest.json"
            if checkpoint.exists() or checkpoint.is_symlink()
            else "client-manifest.json"
        )
        result = _replay_capture(root, manifest_name=name)
        files = _originals(root, name)
        result.update(
            {
                "nfs_client_observation_version": "0.1.0-experimental",
                "measurement_evidence": {"status": "available"},
                "stack_health": _stack_health(files),
            }
        )
        return result
    process = replay_client_process(root)
    sealed = process.pop("artifacts")
    checkpoint = sealed.get("client/workload-manifest.json")
    if checkpoint is None:
        result = {
            "status": "observation_only",
            "serving_verification": "not_assessed",
            "performance_claims": "suppressed",
            "actual_input_binding": "unavailable",
            "baseline_output_equal": None,
            "kv_originals_equal": None,
            "review_reasons": ["workload_checkpoint_unavailable"],
        }
    else:
        if checkpoint != (root / "client/workload-manifest.json").read_bytes():
            raise NfsObservationError("workload checkpoint differs from parent seal")
        originals = _originals(root / "client", "workload-manifest.json")
        if any(
            sealed.get("client/" + name) != data for name, data in originals.items()
        ):
            raise NfsObservationError("workload original differs from parent seal")
        frozen = _decode(originals["frozen.json"])
        response = _decode(originals["retrieve.json"])
        if frozen.get("pid") != process["child_pid"]:
            raise NfsObservationError("workload PID differs from actual child PID")
        if process["clock_domain"] is not None and process["clock_domain"] != {
            "boot_id": frozen.get("boot_id"),
            "time_namespace": frozen.get("time_namespace"),
        }:
            raise NfsObservationError("workload clock domain differs from supervisor")
        bounds, generation = process["interval_ns"], response.get("interval_ns")
        if (
            not isinstance(generation, list)
            or len(generation) != 2
            or any(type(x) is not int for x in generation)
            or not bounds[0] <= generation[0] < generation[1] <= bounds[1]
        ):
            raise NfsObservationError(
                "workload generation is outside supervised interval"
            )
        result = _replay_capture(
            root / "client", manifest_name="workload-manifest.json"
        )
    client_files = {
        name.removeprefix("client/"): data
        for name, data in sealed.items()
        if name.startswith("client/")
    }
    health = _stack_health(client_files, process)
    complete = (
        process["process_status"] == "completed"
        and process["artifact_seal"] == "sealed"
        and checkpoint is not None
        and health["public_shutdown_returned"] is True
        and health["experimental_cleanup_complete"] is not False
        and "client-manifest.json" in client_files
    )
    result.update(
        {
            "nfs_client_observation_version": "0.1.0-experimental",
            "measurement_evidence": {
                "status": "available" if checkpoint is not None else "unavailable"
            },
            "stack_health": health,
            "execution": {
                **process,
                "public_shutdown_returned": health["public_shutdown_returned"],
                "workload_checkpoint_present": checkpoint is not None,
                "client_completion": "recorded_unqualified"
                if complete
                else "incomplete",
            },
        }
    )
    return result
