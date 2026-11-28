# SPDX-License-Identifier: Apache-2.0
"""Experimental parent-owned execution receipts, including aborted reference clients."""

# Standard
from collections.abc import Mapping, Sequence
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import signal
import stat
import subprocess
import time
from typing import Any

# Local
from .nfs_bundle import read_artifact
from .nfs_observation import NfsObservationError


def _json(root: Path, name: str, value: Any) -> Path:
    path = root / name
    with path.open("x") as stream:
        json.dump(value, stream, indent=2, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    return path


def _inventory(root: Path) -> list[dict[str, str]]:
    artifacts = []
    for path in sorted(root.rglob("*")):
        mode = path.lstat().st_mode
        if stat.S_ISDIR(mode):
            if path.stat().st_dev != root.stat().st_dev:
                raise NfsObservationError("artifact tree crosses a filesystem")
            continue
        if not stat.S_ISREG(mode):
            raise NfsObservationError("artifact tree contains a link or special file")
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as stream:
            before = os.fstat(stream.fileno())
            data = stream.read()
            after = os.fstat(stream.fileno())
        if (before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise NfsObservationError("artifact changed during sealing")
        artifacts.append(
            {"path": str(path.relative_to(root)), "sha256": sha256(data).hexdigest()}
        )
    return artifacts


def _seconds(value: float, label: str, maximum: float) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not 0 < value <= maximum
    ):
        raise NfsObservationError(f"{label} must be positive and <= {maximum}")


def _group_present(pid: int) -> bool:
    try:
        os.killpg(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _clock_domain() -> dict[str, str] | None:
    try:
        return {
            "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
            "time_namespace": os.readlink("/proc/self/ns/time"),
        }
    except OSError:
        return None


def _decode(data: bytes) -> dict[str, Any]:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result = {}
        for key, value in items:
            if key in result:
                raise NfsObservationError("duplicate process receipt JSON key")
            result[key] = value
        return result

    def constant(value: str) -> None:
        raise NfsObservationError(f"non-finite process JSON: {value}")

    try:
        value = json.loads(data, object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, UnicodeError) as exc:
        raise NfsObservationError(str(exc)) from exc
    if not isinstance(value, dict):
        raise NfsObservationError("process receipt must be an object")
    return value


def checkpoint_client_workload(bundle_dir: Path) -> Path:
    """Seal the reference client's current originals before entering teardown.

    Args:
        bundle_dir: Existing client artifact directory, outside the measured tier.
            The trusted reference producer must have finished all workload writers.

    Returns:
        An exclusive workload-manifest.json path. It is not a cleanup receipt or
        a completed client manifest; the parent must retain actual process outcomes.

    Raises:
        NfsObservationError: A file changes, a link/special file is found, or the
            artifact tree crosses a filesystem.
        OSError: Missing directory or an existing checkpoint/output failure.
    """
    root = Path(bundle_dir).resolve(strict=True)
    if not root.is_dir() or (root / "workload-manifest.json").exists():
        raise NfsObservationError("workload checkpoint needs an unused manifest path")
    return _json(
        root,
        "workload-manifest.json",
        {
            "nfs_client_version": "0.2.0-experimental",
            "kind": "tiny_reference_workload",
            "artifacts": _inventory(root),
        },
    )


def capture_client_process(
    bundle_dir: Path,
    command: Sequence[str],
    *,
    timeout_seconds: float = 180,
    grace_seconds: float = 5,
    environment: Mapping[str, str] | None = None,
) -> Path:
    """Supervise one trusted POSIX reference command and seal its real outcome.

    Args:
        bundle_dir: Existing empty output directory. The child writes to client/;
            parent launch/source/log/exit receipts live outside that directory.
        command: Explicit argv with an absolute executable and exactly one literal
            {client_dir} argument, replaced by the new client directory. No shell.
        timeout_seconds: Positive bounded main-process timeout, at most 600 s.
        grace_seconds: Positive SIGTERM grace before SIGKILL, at most 10 s.
        environment: Optional environment overrides, merged into the parent's.
            Only key names are recorded; callers retain relevant nonsecret runtime
            values in their own source/config receipts.

    Returns:
        Parent-written process-manifest.json, even for launch error, nonzero exit,
        signal or timeout. Process outcome alone neither grants nor vetoes a
        measurement verdict. A residual process group or unsafe/mutable artifact
        tree prevents an artifact seal.
        This API requires a trusted single-process/in-process reference client;
        detached workers or hostile writers are outside its capture contract.

    Raises:
        NfsObservationError: Unsupported platform, invalid argv/limits/environment
            or nonempty output directory.
        OSError: Parent output cannot be written. Existing artifacts are retained.
    """
    if os.name != "posix":
        raise NfsObservationError("reference process supervision requires POSIX")
    _seconds(timeout_seconds, "timeout", 600)
    _seconds(grace_seconds, "termination grace", 10)
    if (
        isinstance(command, (str, bytes))
        or not command
        or any(not isinstance(x, str) or not x or "\0" in x for x in command)
        or list(command).count("{client_dir}") != 1
        or not Path(command[0]).is_absolute()
    ):
        raise NfsObservationError("explicit argv and one {client_dir} are required")
    overrides = dict(environment or {})
    if any(
        not isinstance(k, str)
        or not isinstance(v, str)
        or not k
        or "=" in k
        or "\0" in k + v
        for k, v in overrides.items()
    ):
        raise NfsObservationError("invalid environment overrides")
    root = Path(bundle_dir).resolve(strict=True)
    if not root.is_dir() or any(root.iterdir()):
        raise NfsObservationError("supervisor needs an empty output directory")
    client = root / "client"
    client.mkdir()
    argv = [str(client) if x == "{client_dir}" else x for x in command]
    (root / "supervisor-source.py").write_bytes(Path(__file__).read_bytes())
    _json(
        root,
        "launch.json",
        {
            "argv": argv,
            "environment_override_keys": sorted(overrides),
            "parent_pid": os.getpid(),
            "clock": "CLOCK_MONOTONIC",
            "timeout_seconds": timeout_seconds,
            "grace_seconds": grace_seconds,
        },
    )
    clock_domain = _clock_domain()
    started = time.monotonic_ns()
    pid = None
    code = None
    launch_error = None
    timed_out = False
    termination_signals = []
    group_present = False
    with (
        (root / "stdout.bin").open("xb") as stdout,
        (root / "stderr.bin").open("xb") as stderr,
    ):
        try:
            process = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                env={**os.environ, **overrides},
                start_new_session=True,
            )
        except OSError as exc:
            launch_error = {"type": type(exc).__name__, "errno": exc.errno}
        else:
            pid = process.pid
            try:
                code = process.wait(timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                timed_out = True
                try:
                    os.killpg(pid, signal.SIGTERM)
                    termination_signals.append("SIGTERM")
                except ProcessLookupError:
                    pass
                try:
                    code = process.wait(timeout=grace_seconds)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(pid, signal.SIGKILL)
                        termination_signals.append("SIGKILL")
                    except ProcessLookupError:
                        pass
                    code = process.wait()
            group_present = _group_present(pid)
    ended = time.monotonic_ns()
    artifacts = []
    seal_error = None
    if group_present:
        seal_error = "child_process_group_still_present"
    else:
        try:
            artifacts = _inventory(root)
        except (OSError, NfsObservationError) as exc:
            seal_error = type(exc).__name__ + ": " + str(exc)
    return _json(
        root,
        "process-manifest.json",
        {
            "nfs_client_process_version": "0.1.0-experimental",
            "clock": "CLOCK_MONOTONIC",
            "clock_domain": clock_domain,
            "clock_domain_after": _clock_domain(),
            "interval_ns": [started, ended],
            "child_pid": pid,
            "process_returncode": code,
            "launch_error": launch_error,
            "timed_out": timed_out,
            "termination_signals": termination_signals,
            "process_group_present_after_wait": group_present,
            "seal_error": seal_error,
            "artifacts": artifacts,
        },
    )


def replay_client_process(bundle_dir: Path) -> dict[str, Any]:
    """Verify parent-sealed originals and derive process state without live access.

    Args:
        bundle_dir: A supervisor output directory containing process-manifest.json.

    Returns:
        Real wait status, derived shell exit code, timeout, artifact seal state and
        verified artifact bytes. All observations withhold serving verification.
        bytes in artifacts are for the higher-level client reader, not JSON output.

    Raises:
        NfsObservationError: Malformed, unsupported, inconsistent or tampered receipt.
        OSError: Missing originals. No child is launched by this reader.
    """
    root = Path(bundle_dir).resolve(strict=True)
    path = root / "process-manifest.json"
    if not path.resolve(strict=True).is_relative_to(root):
        raise NfsObservationError("process manifest escapes bundle")
    record = _decode(path.read_bytes())
    if (
        set(record)
        != {
            "nfs_client_process_version",
            "clock",
            "clock_domain",
            "clock_domain_after",
            "interval_ns",
            "child_pid",
            "process_returncode",
            "launch_error",
            "timed_out",
            "termination_signals",
            "process_group_present_after_wait",
            "seal_error",
            "artifacts",
        }
        or record["nfs_client_process_version"] != "0.1.0-experimental"
    ):
        raise NfsObservationError("unsupported process receipt contract")
    bounds = record["interval_ns"]
    if (
        record["clock"] != "CLOCK_MONOTONIC"
        or not isinstance(bounds, list)
        or len(bounds) != 2
        or any(type(x) is not int or x < 0 for x in bounds)
        or bounds[0] >= bounds[1]
    ):
        raise NfsObservationError("invalid supervisor clock interval")
    for key in ("clock_domain", "clock_domain_after"):
        domain = record[key]
        if domain is not None and (
            not isinstance(domain, dict)
            or set(domain) != {"boot_id", "time_namespace"}
            or any(not isinstance(v, str) or not v for v in domain.values())
        ):
            raise NfsObservationError("invalid supervisor clock domain")
    if record["clock_domain"] != record["clock_domain_after"]:
        raise NfsObservationError("supervisor clock domain changed")
    code, pid, launch_error = (
        record[k] for k in ["process_returncode", "child_pid", "launch_error"]
    )
    if launch_error is None:
        if (
            type(code) is not int
            or not -127 <= code <= 255
            or type(pid) is not int
            or pid <= 0
        ):
            raise NfsObservationError("invalid actual process wait outcome")
    elif (
        code is not None
        or pid is not None
        or not isinstance(launch_error, dict)
        or set(launch_error) != {"type", "errno"}
        or not isinstance(launch_error["type"], str)
    ):
        raise NfsObservationError("invalid launch failure receipt")
    if (
        type(record["timed_out"]) is not bool
        or type(record["process_group_present_after_wait"]) is not bool
        or not isinstance(record["termination_signals"], list)
        or any(
            not isinstance(s, str) or s not in {"SIGTERM", "SIGKILL"}
            for s in record["termination_signals"]
        )
        or (record["termination_signals"] and not record["timed_out"])
        or (record["timed_out"] and launch_error is not None)
    ):
        raise NfsObservationError("inconsistent process termination receipt")
    seal_error = record["seal_error"]
    refs = record["artifacts"]
    if not isinstance(refs, list) or (
        seal_error is not None
        and (not isinstance(seal_error, str) or not seal_error or refs)
    ):
        raise NfsObservationError("invalid artifact seal status")
    if record["process_group_present_after_wait"] and seal_error is None:
        raise NfsObservationError("live process group cannot have an artifact seal")
    files = {}
    for ref in refs:
        if (
            not isinstance(ref, dict)
            or set(ref) != {"path", "sha256"}
            or not isinstance(ref.get("path"), str)
            or ref["path"] in files
        ):
            raise NfsObservationError("invalid/duplicate process artifact")
        files[ref["path"]] = read_artifact(root, ref)
    if seal_error is None:
        if (
            not {"launch.json", "stdout.bin", "stderr.bin", "supervisor-source.py"}
            <= files.keys()
        ):
            raise NfsObservationError("missing supervisor originals")
        launch = _decode(files["launch.json"])
        if launch.get("clock") != "CLOCK_MONOTONIC" or not isinstance(
            launch.get("argv"), list
        ):
            raise NfsObservationError("invalid retained launch original")
    status = (
        "launch_failed"
        if launch_error is not None
        else "timed_out"
        if record["timed_out"]
        else "completed"
        if code == 0
        else "failed"
    )
    return {
        "process_status": status,
        "child_pid": pid,
        "interval_ns": bounds,
        "clock_domain": record["clock_domain"],
        "process_returncode": code,
        "shell_exit_code": None if code is None else code if code >= 0 else 128 - code,
        "timed_out": record["timed_out"],
        "artifact_seal": "sealed" if seal_error is None else "incomplete",
        "seal_error": seal_error,
        "artifacts": files,
        "serving_verification": "not_assessed",
        "performance_claims": "suppressed",
    }
