# SPDX-License-Identifier: Apache-2.0
"""Read-only NVMe trace preflight; never attach probes or claim readiness."""

# Standard
import hashlib
import platform
import re
import shutil
import subprocess
from pathlib import Path

TRACE_ROOTS = (Path("/sys/kernel/tracing"), Path("/sys/kernel/debug/tracing"))


class TracePlanError(ValueError):
    """Raised for an invalid namespace argument, not an unavailable host."""


def _read(path: Path) -> dict:
    """Read a bounded capability file and preserve missing/permission distinctions."""
    try:
        with path.open(encoding="utf-8") as stream:
            content = stream.read(65537)
        if len(content.encode("utf-8")) > 65536:
            return {"status": "invalid", "detail": "file exceeds 64 KiB"}
        return {"status": "available", "value": content.strip()}
    except PermissionError:
        return {"status": "permission_denied"}
    except FileNotFoundError:
        return {"status": "not_found"}
    except (OSError, UnicodeError):
        return {"status": "unreadable"}


def _number(path: Path) -> dict:
    """Read a positive integer without supplying a default."""
    result = _read(path)
    if result["status"] == "available":
        value = result["value"]
        if not re.fullmatch(r"[0-9]{1,20}", value) or int(value) < 1:
            return {"status": "invalid"}
        result["value"] = int(value)
    return result


def _format(path: Path, name: str) -> dict:
    """Check the explicit byte-array tracepoint profile intended for the collector."""
    result = _read(path)
    if result["status"] != "available":
        return {"path": str(path), **result}
    content = result["value"]
    fields = {}
    for declaration, offset, size, signed in re.findall(
        r"field:\s*([^;]+);\s*offset:\s*(\d+);\s*size:\s*(\d+);\s*signed:\s*(\d+);",
        content,
    ):
        field = re.search(r"\b(\w+)(?:\[(\d+)\])?$", declaration.strip())
        if field:
            fields[field[1]] = {
                "declaration": declaration.strip(),
                "offset": int(offset),
                "size": int(size),
                "signed": int(signed),
                "array": field[2],
            }
    required = {
        "ctrl_id": (4, 1),
        "qid": (4, 1),
        "cid": (2, 0) if name == "nvme_setup_cmd" else (4, 1),
    }
    required.update(
        {"opcode": (1, 0)} if name == "nvme_setup_cmd" else {"status": (2, 0)}
    )
    incompatible = []
    for field, (size, signed) in required.items():
        actual = fields.get(field, {})
        if (
            actual.get("size") != size
            or actual.get("signed") != signed
            or actual.get("array")
        ):
            incompatible.append(field)
    disk = fields.get("disk", {})
    if disk.get("size") != 32 or disk.get("declaration") != "char disk[32]":
        incompatible.append("disk (requires fixed char[32])")
    if name == "nvme_setup_cmd":
        command = fields.get("cdw10", {})
        if command.get("size") != 24 or not re.fullmatch(
            r"(?:u8|__u8|unsigned char) cdw10\[24\]", command.get("declaration", "")
        ):
            incompatible.append("cdw10 (requires 24-byte u8 array)")
    return {
        "path": str(path),
        "status": "unsupported" if incompatible else "compatible",
        "profile": "nvme-byte-array-v1",
        "incompatible_fields": incompatible,
        "format_sha256": hashlib.sha256(content.encode()).hexdigest(),
    }


def _tracepoints(roots: tuple[Path, ...]) -> dict:
    """Select a single tracefs root, retaining diagnostics for every attempted root."""
    attempts = []
    for root in roots:
        formats = {
            name: _format(root / "events/nvme" / name / "format", name)
            for name in ("nvme_setup_cmd", "nvme_complete_rq")
        }
        attempts.append({"root": str(root), "formats": formats})
        if all(value["status"] == "compatible" for value in formats.values()):
            return {
                "status": "compatible",
                "selected_root": str(root),
                "attempts": attempts,
            }
    statuses = {
        value["status"] for attempt in attempts for value in attempt["formats"].values()
    }
    status = (
        "unsupported"
        if "unsupported" in statuses
        else ("permission_denied" if "permission_denied" in statuses else "unavailable")
    )
    return {"status": status, "selected_root": None, "attempts": attempts}


def _tool() -> dict:
    """Inspect bpftrace's version only, with bounded subprocess time."""
    executable = shutil.which("bpftrace")
    if executable is None:
        return {"status": "not_found"}
    try:
        result = subprocess.run(
            [executable, "--version"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired, UnicodeError):
        return {"status": "unavailable", "executable": executable}
    # Never interpolate tool output into a command. Version alone is not a
    # validation of probe privileges, compilation or monotonic-clock semantics.
    return {
        "status": "available" if result.returncode == 0 else "unavailable",
        "executable": executable,
        "version_output": result.stdout.strip()[:256],
        "version_exit_code": result.returncode,
    }


def _mounts(sys_root: Path, proc_root: Path, namespace: str, device: dict) -> dict:
    """Match namespace/partition major:minor within this process's mount namespace."""
    table = _read(proc_root / "self/mountinfo")
    if table["status"] != "available":
        return {"status": table["status"], "entries": []}
    devices = {device.get("value")} if device["status"] == "available" else set()
    scan_status = "available"
    try:
        for index, child in enumerate((sys_root / "class/block" / namespace).iterdir()):
            if index >= 1024:
                scan_status = "incomplete"
                break
            if re.fullmatch(re.escape(namespace) + r"p[0-9]+", child.name):
                partition = _read(child / "dev")
                if partition["status"] == "available":
                    devices.add(partition["value"])
                else:
                    scan_status = "incomplete"
    except OSError:
        scan_status = "incomplete"
    entries = []
    for line in table["value"].splitlines():
        parts = line.split()
        try:
            separator = parts.index("-")
            major_minor, mountpoint, filesystem = (
                parts[2],
                parts[4],
                parts[separator + 1],
            )
        except (ValueError, IndexError):
            scan_status = "incomplete"
            continue
        if major_minor in devices:
            decoded = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), mountpoint)
            entries.append(
                {
                    "major_minor": major_minor,
                    "mountpoint": decoded,
                    "filesystem": filesystem,
                }
            )
    return {"status": scan_status, "entries": entries, "scope": "self_mount_namespace"}


