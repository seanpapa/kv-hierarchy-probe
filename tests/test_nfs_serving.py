# SPDX-License-Identifier: Apache-2.0
"""Captured response comparisons do not by themselves establish a serving tier."""

# Standard
import copy
from hashlib import sha256
import json
from pathlib import Path
import tempfile
from typing import Any
import unittest

# First Party
from kvprobe.nfs_observation import (
    BYTE_FIELDS,
    RPC_FIELDS,
    NfsObservationError,
    parse_mountinfo,
    parse_mountstats,
)
from kvprobe.nfs_serving import capture_serving_candidate, replay_serving_candidate

# Local
from test_nfs_bundle import BOOT, INFO, MOUNT, make_legacy, proc, stats
from test_nfs_scope import scoped

PAYLOAD = 67108864
INPUT = b"synthetic-request-input"


def mapping() -> dict[str, Any]:
    """Return the synthetic mount mapping known before the callback runs."""
    return {
        "identity": parse_mountinfo(INFO.decode(), MOUNT),
        "mount_namespace": "mnt:[100]",
        "clock_domain": {"boot_id": BOOT, "time_namespace": "time:[200]"},
    }


def specification() -> dict[str, Any]:
    """Return a frozen synthetic input specification, not a hardware request."""
    return {
        "id": "request-a",
        "arm": "nfs-rdma",
        "cacheable_tokens": 256,
        "input_sha256": sha256(INPUT).hexdigest(),
    }


def capture(
    root: Path,
    *,
    payload: int = PAYLOAD,
    output: bytes = b"answer",
    baseline: bytes | None = b"answer",
    fail: bool = False,
    io_mode: str = "direct",
) -> None:
    """Exercise the producer API with synthetic proc counters and a fake callback."""
    state: dict[str, Any] = {"payload": 0, "ops": 0}
    parameters = scoped()["configurations"][0]["parameters"]
    parameters["arms"][0]["tier_identity"]["io_mode"] = io_mode

    def call() -> bytes:
        # The independent baseline is retained before this callback.
        if baseline is not None:
            assert (root / "baseline.bin").read_bytes() == baseline
        state["payload"] += payload
        state["ops"] += int(payload > 0)
        if fail:
            raise RuntimeError("synthetic callback error")
        return output

    with proc(state):
        capture_serving_candidate(
            root,
            parameters,
            specification(),
            INPUT,
            baseline,
            MOUNT,
            mapping(),
            call,
            quiet_seconds=0.0001,
        )


def manifest(root: Path) -> dict[str, Any]:
    """Read the synthetic capture's top-level manifest."""
    return json.loads((root / "serving-manifest.json").read_text())


def replace_manifest(root: Path, value: dict[str, Any]) -> None:
    """Write an adversarial top-level receipt for reader validation."""
    (root / "serving-manifest.json").write_text(json.dumps(value))


def shift_counters(root: Path, first: str, increments: dict[str, int]) -> None:
    """Build a coherent synthetic counter history and refresh its artifact hashes.

    The first boundary and all later snapshots move by the given increments;
    this creates an event in one interval without fabricating counter resets.
    """
    value = manifest(root)
    snapshots = [("preflight", value["preflight"])] + [
        (phase + "." + boundary, value["phases"][phase][boundary])
        for phase in ("quiet_before", "request", "quiet_after")
        for boundary in ("before", "after")
    ]
    active = False
    for name, ref in snapshots:
        active = active or name == first
        if not active:
            continue
        snapshot_path = root / ref["path"]
        snapshot = json.loads(snapshot_path.read_text())
        raw_ref = snapshot["raw"]["mountstats"]
        path = root / raw_ref["path"]
        lines = path.read_text().splitlines()
        for index, line in enumerate(lines):
            parts = line.split()
            fields = (
                BYTE_FIELDS
                if parts[0] == "bytes:"
                else RPC_FIELDS
                if parts[0] == "READ:"
                else ()
            )
            if fields:
                values = [
                    int(number) + increments.get(field, 0)
                    for field, number in zip(fields, parts[1:], strict=True)
                ]
                assert min(values) >= 0
                lines[index] = " " + parts[0] + " " + " ".join(map(str, values))
        path.write_text("\n".join(lines) + "\n")
        raw_ref["sha256"] = sha256(path.read_bytes()).hexdigest()
        snapshot_path.write_text(json.dumps(snapshot))
        ref["sha256"] = sha256(snapshot_path.read_bytes()).hexdigest()
    assert active, first
    replace_manifest(root, value)


