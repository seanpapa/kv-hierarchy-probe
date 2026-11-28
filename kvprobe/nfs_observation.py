# SPDX-License-Identifier: Apache-2.0
"""Replay raw NFS/RDMA observations without producing serving verdicts."""

# Standard
from dataclasses import dataclass
import re

BYTE_FIELDS = (
    "normal_read_bytes",
    "normal_write_bytes",
    "direct_read_bytes",
    "direct_write_bytes",
    "server_read_payload_bytes",
    "server_write_payload_bytes",
    "read_pages",
    "write_pages",
)
RPC_FIELDS = (
    "read_rpc_operations",
    "read_rpc_transmissions",
    "read_rpc_timeouts",
    "read_rpc_sent_bytes",
    "read_rpc_received_bytes",
    "read_rpc_queue_ms",
    "read_rpc_rtt_ms",
    "read_rpc_execute_ms",
    "read_rpc_errors",
)


class NfsObservationError(ValueError):
    """The selected mount or counter interval cannot be interpreted safely."""


def _unescape(value: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), value)


def _numbers(values: list[str], count: int, label: str) -> tuple[int, ...]:
    if len(values) != count or any(not re.fullmatch(r"[0-9]+", v) for v in values):
        raise NfsObservationError(
            f"unsupported or malformed {label}: expected {count} counters"
        )
    return tuple(int(v) for v in values)


@dataclass(frozen=True)
class MountCounters:
    """One NFS statvers=1.1 block, with payload and RPC counters kept separate."""

    export: str
    mountpoint: str
    fstype: str
    options: tuple[str, ...]
    transports: tuple[str, ...]
    age_s: int
    byte_counts: tuple[int, ...]
    read_rpc_counts: tuple[int, ...]


def parse_mountstats(text: str, mountpoint: str) -> MountCounters:
    """Parse exactly one NFS mount from Linux mountstats text.

    Args:
        text: Unmodified /proc/self/mountstats contents.
        mountpoint: Exact decoded mountpoint, never a substring or parent path.

    Returns:
        Counters from the selected statvers=1.1 NFS mount.

    Raises:
        NfsObservationError: Missing/duplicate mount or fields, unsupported layout,
            or an unverified RDMA transport.

    Notes:
        Byte positions follow Linux nfs_iostat.h. READ RPC receive bytes include
        protocol overhead and are not substituted for NFSIOS_SERVERREADBYTES.
    """
    header = re.compile(
        r"^device (.+) mounted on (.+) with fstype (\S+)(?: statvers=(\S+))?$"
    )
    blocks: list[tuple[re.Match[str], list[str]]] = []
    current: tuple[re.Match[str], list[str]] | None = None
    for line in text.splitlines():
        if line.startswith("device "):
            match = header.fullmatch(line)
            current = None
            if match and _unescape(match[2]) == mountpoint:
                current = (match, [])
                blocks.append(current)
        elif current is not None:
            current[1].append(line.strip())
    if len(blocks) != 1:
        raise NfsObservationError(
            f"expected one exact mount block, found {len(blocks)}"
        )
    match, lines = blocks[0]
    if match[3] not in {"nfs", "nfs4"} or match[4] != "1.1":
        raise NfsObservationError("only NFS statvers=1.1 is supported")

    def one(label: str) -> list[str]:
        values = [line.split()[1:] for line in lines if line.split()[:1] == [label]]
        if len(values) != 1:
            raise NfsObservationError(f"expected one {label} field")
        return values[0]

    opts = one("opts:")
    if len(opts) != 1:
        raise NfsObservationError("malformed NFS options")
    options = tuple(sorted(opts[0].split(",")))
    transport_rows = [line.split() for line in lines if line.startswith("xprt:")]
    if any(len(row) < 2 for row in transport_rows):
        raise NfsObservationError("malformed transport")
    transports = tuple(row[1] for row in transport_rows)
    if "proto=rdma" not in options or not transports or set(transports) != {"rdma"}:
        raise NfsObservationError("both mount options and transport must identify RDMA")
    return MountCounters(
        _unescape(match[1]),
        mountpoint,
        match[3],
        options,
        transports,
        _numbers(one("age:"), 1, "age")[0],
        _numbers(one("bytes:"), 8, "bytes"),
        _numbers(one("READ:"), 9, "READ RPC"),
    )


def parse_mountinfo(text: str, mountpoint: str) -> dict[str, str]:
    """Select one exact NFS mountinfo record or raise NfsObservationError.

    Args:
        text: Raw /proc/self/mountinfo contents.
        mountpoint: Exact decoded mountpoint.

    Returns:
        Mount ID, filesystem device number, root and filesystem type.

    Raises:
        NfsObservationError: Missing, ambiguous, malformed or non-NFS identity.
    """
    matches = []
    for line in text.splitlines():
        fields = line.split()
        if len(fields) >= 6 and _unescape(fields[4]) == mountpoint:
            try:
                separator = fields.index("-")
                fstype = fields[separator + 1]
                source = fields[separator + 2]
            except (ValueError, IndexError) as exc:
                raise NfsObservationError("malformed mountinfo") from exc
            if fstype not in {"nfs", "nfs4"}:
                raise NfsObservationError("selected mountinfo entry is not NFS")
            matches.append(
                {
                    "mount_id": fields[0],
                    "major_minor": fields[2],
                    "root": _unescape(fields[3]),
                    "fstype": fstype,
                    "mount_source": _unescape(source),
                }
            )
    if len(matches) != 1:
        raise NfsObservationError("missing or ambiguous mountinfo identity")
    return matches[0]


def counter_delta(before: MountCounters, after: MountCounters) -> dict[str, int]:
    """Recompute counter differences for one stable NFS/RDMA filesystem.

    Args:
        before: Parsed counters before an operation.
        after: Parsed counters after an operation.

    Returns:
        Deltas with server payload and RPC receive bytes kept distinct.

    Raises:
        NfsObservationError: Identity/options change, mount age reset or counter reset.

    Notes:
        This validates filesystem counters only. Namespace, clock and operation
        provenance require the bundle reader; counts alone do not verify serving.
    """
    if (
        before.export,
        before.mountpoint,
        before.fstype,
        before.options,
        before.transports,
    ) != (
        after.export,
        after.mountpoint,
        after.fstype,
        after.options,
        after.transports,
    ):
        raise NfsObservationError("mount identity or transport changed")
    if after.age_s < before.age_s:
        raise NfsObservationError("mount age moved backwards")
    values = [
        b - a
        for a, b in zip(
            before.byte_counts + before.read_rpc_counts,
            after.byte_counts + after.read_rpc_counts,
            strict=True,
        )
    ]
    if any(value < 0 for value in values):
        raise NfsObservationError("counter reset or backwards counter")
    return dict(zip(BYTE_FIELDS + RPC_FIELDS, values, strict=True))
