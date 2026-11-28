# SPDX-License-Identifier: Apache-2.0
"""Raw NFS observations never become serving proof through replay."""

# Standard
from contextlib import contextmanager
from hashlib import sha256
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Iterator
import unittest
from unittest.mock import patch

# First Party
from kvprobe.nfs_bundle import (
    capture_operation,
    capture_snapshot,
    replay_bundle,
    seal_bundle,
)
from kvprobe.nfs_observation import (
    NfsObservationError,
    counter_delta,
    parse_mountinfo,
    parse_mountstats,
)

MOUNT = "/mnt/nfs-test"
INFO = b"10 1 0:55 / /mnt/nfs-test rw - nfs4 storage.invalid:/export rw\n"
BOOT = "00000000-0000-0000-0000-000000000001"


def stats(
    payload: int = 0, ops: int = 0, export: str = "storage.invalid:/export"
) -> bytes:
    """Return synthetic Linux proc data, not a measured storage result."""
    return (
        f"device {export} mounted on {MOUNT} with fstype nfs4 statvers=1.1\n"
        " opts: rw,vers=4.2,proto=rdma,nosharecache\n age: 10\n"
        f" bytes: 0 0 {payload} 0 {payload} 0 0 0\n"
        " xprt: rdma 0 0\n"
        f" READ: {ops} {ops} 0 0 {payload + ops * 100} 0 0 0 0\n"
    ).encode()


@contextmanager
def proc(state: dict[str, Any]) -> Iterator[None]:
    """Replace Linux proc boundaries while leaving artifact file I/O real."""
    original_bytes, original_text = Path.read_bytes, Path.read_text
    original_readlink = os.readlink

    def read_bytes(path: Path) -> bytes:
        if str(path) == "/proc/self/mountstats":
            return stats(state.get("payload", 0), state.get("ops", 0))
        if str(path) == "/proc/self/mountinfo":
            return INFO
        return original_bytes(path)

    def read_text(path: Path, *args: Any, **kwargs: Any) -> str:
        if str(path) == "/proc/sys/kernel/random/boot_id":
            return state.get("boot", BOOT)
        return original_text(path, *args, **kwargs)

    def readlink(path: str, *args: Any, **kwargs: Any) -> str:
        if str(path) == "/proc/self/ns/mnt":
            return state.get("namespace", "mnt:[100]")
        if str(path) == "/proc/self/ns/time":
            return state.get("time_namespace", "time:[200]")
        return original_readlink(path, *args, **kwargs)

    with (
        patch.object(Path, "read_bytes", read_bytes),
        patch.object(Path, "read_text", read_text),
        patch("kvprobe.nfs_bundle.os.readlink", side_effect=readlink),
    ):
        yield


def make_new(root: Path, count: int = 1) -> None:
    """Capture synthetic controlled operations through the public API."""
    state: dict[str, Any] = {"payload": 0, "ops": 0}

    def read() -> bytes:
        state["payload"] += 4
        state["ops"] += 1
        return b"data"

    with proc(state):
        refs = [
            capture_operation(root, MOUNT, f"read-{i}", read, b"data")
            for i in range(count)
        ]
        seal_bundle(root, refs)


def rewrite_snapshot(
    root: Path, field: str, value: Any, direction: str = "after"
) -> None:
    """Change synthetic metadata and refresh hashes to exercise semantic validation."""
    op_path = root / "read-0.operation.json"
    op = json.loads(op_path.read_text())
    snap_path = root / op[direction]["path"]
    snap = json.loads(snap_path.read_text())
    snap[field] = value
    snap_path.write_text(json.dumps(snap))
    op[direction]["sha256"] = sha256(snap_path.read_bytes()).hexdigest()
    op_path.write_text(json.dumps(op))
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["operations"][0]["sha256"] = sha256(op_path.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest))