class ServingCaptureTests(unittest.TestCase):
    """Public producer/replay API tests; no real serving or remote I/O occurs."""

    def test_roundtrip_keeps_observation_boundary(self) -> None:
        """Matching geometry, output and bytes still require independent review."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            capture(root)
            result = replay_serving_candidate(root)
        self.assertEqual(result["expected_kv_bytes"], PAYLOAD)
        self.assertTrue(result["correct_output_comparison"])
        self.assertEqual(
            result["phase_deltas"]["request"]["server_read_payload_bytes"], PAYLOAD
        )
        self.assertEqual(
            result["phase_deltas"]["quiet_before"]["server_read_payload_bytes"], 0
        )
        self.assertEqual(result["status"], "observation_only")
        self.assertEqual(result["serving_verification"], "not_assessed")
        self.assertEqual(result["performance_claims"], "suppressed")
        self.assertIn("competing_io_review_missing", result["review_reasons"])
        self.assertNotIn("gates", result)

    def test_clean_candidate_diagnostics_remain_unqualified(self) -> None:
        """No adverse deltas still leave client, baseline and isolation unqualified."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            capture(root)
            result = replay_serving_candidate(root)
        self.assertEqual(
            result["review_reasons"],
            [
                "serving_client_binding_missing",
                "competing_io_review_missing",
                "baseline_provenance_not_independently_qualified",
            ],
        )
        self.assertEqual(result["serving_verification"], "not_assessed")
        self.assertEqual(result["performance_claims"], "suppressed")

    def test_quiet_activity_covers_payload_and_cached_application_reads(self) -> None:
        """Both quiet controls flag counter movement without changing request bytes."""
        for phase in ("quiet_before", "quiet_after"):
            for increments in (
                {"server_read_payload_bytes": 4096},
                {"normal_read_bytes": 4096},
            ):
                with (
                    self.subTest(phase=phase, increments=increments),
                    tempfile.TemporaryDirectory() as temp,
                ):
                    root = Path(temp)
                    capture(root)
                    shift_counters(root, phase + ".after", increments)
                    result = replay_serving_candidate(root)
                self.assertIn("quiet_phase_activity", result["review_reasons"])
                self.assertNotIn("io_mode_mismatch", result["review_reasons"])
                self.assertEqual(
                    result["phase_deltas"]["request"]["server_read_payload_bytes"],
                    PAYLOAD,
                )
                self.assertEqual(result["status"], "observation_only")

    def test_rpc_diagnostics_cover_each_phase_and_gaps(self) -> None:
        """Adverse RPC deltas stay visible even outside the measured request."""
        cases = [
            ({"read_rpc_transmissions": 5}, "rpc_retransmission"),
            ({"read_rpc_timeouts": 3}, "rpc_timeout_or_error"),
            ({"read_rpc_errors": 2}, "rpc_timeout_or_error"),
        ]
        for boundary in (
            "quiet_before.after",
            "request.after",
            "quiet_after.after",
            "request.before",
        ):
            for increments, reason in cases:
                with (
                    self.subTest(boundary=boundary, increments=increments),
                    tempfile.TemporaryDirectory() as temp,
                ):
                    root = Path(temp)
                    capture(root)
                    shift_counters(root, boundary, increments)
                    result = replay_serving_candidate(root)
                self.assertEqual(result["review_reasons"].count(reason), 1)
                if boundary == "request.before":
                    self.assertIn("interphase_activity", result["review_reasons"])
                self.assertEqual(result["serving_verification"], "not_assessed")
                self.assertEqual(result["performance_claims"], "suppressed")

    def test_declared_io_mode_checks_opposite_request_counter(self) -> None:
        """Mode conflict is a mount-scoped diagnostic, including mixed read paths."""
        cases = [
            (
                "direct",
                {"direct_read_bytes": -PAYLOAD, "normal_read_bytes": PAYLOAD},
                True,
            ),
            ("direct", {"normal_read_bytes": 4096}, True),
            ("buffered", {}, True),
            (
                "buffered",
                {"direct_read_bytes": -PAYLOAD, "normal_read_bytes": PAYLOAD},
                False,
            ),
        ]
        for mode, increments, conflict in cases:
            with (
                self.subTest(mode=mode, increments=increments),
                tempfile.TemporaryDirectory() as temp,
            ):
                root = Path(temp)
                capture(root, io_mode=mode)
                if increments:
                    shift_counters(root, "request.after", increments)
                result = replay_serving_candidate(root)
            self.assertEqual("io_mode_mismatch" in result["review_reasons"], conflict)
            self.assertTrue(result["correct_output_comparison"])
            self.assertEqual(result["status"], "observation_only")

    def test_zero_and_partial_payload_do_not_fill_expected_bytes(self) -> None:
        """Observed counters remain distinct from independently calculated geometry."""
        for payload in (0, 8192):
            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                capture(root, payload=payload)
                result = replay_serving_candidate(root)
            self.assertEqual(result["expected_kv_bytes"], PAYLOAD)
            self.assertEqual(
                result["phase_deltas"]["request"]["server_read_payload_bytes"], payload
            )
            self.assertTrue(result["correct_output_comparison"])
            self.assertEqual(result["serving_verification"], "not_assessed")

    def test_incorrect_and_missing_baseline_stay_distinct(self) -> None:
        """Wrong output is false; unavailable independent baseline stays unknown."""
        for baseline, output, expected in [
            (b"answer", b"wrong", False),
            (None, b"answer", None),
        ]:
            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                capture(root, baseline=baseline, output=output)
                result = replay_serving_candidate(root)
            self.assertIs(result["correct_output_comparison"], expected)
            self.assertEqual(result["status"], "observation_only")

    def test_failed_callback_retains_originals(self) -> None:
        """An exception yields an error receipt and unknown correctness."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            capture(root, fail=True)
            self.assertTrue((root / "request-before.mountstats.txt").exists())
            self.assertTrue((root / "request-after.mountstats.txt").exists())
            result = replay_serving_candidate(root)
        self.assertEqual(result["callback_errors"], ["request"])
        self.assertIsNone(result["correct_output_comparison"])

    def test_unsafe_or_nonempty_output_refuses(self) -> None:
        """A second capture cannot overwrite the first one."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            capture(root)
            digest = sha256((root / "serving-manifest.json").read_bytes()).hexdigest()
            with self.assertRaises(NfsObservationError):
                capture(root)
            self.assertEqual(
                sha256((root / "serving-manifest.json").read_bytes()).hexdigest(),
                digest,
            )

    def test_wrong_target_is_refused_before_callback(self) -> None:
        """A prior mount mapping is checked before executing a request."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            wrong = mapping()
            wrong["identity"]["mount_source"] = "other.invalid:/export"
            called = []
            with (
                proc({}),
                self.assertRaisesRegex(NfsObservationError, "callback was not run"),
            ):
                capture_serving_candidate(
                    root,
                    scoped()["configurations"][0]["parameters"],
                    specification(),
                    INPUT,
                    b"answer",
                    MOUNT,
                    wrong,
                    lambda: called.append(True),
                    quiet_seconds=0.0001,
                )
            self.assertEqual(called, [])
            self.assertTrue((root / "preflight.mountstats.txt").exists())

    def test_non_chunk_aligned_input_is_refused(self) -> None:
        """The first producer does not guess partial chunks or header padding."""
        with tempfile.TemporaryDirectory() as temp:
            request = specification()
            request["cacheable_tokens"] = 257
            with self.assertRaises(NfsObservationError):
                capture_serving_candidate(
                    Path(temp),
                    scoped()["configurations"][0]["parameters"],
                    request,
                    INPUT,
                    b"answer",
                    MOUNT,
                    mapping(),
                    lambda: b"answer",
                )
            self.assertFalse(list(Path(temp).iterdir()))

    def test_invalid_callback_output_is_retained_as_an_error(self) -> None:
        """A wrong callback return type is not interpreted as correct data."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            capture(root, output="wrong-type")
            result = replay_serving_candidate(root)
        self.assertEqual(result["callback_errors"], ["request"])
        self.assertIsNone(result["correct_output_comparison"])

    def test_response_survives_a_later_namespace_failure(self) -> None:
        """Raw response is retained even when the next phase's mapping changes."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state: dict[str, Any] = {}

            def change_namespace() -> bytes:
                state["namespace"] = "mnt:[999]"
                return b"answer"

            with proc(state), self.assertRaises(NfsObservationError):
                capture_serving_candidate(
                    root,
                    scoped()["configurations"][0]["parameters"],
                    specification(),
                    INPUT,
                    b"answer",
                    MOUNT,
                    mapping(),
                    change_namespace,
                    quiet_seconds=0.0001,
                )
            self.assertEqual((root / "request.response.bin").read_bytes(), b"answer")
            self.assertFalse((root / "serving-manifest.json").exists())

    def test_between_phase_activity_is_visible(self) -> None:
        """Payload outside the request and controls is not silently dropped."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            capture(root)
            value = manifest(root)
            # Retained history has 8 KiB between the control and request.
            for phase in ("request", "quiet_after"):
                for direction in ("before", "after"):
                    ref = value["phases"][phase][direction]
                    snapshot_path = root / ref["path"]
                    snapshot = json.loads(snapshot_path.read_text())
                    raw_ref = snapshot["raw"]["mountstats"]
                    raw_path = root / raw_ref["path"]
                    old = parse_mountstats(raw_path.read_text(), MOUNT)
                    raw_path.write_bytes(
                        stats(old.byte_counts[4] + 8192, old.read_rpc_counts[0] + 1)
                    )
                    raw_ref["sha256"] = sha256(raw_path.read_bytes()).hexdigest()
                    snapshot_path.write_text(json.dumps(snapshot))
                    ref["sha256"] = sha256(snapshot_path.read_bytes()).hexdigest()
            replace_manifest(root, value)
            result = replay_serving_candidate(root)
        self.assertEqual(
            result["phase_deltas"]["request"]["server_read_payload_bytes"], PAYLOAD
        )
        self.assertEqual(
            result["between_phase_deltas"]["request"]["server_read_payload_bytes"], 8192
        )
        self.assertIn("interphase_activity", result["review_reasons"])

    def test_input_hash_mismatch_is_refused_before_capture(self) -> None:
        """Submitted input must match the prior frozen request specification."""
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(NfsObservationError):
                capture_serving_candidate(
                    Path(temp),
                    scoped()["configurations"][0]["parameters"],
                    specification(),
                    b"other",
                    b"answer",
                    MOUNT,
                    mapping(),
                    lambda: b"answer",
                )
            self.assertFalse(list(Path(temp).iterdir()))

    def test_invalid_quiet_intervals_are_refused(self) -> None:
        """Timing uses finite positive bounded intervals, not coerced values."""
        for quiet in (True, 0, -1, 61, float("nan"), float("inf"), "1"):
            with (
                tempfile.TemporaryDirectory() as temp,
                self.assertRaises(NfsObservationError),
            ):
                capture_serving_candidate(
                    Path(temp),
                    scoped()["configurations"][0]["parameters"],
                    specification(),
                    INPUT,
                    b"answer",
                    MOUNT,
                    mapping(),
                    lambda: b"answer",
                    quiet_seconds=quiet,
                )

    def test_expected_bytes_cannot_be_fitted_to_the_counter(self) -> None:
        """Changing a receipt's expected bytes cannot override geometry."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            capture(root, payload=8192)
            value = manifest(root)
            value["expected_kv_bytes"] = 8192
            replace_manifest(root, value)
            with self.assertRaisesRegex(NfsObservationError, "geometry"):
                replay_serving_candidate(root)

    def test_original_artifact_tampering_and_missing_data_refuse(self) -> None:
        """Hashes protect response, baseline, configuration, input and snapshots."""
        for name in (
            "baseline.bin",
            "request.response.bin",
            "configuration.json",
            "input.bin",
            "request-before.mountstats.txt",
        ):
            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                capture(root)
                (root / name).write_bytes(b"tampered")
                with self.assertRaises(NfsObservationError):
                    replay_serving_candidate(root)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            capture(root)
            (root / "baseline.bin").unlink()
            with self.assertRaises(NfsObservationError):
                replay_serving_candidate(root)

    def test_manifest_and_artifact_escape_refuse(self) -> None:
        """Neither a manifest symlink nor a traversal can leave the bundle."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "bundle"
            root.mkdir()
            capture(root)
            outside = Path(temp) / "outside.json"
            outside.write_bytes((root / "serving-manifest.json").read_bytes())
            (root / "serving-manifest.json").unlink()
            (root / "serving-manifest.json").symlink_to(outside)
            with self.assertRaises(NfsObservationError):
                replay_serving_candidate(root)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "bundle"
            root.mkdir()
            capture(root)
            outside = Path(temp) / "outside.bin"
            outside.write_bytes(b"answer")
            value = manifest(root)
            value["baseline"] = {
                "path": "../outside.bin",
                "sha256": sha256(b"answer").hexdigest(),
            }
            replace_manifest(root, value)
            with self.assertRaises(NfsObservationError):
                replay_serving_candidate(root)

    def test_legacy_workload_relabel_is_not_a_serving_capture(self) -> None:
        """A legacy file-I/O record cannot become a new producer's evidence."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            make_legacy(root)
            replace_manifest(
                root,
                {
                    "nfs_serving_capture_version": "0.1.0-experimental",
                    "kind": "serving_callback_capture",
                },
            )
            with self.assertRaises(NfsObservationError):
                replay_serving_candidate(root)

    def test_missing_or_additional_phase_is_refused(self) -> None:
        """A completed capture contains exactly both controls and one request."""
        for action in ("missing", "extra"):
            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                capture(root)
                value = manifest(root)
                if action == "missing":
                    del value["phases"]["quiet_before"]
                else:
                    value["phases"]["extra"] = copy.deepcopy(value["phases"]["request"])
                replace_manifest(root, value)
                with self.assertRaises(NfsObservationError):
                    replay_serving_candidate(root)

    def test_timeline_overlap_is_refused(self) -> None:
        """Changing a request's bounds cannot place it outside raw snapshots."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            capture(root)
            value = manifest(root)
            value["phases"]["request"]["interval_ns"] = [0, 1]
            replace_manifest(root, value)
            with self.assertRaises(NfsObservationError):
                replay_serving_candidate(root)

    def test_scope_mapping_tampering_is_checked_after_hash_refresh(self) -> None:
        """A matching outer hash cannot make a different expected mount valid."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            capture(root)
            value = manifest(root)
            path = root / value["mapping"]["path"]
            changed = json.loads(path.read_text())
            changed["mount_namespace"] = "mnt:[999]"
            path.write_text(json.dumps(changed))
            value["mapping"]["sha256"] = sha256(path.read_bytes()).hexdigest()
            replace_manifest(root, value)
            with self.assertRaises(NfsObservationError):
                replay_serving_candidate(root)
