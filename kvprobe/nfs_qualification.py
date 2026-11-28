# SPDX-License-Identifier: Apache-2.0
"""Derive reference source/file/namespace facts from retained originals only."""

# Standard
import ast
from hashlib import sha256
import json
from pathlib import Path
from typing import Any


class QualificationError(ValueError):
    """Raised when retained reference records are malformed or inconsistent."""


def read_json(root: Path, name: str) -> dict[str, Any]:
    """Read a contained, finite JSON object, rejecting duplicate keys.

    Args:
        root: Extracted local artifact directory.
        name: Relative artifact path, never a replay-host absolute path.

    Returns:
        Parsed object; no live process or measured mount is consulted.

    Raises:
        QualificationError: Escaping paths, malformed/duplicate/nonfinite JSON.
        OSError: Missing/unreadable original.
    """
    path = contained(root, name)

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in items:
            if key in value:
                raise QualificationError("duplicate original JSON key")
            value[key] = item
        return value

    def constant(value: str) -> None:
        raise QualificationError("nonfinite original JSON")

    try:
        value = json.loads(
            path.read_bytes(), object_pairs_hook=pairs, parse_constant=constant
        )
    except (ValueError, UnicodeError) as exc:
        raise QualificationError("malformed original JSON") from exc
    if not isinstance(value, dict):
        raise QualificationError("original JSON must be an object")
    return value


def contained(root: Path, name: str) -> Path:
    """Resolve one original under its bundle; never follow an external alias.

    Args:
        root: Bundle root.
        name: Nonempty canonical relative path.

    Returns:
        Existing contained regular file.

    Raises:
        QualificationError: Noncanonical/escaping/special-file path.
        OSError: Original unavailable.
    """
    if (
        not isinstance(name, str)
        or not name
        or Path(name).is_absolute()
        or "\\" in name
        or any(p in {"", ".", ".."} for p in name.split("/"))
    ):
        raise QualificationError("noncanonical original path")
    path = (root / name).resolve(strict=True)
    if not path.is_relative_to(root.resolve()) or not path.is_file():
        raise QualificationError("original escapes its bundle or is not a file")
    return path


# Fingerprints of AST method bodies, not a host/build fingerprint. The recognized
# layout opens a fresh headerless file, offset zero, with one buffer read/write.
# Formatting, docstrings, argument annotations and NVTX decoration are excluded;
# executable statements (including exception handling/logging) remain in the hash.
_LAYOUT_METHODS: dict[str, str] = {
    "_key_to_path": "5763a70ed70910c1e4b626c0c81792dec267ac0548a101b722dfbc70c27f8dd8",
    "write_file": "33e970db12412160d90df8901605d316fed6a18a63540ddf7397ca8d283f2cd3",
    "read_file": "f912ad291bc0499b5ec5279db3abbabaa444bada9929a90d49ccf5814d5ab27d",
}


def _ast_identity(node: Any) -> Any:
    if isinstance(node, ast.AST):
        return [
            type(node).__name__,
            {
                key: _ast_identity(value)
                for key, value in ast.iter_fields(node)
                if key not in {"type_params", "type_comment", "type_ignores", "kind"}
            },
        ]
    if isinstance(node, list):
        return [_ast_identity(value) for value in node]
    return node