def make_legacy(root: Path) -> None:
    """Write a synthetic fixture of the archived N1 diagnostic format."""
    artifacts = root / "artifacts"
    artifacts.mkdir()
    refs = {}
    for label, raw in [("before", stats()), ("after", stats(8192, 1))]:
        refs[label] = {}
        for kind, data in [("mountstats", raw), ("mountinfo", INFO)]:
            name = f"r1-{label}.{kind}.txt"
            (artifacts / name).write_bytes(data)
            refs[label][kind] = {"path": name, "sha256": sha256(data).hexdigest()}
    a, b = (
        parse_mountstats(stats().decode(), MOUNT),
        parse_mountstats(stats(8192, 1).decode(), MOUNT),
    )
    identity = parse_mountinfo(INFO.decode(), MOUNT)
    op = {
        "request_id": "r1",
        "correct": True,
        "expected_bytes": 8192,
        "capture_interval_ns": [1, 6],
        "operation_interval_ns": [3, 4],
        "counter_capture_intervals_ns": [[1, 2], [5, 6]],
        "mount": {
            **identity,
            "mountpoint": MOUNT,
            "namespace": "mnt:[100]",
            "export": a.export,
            "options": list(a.options),
            "transport": list(a.transports),
        },
        "counter_delta": counter_delta(a, b),
        "artifacts": refs,
    }
    (artifacts / "r1.json").write_text(json.dumps(op))
    (artifacts / "summary.json").write_text(
        json.dumps(
            {
                "diagnostic_version": "0.1.0-experimental",
                "workload": "isolated_file_io_not_kv",
                "status": "diagnostic_completed",
                "clock": "CLOCK_MONOTONIC",
                "requests": [op],
            }
        )
    )


