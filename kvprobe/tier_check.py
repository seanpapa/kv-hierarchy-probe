# SPDX-License-Identifier: Apache-2.0
"""Read-only checks that a local NVMe tier can support a Gate 1 run.

A local NVMe run takes about half an hour, and several host conditions decide
whether it can establish Gate 1 at all: whether the tier resolves to the device
the per-device counter reads, whether that device is the tier's alone, whether
the counters exist, and whether cuFile takes the native path. Missing one shows
up only after the run, as ``UNPROVEN`` or as a measurement of something other
than the tier. These checks read ``/sys``, ``/proc`` and the mount table, and
run ``gdscheck -p`` with a timeout. They never write, remount, change a module
parameter or start a container, and a value they cannot read is reported as
``SKIP`` rather than guessed.

A ``READY`` result is not Gate 1 evidence. Only the run's per-request counters
are.
"""

# Standard
import math
import os
import platform
import re
import subprocess
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Protocol

#: Arms the check knows. NIXL static ``GDS_MT`` goes through cuFile and is
#: checked as ``gds``.
ARMS = ("gds", "posix")

PASS = "PASS"
WARN = "WARN"
FAIL = "FAIL"
SKIP = "SKIP"
STATUSES = (PASS, WARN, FAIL, SKIP)

READY = "READY"
READY_WITH_WARNINGS = "READY WITH WARNINGS"
NOT_READY = "NOT READY"

#: Runbook values used when the operator gives the staging-pool size but not
#: the workload (scripts/README.md, minimal runbook step 1).
DEFAULT_CONTEXT_TOKENS = 4096
DEFAULT_FILLERS = 6
DEFAULT_CHUNK_TOKENS = 256

COMMAND_TIMEOUT_SECONDS = 10

_FAILURE_CATALOG = "docs/FAILURE-CATALOG.md"
_PRECONDITIONS = "scripts/README.md, 'Preconditions on the host'"
_COUNTER_NOTE = "scripts/README.md, 'The POSIX arm's cgroup counter reads one device'"
_SCOPE_TABLE = "README.md, scope table"


class Host(Protocol):
    """Read-only view of the host that every check goes through."""

    def read_text(self, path: str) -> str | None:
        """Return a file's text, or ``None`` when it is absent or unreadable."""

    def list_dir(self, path: str) -> list[str] | None:
        """Return a directory's entry names, or ``None`` when it cannot be listed."""

    def exists(self, path: str) -> bool:
        """Return whether a path exists, following symbolic links."""

    def is_dir(self, path: str) -> bool:
        """Return whether a path is a directory, following symbolic links."""

    def realpath(self, path: str) -> str:
        """Return the canonical path with every symbolic link resolved."""

    def device_numbers(self, path: str) -> tuple[int, int] | None:
        """Return ``(major, minor)`` of the device holding a path, or ``None``."""

    def run(self, command: list[str]) -> tuple[int, str]:
        """Run a bounded read-only command; return its exit code and output."""


class LocalHost:
    """The real host, read through the standard library only."""

    def read_text(self, path: str) -> str | None:
        """Return a file's text, or ``None`` when it is absent or unreadable.

        Args:
            path: File to read.

        Returns:
            The file's text, or ``None``.
        """
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                return handle.read()
        except OSError:
            return None

    def list_dir(self, path: str) -> list[str] | None:
        """Return a directory's entry names, or ``None`` when it cannot be listed.

        Args:
            path: Directory to list.

        Returns:
            Sorted entry names, or ``None``.
        """
        try:
            return sorted(os.listdir(path))
        except OSError:
            return None

    def exists(self, path: str) -> bool:
        """Return whether a path exists, following symbolic links.

        Args:
            path: Path to test.

        Returns:
            True when the path exists.
        """
        return os.path.exists(path)

    def is_dir(self, path: str) -> bool:
        """Return whether a path is a directory, following symbolic links.

        Args:
            path: Path to test.

        Returns:
            True when the path is a directory.
        """
        return os.path.isdir(path)

    def realpath(self, path: str) -> str:
        """Return the canonical path with every symbolic link resolved.

        Args:
            path: Path to resolve.

        Returns:
            The resolved path.
        """
        return os.path.realpath(path)

    def device_numbers(self, path: str) -> tuple[int, int] | None:
        """Return ``(major, minor)`` of the device holding a path, or ``None``.

        Args:
            path: Path whose filesystem device is wanted.

        Returns:
            The device numbers, or ``None`` when the path cannot be read.
        """
        try:
            st_dev = os.stat(path).st_dev
        except OSError:
            return None
        return os.major(st_dev), os.minor(st_dev)

    def run(self, command: list[str]) -> tuple[int, str]:
        """Run a bounded read-only command.

        Args:
            command: Executable and arguments.

        Returns:
            Exit code and combined output. A command that cannot be started or
            does not finish within the timeout returns 127.
        """
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                check=False,
                text=True,
                timeout=COMMAND_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return 127, str(exc)
        output = "\n".join(part for part in (result.stdout, result.stderr) if part)
        return result.returncode, output.strip()


def is_supported_platform() -> bool:
    """Return whether this host exposes the Linux interfaces the checks read.

    Returns:
        True on Linux.
    """
    return platform.system() == "Linux"