def recognized_layout(source: bytes) -> bool:
    """Recognize the reviewed LocalDisk raw-layout method shapes without execution.

    Args:
        source: Loaded disk-backend module original, hash-bound to its manifest.

    Returns:
        True only when all three canonical method bodies match. Unknown layouts
        remain unqualified, rather than widening byte tolerance or running code.
    """
    try:
        tree = ast.parse(source)
    except (SyntaxError, UnicodeError):
        return False
    classes = [
        n
        for n in tree.body
        if isinstance(n, ast.ClassDef) and n.name == "LocalDiskBackend"
    ]
    if len(classes) != 1:
        return False
    found = {}
    for node in classes[0].body:
        if not isinstance(node, ast.FunctionDef) or node.name not in _LAYOUT_METHODS:
            continue
        if node.name in found:
            return False
        if any(
            not isinstance(d, ast.Name) or d.id != "_lmcache_nvtx_annotate"
            for d in node.decorator_list
        ):
            return False
        node.decorator_list = []
        if (
            node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)
        ):
            node.body.pop(0)
        for arg in node.args.args:
            arg.annotation = None
        node.returns = None
        found[node.name] = sha256(
            json.dumps(
                _ast_identity(node), sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()
    return found == _LAYOUT_METHODS


def namespace_facts(run: Path, client: Path, replay: dict[str, Any]) -> str:
    """Derive a bounded trusted namespace connection from raw before/after records.

    Args:
        run: Full extracted reference run.
        client: Candidate's retained client originals.
        replay: Independently replayed reference client observation.

    Returns:
        bounded, shared, mismatch, or unknown. Missing files propagate to the
        adapter, which records unavailable evidence. Privileged external setns
        and hostile writers are outside this controlled-producer contract.

    Raises:
        QualificationError: Malformed original record.
        OSError: Original unavailable.
    """
    before, after = [
        read_json(run, name) for name in ("scope-before.json", "scope-after.json")
    ]
    frozen = read_json(client, "frozen.json")
    request = read_json(client, "retrieve.json")["interval_ns"]
    outer = contained(run, "outer-mount-namespace.txt").read_text().strip()
    binding = replay["path_bindings"]["binding-before"]
    namespace = binding["process_identity"]["mount_namespace"]
    dev = binding["path_observations"]["before"]["observer"]["metadata"]["major_minor"]
    owner = before["owner_pid"]
    if type(owner) is not int or owner <= 0:
        raise QualificationError("malformed namespace owner identity")
    if (
        before["mount_namespace"] != namespace
        or after["mount_namespace"] != namespace
        or before["boot_id"] != frozen["boot_id"]
        or after["boot_id"] != frozen["boot_id"]
        or any(x["time_namespace"] != frozen["time_namespace"] for x in (before, after))
    ):
        return "mismatch"
    if namespace == outer:
        return "shared"
    if (
        before["interval_ns"][1] >= request[0]
        or request[1] >= after["interval_ns"][0]
        or any(
            x["clock"] != "CLOCK_MONOTONIC" or x["owner_pid"] != owner
            for x in (before, after)
        )
    ):
        return "mismatch"
    for frame in (before, after):
        if frame.get("scope_inventory_version") != "0.1.0-experimental":
            raise QualificationError(
                "malformed or unsupported namespace inventory version"
            )
        if (
            not isinstance(frame["unresolved_processes"], list)
            or not isinstance(frame["members"], list)
            or not isinstance(frame["mountinfo"], str)
        ):
            raise QualificationError("malformed namespace inventory")
        if frame["unresolved_processes"]:
            return "unknown"
        parents = {}
        for member in frame["members"]:
            text = member["stat"]
            opening, closing = text.index("("), text.rindex(")")
            pid = int(text[:opening])
            if type(member["pid"]) is not int or pid != member["pid"] or pid in parents:
                raise QualificationError("inconsistent process inventory")
            parents[pid] = int(text[closing + 1 :].split()[1])
        if owner not in parents:
            return "unknown"
        for pid in parents:
            seen = set()
            while pid != owner:
                if pid not in parents or pid in seen:
                    return "shared"
                seen.add(pid)
                pid = parents[pid]
        rows = [line.split() for line in frame["mountinfo"].splitlines()]
        if not rows:
            return "unknown"
        if sum(row[2] == dev for row in rows) != 1:
            return "shared"
        for row in rows:
            split = row.index("-")
            if any(
                x.startswith(("shared:", "master:", "propagate_from:"))
                for x in row[6:split]
            ):
                return "shared"
    return "bounded"


def layout_facts(client: Path, replay: dict[str, Any]) -> dict[str, Any]:
    """Recompute loaded-source, key/file/range and physical KV byte comparisons.

    Args:
        client: Candidate client bundle with I/O supplement originals.
        replay: Replay result binding actual client/input/runtime/counters.

    Returns:
        source_binding, payload_mapping and expected_payload_bytes, plus an
        independent KV correctness comparison. Unknown source/layout never passes.

    Raises:
        QualificationError: Source/hash/key/process/timeline contradictions.
        OSError: Required original unavailable.
    """
    frozen = read_json(client, "frozen.json")
    root = client / "io"
    inventory = read_json(root, "implementation.json")
    canonical = json.dumps(inventory, sort_keys=True, separators=(",", ":")).encode()
    if sha256(canonical).hexdigest() != frozen.get("io_implementation_sha256"):
        raise QualificationError("source inventory differs from frozen identity")
    disk_source = None
    for name, entry in inventory.items():
        data = contained(root, entry["path"]).read_bytes()
        if sha256(data).hexdigest() != entry["sha256"]:
            raise QualificationError("loaded source hash mismatch")
        if name.endswith(".storage_backend.local_disk_backend"):
            disk_source = data
    if disk_source is None:
        raise QualificationError("disk implementation original missing")
    helper = contained(client, "io_receipts.py.source").read_bytes()
    if sha256(helper).hexdigest() != frozen.get("io_receipts_source_sha256"):
        raise QualificationError("I/O producer source hash mismatch")
    config = read_json(client / "serving", "configuration.json")
    identity = {
        "packages": read_json(client, "stack.json"),
        "driver": frozen["source_sha256"],
        "copy": frozen["copy_source_sha256"],
        "cleanup": frozen.get("cleanup_source_sha256"),
        "cleanup_enabled": frozen.get("reference_cleanup_override", False),
        "io_receipts": frozen["io_receipts_source_sha256"],
        "io_implementation": frozen["io_implementation_sha256"],
    }
    digest = sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    if config["runtime"]["image_digest"] != "reference-source-sha256:" + digest:
        raise QualificationError("reference source-build identity mismatch")
    source_match = recognized_layout(disk_source)
    calls = [
        (p.name, read_json(root, p.name)) for p in sorted(root.glob("*-call.json"))
    ]
    reads = [(name, row) for name, row in calls if row["operation"] == "read"]
    writes = [(name, row) for name, row in calls if row["operation"] == "write"]
    result = {
        "source_binding": "matched" if source_match else "unrecognized",
        "payload_mapping": "unknown",
        "expected_payload_bytes": None,
        "kv_correct": replay["kv_originals_equal"],
    }
    if len(reads) != 1 or len(writes) != 1:
        return result
    read_name, read = reads[0]
    write_name, write = writes[0]
    actual = read_json(client, "actual-runtime.json")["lmcache"]
    if (
        read["path"] != write["path"]
        or Path(read["path"]).parent != Path(actual["local_disk"])
        or not isinstance(read["key"], str)
        or Path(read["path"]).name != read["key"].replace("/", "-") + ".pt"
    ):
        raise QualificationError("key/file/runtime path mismatch")
    request = read_json(client, "retrieve.json")["interval_ns"]
    if not request[0] <= read["interval_ns"][0] < read["interval_ns"][1] <= request[1]:
        raise QualificationError("backend read lies outside the request")
    for _, row in calls:
        if (
            row["pid"] != frozen["pid"]
            or row["offset"] != 0
            or row["syscall_return_bytes"] is not None
        ):
            raise QualificationError("unsupported I/O receipt binding")
        if row["call_outcome"] != "returned":
            return result
    snapshots = [
        read_json(root, name + "-files.json")
        for name in ("before-request", "after-request")
    ]
    if any(len(x["files"]) != 1 for x in snapshots):
        return result
    a, b = (x["files"][0] for x in snapshots)
    phases = read_json(client / "serving", "serving-manifest.json")["phases"]
    first = read_json(client / "serving", phases["quiet_before"]["before"]["path"])
    last = read_json(client / "serving", phases["quiet_after"]["after"]["path"])
    if not (
        snapshots[0]["interval_ns"][1] < first["capture_interval_ns"][0]
        and last["capture_interval_ns"][1] < snapshots[1]["interval_ns"][0]
    ):
        raise QualificationError("file inspection overlaps captured controls")
    stable = all(
        a[k] == b[k]
        for k in ("path", "bytes", "sha256", "inode", "device", "mtime_ns", "ctime_ns")
    )
    if not stable:
        result["payload_mapping"] = "unstable"
        return result
    dev = replay["path_bindings"]["binding-before"]["path_observations"]["before"][
        "observer"
    ]["metadata"]["major_minor"]
    if a["path"] != read["path"] or a["device"] != dev:
        raise QualificationError(
            "file does not belong to the target counter filesystem"
        )
    expected = replay["nfs_observations"]["expected_kv_bytes"]
    originals = []
    for entry in (a, b):
        data = contained(root, entry["artifact"]).read_bytes()
        if len(data) != entry["bytes"] or sha256(data).hexdigest() != entry["sha256"]:
            raise QualificationError("file original differs from receipt")
        originals.append(data)
    for name, row in (reads[0], writes[0]):
        buffer_name = name.replace("-call.json", "-" + row["operation"] + "-buffer.bin")
        data = contained(root, buffer_name).read_bytes()
        if sha256(data).hexdigest() != row["buffer_sha256"]:
            raise QualificationError("I/O buffer original differs from receipt")
        originals.append(data)
    for name in ("store_gpu", "store_cpu", "load_cpu", "load_gpu"):
        originals.append(contained(client, f"proof/{name}-0-32-call1.bin").read_bytes())
    result["kv_correct"] = all(data == originals[0] for data in originals)
    if (
        source_match
        and all(len(data) == expected for data in originals)
        and read["requested_bytes"] == write["requested_bytes"] == expected
    ):
        result["payload_mapping"] = "identity"
        result["expected_payload_bytes"] = expected
    return result


def reference_client_binding(client: Path, configuration: dict[str, Any]) -> bool:
    """Bind this narrow producer's actual input/cache/device facts to configuration.

    Args:
        client: Retained reference originals.
        configuration: Same captured frozen configuration, already validated.

    Returns:
        True after the reference schedule, disabled upper cache and GPU process
        inventory agree. It does not authenticate execution or arbitrary sources.

    Raises:
        QualificationError: Explicit schedule/cache/device contradiction.
        OSError: Required original unavailable.
    """
    frozen = read_json(client, "frozen.json")
    prompt = read_json(client, "input.bin")["prompt_token_ids"]
    schedule = configuration["measurement"]
    if (
        schedule
        != {"contexts": [len(prompt)], "concurrency": [1], "samples": 1, "warmup": 0}
        or configuration["cache"]["upper_tier_capacity_bytes"] != 0
    ):
        raise QualificationError("reference schedule/cache contradicts actual request")
    inventory = read_json(client, "device.json")
    if inventory["exit_code"] != 0:
        raise QualificationError("device inventory failed")
    rows = [
        line.split(",") for line in inventory["stdout"].splitlines() if line.strip()
    ]
    target = configuration["runtime"]
    selected = [row for row in rows if int(row[0].strip()) == target["gpu_index"]]
    if len(selected) != 1 or len(selected[0]) < 3:
        raise QualificationError("selected device is unavailable or ambiguous")
    gpu = selected[0]
    if gpu[2].strip() != target["device_id"]:
        raise QualificationError("configured GPU differs from actual device")
    for name in ("pre-request.gpu.json", "after-request.gpu.json"):
        observed = read_json(client, name)
        if observed["exit_code"] != 0:
            raise QualificationError("GPU process inventory failed")
        entries = [
            row.split(",") for row in observed["stdout"].splitlines() if row.strip()
        ]
        matches = [row for row in entries if int(row[0].strip()) == frozen["pid"]]
        if (
            len(matches) != 1
            or len(matches[0]) != 2
            or matches[0][1].strip() != gpu[1].strip()
        ):
            raise QualificationError("reference process/device binding differs")
    return True