def build_trace_plan(
    disk: str,
    *,
    sys_root: Path = Path("/sys"),
    proc_root: Path = Path("/proc"),
    trace_roots: tuple[Path, ...] = TRACE_ROOTS,
) -> dict:
    """Inspect local NVMe trace prerequisites without privilege escalation.

    Args:
        disk: Whole namespace name or literal /dev/nvmeNnM path. Partitions,
            aliases, NVMe multipath names and shell expressions are refused.
        sys_root: Sysfs root; alternate roots support synthetic tests only.
        proc_root: Procfs root; alternate roots support synthetic tests only.
        trace_roots: Candidate tracefs roots inspected in order.

    Returns:
        Versioned read-only preflight with eligible/incomplete/unsupported status.
        Eligible means inspected prerequisites only, not live-capture readiness.
        capture_command is always null until a validated collector is shipped.

    Raises:
        TracePlanError: If the namespace argument is not a literal whole name.
    """
    namespace = disk.removeprefix("/dev/")
    match = re.fullmatch(r"nvme([0-9]+)n([1-9][0-9]*)", namespace)
    if match is None or len(namespace) >= 32:
        raise TracePlanError(
            "--disk requires nvmeNnM or /dev/nvmeNnM (whole namespace)"
        )
    host_system = platform.system()
    result = {
        "trace_plan_version": "0.1.0",
        "system": host_system,
        "kernel_release": platform.release(),
        "architecture": platform.machine(),
        "namespace": namespace,
        "device_path": f"/dev/{namespace}",
        "preflight_status": "unsupported",
        "reasons": [],
        "capture_status": "experimental_unvalidated",
        "capture_command": None,
        "serving_verification": "not_assessed",
        "limitations": [
            "No sudo, probe attachment, cache mutation "
            "or serving process control is performed.",
            "Eligible is preflight only, not a capture-ready signal or Gate 1 PASS.",
            "Mounts are observed in this process's namespace; "
            "emptiness does not prove exclusive use.",
            "Bpftrace version does not verify privilege, "
            "compilation or clock semantics.",
            "Controller names are inventory; "
            "trace controller_id must be observed, not inferred.",
        ],
    }
    if host_system != "Linux":
        result["reasons"] = ["linux_required"]
        return result
    block = sys_root / "class/block" / namespace
    identity = _read(block / "dev")
    if identity["status"] == "available" and not re.fullmatch(
        r"[0-9]+:[0-9]+", identity["value"]
    ):
        identity = {"status": "invalid"}
    lbs = _number(block / "queue/logical_block_size")
    if lbs["status"] == "available" and lbs["value"] & (lbs["value"] - 1):
        lbs = {"status": "invalid"}
    controller = f"nvme{match[1]}"
    transport = _read(sys_root / "class/nvme" / controller / "transport")
    formats = _tracepoints(trace_roots)
    tool = _tool()
    mounts = _mounts(sys_root, proc_root, namespace, identity)
    result.update(
        device_identity=identity,
        controller_name=controller,
        transport=transport,
        logical_block_size=lbs,
        queue_limits={
            name: _number(block / "queue" / name)
            for name in ("max_sectors_kb", "max_hw_sectors_kb")
        },
        tracepoints=formats,
        bpftrace=tool,
        mounts=mounts,
    )
    checks = {
        "namespace_identity": identity["status"] == "available",
        "logical_block_size": lbs["status"] == "available",
        "transport": transport["status"] == "available",
        "tracepoints": formats["status"] == "compatible",
        "bpftrace": tool["status"] == "available",
        "mount_inventory": mounts["status"] == "available",
    }
    reasons = [
        f"{name}_unverified" for name, available in checks.items() if not available
    ]
    unsupported = formats["status"] == "unsupported"
    if transport["status"] == "available" and transport["value"] != "pcie":
        reasons.append("only_local_pcie_transport_supported")
        unsupported = True
    result["reasons"] = reasons
    result["preflight_status"] = (
        "unsupported" if unsupported else ("incomplete" if reasons else "eligible")
    )
    return result


def render_trace_plan(plan: dict) -> str:
    """Render a preflight summary with capability blockers and no capture claim.

    Args:
        plan: Result returned by build_trace_plan.

    Returns:
        English operator summary. A capture command is intentionally not printed.
    """
    lines = [
        "Gate 1 — NVMe capture preflight (experimental)",
        f"Namespace: {plan['namespace']}",
        f"Preflight: {plan['preflight_status']}",
    ]
    if "logical_block_size" in plan:
        lbs = plan["logical_block_size"]
        lines.append(f"Logical block size: {lbs.get('value', lbs['status'])}")
    lines.extend(f"  Review: {reason}" for reason in plan["reasons"])
    lines.extend(
        [
            "Live collector: experimental, Linux acceptance pending; "
            "no executable capture command is emitted.",
            "Next: retain preflight JSON, resolve blockers, "
            "then validate the collector on Linux.",
            "Serving verification: not assessed. "
            "Preflight is not capture readiness or a Gate 1 PASS.",
        ]
    )
    return "\n".join(lines)