@dataclass(frozen=True)
class PoolSizes:
    """Staging-pool and workload sizes for the T11 check, in runbook terms.

    Attributes:
        cufile_buf_mib: cuFile staging pool (``PROBE_CUFILE_BUF_MIB``).
        mib_per_chunk: KV MiB of one chunk of the served model
            (``PROBE_MIB_PER_CHUNK``).
        context_tokens: Tokens of the measured context (``REP_CONTEXT``).
        fillers: Distinct filler contexts between measurements (``FILLERS``).
        chunk_tokens: Tokens per stored chunk.
    """

    cufile_buf_mib: int
    mib_per_chunk: int
    context_tokens: int = DEFAULT_CONTEXT_TOKENS
    fillers: int = DEFAULT_FILLERS
    chunk_tokens: int = DEFAULT_CHUNK_TOKENS


@dataclass(frozen=True)
class TierCheckRequest:
    """What to check.

    Attributes:
        tier_mount: Host path of the tier filesystem (``GDS_MOUNT``).
        arm: ``gds`` (also NIXL static ``GDS_MT``) or ``posix``.
        tier_dir: Directory a launcher reuses between runs; empty to skip T12.
        model_dir: Host model directory to compare disks with; empty to omit.
        gdscheck: Path of ``gdscheck``; empty when it was not found.
        pool: Sizes for T11; ``None`` to skip it.
    """

    tier_mount: str
    arm: str = "gds"
    tier_dir: str = ""
    model_dir: str = ""
    gdscheck: str = ""
    pool: PoolSizes | None = None


@dataclass(frozen=True)
class CheckResult:
    """Outcome of one check.

    Attributes:
        id: Check identifier, ``T1`` to ``T12``.
        title: What the check establishes.
        status: One of ``PASS``, ``WARN``, ``FAIL`` or ``SKIP``.
        finding: What was observed, or why it could not be checked.
        why: Why a warning, failure or required unchecked condition matters.
        fix: Next action for a warning, failure or required unchecked condition.
        required_for_ready: Whether an unchecked result blocks ``READY``.
            False only for checks that do not apply or were not requested.
    """

    id: str
    title: str
    status: str
    finding: str
    why: str = ""
    fix: str = ""
    required_for_ready: bool = True

    def as_dict(self) -> dict[str, str | bool]:
        """Return the result as a JSON-ready mapping.

        Returns:
            Mapping with ``id``, ``title``, ``status``, ``finding``, ``why``,
            ``fix`` and ``required_for_ready``.
        """
        return {
            "id": self.id,
            "title": self.title,
            "status": self.status,
            "finding": self.finding,
            "why": self.why,
            "fix": self.fix,
            "required_for_ready": self.required_for_ready,
        }


@dataclass(frozen=True)
class TierDevice:
    """The ``io.stat`` device a path is counted under.

    Attributes:
        key: ``"<major>:<minor>"`` as ``io.stat`` writes it; empty when
            unresolved.
        scope: ``whole_disk``, ``widened_to_parent_disk`` or ``unresolved``,
            the same values the controlled client records.
        disk: Disk name under ``/sys/block``; empty when unresolved.
        detail: Why the device is unresolved; empty otherwise.
    """

    key: str
    scope: str
    disk: str
    detail: str = ""

    @property
    def resolved(self) -> bool:
        """Return whether a device key was found.

        Returns:
            True when ``key`` is set.
        """
        return bool(self.key)


@dataclass(frozen=True)
class MountEntry:
    """One line of ``/proc/self/mountinfo``.

    Attributes:
        device: ``"<major>:<minor>"`` of the mounted filesystem.
        mount_point: Where it is mounted.
        fstype: Filesystem type.
        options: Per-mount options followed by the superblock options.
    """

    device: str
    mount_point: str
    fstype: str
    options: tuple[str, ...]


def _unescape_mount_field(value: str) -> str:
    """Decode the octal escapes the kernel writes in mountinfo paths.

    Args:
        value: Raw field, for example ``/mnt/my\\040tier``.

    Returns:
        The decoded path.
    """
    return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match.group(1), 8)), value)


def parse_mountinfo(text: str) -> list[MountEntry]:
    """Parse ``/proc/self/mountinfo``.

    Args:
        text: File contents.

    Returns:
        Entries in file order. Malformed lines are skipped.
    """
    entries: list[MountEntry] = []
    for line in text.splitlines():
        fields = line.split()
        if "-" not in fields:
            continue
        separator = fields.index("-")
        if separator < 6 or len(fields) < separator + 3:
            continue
        options = fields[5].split(",")
        if len(fields) > separator + 3:
            options += fields[separator + 3].split(",")
        entries.append(
            MountEntry(
                device=fields[2],
                mount_point=_unescape_mount_field(fields[4]),
                fstype=fields[separator + 1],
                options=tuple(option for option in options if option),
            )
        )
    return entries


def mount_for(path: str, mounts: list[MountEntry]) -> MountEntry | None:
    """Return the mount that holds a resolved path.

    Args:
        path: Canonical path.
        mounts: Parsed mount table.

    Returns:
        The entry with the longest mount point containing ``path``; the last
        such entry when mounts are stacked. ``None`` when none matches.
    """
    best: MountEntry | None = None
    target = PurePosixPath(path)
    for entry in mounts:
        point = PurePosixPath(entry.mount_point)
        if target != point and point not in target.parents:
            continue
        if best is None or len(point.parts) >= len(
            PurePosixPath(best.mount_point).parts
        ):
            best = entry
    return best


