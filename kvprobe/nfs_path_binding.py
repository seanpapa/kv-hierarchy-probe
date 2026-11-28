# SPDX-License-Identifier: Apache-2.0
"""Experimental process/path to NFS-counter-filesystem binding, not I/O proof."""

# Standard
from collections.abc import Callable
from contextlib import ExitStack
import ctypes
import errno
from hashlib import sha256
import json
import os
from pathlib import Path
import platform
import re
import select
import stat
import time
from typing import Any

# Local
from .configuration import fingerprint_configuration
from .nfs_bundle import read_artifact
from .nfs_observation import NfsObservationError, parse_mountstats


class PathBindingError(ValueError):
    """Raised for malformed or inconsistent binding artifacts/specifications."""


def _object(value: Any, keys: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise PathBindingError(f"malformed {label}")
    return value


def _integer(value: Any, name: str, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise PathBindingError(f"{name} must be an integer >= {minimum}")
    return value


def _absolute(value: Any) -> str:
    if not isinstance(value, str) or not value.startswith("/") or "\0" in value:
        raise PathBindingError("an absolute filesystem path is required")
    return value


def _decode(data: bytes) -> dict[str, Any]:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        value = {}
        for key, item in items:
            if key in value:
                raise PathBindingError("duplicate JSON key")
            value[key] = item
        return value

    def constant(text: str) -> None:
        raise PathBindingError(f"non-finite JSON: {text}")

    try:
        value = json.loads(data, object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, UnicodeError) as exc:
        raise PathBindingError(str(exc)) from exc
    if not isinstance(value, dict):
        raise PathBindingError("expected JSON object")
    return value


def _read(root: Path, ref: Any) -> bytes:
    try:
        return read_artifact(root, ref)
    except (NfsObservationError, OSError) as exc:
        raise PathBindingError(str(exc)) from exc


def _save(root: Path, name: str, data: bytes) -> dict[str, str]:
    with (root / name).open("xb") as output:
        output.write(data)
    return {"path": name, "sha256": sha256(data).hexdigest()}


def _unescape(value: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), value)


def _mounts(data: bytes) -> list[dict[str, str]]:
    rows = []
    ids = set()
    for line in data.decode("utf-8", "surrogateescape").splitlines():
        fields = line.split()
        try:
            split = fields.index("-")
            if split < 6 or len(fields) != split + 4:
                raise ValueError("mountinfo fields")
            if (
                not fields[0].isdigit()
                or not fields[1].isdigit()
                or not re.fullmatch(r"[0-9]+:[0-9]+", fields[2])
            ):
                raise ValueError("mountinfo identity")
            if fields[0] in ids:
                raise ValueError("duplicate mount ID")
            ids.add(fields[0])
            rows.append(
                {
                    "mount_id": fields[0],
                    "parent_id": fields[1],
                    "major_minor": fields[2],
                    "root": _absolute(_unescape(fields[3])),
                    "mountpoint": _absolute(_unescape(fields[4])),
                    "fstype": fields[split + 1],
                    "source": _unescape(fields[split + 2]),
                }
            )
        except (ValueError, IndexError) as exc:
            raise PathBindingError(f"invalid mountinfo: {exc}") from exc
    if not rows:
        raise PathBindingError("empty mountinfo")
    return rows


def _process(data: bytes) -> dict[str, Any]:
    opening, closing = data.find(b"("), data.rfind(b")")
    try:
        if opening < 1 or closing <= opening:
            raise ValueError("comm boundaries")
        pid = data[:opening].strip()
        tail = data[closing + 1 :].split()
        if (
            not pid.isdigit()
            or len(tail) < 20
            or not tail[19].isdigit()
            or len(tail[0]) != 1
        ):
            raise ValueError("stat fields")
        return {
            "pid": int(pid),
            "start_ticks": int(tail[19]),
            "state": tail[0].decode("ascii"),
        }
    except (ValueError, IndexError, UnicodeError) as exc:
        raise PathBindingError(f"invalid process stat: {exc}") from exc


def _metadata(value: Any) -> dict[str, Any]:
    value = _object(value, {"major_minor", "inode", "mode"}, "fstat receipt")
    if not isinstance(value["major_minor"], str) or not re.fullmatch(
        r"[0-9]+:[0-9]+", value["major_minor"]
    ):
        raise PathBindingError("invalid filesystem device")
    _integer(value["inode"], "inode")
    mode = _integer(value["mode"], "mode")
    if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
        raise PathBindingError("path must be a regular file or directory")
    return value


def _mnt_id(data: bytes) -> str:
    rows = [
        line.split()
        for line in data.decode().splitlines()
        if line.split()[:1] == ["mnt_id:"]
    ]
    if len(rows) != 1 or len(rows[0]) != 2 or not rows[0][1].isdigit():
        raise PathBindingError("fdinfo needs one mount ID")
    return rows[0][1]


def _stat_record(value: os.stat_result) -> dict[str, Any]:
    return {
        "major_minor": f"{os.major(value.st_dev)}:{os.minor(value.st_dev)}",
        "inode": value.st_ino,
        "mode": value.st_mode,
    }


def _open_rooted(root_fd: int, path: str) -> int:
    # Linux 5.6+, syscall 437 on the explicitly supported 64-bit ABIs.
    if ctypes.sizeof(ctypes.c_void_p) != 8 or platform.machine().lower() not in {
        "x86_64",
        "amd64",
        "aarch64",
        "arm64",
    }:
        raise OSError(errno.ENOSYS, "openat2 ABI is not supported")

    class OpenHow(ctypes.Structure):
        _fields_ = [
            ("flags", ctypes.c_uint64),
            ("mode", ctypes.c_uint64),
            ("resolve", ctypes.c_uint64),
        ]

    how = OpenHow(os.O_PATH | os.O_CLOEXEC, 0, 0x10 | 0x02)
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    result = libc.syscall(
        ctypes.c_long(437),
        ctypes.c_int(root_fd),
        ctypes.c_char_p(os.fsencode(path)),
        ctypes.byref(how),
        ctypes.c_size_t(ctypes.sizeof(how)),
    )
    if result < 0:
        number = ctypes.get_errno()
        raise OSError(number, os.strerror(number))
    return int(result)


def _probe(pid: int | None, path: str) -> tuple[dict[str, Any], bytes]:
    with ExitStack() as resources:
        if pid is None:
            fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
        else:
            root_fd = os.open(
                f"/proc/{pid}/root", os.O_PATH | os.O_DIRECTORY | os.O_CLOEXEC
            )
            resources.callback(os.close, root_fd)
            fd = _open_rooted(root_fd, path)
        resources.callback(os.close, fd)
        metadata = _stat_record(os.fstat(fd))
        _metadata(metadata)
        return metadata, Path(f"/proc/self/fdinfo/{fd}").read_bytes()


def capture_path_binding(
    bundle_dir: Path,
    serving_pid: int,
    configuration: dict[str, Any],
    arm_name: str,
    observer_mountpoint: str,
) -> Path:
    """Retain read-only process and kernel-resolved path metadata on Linux.

    Args:
        bundle_dir: Existing empty artifact directory outside visible NFS filesystems.
        serving_pid: Explicit observer-visible PID, selected by the operator.
        configuration: Draft 0.4.0 parameters containing a named NFS arm.
        arm_name: Arm whose actual tier_path is resolved in the selected PID's root.
        observer_mountpoint: Exact mountpoint whose NFS counter scope is observed.

    Returns:
        New binding manifest. Missing permission, kernel support or process data
        is retained as unavailable. No sudo, setns, payload read or serving call occurs.
        Path lookup/stat may cause filesystem metadata traffic; use outside I/O phases.

    Raises:
        PathBindingError: Invalid input or unsafe/nonempty output directory.
        ConfigurationError: Invalid versioned configuration.
        OSError: Output files cannot be retained. Existing files are never replaced.
    """
    _integer(serving_pid, "serving PID", 1)
    fingerprint = fingerprint_configuration(configuration, evidence_version="0.4.0")
    arms = [
        arm
        for arm in configuration["arms"]
        if arm["name"] == arm_name and arm["attribution_provider"] == "nfs_mountstats"
    ]
    if len(arms) != 1:
        raise PathBindingError("select one configured NFS arm")
    tier_path = _absolute(arms[0]["tier_path"])
    observer_mountpoint = _absolute(observer_mountpoint)
    root = Path(bundle_dir).resolve(strict=True)
    if not root.is_dir() or any(root.iterdir()):
        raise PathBindingError(
            "output needs an empty directory outside the observed mount"
        )
    # Directory aliases must not let output writes contaminate the NFS filesystem.
    if platform.system() == "Linux":
        rows = _mounts(Path("/proc/self/mountinfo").read_bytes())
        dev = _stat_record(root.stat())["major_minor"]
        if any(
            row["fstype"] in {"nfs", "nfs4"} and row["major_minor"] == dev
            for row in rows
        ):
            raise PathBindingError("output shares the observed NFS filesystem")
    config = _save(
        root,
        "configuration.json",
        (json.dumps(configuration, allow_nan=False, indent=2) + "\n").encode(),
    )
    errors: dict[str, Any] = {}

    def attempt(label: str, operation: Callable[[], Any]) -> Any:
        try:
            return operation()
        except OSError as exc:
            errors[label] = {"type": type(exc).__name__, "errno": exc.errno}
            return None

    def artifact(label: str, path: str) -> dict[str, str] | None:
        data = attempt(label, lambda: Path(path).read_bytes())
        return None if data is None else _save(root, label + ".bin", data)

    def epoch(name: str) -> dict[str, Any]:
        started = time.monotonic_ns()
        roles = {}
        for role, pid, path in [
            ("observer", None, observer_mountpoint),
            ("serving", serving_pid, tier_path),
        ]:
            proc = "/proc/self" if pid is None else f"/proc/{pid}"
            prefix = name + "." + role
            value = {
                "namespace": attempt(
                    prefix + ".namespace",
                    lambda proc=proc: os.readlink(proc + "/ns/mnt"),
                ),
                "root": attempt(
                    prefix + ".root",
                    lambda proc=proc: _stat_record(os.stat(proc + "/root")),
                ),
                "stat": artifact(prefix + ".stat", proc + "/stat"),
                "cmdline": artifact(prefix + ".cmdline", proc + "/cmdline"),
                "mountinfo": artifact(prefix + ".mountinfo", proc + "/mountinfo"),
                "mountstats": artifact(prefix + ".mountstats", proc + "/mountstats")
                if pid is None
                else None,
                "probe": None,
            }
            probe = attempt(
                prefix + ".probe", lambda pid=pid, path=path: _probe(pid, path)
            )
            if probe is not None:
                metadata, fdinfo = probe
                value["probe"] = {
                    "metadata": metadata,
                    "fdinfo": _save(root, prefix + ".fdinfo.bin", fdinfo),
                    "resolver": "kernel_open_path"
                    if pid is None
                    else "openat2_in_root_no_magiclinks",
                }
            roles[role] = value
        return {
            "interval_ns": [started, time.monotonic_ns()],
            "boot_id": attempt(
                name + ".boot_id",
                lambda: Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
            ),
            "roles": roles,
        }

    def alive(fd: int) -> bool:
        poll = select.poll()
        poll.register(fd, select.POLLIN | select.POLLHUP | select.POLLERR)
        return not poll.poll(0)

    started = time.monotonic_ns()
    epochs: dict[str, Any] = {}
    liveness: dict[str, Any] = {"before": None, "after": None}
    if platform.system() != "Linux":
        errors["platform"] = {"type": "UnsupportedPlatform", "errno": None}
    else:
        with ExitStack() as resources:
            pidfd = (
                attempt("pidfd", lambda: os.pidfd_open(serving_pid, 0))
                if hasattr(os, "pidfd_open")
                else None
            )
            if pidfd is None and "pidfd" not in errors:
                errors["pidfd"] = {"type": "UnsupportedPidfd", "errno": None}
            if pidfd is not None:
                resources.callback(os.close, pidfd)
                liveness["before"] = alive(pidfd)
            epochs["before"] = epoch("before")
            epochs["after"] = epoch("after")
            if pidfd is not None:
                liveness["after"] = alive(pidfd)
    manifest = {
        "nfs_path_binding_version": "0.1.0-experimental",
        "clock": "CLOCK_MONOTONIC",
        "capture_interval_ns": [started, time.monotonic_ns()],
        "configuration": config,
        "configuration_fingerprint": fingerprint,
        "serving_pid": serving_pid,
        "observer_pid": os.getpid(),
        "arm": arm_name,
        "observer_mountpoint": observer_mountpoint,
        "pidfd_alive": liveness,
        "epochs": epochs,
        "capture_errors": errors,
    }
    _save(
        root,
        "path-binding.json",
        (json.dumps(manifest, allow_nan=False, indent=2) + "\n").encode(),
    )
    return root / "path-binding.json"


def replay_path_binding(bundle_dir: Path) -> dict[str, Any]:
    """Recompute a point-in-time NFS filesystem connection from retained metadata.

    Args:
        bundle_dir: Extracted binding capture, containing path-binding.json.

    Returns:
        matched/mismatch/unproven/invalid path_binding_status, reasons and visible
        aliases. Matched means the selected path and counter view share an NFS
        filesystem; it proves no file payload access, process role, export-subtree
        isolation or serving request. Verification/claims remain withheld.

    Raises:
        PathBindingError: Malformed/tampered/escaping artifacts or inconsistent fields.
        ConfigurationError: Invalid captured configuration.
        OSError: The input bundle cannot be accessed.
    """
    root = Path(bundle_dir).resolve(strict=True)
    path = root / "path-binding.json"
    if not path.resolve(strict=True).is_relative_to(root):
        raise PathBindingError("manifest escapes bundle")
    value = _decode(path.read_bytes())
    _object(
        value,
        {
            "nfs_path_binding_version",
            "clock",
            "capture_interval_ns",
            "configuration",
            "configuration_fingerprint",
            "serving_pid",
            "observer_pid",
            "arm",
            "observer_mountpoint",
            "pidfd_alive",
            "epochs",
            "capture_errors",
        },
        "path binding manifest",
    )
    if (
        value["nfs_path_binding_version"] != "0.1.0-experimental"
        or value["clock"] != "CLOCK_MONOTONIC"
    ):
        raise PathBindingError("unsupported binding contract/clock")
    configuration = _decode(_read(root, value["configuration"]))
    if (
        fingerprint_configuration(configuration, evidence_version="0.4.0")
        != value["configuration_fingerprint"]
    ):
        raise PathBindingError("configuration fingerprint mismatch")
    arms = [
        arm
        for arm in configuration["arms"]
        if arm["name"] == value["arm"]
        and arm["attribution_provider"] == "nfs_mountstats"
    ]
    if len(arms) != 1:
        raise PathBindingError("unknown configured NFS arm")
    tier_path = _absolute(arms[0]["tier_path"])
    point = _absolute(value["observer_mountpoint"])
    serving_pid = _integer(value["serving_pid"], "serving PID", 1)
    observer_pid = _integer(value["observer_pid"], "observer PID", 1)
    interval = value["capture_interval_ns"]
    if (
        not isinstance(interval, list)
        or len(interval) != 2
        or any(type(x) is not int or x < 0 for x in interval)
        or interval[0] > interval[1]
    ):
        raise PathBindingError("invalid capture interval")
    live = _object(value["pidfd_alive"], {"before", "after"}, "pidfd liveness")
    if any(x is not None and type(x) is not bool for x in live.values()):
        raise PathBindingError("invalid liveness receipt")
    if not isinstance(value["capture_errors"], dict):
        raise PathBindingError("capture errors must be an object")
    for error in value["capture_errors"].values():
        _object(error, {"type", "errno"}, "capture error")
        if not isinstance(error["type"], str) or (
            error["errno"] is not None and type(error["errno"]) is not int
        ):
            raise PathBindingError("malformed capture error")
    result = {
        "path_binding_status": "unproven",
        "reason_codes": [],
        "serving_verification": "not_assessed",
        "performance_claims": "suppressed",
        "configuration_fingerprint": value["configuration_fingerprint"],
        "target_scope": arms[0]["tier_identity"],
        "configured_tier_path": tier_path,
        "serving_pid": serving_pid,
        "observer_mountpoint": point,
        "diagnostics": [
            "process_role_operator_selected",
            "cross_namespace_users_not_excluded",
            "path_metadata_is_not_io_evidence",
        ],
        "visible_aliases": {},
    }

    def finish(status: str, reason: str) -> dict[str, Any]:
        result["path_binding_status"] = status
        result["reason_codes"] = [reason]
        return result

    epochs = value["epochs"]
    if not isinstance(epochs, dict) or set(epochs) not in (set(), {"before", "after"}):
        raise PathBindingError("invalid epoch inventory")
    if not epochs:
        return finish("unproven", "capture_sources_unavailable")
    parsed = {}
    missing = bool(value["capture_errors"])
    previous_end = interval[0]
    for name in ("before", "after"):
        epoch = _object(epochs[name], {"interval_ns", "boot_id", "roles"}, "epoch")
        bounds = epoch["interval_ns"]
        if (
            not isinstance(bounds, list)
            or len(bounds) != 2
            or any(type(x) is not int or x < 0 for x in bounds)
        ):
            raise PathBindingError("malformed epoch interval")
        if not previous_end <= bounds[0] <= bounds[1] <= interval[1]:
            return finish("invalid", "capture_interval_changed")
        previous_end = bounds[1]
        if epoch["boot_id"] is not None and (
            not isinstance(epoch["boot_id"], str)
            or not re.fullmatch(
                r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", epoch["boot_id"]
            )
        ):
            raise PathBindingError("invalid boot identity")
        missing = missing or epoch["boot_id"] is None
        roles = _object(epoch["roles"], {"observer", "serving"}, "roles")
        parsed[name] = {}
        for role, pid in [("observer", observer_pid), ("serving", serving_pid)]:
            raw = _object(
                roles[role],
                {
                    "namespace",
                    "root",
                    "stat",
                    "cmdline",
                    "mountinfo",
                    "mountstats",
                    "probe",
                },
                "role capture",
            )
            if raw["namespace"] is not None and (
                not isinstance(raw["namespace"], str)
                or not re.fullmatch(r"mnt:\[[0-9]+\]", raw["namespace"])
            ):
                raise PathBindingError("invalid mount namespace")
            item = {
                "namespace": raw["namespace"],
                "root": None,
                "process": None,
                "cmdline": None,
                "mounts": None,
                "probe": None,
                "mountstats": None,
            }
            for key in ("stat", "cmdline", "mountinfo", "mountstats"):
                data = None if raw[key] is None else _read(root, raw[key])
                if key == "stat" and data is not None:
                    item["process"] = _process(data)
                    if item["process"]["pid"] != pid or item["process"]["state"] in {
                        "Z",
                        "X",
                        "x",
                    }:
                        return finish("invalid", "process_identity_unavailable")
                elif key == "cmdline":
                    item["cmdline"] = data
                elif key == "mountinfo" and data is not None:
                    item["mounts"] = _mounts(data)
                elif key == "mountstats":
                    item["mountstats"] = data
            if raw["root"] is not None:
                item["root"] = _metadata(raw["root"])
            if raw["probe"] is not None:
                probe = _object(
                    raw["probe"], {"metadata", "fdinfo", "resolver"}, "path probe"
                )
                expected = (
                    "kernel_open_path"
                    if role == "observer"
                    else "openat2_in_root_no_magiclinks"
                )
                if probe["resolver"] != expected:
                    raise PathBindingError("unsupported path resolver")
                item["probe"] = {
                    "metadata": _metadata(probe["metadata"]),
                    "mount_id": _mnt_id(_read(root, probe["fdinfo"])),
                }
            missing = (
                missing
                or any(
                    item[key] is None
                    for key in (
                        "namespace",
                        "root",
                        "process",
                        "cmdline",
                        "mounts",
                        "probe",
                    )
                )
                or not item["cmdline"]
            )
            if role == "observer":
                missing = missing or item["mountstats"] is None
            elif raw["mountstats"] is not None:
                raise PathBindingError(
                    "serving mountstats must be unavailable in this contract"
                )
            parsed[name][role] = item
    if False in live.values():
        return finish("invalid", "selected_process_exited")
    if any(x is None for x in live.values()):
        missing = True
    if (
        epochs["before"]["boot_id"] is not None
        and epochs["after"]["boot_id"] is not None
        and epochs["before"]["boot_id"] != epochs["after"]["boot_id"]
    ):
        return finish("invalid", "boot_identity_changed")
    for role in ("observer", "serving"):
        a, b = parsed["before"][role], parsed["after"][role]
        for field in ("namespace", "root", "cmdline", "probe"):
            if a[field] is not None and b[field] is not None and a[field] != b[field]:
                return finish("invalid", role + "_" + field + "_changed")
        if (
            a["process"]
            and b["process"]
            and a["process"]["start_ticks"] != b["process"]["start_ticks"]
        ):
            return finish("invalid", "process_start_time_changed")
    if missing:
        return finish("unproven", "capture_sources_unavailable")
    result["path_observations"] = {}
    for name in ("before", "after"):
        result["path_observations"][name] = {}
        for role in ("observer", "serving"):
            item = parsed[name][role]
            rows = [
                row
                for row in item["mounts"]
                if row["mount_id"] == item["probe"]["mount_id"]
            ]
            if len(rows) != 1:
                return finish("unproven", "fd_mount_not_visible")
            row = rows[0]
            if row["major_minor"] != item["probe"]["metadata"]["major_minor"]:
                return finish("invalid", "fd_device_receipt_mismatch")
            item["selected_mount"] = row
            result["path_observations"][name][role] = {
                "mount": row,
                "metadata": item["probe"]["metadata"],
                "mount_namespace": item["namespace"],
            }
    # Compare a view across epochs; IDs need not agree across namespace views.
    for role in ("observer", "serving"):
        if (
            parsed["before"][role]["selected_mount"]
            != parsed["after"][role]["selected_mount"]
        ):
            return finish("invalid", "selected_mount_changed")
    result["process_identity"] = {
        "pid": serving_pid,
        "start_ticks": parsed["before"]["serving"]["process"]["start_ticks"],
        "cmdline_sha256": sha256(parsed["before"]["serving"]["cmdline"]).hexdigest(),
        "mount_namespace": parsed["before"]["serving"]["namespace"],
    }
    for name in ("before", "after"):
        observer, serving = parsed[name]["observer"], parsed[name]["serving"]
        obs, target = observer["selected_mount"], serving["selected_mount"]
        if observer["namespace"] == serving["namespace"]:
            # A process root can hide or relabel mounts in the same namespace.
            # Check shared visible IDs, not full table/path equality.
            for row, view in ((obs, serving), (target, observer)):
                common = [
                    entry
                    for entry in view["mounts"]
                    if entry["mount_id"] == row["mount_id"]
                ]
                if common and any(
                    common[0][key] != row[key] for key in ("major_minor", "fstype")
                ):
                    return finish("invalid", "shared_namespace_mount_view_changed")
        if obs["mountpoint"] != point:
            return finish("mismatch", "observer_path_is_not_selected_mount")
        if obs["fstype"] not in {"nfs", "nfs4"}:
            return finish("mismatch", "observer_filesystem_is_not_nfs")
        try:
            counters = parse_mountstats(
                observer["mountstats"].decode("utf-8", "surrogateescape"), point
            )
        except NfsObservationError:
            return finish("unproven", "nfs_transport_or_format_unqualified")
        if counters.export != obs["source"] or counters.fstype != obs["fstype"]:
            return finish("invalid", "observer_counter_identity_mismatch")
        if (
            target["fstype"] != obs["fstype"]
            or target["major_minor"] != obs["major_minor"]
        ):
            return finish("mismatch", "serving_path_is_different_filesystem")
        for role, item in [("observer", observer), ("serving", serving)]:
            aliases = [
                row
                for row in item["mounts"]
                if row["major_minor"] == obs["major_minor"]
                and row["fstype"] == obs["fstype"]
            ]
            result["visible_aliases"][name + "." + role] = aliases
            if (
                len(aliases) > 1
                and "counter_aliases_observed" not in result["diagnostics"]
            ):
                result["diagnostics"].append("counter_aliases_observed")
        if (
            observer["namespace"] != serving["namespace"]
            and "different_namespace_views" not in result["diagnostics"]
        ):
            result["diagnostics"].append("different_namespace_views")
    return finish("matched", "same_nfs_filesystem_counter_scope")
