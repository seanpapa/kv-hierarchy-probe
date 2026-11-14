# SPDX-License-Identifier: Apache-2.0
"""Conservative, dependency-free host capability discovery."""

# Standard
from collections import Counter
import importlib.util
import os
import platform
import shutil
import subprocess
from pathlib import Path
from typing import Any


def find_gdscheck() -> str | None:
    """Find ``gdscheck`` on PATH or in common CUDA toolkit locations.

    Returns:
        Absolute executable path, or ``None`` when it cannot be found.
    """
    path = shutil.which("gdscheck")
    if path:
        return path
    candidates = [Path("/usr/local/cuda/gds/tools/gdscheck")]
    candidates.extend(
        sorted(
            Path("/usr/local").glob("cuda-*/gds/tools/gdscheck"),
            reverse=True,
        )
    )
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def _run(command: list[str]) -> tuple[int, str]:
    """Run a bounded read-only capability command.

    Args:
        command: Executable and arguments.

    Returns:
        Return code and combined output. A missing executable returns 127.
    """
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            check=False,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, str(exc)
    output = "\n".join(part for part in (result.stdout, result.stderr) if part)
    return result.returncode, output.strip()


def _package_status(name: str) -> dict[str, str]:
    """Describe whether a Python package can be imported.

    Args:
        name: Importable package name.

    Returns:
        Capability status and a short detail.
    """
    try:
        found = importlib.util.find_spec(name) is not None
    except (ImportError, ModuleNotFoundError, ValueError):
        found = False
    return {
        "status": "available" if found else "not_detected",
        "detail": f"Python package {name!r} {'is' if found else 'is not'} importable",
    }


def _gpu_capability() -> tuple[dict[str, Any], dict[str, str]]:
    """Detect an NVIDIA GPU separately from optional memory telemetry.

    Returns:
        GPU identity data and memory-usage query capability.
    """
    if shutil.which("nvidia-smi") is None:
        return (
            {"present": False, "names": [], "status": "not_detected"},
            {"status": "not_applicable", "detail": "no NVIDIA GPU detected"},
        )

    identity_code, identity_output = _run(
        [
            "nvidia-smi",
            "--query-gpu=name",
            "--format=csv,noheader",
        ]
    )
    if identity_code != 0:
        return (
            {
                "present": False,
                "names": [],
                "status": "unavailable",
                "detail": identity_output,
            },
            {"status": "unknown", "detail": "GPU identity query failed"},
        )

    names = [line.strip() for line in identity_output.splitlines() if line.strip()]
    memory_code, memory_output = _run(
        [
            "nvidia-smi",
            "--query-gpu=memory.used",
            "--format=csv,noheader,nounits",
        ]
    )
    memory_supported = memory_code == 0 and "Not Supported" not in memory_output
    memory_capability = {
        "status": "available" if memory_supported else "unsupported",
        "detail": (
            "nvidia-smi memory.used returned numeric telemetry"
            if memory_supported
            else "nvidia-smi is present but memory.used is not available"
        ),
    }
    return (
        {"present": bool(names), "names": names, "status": "available"},
        memory_capability,
    )


def _memory_architecture(gpu_names: list[str]) -> dict[str, str]:
    """Classify only memory architectures that can be identified safely.

    Args:
        gpu_names: Names reported by ``nvidia-smi``.

    Returns:
        Architecture label, confidence, and classification basis.
    """
    if not gpu_names:
        return {
            "kind": "cpu-only",
            "confidence": "high",
            "basis": "no NVIDIA GPU detected",
        }
    if any("GB10" in name.upper() for name in gpu_names):
        return {
            "kind": "unified",
            "confidence": "high",
            "basis": "GB10 uses coherent unified system memory",
        }
    return {
        "kind": "unknown",
        "confidence": "conservative",
        "basis": "GPU presence alone does not prove a discrete memory hierarchy",
    }


def _parse_topology_links(output: str) -> list[dict[str, str]]:
    """Extract unique GPU-pair classes from ``nvidia-smi topo -m``.

    Args:
        output: Raw topology matrix.

    Returns:
        One entry per unique GPU pair. An unrecognized matrix returns no links.
    """
    rows = [line.split() for line in output.splitlines() if line.strip()]
    header = next(
        (
            row
            for row in rows
            if len(row) >= 2
            and row[0].startswith("GPU")
            and row[1].startswith("GPU")
        ),
        [],
    )
    gpu_names: list[str] = []
    for token in header:
        if not token.startswith("GPU"):
            break
        gpu_names.append(token)
    if len(gpu_names) < 2:
        return []

    matrix_rows = {
        row[0]: row for row in rows if row and row[0] in gpu_names
    }
    links: list[dict[str, str]] = []
    for left_index, left in enumerate(gpu_names):
        row = matrix_rows.get(left, [])
        for right_index in range(left_index + 1, len(gpu_names)):
            cell_index = right_index + 1
            if cell_index >= len(row):
                return []
            links.append(
                {
                    "left": left,
                    "right": gpu_names[right_index],
                    "connection": row[cell_index],
                }
            )
    return links