def resolve_block_device(host: Host, major: int, minor: int) -> TierDevice:
    """Return the ``io.stat`` device for a filesystem's device numbers.

    ``io.stat`` accounts I/O against whole disks. A partition is therefore
    widened to its parent disk, and the scope says so. The decision is the
    same as ``tier_io_device`` in ``scripts/g5_controlled_kv_experiment.py``;
    a parity test holds the two together.

    Args:
        host: Host view.
        major: Device major number.
        minor: Device minor number.

    Returns:
        The resolved device, or an unresolved one with the reason.
    """
    block = f"/sys/dev/block/{major}:{minor}"
    if not host.exists(block):
        return TierDevice(
            "",
            "unresolved",
            "",
            f"{major}:{minor} has no block device (for example tmpfs, overlay or a network filesystem)",
        )
    target = host.realpath(block)
    if not host.exists(f"{block}/partition"):
        return TierDevice(f"{major}:{minor}", "whole_disk", PurePosixPath(target).name)
    parent = PurePosixPath(target).parent
    key = host.read_text(str(parent / "dev"))
    if key is None or not key.strip():
        return TierDevice(
            "",
            "unresolved",
            "",
            f"the parent disk of partition {major}:{minor} could not be read",
        )
    return TierDevice(key.strip(), "widened_to_parent_disk", parent.name)


def resolve_path_device(host: Host, path: str) -> TierDevice:
    """Return the ``io.stat`` device for the filesystem holding a path.

    Args:
        host: Host view.
        path: Path on the filesystem.

    Returns:
        The resolved device, or an unresolved one with the reason.
    """
    numbers = host.device_numbers(path)
    if numbers is None:
        return TierDevice(
            "", "unresolved", "", f"{path} does not exist or cannot be read"
        )
    return resolve_block_device(host, *numbers)


def _disk_of_block_name(host: Host, name: str) -> str:
    """Return the disk a block device name belongs to.

    Args:
        host: Host view.
        name: Name under ``/sys/class/block``, a disk or a partition.

    Returns:
        The disk name.
    """
    entry = f"/sys/class/block/{name}"
    if host.exists(f"{entry}/partition"):
        return PurePosixPath(host.realpath(entry)).parent.name
    return name


def physical_disks(host: Host, disk: str, depth: int = 0) -> set[str]:
    """Return the disks under a block device, through device-mapper and RAID.

    Args:
        host: Host view.
        disk: Disk name under ``/sys/block``.
        depth: Recursion depth; stacking deeper than eight layers is cut off.

    Returns:
        Names of the lowest devices found; ``{disk}`` when nothing is stacked.
    """
    slaves = host.list_dir(f"/sys/block/{disk}/slaves") or []
    if not slaves or depth >= 8:
        return {disk}
    found: set[str] = set()
    for slave in slaves:
        found |= physical_disks(host, _disk_of_block_name(host, slave), depth + 1)
    return found


def _skip(
    check_id: str, title: str, reason: str, *, required_for_ready: bool = True
) -> CheckResult:
    """Return a ``SKIP`` result.

    Args:
        check_id: Check identifier.
        title: Check title.
        reason: Why it could not be checked.
        required_for_ready: False for an inapplicable or unrequested check.

    Returns:
        The result.
    """
    return CheckResult(
        check_id,
        title,
        SKIP,
        reason,
        "This required host condition has not been established."
        if required_for_ready
        else "",
        "Resolve the unreadable or missing information named above and rerun tier-check."
        if required_for_ready
        else "",
        required_for_ready=required_for_ready,
    )


def check_device(
    host: Host, request: TierCheckRequest, device: TierDevice
) -> CheckResult:
    """T1: the tier resolves to a device the per-device counter can read.

    Args:
        host: Host view.
        request: What to check.
        device: Resolution of the tier mount.

    Returns:
        ``FAIL`` when unresolved, ``WARN`` for a partition, else ``PASS``.
    """
    title = "Tier resolves to a counted block device"
    if not device.resolved:
        return CheckResult(
            "T1",
            title,
            FAIL,
            device.detail,
            "The run's cgroup counter records no device, so Gate 1 is UNPROVEN.",
            "Point --tier-mount at the mounted tier filesystem (GDS_MOUNT in the runbook).",
        )
    if device.scope == "whole_disk":
        return CheckResult(
            "T1", title, PASS, f"{device.key} ({device.disk}), whole disk"
        )
    others = _other_mounts_on_disk(host, request, device)
    also = f"; also mounted from that disk: {', '.join(others)}" if others else ""
    return CheckResult(
        "T1",
        title,
        WARN,
        f"the tier is on a partition, so io.stat counts the whole disk {device.disk} ({device.key}){also}",
        "The counter also covers the disk's other partitions, which weakens what Gate 1's bytes "
        "attribute to the tier.",
        f"Put the tier on a disk of its own ({_COUNTER_NOTE}).",
    )


