# SPDX-License-Identifier: Apache-2.0
"""Process/path bindings are kernel metadata observations, never serving verdicts."""

# Standard
import copy
from hashlib import sha256
import json
from pathlib import Path
import stat
import tempfile
from typing import Any
import unittest
from unittest.mock import patch

# First Party
from kvprobe.configuration import fingerprint_configuration
from kvprobe.nfs_path_binding import (
    PathBindingError,
    capture_path_binding,
    replay_path_binding,
)

# Local
from test_nfs_bundle import BOOT, MOUNT, stats
from test_nfs_scope import scoped

OBSERVER_PID = 321
SERVING_PID = 123
ROOT_META = {"major_minor": "0:1", "inode": 2, "mode": stat.S_IFDIR | 0o755}
TABLE = (
    "1 1 0:1 / / rw - ext4 /dev/reference rw\n"
    "10 1 0:55 / /mnt/nfs-test rw - nfs4 storage.invalid:/export rw\n"
    "77 1 0:55 / /tier/nfs rw - nfs4 storage.invalid:/export rw\n"
)


def stat_bytes(pid: int, ticks: int) -> bytes:
    """Make stat field 22 with a comm containing spaces and closing parentheses."""
    tail = ["S"] + ["0"] * 18 + [str(ticks)] + ["0"] * 30
    return f"{pid} (worker ) with spaces) {' '.join(tail)}\n".encode()


def load(root: Path) -> dict[str, Any]:
    """Read the current synthetic binding manifest."""
    return json.loads((root / "path-binding.json").read_text())


def write(root: Path, value: dict[str, Any]) -> None:
    """Refresh the top-level synthetic capture after a test mutation."""
    (root / "path-binding.json").write_text(json.dumps(value))


def save(root: Path, name: str, data: bytes) -> dict[str, str]:
    """Retain synthetic raw bytes with an independently calculated SHA-256."""
    (root / name).write_bytes(data)
    return {"path": name, "sha256": sha256(data).hexdigest()}


def fixture(root: Path) -> dict[str, Any]:
    """Build the documented experimental raw contract without the collector."""
    parameters = scoped()["configurations"][0]["parameters"]
    value: dict[str, Any] = {
        "nfs_path_binding_version": "0.1.0-experimental",
        "clock": "CLOCK_MONOTONIC",
        "capture_interval_ns": [0, 100],
        "configuration": save(
            root, "configuration.json", json.dumps(parameters).encode()
        ),
        "configuration_fingerprint": fingerprint_configuration(
            parameters, evidence_version="0.4.0"
        ),
        "serving_pid": SERVING_PID,
        "observer_pid": OBSERVER_PID,
        "arm": "nfs-rdma",
        "observer_mountpoint": MOUNT,
        "pidfd_alive": {"before": True, "after": True},
        "epochs": {},
        "capture_errors": {},
    }
    for epoch, bounds in [("before", [10, 20]), ("after", [30, 40])]:
        roles = {}
        for role, pid, mnt in [
            ("observer", OBSERVER_PID, "10"),
            ("serving", SERVING_PID, "77"),
        ]:
            prefix = epoch + "." + role
            roles[role] = {
                "namespace": "mnt:[100]",
                "root": copy.deepcopy(ROOT_META),
                "stat": save(root, prefix + ".stat", stat_bytes(pid, 1000 + pid)),
                "cmdline": save(
                    root, prefix + ".cmdline", b"python3\0reference-worker\0"
                ),
                "mountinfo": save(root, prefix + ".mountinfo", TABLE.encode()),
                "mountstats": save(root, prefix + ".mountstats", stats())
                if role == "observer"
                else None,
                "probe": {
                    "metadata": {
                        "major_minor": "0:55",
                        "inode": 500,
                        "mode": stat.S_IFDIR | 0o755,
                    },
                    "fdinfo": save(
                        root,
                        prefix + ".fdinfo",
                        f"pos:\t0\nflags:\t012000000\nmnt_id:\t{mnt}\n".encode(),
                    ),
                    "resolver": "kernel_open_path"
                    if role == "observer"
                    else "openat2_in_root_no_magiclinks",
                },
            }
        value["epochs"][epoch] = {
            "interval_ns": bounds,
            "boot_id": BOOT,
            "roles": roles,
        }
    write(root, value)
    return value