def _multi_gpu_capability(gpu_count: int) -> dict[str, Any]:
    """Describe host topology without claiming a runtime communication path.

    Args:
        gpu_count: Number of NVIDIA GPUs found by identity discovery.

    Returns:
        GPU-pair topology capability and an explicit runtime-evidence boundary.
    """
    runtime_collective = {
        "status": "not_observed",
        "detail": (
            "host discovery cannot prove TP size, rank binding, collective "
            "backend, or the path selected by a serving process"
        ),
    }
    if gpu_count < 2:
        return {
            "gpu_count": gpu_count,
            "topology": {
                "status": "not_applicable",
                "source": "nvidia-smi topo -m",
                "links": [],
                "detail": "fewer than two NVIDIA GPUs detected",
            },
            "runtime_collective": runtime_collective,
        }

    code, output = _run(["nvidia-smi", "topo", "-m"])
    if code != 0:
        return {
            "gpu_count": gpu_count,
            "topology": {
                "status": "unavailable",
                "source": "nvidia-smi topo -m",
                "links": [],
                "detail": output or "topology query failed",
            },
            "runtime_collective": runtime_collective,
        }

    links = _parse_topology_links(output)
    return {
        "gpu_count": gpu_count,
        "topology": {
            "status": "available" if links else "unavailable",
            "source": "nvidia-smi topo -m",
            "links": links,
            "detail": (
                "GPU pair connection classes captured"
                if links
                else "topology output was not recognized"
            ),
            "raw": output,
        },
        "runtime_collective": runtime_collective,
    }


def build_manifest() -> dict[str, Any]:
    """Build a machine-readable host capability manifest.

    Returns:
        Manifest that distinguishes absent, unsupported, and untested features.
    """
    gpu, memory_usage = _gpu_capability()
    nvidia_fs_path = Path("/proc/driver/nvidia-fs/stats")
    nvidia_fs = {
        "status": "available" if nvidia_fs_path.exists() else "not_detected",
        "detail": str(nvidia_fs_path),
    }

    docker_path = shutil.which("docker")
    docker = {
        "status": "available" if docker_path else "not_detected",
        "detail": docker_path or "docker executable not found",
    }

    gdscheck_path = find_gdscheck()
    gds = {
        "status": "not_tested" if gdscheck_path else "not_detected",
        "detail": (
            f"gdscheck found at {gdscheck_path}; "
            "filesystem-specific validation is still required"
            if gdscheck_path
            else "gdscheck executable not found"
        ),
    }

    attribution = "available" if nvidia_fs_path.exists() else "limited"
    measurement_ready = bool(
        gpu["present"] and docker_path and nvidia_fs_path.exists()
    )
    return {
        "manifest_version": "0.2.1",
        "host": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
        },
        "gpu": gpu,
        "multi_gpu": _multi_gpu_capability(len(gpu["names"])),
        "memory_architecture": _memory_architecture(list(gpu["names"])),
        "capabilities": {
            "gpu_memory_usage_query": memory_usage,
            "docker": docker,
            "nvidia_fs": nvidia_fs,
            "gds": gds,
            "nixl": _package_status("nixl"),
            "lmcache": _package_status("lmcache"),
            "kvbm": _package_status("kvbm"),
        },
        "current_adapter": {
            "name": "vllm-lmcache-nvidia-fs",
            "attribution": attribution,
            "attribution_scope": "counter_source_presence_only",
            "measurement_ready": measurement_ready,
            "measurement_ready_scope": "host_prerequisites_only",
            "route_status": (
                "candidate" if measurement_ready else "adapter_required"
            ),
            "serving_verification": "not_assessed",
            "note": (
                "Route status classifies host prerequisites only. GDS "
                "remains unproven until the target filesystem and controlled "
                "request evidence pass Gate 1."
            ),
        },
    }


def render_manifest(manifest: dict[str, Any]) -> str:
    """Render a compact human-readable capability summary.

    Args:
        manifest: Manifest returned by :func:`build_manifest`.

    Returns:
        Multi-line summary.
    """
    gpu = manifest["gpu"]
    names = ", ".join(gpu["names"]) if gpu["names"] else "none"
    multi_gpu = manifest["multi_gpu"]
    topology = multi_gpu["topology"]
    connection_counts = Counter(
        link["connection"] for link in topology["links"]
    )
    topology_detail = ", ".join(
        f"{connection}={count} pair{'s' if count != 1 else ''}"
        for connection, count in sorted(connection_counts.items())
    )
    if not topology_detail:
        topology_detail = topology["detail"]
    lines = [
        "kvprobe doctor",
        f"  GPU: {gpu['status']} ({len(gpu['names'])} devices: {names})",
        (
            "  inter-GPU topology: "
            f"{topology['status']} ({topology_detail})"
        ),
        (
            "  runtime collective: "
            f"{multi_gpu['runtime_collective']['status']}"
        ),
        (
            "  memory architecture: "
            f"{manifest['memory_architecture']['kind']} "
            f"({manifest['memory_architecture']['confidence']})"
        ),
    ]
    for name, capability in manifest["capabilities"].items():
        lines.append(f"  {name}: {capability['status']}")
    adapter = manifest["current_adapter"]
    lines.extend(
        [
            "  attribution adapter: "
            f"{adapter['attribution']} (counter source presence only)",
            "  measurement ready: "
            f"{str(adapter['measurement_ready']).lower()} "
            "(host prerequisites only)",
            f"  reference adapter route: {adapter['route_status']}",
            f"  serving verification: {adapter['serving_verification']}",
            "  note: nixl/lmcache/kvbm report host Python imports only; "
            "the reference adapter runs them inside a container image.",
            "  note: capability discovery is not serving tier verification evidence.",
        ]
    )
    return "\n".join(lines)