def _other_mounts_on_disk(
    host: Host, request: TierCheckRequest, device: TierDevice
) -> list[str]:
    """Return mount points other than the tier's that resolve to the tier disk.

    Args:
        host: Host view.
        request: What to check.
        device: Resolution of the tier mount.

    Returns:
        Mount points in mount-table order.
    """
    mounts = parse_mountinfo(host.read_text("/proc/self/mountinfo") or "")
    own = mount_for(host.realpath(request.tier_mount), mounts)
    others: list[str] = []
    for entry in mounts:
        if own is not None and entry.mount_point == own.mount_point:
            continue
        major, _, minor = entry.device.partition(":")
        if not (major.isdigit() and minor.isdigit()):
            continue
        if resolve_block_device(host, int(major), int(minor)).key == device.key:
            if entry.mount_point not in others:
                others.append(entry.mount_point)
    return others


def _docker_root(host: Host) -> str:
    """Return Docker's root directory, or an empty string when unavailable.

    Args:
        host: Host view.

    Returns:
        The directory reported by ``docker info``.
    """
    code, output = host.run(["docker", "info", "--format", "{{.DockerRootDir}}"])
    root = output.strip().splitlines()[-1].strip() if output.strip() else ""
    return root if code == 0 and root.startswith("/") else ""


def check_dedicated_disk(
    host: Host, request: TierCheckRequest, device: TierDevice
) -> CheckResult:
    """T2: the tier is on its own mount, on a disk holding nothing else of the run.

    Args:
        host: Host view.
        request: What to check.
        device: Resolution of the tier mount.

    Returns:
        ``FAIL`` when the tier path is on the root filesystem itself, ``WARN``
        when its disk also holds the root filesystem, Docker's root or the
        model directory, ``SKIP`` when required comparisons cannot be made,
        else ``PASS``. The model comparison applies only when requested.
    """
    title = "Tier disk is dedicated"
    if not device.resolved:
        return _skip("T2", title, "the tier device is unresolved (T1)")
    mounts = parse_mountinfo(host.read_text("/proc/self/mountinfo") or "")
    own = mount_for(host.realpath(request.tier_mount), mounts)
    if own is None:
        return _skip(
            "T2",
            title,
            "the tier mount could not be established from /proc/self/mountinfo",
        )
    if own.mount_point == "/":
        return CheckResult(
            "T2",
            title,
            FAIL,
            f"{request.tier_mount} is on the root filesystem, not on a mount of its own",
            "A tier mount that is gone (for example after a reboot without an fstab entry) leaves "
            "the directory on the boot disk, and the run measures that disk.",
            f"Mount the tier filesystem at {request.tier_mount} and persist it by UUID "
            f"({_FAILURE_CATALOG} §9).",
        )
    tier_disks = physical_disks(host, device.disk)
    candidates = [("/", "root filesystem")]
    docker_root = _docker_root(host)
    if docker_root:
        candidates.append((docker_root, "Docker root"))
    if request.model_dir:
        candidates.append((request.model_dir, "model directory"))
    shared: list[str] = []
    compared: list[str] = []
    not_compared: list[str] = (
        [] if docker_root else ["Docker root (docker info unavailable)"]
    )
    for path, label in candidates:
        other = resolve_path_device(host, path)
        if not other.resolved:
            not_compared.append(f"{label} {path} ({other.detail})")
            continue
        compared.append(label)
        if physical_disks(host, other.disk) & tier_disks:
            shared.append(f"{label} {path}")
    note = f"; not compared: {', '.join(not_compared)}" if not_compared else ""
    if shared:
        return CheckResult(
            "T2",
            title,
            WARN,
            f"the tier disk {device.disk} also holds: {', '.join(shared)}{note}",
            "The per-device counter counts every byte the container moves on that disk, so its "
            "other reads there cannot be told from tier reads.",
            "Use a whole NVMe disk that holds nothing but the tier (runbook prerequisites).",
        )
    if not_compared:
        return _skip("T2", title, f"disk comparisons incomplete{note}")
    return CheckResult(
        "T2", title, PASS, f"the tier disk holds none of: {', '.join(compared)}{note}"
    )