class NfsPathBindingTests(unittest.TestCase):
    """Positive bindings use synthetic proc data; actual Linux smoke is separate."""

    def test_same_namespace_alias_matches_counter_filesystem(self) -> None:
        """Different path names and mount IDs can share an observed NFS filesystem."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fixture(root)
            result = replay_path_binding(root)
        self.assertEqual(result["path_binding_status"], "matched")
        self.assertEqual(result["process_identity"]["start_ticks"], 1123)
        self.assertIn("counter_aliases_observed", result["diagnostics"])
        self.assertIn("cross_namespace_users_not_excluded", result["diagnostics"])
        self.assertEqual(result["serving_verification"], "not_assessed")
        self.assertEqual(result["performance_claims"], "suppressed")
        self.assertNotIn("gates", result)

    def test_different_namespace_can_match_same_counter_filesystem(self) -> None:
        """Namespace inequality is not itself a filesystem mismatch."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            value = fixture(root)
            for epoch in value["epochs"].values():
                epoch["roles"]["serving"]["namespace"] = "mnt:[200]"
            write(root, value)
            result = replay_path_binding(root)
        self.assertEqual(result["path_binding_status"], "matched")
        self.assertIn("different_namespace_views", result["diagnostics"])

    def test_nested_or_lexically_contained_other_filesystem_mismatches(self) -> None:
        """A lexical child or parent traversal cannot override the FD's mount ID."""
        for path in ("/mnt/nfs-test/../local", "/mnt/nfs-test/nested"):
            with self.subTest(path=path), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                value = fixture(root)
                config = json.loads((root / "configuration.json").read_text())
                config["arms"][0]["tier_path"] = path
                value["configuration"] = save(
                    root, "configuration.json", json.dumps(config).encode()
                )
                value["configuration_fingerprint"] = fingerprint_configuration(
                    config, evidence_version="0.4.0"
                )
                for name, epoch in value["epochs"].items():
                    item = epoch["roles"]["serving"]
                    item["probe"]["metadata"]["major_minor"] = "0:1"
                    item["probe"]["fdinfo"] = save(
                        root, name + ".serving.fdinfo", b"mnt_id: 1\n"
                    )
                write(root, value)
                result = replay_path_binding(root)
            self.assertEqual(result["path_binding_status"], "mismatch")
            self.assertEqual(
                result["reason_codes"], ["serving_path_is_different_filesystem"]
            )

    def test_same_namespace_different_mount_view_is_invalid(self) -> None:
        """Conflicting selected mounts in the same root/namespace are not accepted."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            value = fixture(root)
            for name, epoch in value["epochs"].items():
                item = epoch["roles"]["serving"]
                item["mountinfo"] = save(
                    root,
                    name + ".serving.mountinfo",
                    TABLE.replace(
                        "0:55 / /mnt/nfs-test", "0:66 / /mnt/nfs-test"
                    ).encode(),
                )
            write(root, value)
            result = replay_path_binding(root)
        self.assertEqual(result["path_binding_status"], "invalid")
        self.assertEqual(
            result["reason_codes"], ["shared_namespace_mount_view_changed"]
        )

    def test_same_namespace_root_views_can_hide_or_relabel_mounts(self) -> None:
        """A chroot-like mount view need not list the observer's mountpoint."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            value = fixture(root)
            view = TABLE.replace(
                "10 1 0:55 / /mnt/nfs-test rw - nfs4 storage.invalid:/export rw\n", ""
            )
            view = view.replace("/ /tier/nfs rw", "/ /cache rw")
            for name, epoch in value["epochs"].items():
                item = epoch["roles"]["serving"]
                item["mountinfo"] = save(
                    root, name + ".serving.mountinfo", view.encode()
                )
            write(root, value)
            result = replay_path_binding(root)
        self.assertEqual(result["path_binding_status"], "matched")
        self.assertEqual(result["serving_verification"], "not_assessed")

    def test_process_start_change_and_exit_are_invalid(self) -> None:
        """PID reuse and pidfd exit cannot be interpreted as a stable process."""
        for change in ("ticks", "exit"):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                value = fixture(root)
                if change == "ticks":
                    value["epochs"]["after"]["roles"]["serving"]["stat"] = save(
                        root, "after.serving.stat", stat_bytes(SERVING_PID, 9999)
                    )
                else:
                    value["pidfd_alive"]["after"] = False
                write(root, value)
                result = replay_path_binding(root)
            self.assertEqual(result["path_binding_status"], "invalid")

    def test_process_namespace_root_exec_and_path_changes_are_invalid(self) -> None:
        """A selected process/view/path changing within collection invalidates it."""
        for change in ("namespace", "root", "cmdline", "probe"):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                value = fixture(root)
                item = value["epochs"]["after"]["roles"]["serving"]
                if change == "namespace":
                    item["namespace"] = "mnt:[999]"
                elif change == "root":
                    item["root"]["inode"] += 1
                elif change == "cmdline":
                    item["cmdline"] = save(
                        root, "after.serving.cmdline", b"other-program\0"
                    )
                else:
                    item["probe"]["metadata"]["inode"] += 1
                write(root, value)
                result = replay_path_binding(root)
            self.assertEqual(result["path_binding_status"], "invalid")

    def test_unknown_sources_and_liveness_remain_unproven(self) -> None:
        """Permission or missing kernel data is not zero or a match."""
        for field in (
            "namespace",
            "root",
            "stat",
            "cmdline",
            "mountinfo",
            "probe",
            "liveness",
            "boot_id",
        ):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                value = fixture(root)
                if field == "liveness":
                    value["pidfd_alive"]["after"] = None
                elif field == "boot_id":
                    value["epochs"]["after"]["boot_id"] = None
                else:
                    value["epochs"]["after"]["roles"]["serving"][field] = None
                write(root, value)
                result = replay_path_binding(root)
            self.assertEqual(result["path_binding_status"], "unproven")
            self.assertEqual(result["serving_verification"], "not_assessed")

    def test_missing_fd_mount_is_unproven(self) -> None:
        """A detached or unavailable mount is not reconstructed from path strings."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            value = fixture(root)
            for name, epoch in value["epochs"].items():
                epoch["roles"]["serving"]["probe"]["fdinfo"] = save(
                    root, name + ".serving.fdinfo", b"mnt_id: 999\n"
                )
            write(root, value)
            result = replay_path_binding(root)
        self.assertEqual(result["reason_codes"], ["fd_mount_not_visible"])

    def test_stat_and_mountinfo_device_conflict_is_invalid(self) -> None:
        """Fstat metadata cannot contradict the selected mountinfo row."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            value = fixture(root)
            for epoch in value["epochs"].values():
                epoch["roles"]["serving"]["probe"]["metadata"]["major_minor"] = "0:66"
            write(root, value)
            result = replay_path_binding(root)
        self.assertEqual(result["reason_codes"], ["fd_device_receipt_mismatch"])

    def test_observer_mountpoint_must_be_the_opened_mount(self) -> None:
        """The observed counter path is tied to its FD, not a guessed prefix."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            value = fixture(root)
            value["observer_mountpoint"] = "/different/path"
            write(root, value)
            result = replay_path_binding(root)
        self.assertEqual(result["path_binding_status"], "mismatch")

    def test_non_rdma_and_source_disagreement_do_not_match(self) -> None:
        """The counter view must retain matching NFS/RDMA transport and identity."""
        for change in ("tcp", "source"):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                value = fixture(root)
                for name, epoch in value["epochs"].items():
                    data = (
                        stats().replace(b"proto=rdma", b"proto=tcp")
                        if change == "tcp"
                        else stats().replace(
                            b"storage.invalid:/export", b"other.invalid:/export"
                        )
                    )
                    epoch["roles"]["observer"]["mountstats"] = save(
                        root, name + ".observer.mountstats", data
                    )
                write(root, value)
                result = replay_path_binding(root)
            self.assertEqual(
                result["path_binding_status"],
                "unproven" if change == "tcp" else "invalid",
            )

    def test_changed_boot_and_overlapping_epochs_are_invalid(self) -> None:
        """Before/after metadata must belong to one ordered kernel lifetime."""
        for change in ("boot", "interval"):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                value = fixture(root)
                if change == "boot":
                    value["epochs"]["after"]["boot_id"] = (
                        "00000000-0000-0000-0000-000000000002"
                    )
                else:
                    value["epochs"]["after"]["interval_ns"] = [15, 25]
                write(root, value)
                result = replay_path_binding(root)
            self.assertEqual(result["path_binding_status"], "invalid")

    def test_raw_hash_and_configuration_fingerprint_are_verified(self) -> None:
        """Edited originals or a changed expected configuration are refused."""
        for change in ("raw", "fingerprint"):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                value = fixture(root)
                if change == "raw":
                    (root / "after.serving.stat").write_bytes(b"edited")
                else:
                    value["configuration_fingerprint"] = "0" * 64
                    write(root, value)
                with self.assertRaises(PathBindingError):
                    replay_path_binding(root)

    def test_escape_and_unknown_contract_are_refused(self) -> None:
        """Manifest symlinks and other raw contracts cannot become binding proof."""
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "bundle"
            root.mkdir()
            value = fixture(root)
            outside = base / "outside.json"
            outside.write_bytes((root / "path-binding.json").read_bytes())
            (root / "path-binding.json").unlink()
            (root / "path-binding.json").symlink_to(outside)
            with self.assertRaises(PathBindingError):
                replay_path_binding(root)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            value = fixture(root)
            value["nfs_path_binding_version"] = "0.2.0"
            write(root, value)
            with self.assertRaises(PathBindingError):
                replay_path_binding(root)

    def test_selected_mount_change_is_invalid(self) -> None:
        """A stable FD device alone cannot hide a changing mount source."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            value = fixture(root)
            for role in ("observer", "serving"):
                value["epochs"]["after"]["roles"][role]["mountinfo"] = save(
                    root,
                    "after." + role + ".mountinfo",
                    TABLE.replace(
                        "storage.invalid:/export", "different.invalid:/export"
                    ).encode(),
                )
            write(root, value)
            result = replay_path_binding(root)
        self.assertEqual(result["path_binding_status"], "invalid")
        self.assertEqual(result["reason_codes"], ["selected_mount_changed"])

    def test_pid_receipt_must_match_the_selected_process(self) -> None:
        """A valid proc record for another PID is still not the selected process."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            value = fixture(root)
            value["epochs"]["before"]["roles"]["serving"]["stat"] = save(
                root, "before.serving.stat", stat_bytes(999, 1123)
            )
            write(root, value)
            result = replay_path_binding(root)
        self.assertEqual(result["path_binding_status"], "invalid")

    def test_malformed_proc_and_namespace_receipts_are_refused(self) -> None:
        """Hashes do not validate malformed stat, fdinfo or mountinfo contents."""
        for field, data in (
            ("stat", b"123 broken"),
            ("fdinfo", b"mnt_id: 77\nmnt_id: 10\n"),
            ("mountinfo", b"broken row"),
        ):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                value = fixture(root)
                item = value["epochs"]["before"]["roles"]["serving"]
                ref = save(root, "bad." + field, data)
                if field == "fdinfo":
                    item["probe"]["fdinfo"] = ref
                else:
                    item[field] = ref
                write(root, value)
                with self.assertRaises(PathBindingError):
                    replay_path_binding(root)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            value = fixture(root)
            value["epochs"]["before"]["roles"]["serving"]["namespace"] = []
            write(root, value)
            with self.assertRaises(PathBindingError):
                replay_path_binding(root)

    def test_fabricated_lexical_resolver_and_bad_intervals_are_refused(self) -> None:
        """Only the declared kernel resolver and integer clock fields are defined."""
        for change in ("resolver", "interval"):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                value = fixture(root)
                if change == "resolver":
                    value["epochs"]["before"]["roles"]["serving"]["probe"][
                        "resolver"
                    ] = "lexical_prefix"
                else:
                    value["epochs"]["before"]["interval_ns"] = [True, 20]
                write(root, value)
                with self.assertRaises(PathBindingError):
                    replay_path_binding(root)

    def test_unsupported_collector_retains_unknown_receipt(self) -> None:
        """Portable imports and unsupported hosts do not imply a connected path."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with patch(
                "kvprobe.nfs_path_binding.platform.system", return_value="OtherOS"
            ):
                capture_path_binding(
                    root,
                    SERVING_PID,
                    scoped()["configurations"][0]["parameters"],
                    "nfs-rdma",
                    MOUNT,
                )
            result = replay_path_binding(root)
            original = (root / "path-binding.json").read_bytes()
            with (
                patch(
                    "kvprobe.nfs_path_binding.platform.system", return_value="OtherOS"
                ),
                self.assertRaises(PathBindingError),
            ):
                capture_path_binding(
                    root,
                    SERVING_PID,
                    scoped()["configurations"][0]["parameters"],
                    "nfs-rdma",
                    MOUNT,
                )
            self.assertEqual((root / "path-binding.json").read_bytes(), original)
        self.assertEqual(result["path_binding_status"], "unproven")

    def test_bad_pid_and_relative_configured_path_are_refused(self) -> None:
        """The selected PID and process-root path are explicit validated inputs."""
        for pid in (True, 0, -1, "123"):
            with (
                tempfile.TemporaryDirectory() as temp,
                self.assertRaises(PathBindingError),
            ):
                capture_path_binding(
                    Path(temp),
                    pid,
                    scoped()["configurations"][0]["parameters"],
                    "nfs-rdma",
                    MOUNT,
                )
        with tempfile.TemporaryDirectory() as temp, self.assertRaises(PathBindingError):
            config = scoped()["configurations"][0]["parameters"]
            config["arms"][0]["tier_path"] = "relative/path"
            capture_path_binding(Path(temp), SERVING_PID, config, "nfs-rdma", MOUNT)
