# SPDX-License-Identifier: Apache-2.0
"""Synthetic child process for public supervisor tests; no serving or device I/O."""

# Standard
import json
import os
from pathlib import Path
import resource
import signal
import sys
import time

# First Party
from kvprobe.nfs_client_process import checkpoint_client_workload

# Local
from test_nfs_client import fixture, seal
from test_nfs_path_binding import stat_bytes


def main() -> None:
    """Produce synthetic original files, then perform the selected real exit action."""
    root, action = Path(sys.argv[1]), sys.argv[2]
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    if action == "early":
        raise SystemExit(7)
    fixture(root, "baseline")
    (root / "client-manifest.json").unlink()
    (root / "shutdown.json").unlink()
    frozen = json.loads((root / "frozen.json").read_text())
    frozen["pid"] = os.getpid() if action != "wrong-pid" else os.getpid() + 100000
    if Path("/proc/sys/kernel/random/boot_id").exists():
        frozen["boot_id"] = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        frozen["time_namespace"] = os.readlink("/proc/self/ns/time")
    (root / "frozen.json").write_text(json.dumps(frozen))
    for name in ["before.stat.bin", "after-request.stat.bin"]:
        (root / name).write_bytes(stat_bytes(frozen["pid"], frozen["pid"] + 1000))
    response = json.loads((root / "retrieve.json").read_text())
    now = time.monotonic_ns()
    response["interval_ns"] = [now, now + 1]
    (root / "retrieve.json").write_text(json.dumps(response))
    checkpoint_client_workload(root)
    print("synthetic workload checkpoint retained", flush=True)
    if action == "abort":
        os.abort()
    if action == "no-cleanup":
        return
    if action in {"timeout", "kill", "timeout-zero"}:
        if action == "kill":
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
        elif action == "timeout-zero":
            signal.signal(signal.SIGTERM, lambda number, frame: sys.exit(0))
        time.sleep(20)
    if action == "forged":
        (root / "exit-code.txt").write_text("0\n")
        raise SystemExit(9)
    if action == "link":
        (root / "unsafe-link").symlink_to("/etc/passwd")
        return
    (root / "shutdown.json").write_text(
        json.dumps({"public_engine_shutdown_returned": True})
    )
    seal(root)
    if action == "abort-after-seal":
        os.abort()


if __name__ == "__main__":
    main()