def check_device_class(host: Host, device: TierDevice) -> CheckResult:
    """T3: the tier is on local PCIe NVMe, non-rotational media.

    Args:
        host: Host view.
        device: Resolution of the tier mount.

    Returns:
        ``WARN`` for another device class or transport, ``SKIP`` when required
        attributes cannot be read, else ``PASS``.
    """
    title = "Tier device is local PCIe NVMe"
    if not device.resolved:
        return _skip("T3", title, "the tier device is unresolved (T1)")
    disks = sorted(physical_disks(host, device.disk))
    other: list[str] = []
    unknown: list[str] = []
    for disk in disks:
        rotational = (
            host.read_text(f"/sys/block/{disk}/queue/rotational") or ""
        ).strip()
        if not disk.startswith("nvme"):
            other.append(f"{disk} is not an NVMe namespace")
        elif rotational == "1":
            other.append(f"{disk} reports rotational media")
        elif rotational != "0":
            unknown.append(f"{disk}: queue/rotational is unreadable or unrecognized")
        if not disk.startswith("nvme"):
            continue
        # A namespace name alone does not distinguish local PCIe from NVMe-oF.
        # Find its controller in the resolved sysfs ancestry, without guessing
        # a controller number from the namespace name under multipath.
        ancestors = PurePosixPath(host.realpath(f"/sys/block/{disk}")).parents
        controller = next(
            (
                parent.name
                for parent in ancestors
                if re.fullmatch(r"nvme[0-9]+", parent.name)
            ),
            "",
        )
        transport = (
            host.read_text(f"/sys/class/nvme/{controller}/transport")
            if controller
            else None
        )
        if transport is None or not transport.strip():
            unknown.append(f"{disk}: controller transport is unreadable or unresolved")
        elif transport.strip() != "pcie":
            other.append(
                f"{disk} transport={transport.strip()} is outside the local PCIe baseline"
            )
    if other:
        return CheckResult(
            "T3",
            title,
            WARN,
            "; ".join(other + unknown),
            "Gate 1 has passed on hardware only for a local NVMe tier; another device class is "
            "unverified, including NVMe-oF.",
            f"Read results on this device as unverified ({_SCOPE_TABLE}).",
        )
    if unknown:
        return _skip("T3", title, "; ".join(unknown))
    return CheckResult("T3", title, PASS, f"{', '.join(disks)}: NVMe, transport=pcie")


def check_native_multipath(host: Host, device: TierDevice) -> CheckResult:
    """T4: native NVMe multipath is off.

    Args:
        host: Host view.
        device: Resolution of the tier mount.

    Returns:
        ``PASS`` for ``N``, ``WARN`` for ``Y``, ``SKIP`` when it does not apply
        or cannot be read.
    """
    title = "Native NVMe multipath is off"
    if not device.resolved:
        return _skip("T4", title, "the tier device is unresolved (T1)")
    if not any(disk.startswith("nvme") for disk in physical_disks(host, device.disk)):
        return _skip(
            "T4",
            title,
            "the tier is not on an NVMe namespace",
            required_for_ready=False,
        )
    value = host.read_text("/sys/module/nvme_core/parameters/multipath")
    if value is None:
        return _skip(
            "T4", title, "/sys/module/nvme_core/parameters/multipath is not readable"
        )
    if value.strip() in ("N", "0"):
        return CheckResult("T4", title, PASS, "nvme_core multipath=N")
    return CheckResult(
        "T4",
        title,
        WARN,
        f"nvme_core multipath={value.strip()}",
        "Gate 1's byte attribution was verified with native multipath off; with it on, which "
        "device number io.stat records is unverified.",
        f"Boot with nvme_core.multipath=N for the reference configuration, or read Gate 1 on "
        f"this host as unverified ({_SCOPE_TABLE}).",
    )


def check_stacking(host: Host, device: TierDevice) -> CheckResult:
    """T5: no device-mapper, LVM or RAID layer between the tier and the disk.

    Args:
        host: Host view.
        device: Resolution of the tier mount.

    Returns:
        ``WARN`` when stacked or held, ``SKIP`` when unresolved, else ``PASS``.
    """
    title = "No device stacking"
    if not device.resolved:
        return _skip("T5", title, "the tier device is unresolved (T1)")
    findings: list[str] = []
    slaves = host.list_dir(f"/sys/block/{device.disk}/slaves")
    holders = host.list_dir(f"/sys/block/{device.disk}/holders")
    if slaves is None or holders is None:
        return _skip(
            "T5", title, f"{device.disk}: slaves or holders could not be listed"
        )
    lower = sorted(physical_disks(host, device.disk) - {device.disk})
    if lower:
        findings.append(f"{device.disk} is stacked on {', '.join(lower)}")
    elif device.disk.startswith(("dm-", "md")):
        findings.append(f"{device.disk} is a device-mapper or RAID device")
    if holders:
        findings.append(f"{device.disk} is held by {', '.join(holders)}")
    if findings:
        return CheckResult(
            "T5",
            title,
            WARN,
            "; ".join(findings),
            "Byte attribution through device-mapper, LVM or RAID is unverified; io.stat may record "
            "the bytes on a different device.",
            "Use a plain NVMe namespace for the reference configuration (docs/ROADMAP.md, "
            "NVMe-oF hardware check).",
        )
    return CheckResult(
        "T5", title, PASS, f"{device.disk} has no device-mapper or RAID layer"
    )


def check_cgroup_io(host: Host) -> CheckResult:
    """T6: cgroup v2 with the ``io`` controller.

    Args:
        host: Host view.

    Returns:
        ``FAIL`` without cgroup v2 or its ``io`` controller, else ``PASS``.
    """
    title = "cgroup v2 io controller"
    controllers = host.read_text("/sys/fs/cgroup/cgroup.controllers")
    why = (
        "The per-device counter reads the container's io.stat; without it the observation is "
        "withheld and Gate 1 is UNPROVEN."
    )
    fix = "Run on a host with the unified cgroup v2 hierarchy (runbook prerequisites)."
    if controllers is None:
        return CheckResult(
            "T6", title, FAIL, "cgroup v2 is not mounted at /sys/fs/cgroup", why, fix
        )
    if "io" not in controllers.split():
        return CheckResult(
            "T6",
            title,
            FAIL,
            "cgroup v2 is mounted but the io controller is not available",
            why,
            fix,
        )
    return CheckResult("T6", title, PASS, "cgroup v2 with the io controller")