class NfsBundleTests(unittest.TestCase):
    """Exercise capture/replay and fail-closed raw provenance checks."""

    def test_new_roundtrip_is_observation_only(self) -> None:
        """Per-snapshot metadata and exact payload counts do not imply serving."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            make_new(root, 2)
            result = replay_bundle(root)
            self.assertEqual(result["status"], "observation_only")
            self.assertEqual(
                [
                    r["counter_delta"]["server_read_payload_bytes"]
                    for r in result["operations"]
                ],
                [4, 4],
            )
            self.assertEqual(result["serving_verification"], "not_assessed")
            self.assertEqual(result["performance_claims"], "suppressed")
            self.assertNotIn("verdict", result)
            self.assertTrue(all(r["reported_correct"] for r in result["operations"]))

    def test_legacy_replay_does_not_invent_missing_provenance(self) -> None:
        """Legacy namespaces and clock domains stay unavailable."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            make_legacy(root)
            result = replay_bundle(root)
            operation = result["operations"][0]
            self.assertEqual(
                operation["counter_delta"]["server_read_payload_bytes"], 8192
            )
            self.assertEqual(
                operation["counter_delta"]["read_rpc_received_bytes"], 8292
            )
            self.assertIsNone(operation["scope"]["clock_domain"])
            self.assertIsNone(operation["scope"]["mount_namespace"])
            self.assertTrue(result["provenance_gaps"])

    def test_legacy_artifacts_cannot_escape_bundle(self) -> None:
        """A legacy directory symlink cannot redefine the containment root."""
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            outside, bundle = base / "outside", base / "bundle"
            outside.mkdir()
            bundle.mkdir()
            make_legacy(outside)
            (bundle / "artifacts").symlink_to(
                outside / "artifacts", target_is_directory=True
            )
            with self.assertRaisesRegex(
                NfsObservationError, "legacy artifacts directory escapes"
            ):
                replay_bundle(bundle)

    def test_legacy_artifacts_internal_alias_is_allowed(self) -> None:
        """A directory alias inside the bundle keeps valid legacy replay working."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            make_legacy(root)
            (root / "artifacts").rename(root / "retained")
            (root / "artifacts").symlink_to("retained", target_is_directory=True)
            result = replay_bundle(root)
            self.assertEqual(
                result["operations"][0]["counter_delta"]["server_read_payload_bytes"],
                8192,
            )
            self.assertEqual(result["status"], "observation_only")
            self.assertEqual(result["serving_verification"], "not_assessed")

    def test_legacy_artifacts_invalid_directory_is_refused(self) -> None:
        """Missing, dangling, looping and non-directory legacy roots are invalid."""
        for kind in ("missing", "dangling", "loop", "file"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                path = root / "artifacts"
                if kind == "dangling":
                    path.symlink_to("missing", target_is_directory=True)
                elif kind == "loop":
                    path.symlink_to("artifacts", target_is_directory=True)
                elif kind == "file":
                    path.write_bytes(b"not a directory")
                with self.assertRaises(NfsObservationError):
                    replay_bundle(root)

    def test_raw_tampering_or_missing_file_refuses(self) -> None:
        """Missing and changed raw artifacts cannot become zero counts."""
        for action in ("tamper", "missing"):
            with self.subTest(action=action), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                make_new(root)
                path = root / "read-0-after.mountstats.txt"
                if action == "tamper":
                    path.write_bytes(stats(10000, 99))
                else:
                    path.unlink()
                with self.assertRaises(NfsObservationError):
                    replay_bundle(root)

    def test_changed_namespace_clock_or_mount_refuses_even_with_fresh_hashes(
        self,
    ) -> None:
        """Hash integrity alone cannot validate cross-snapshot identity."""
        cases = [
            ("mount_namespace", "mnt:[999]"),
            ("clock_domain", {"boot_id": BOOT, "time_namespace": "time:[999]"}),
            (
                "clock_domain",
                {
                    "boot_id": "00000000-0000-0000-0000-000000000002",
                    "time_namespace": "time:[200]",
                },
            ),
            ("capture_interval_ns", [0, 1]),
            ("clock", "CLOCK_REALTIME"),
            ("nfs_capture_version", "9.0.0"),
            ("mountpoint", "/mnt/other"),
        ]
        for field, value in cases:
            with (
                self.subTest(field=field, value=value),
                tempfile.TemporaryDirectory() as temp,
            ):
                root = Path(temp)
                make_new(root)
                rewrite_snapshot(root, field, value)
                with self.assertRaises(NfsObservationError):
                    replay_bundle(root)

    def test_outside_symlink_is_not_read(self) -> None:
        """An artifact path cannot escape the extracted bundle through a symlink."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "bundle"
            root.mkdir()
            make_new(root)
            outside = Path(temp) / "outside.txt"
            outside.write_bytes(stats(4, 1))
            path = root / "read-0-after.mountstats.txt"
            path.unlink()
            path.symlink_to(outside)
            with self.assertRaises(NfsObservationError):
                replay_bundle(root)

    def test_omitted_or_repeated_operations_refuse(self) -> None:
        """A completed manifest must include every operation once, in order."""
        for mode in ("omit", "repeat", "reverse"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                make_new(root, 2)
                path = root / "manifest.json"
                manifest = json.loads(path.read_text())
                values = manifest["operations"]
                manifest["operations"] = (
                    values[:1]
                    if mode == "omit"
                    else (values + values[:1] if mode == "repeat" else values[::-1])
                )
                path.write_text(json.dumps(manifest))
                with self.assertRaises(NfsObservationError):
                    replay_bundle(root)

    def test_new_manifest_never_falls_back_to_legacy(self) -> None:
        """An invalid new manifest cannot be bypassed by a legacy summary."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            make_legacy(root)
            (root / "manifest.json").write_text('{"kind":"unknown"}')
            with self.assertRaises(NfsObservationError):
                replay_bundle(root)

    def test_legacy_summary_and_raw_delta_are_both_checked(self) -> None:
        """Matching edited receipts cannot hide different raw counter values."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            make_legacy(root)
            p = root / "artifacts/summary.json"
            summary = json.loads(p.read_text())
            summary["requests"][0]["counter_delta"]["server_read_payload_bytes"] = 0
            p.write_text(json.dumps(summary))
            (root / "artifacts/r1.json").write_text(json.dumps(summary["requests"][0]))
            with self.assertRaises(NfsObservationError):
                replay_bundle(root)

    def test_capture_preserves_error_and_does_not_seal_failed_operation(self) -> None:
        """A callback failure leaves raw records, not a completed observation."""
        with tempfile.TemporaryDirectory() as temp, proc({}):
            root = Path(temp)

            def failed() -> bytes:
                raise OSError("synthetic failure")

            with self.assertRaises(NfsObservationError):
                capture_operation(root, MOUNT, "failed", failed, b"data")
            record = json.loads((root / "failed.operation.json").read_text())
            self.assertIn("synthetic failure", record["error"])
            with self.assertRaises(NfsObservationError):
                seal_bundle(root, [])
            self.assertFalse((root / "manifest.json").exists())

    def test_capture_and_manifest_refuse_overwrite(self) -> None:
        """Repeated capture/seal calls do not replace original files."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with proc({}):
                capture_snapshot(root, MOUNT, "one")
                original = (root / "one.snapshot.json").read_bytes()
                with self.assertRaises(FileExistsError):
                    capture_snapshot(root, MOUNT, "one")
                self.assertEqual((root / "one.snapshot.json").read_bytes(), original)
            # A completed manifest covers operation files, not unrelated snapshots.
            make_new(root)
            manifest = json.loads((root / "manifest.json").read_text())
            with self.assertRaises(FileExistsError):
                seal_bundle(root, manifest["operations"])

    def test_zero_and_corrupt_reads_are_observations_not_serving_verdicts(self) -> None:
        """Payload and correctness remain separately inspectable."""
        with tempfile.TemporaryDirectory() as temp, proc({}):
            root = Path(temp)
            ref = capture_operation(root, MOUNT, "cached", lambda: b"oops", b"data")
            seal_bundle(root, [ref])
            operation = replay_bundle(root)["operations"][0]
            self.assertFalse(operation["reported_correct"])
            self.assertEqual(operation["counter_delta"]["server_read_payload_bytes"], 0)

    def test_payload_counter_not_rpc_bytes(self) -> None:
        """The parser names the payload and RPC overhead counters separately."""
        a = parse_mountstats(stats().decode(), MOUNT)
        b = parse_mountstats(stats(8192, 2).decode(), MOUNT)
        delta = counter_delta(a, b)
        self.assertEqual(delta["server_read_payload_bytes"], 8192)
        self.assertEqual(delta["read_rpc_received_bytes"], 8392)

    def test_context_change_during_capture_retains_invalid_raw_artifacts(self) -> None:
        """An unstable capture is retained as an error, not a valid snapshot."""
        original = os.readlink
        namespaces = iter(("mnt:[100]", "mnt:[999]"))

        def readlink(path: str, *args: Any, **kwargs: Any) -> str:
            if str(path) == "/proc/self/ns/mnt":
                return next(namespaces)
            if str(path) == "/proc/self/ns/time":
                return "time:[200]"
            return original(path, *args, **kwargs)

        with tempfile.TemporaryDirectory() as temp, proc({}):
            root = Path(temp)
            with patch("kvprobe.nfs_bundle.os.readlink", side_effect=readlink):
                with self.assertRaises(NfsObservationError):
                    capture_snapshot(root, MOUNT, "unstable")
            metadata = json.loads((root / "unstable.snapshot.json").read_text())
            self.assertEqual(
                metadata["capture_error"], "namespace_or_clock_domain_changed"
            )
            self.assertTrue((root / "unstable.mountstats.txt").exists())
            self.assertTrue((root / "unstable.mountinfo.txt").exists())

    def test_legacy_summary_cannot_omit_a_retained_request(self) -> None:
        """A legacy summary must account for every individual request record."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            make_legacy(root)
            (root / "artifacts/omitted.json").write_text("{}")
            with self.assertRaises(NfsObservationError):
                replay_bundle(root)

    def test_reset_or_wrong_transport_refuses(self) -> None:
        """Counter resets and TCP-only observations cannot become RDMA evidence."""
        a = parse_mountstats(stats(8192, 2).decode(), MOUNT)
        b = parse_mountstats(stats().decode(), MOUNT)
        with self.assertRaises(NfsObservationError):
            counter_delta(a, b)
        for text in (
            stats().decode().replace("proto=rdma", "proto=tcp"),
            stats().decode().replace("xprt: rdma", "xprt: tcp"),
        ):
            with self.assertRaises(NfsObservationError):
                parse_mountstats(text, MOUNT)

    def test_artifact_path_traversal_is_refused(self) -> None:
        """A parent-directory reference is rejected even when the file exists."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "bundle"
            root.mkdir()
            make_new(root)
            (Path(temp) / "outside.txt").write_bytes(stats(4, 1))
            op_path = root / "read-0.operation.json"
            op = json.loads(op_path.read_text())
            snapshot = json.loads((root / op["after"]["path"]).read_text())
            snapshot["raw"]["mountstats"]["path"] = "../outside.txt"
            # Refresh outer hashes so path validation is reached.
            rewrite_snapshot(root, "raw", snapshot["raw"])
            with self.assertRaises(NfsObservationError):
                replay_bundle(root)