def check_filesystem(host: Host, request: TierCheckRequest) -> CheckResult:
    """T7: a filesystem native GDS accepts on local NVMe.

    Args:
        host: Host view.
        request: What to check.

    Returns:
        ``PASS`` for XFS or ext4 with ``data=ordered``, ``FAIL`` otherwise,
        ``SKIP`` for the POSIX arm or when the mount cannot be found.
    """
    title = "Filesystem accepted by cuFile"
    if request.arm != "gds":
        return _skip(
            "T7", title, "the POSIX arm does not use cuFile", required_for_ready=False
        )
    mounts = parse_mountinfo(host.read_text("/proc/self/mountinfo") or "")
    entry = mount_for(host.realpath(request.tier_mount), mounts)
    if entry is None:
        return _skip("T7", title, "the tier's mount is not in /proc/self/mountinfo")
    if entry.fstype == "xfs":
        return CheckResult("T7", title, PASS, f"xfs at {entry.mount_point}")
    if entry.fstype == "ext4" and "data=ordered" in entry.options:
        return CheckResult(
            "T7", title, PASS, f"ext4 with data=ordered at {entry.mount_point}"
        )
    if entry.fstype == "ext4":
        return CheckResult(
            "T7",
            title,
            FAIL,
            f"ext4 at {entry.mount_point} without data=ordered in its options",
            "cuFile needs data=ordered to appear in the mount options; without it GDS falls back "
            "to POSIX or registration fails with err 5008.",
            f"Remount with data=ordered and persist it by UUID ({_FAILURE_CATALOG} §4).",
        )
    return CheckResult(
        "T7",
        title,
        FAIL,
        f"{entry.fstype} at {entry.mount_point}",
        "Native GDS on a local NVMe supports XFS and ext4 in ordered mode; on another filesystem "
        "the run does not take the native path.",
        f"Put the tier on XFS, or on ext4 mounted with data=ordered ({_FAILURE_CATALOG} §4).",
    )


def check_compat_mode(host: Host, request: TierCheckRequest) -> CheckResult:
    """T8: the reported cuFile settings do not allow or force compat.

    Args:
        host: Host view.
        request: What to check.

    Returns:
        ``PASS`` when both compat settings are false in successful output,
        ``FAIL`` if either is true, and ``SKIP`` for failed or incomplete
        output. These settings do not prove a native transfer took place.
    """
    title = "cuFile compat settings disabled"
    if request.arm != "gds":
        return _skip(
            "T8", title, "the POSIX arm does not use cuFile", required_for_ready=False
        )
    if not request.gdscheck:
        return _skip("T8", title, "gdscheck not found; pass --gdscheck or set GDSCHECK")
    code, output = host.run([request.gdscheck, "-p"])
    if code != 0:
        return _skip("T8", title, f"{request.gdscheck} -p failed (exit {code})")
    settings: dict[str, str] = {}
    for name in ("use_compat_mode", "force_compat_mode"):
        match = re.search(
            rf"^\s*(?:#\s*)?(?:properties\.)?{name}\s*:\s*(\w+)", output, re.MULTILINE
        )
        if match is not None:
            settings[name] = match.group(1).lower()
    nvme = re.search(r"^\s*NVMe\s*:\s*(.+?)\s*$", output, re.MULTILINE)
    nvme_note = f"; NVMe: {nvme.group(1)}" if nvme else ""
    enabled = [name for name, value in settings.items() if value == "true"]
    if enabled:
        return CheckResult(
            "T8",
            title,
            FAIL,
            f"{', '.join(enabled)} : true{nvme_note}",
            "Compat is allowed or forced; these settings do not establish a native GDS path.",
            'Set "allow_compat_mode" and "force_compat_mode" to false in the cuFile configuration; '
            f"check CUFILE_FORCE_COMPAT_MODE overrides as well ({_FAILURE_CATALOG} §3).",
        )
    missing = [
        name
        for name in ("use_compat_mode", "force_compat_mode")
        if settings.get(name) != "false"
    ]
    if missing:
        return _skip(
            "T8",
            title,
            f"{request.gdscheck} -p printed no recognized {', '.join(missing)} value",
        )
    return CheckResult(
        "T8",
        title,
        PASS,
        f"use_compat_mode : false; force_compat_mode : false{nvme_note}",
    )


def check_nvidia_fs(host: Host, request: TierCheckRequest) -> CheckResult:
    """T9: ``nvidia-fs`` is loaded and counting, with debug logging off.

    Args:
        host: Host view.
        request: What to check.

    Returns:
        ``FAIL`` when the module, its devices or its counters are missing,
        ``WARN`` when debug logging is on, ``SKIP`` for the POSIX arm or an
        unreadable parameter, else ``PASS``.
    """
    title = "nvidia-fs loaded and counting"
    if request.arm != "gds":
        return _skip(
            "T9",
            title,
            "the POSIX arm does not use nvidia-fs",
            required_for_ready=False,
        )
    missing: list[str] = []
    if not host.exists("/proc/driver/nvidia-fs/stats"):
        missing.append("/proc/driver/nvidia-fs/stats")
    if not any(name.startswith("nvidia-fs") for name in host.list_dir("/dev") or []):
        missing.append("/dev/nvidia-fs*")
    if missing:
        return CheckResult(
            "T9",
            title,
            FAIL,
            f"nvidia-fs is not loaded: no {' and no '.join(missing)}",
            "Native GDS needs the nvidia-fs driver, and its counters corroborate Gate 1 and "
            "attribute Gate 3 and 4 stores.",
            f"Load nvidia_fs and pass /dev/nvidia-fs* to the container ({_PRECONDITIONS}, 3).",
        )
    parameters = "/sys/module/nvidia_fs/parameters"
    rw_stats = host.read_text(f"{parameters}/rw_stats_enabled")
    if rw_stats is None:
        return _skip("T9", title, f"{parameters}/rw_stats_enabled is not readable")
    if rw_stats.strip() not in ("1", "Y"):
        return CheckResult(
            "T9",
            title,
            FAIL,
            f"rw_stats_enabled={rw_stats.strip()}",
            "With the counters off, nvidia-fs Reads and Writes never rise: native runs look like "
            "compat and every store looks unwritten.",
            "Enable it before the run: echo 1 | sudo tee "
            f"{parameters}/rw_stats_enabled. A module parameter set this way returns to "
            f"its default after a reboot ({_FAILURE_CATALOG} §9).",
        )
    debug_value = host.read_text(f"{parameters}/dbg_enabled")
    if debug_value is None or not debug_value.strip():
        return _skip(
            "T9", title, f"rw_stats_enabled=1; {parameters}/dbg_enabled is not readable"
        )
    debug = debug_value.strip()
    if debug not in ("0", "N"):
        return CheckResult(
            "T9",
            title,
            WARN,
            f"rw_stats_enabled=1, dbg_enabled={debug}",
            "Debug logging depresses throughput; keep it for diagnostic runs.",
            f"Set dbg_enabled to 0 for performance runs ({_FAILURE_CATALOG} §9).",
        )
    return CheckResult(
        "T9", title, PASS, "stats present, rw_stats_enabled=1, dbg_enabled=0"
    )


def check_container_prerequisites(host: Host, request: TierCheckRequest) -> CheckResult:
    """T10: what the container needs from the host for cuFile exists.

    Args:
        host: Host view.
        request: What to check.

    Returns:
        ``FAIL`` when ``/run/udev`` or ``/etc/cufile.json`` is missing,
        ``SKIP`` for the POSIX arm, else ``PASS``.
    """
    title = "Container prerequisites for cuFile"
    if request.arm != "gds":
        return _skip(
            "T10", title, "the POSIX arm does not use cuFile", required_for_ready=False
        )
    missing: list[str] = []
    if not host.is_dir("/run/udev"):
        missing.append("/run/udev")
    if host.read_text("/etc/cufile.json") is None:
        missing.append("readable /etc/cufile.json")
    if missing:
        return CheckResult(
            "T10",
            title,
            FAIL,
            f"missing: {', '.join(missing)}",
            "Without the udev database cuFile cannot decide whether the volume supports direct "
            "I/O and registration fails inside the container only (err 5008).",
            f"Provide both on the host; the reference launcher mounts them read-only "
            f"({_FAILURE_CATALOG} §5; {_PRECONDITIONS}, 3).",
        )
    return CheckResult("T10", title, PASS, "/run/udev and /etc/cufile.json present")


def check_staging_pool(request: TierCheckRequest) -> CheckResult:
    """T11: the cuFile staging pool against one entry and the working set.

    Args:
        request: What to check.

    Returns:
        ``PASS`` when the pool holds at most one entry, ``WARN`` when it holds
        more but less than the working set, ``FAIL`` when the working set fits,
        ``SKIP`` for the POSIX arm or when no sizes were given.
    """
    title = "Staging pool below the working set"
    if request.arm != "gds":
        return _skip(
            "T11",
            title,
            "the cuFile staging pool applies to the GDS arm",
            required_for_ready=False,
        )
    pool = request.pool
    if pool is None:
        return _skip(
            "T11",
            title,
            "pass --cufile-buf-mib and --mib-per-chunk to check the staging pool",
            required_for_ready=False,
        )
    entry = pool.mib_per_chunk * math.ceil(pool.context_tokens / pool.chunk_tokens)
    working_set = (1 + pool.fillers) * entry
    sizes = (
        f"pool {pool.cufile_buf_mib} MiB, entry {entry} MiB "
        f"({pool.context_tokens} tokens), working set {working_set} MiB "
        f"(1 + {pool.fillers} fillers)"
    )
    if pool.cufile_buf_mib >= working_set:
        return CheckResult(
            "T11",
            title,
            FAIL,
            sizes,
            "The whole working set fits in the GPU staging pool, so requests can be served from "
            "GPU memory without touching the device: a phantom tier.",
            f"Set PROBE_CUFILE_BUF_MIB to one entry or less ({_FAILURE_CATALOG} §2).",
        )
    if pool.cufile_buf_mib > entry:
        return CheckResult(
            "T11",
            title,
            WARN,
            sizes,
            "A pool larger than one entry lets Gate 4 misread GPU-buffer hits as tier hits.",
            f"Keep PROBE_CUFILE_BUF_MIB at one entry or less ({_FAILURE_CATALOG} §13).",
        )
    return CheckResult("T11", title, PASS, sizes)


def check_tier_directory(host: Host, request: TierCheckRequest) -> CheckResult:
    """T12: the directory a launcher reuses holds nothing from an earlier run.

    Args:
        host: Host view.
        request: What to check.

    Returns:
        ``FAIL`` when it has entries other than ``lost+found``, ``SKIP`` when
        not requested or not listable, else ``PASS``.
    """
    title = "Tier directory is empty"
    if not request.tier_dir:
        return _skip(
            "T12",
            title,
            "pass --tier-dir for a directory your launcher reuses; the reference launchers clear "
            "theirs before each arm",
            required_for_ready=False,
        )
    if not host.exists(request.tier_dir):
        return CheckResult("T12", title, PASS, f"{request.tier_dir} does not exist yet")
    entries = host.list_dir(request.tier_dir)
    if entries is None:
        return _skip("T12", title, f"{request.tier_dir} cannot be listed by this user")
    left = [name for name in entries if name != "lost+found"]
    if left:
        return CheckResult(
            "T12",
            title,
            FAIL,
            f"{len(left)} entries left in {request.tier_dir}",
            "Chunks left by an earlier run are served as cache hits, so the run measures those "
            "instead of its own stores.",
            f"Empty the directory before the run ({_PRECONDITIONS}, 5).",
        )
    return CheckResult("T12", title, PASS, f"{request.tier_dir} is empty")


def overall_status(results: list[CheckResult]) -> str:
    """Return the overall line for a set of results.

    Args:
        results: Every check's result.

    Returns:
        ``NOT READY`` with any ``FAIL`` or required ``SKIP``, otherwise
        ``READY WITH WARNINGS`` with any ``WARN``, else ``READY``. Unrequested
        and inapplicable checks do not block readiness.
    """
    statuses = {result.status for result in results}
    if FAIL in statuses or any(
        r.status == SKIP and r.required_for_ready for r in results
    ):
        return NOT_READY
    if WARN in statuses:
        return READY_WITH_WARNINGS
    return READY


def run_tier_check(request: TierCheckRequest, host: Host) -> dict[str, object]:
    """Run every check and return the result document.

    Args:
        request: What to check.
        host: Host view.

    Returns:
        Mapping with ``tier_check_version``, ``tier_mount``, ``arm``, ``device``
        (``key``, ``scope``, ``disk``), ``checks`` (one mapping per check in
        T1–T12 order), ``counts`` (per status) and ``overall``.

    Raises:
        ValueError: When ``request.arm`` is not a known arm.
    """
    if request.arm not in ARMS:
        raise ValueError(
            f"unknown arm {request.arm!r}; expected one of {', '.join(ARMS)}"
        )
    device = resolve_path_device(host, request.tier_mount)
    results = [
        check_device(host, request, device),
        check_dedicated_disk(host, request, device),
        check_device_class(host, device),
        check_native_multipath(host, device),
        check_stacking(host, device),
        check_cgroup_io(host),
        check_filesystem(host, request),
        check_compat_mode(host, request),
        check_nvidia_fs(host, request),
        check_container_prerequisites(host, request),
        check_staging_pool(request),
        check_tier_directory(host, request),
    ]
    return {
        "tier_check_version": "0.1.0",
        "tier_mount": request.tier_mount,
        "arm": request.arm,
        "device": {"key": device.key, "scope": device.scope, "disk": device.disk},
        "checks": [result.as_dict() for result in results],
        "counts": {
            status: sum(r.status == status for r in results) for status in STATUSES
        },
        "overall": overall_status(results),
    }


def render_tier_check(document: dict[str, object]) -> str:
    """Render a result document for an operator.

    Args:
        document: Output of :func:`run_tier_check`.

    Returns:
        Text with one line per check, *Why* and *Fix* lines under each
        ``WARN``, ``FAIL`` and required ``SKIP``, and the overall line.

    Raises:
        ValueError: When ``document`` does not have the result shape.
    """
    device = document["device"]
    checks = document["checks"]
    counts = document["counts"]
    if (
        not isinstance(device, dict)
        or not isinstance(checks, list)
        or not isinstance(counts, dict)
    ):
        raise ValueError("not a tier-check result document")
    scope = device["scope"]
    lines = [
        f"Tier check: {document['tier_mount']} (arm: {document['arm']})",
        f"Counted device: {device['key'] or 'none'}"
        + (f" ({device['disk']}, {scope})" if device["key"] else f" ({scope})"),
        "",
    ]
    for check in checks:
        lines.append(
            f"  {check['status']:<4}  {check['id']:<3}  {check['title']}: {check['finding']}"
        )
        if check["status"] in (WARN, FAIL) or (
            check["status"] == SKIP and check["required_for_ready"]
        ):
            lines.append(f"              Why: {check['why']}")
            lines.append(f"              Fix: {check['fix']}")
    skipped = counts.get(SKIP, 0)
    unchecked = f" ({skipped} not checked)" if skipped else ""
    lines += [
        "",
        f"Overall: {document['overall']}{unchecked}",
        "READY means the required checks passed; optional or inapplicable checks may be skipped. "
        "It is not Gate 1 evidence; only the run's per-request counters are.",
    ]
    return "\n".join(lines)
