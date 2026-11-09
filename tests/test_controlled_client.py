# SPDX-License-Identifier: Apache-2.0
"""The controlled client keeps arm identity separate from tier attribution."""

# Standard
import importlib.util
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).parents[1] / "scripts" / "g5_controlled_kv_experiment.py"
SCRIPTS_ROOT = SCRIPT.parent
SPEC = importlib.util.spec_from_file_location("controlled_client", SCRIPT)
assert SPEC is not None
assert SPEC.loader is not None
CLIENT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CLIENT)


class ControlledClientArgumentsTest(unittest.TestCase):
    """Verify legacy compatibility and explicit matrix-arm identity."""

    def test_legacy_arm_infers_attribution(self) -> None:
        """Existing callers that use --arm gds keep their old behavior."""
        with patch.object(
            sys,
            "argv",
            ["client", "context", "--arm", "gds", "--output", "/tmp/out.json"],
        ):
            args = CLIENT.parse_args()

        self.assertEqual(args.arm, "gds")
        self.assertEqual(args.attribution, "gds")
        self.assertIsNone(args.backend)
        self.assertEqual(args.filler_key_policy, "unique")
        self.assertEqual(args.prompt_profile, "transport")
        self.assertEqual(args.prompt_seed, 17)
        self.assertEqual(args.needle_position, "early")

    def test_matrix_arm_requires_explicit_attribution(self) -> None:
        """An opaque arm cannot silently choose the wrong counter provider."""
        with patch.object(
            sys,
            "argv",
            [
                "client",
                "context",
                "--arm",
                "nixl_dynamic_gds_mt",
                "--output",
                "/tmp/out.json",
            ],
        ), self.assertRaises(SystemExit):
            CLIENT.parse_args()

    def test_matrix_arm_records_backend_identity(self) -> None:
        """Mode/plugin identity is accepted separately from attribution."""
        with patch.object(
            sys,
            "argv",
            [
                "client",
                "context",
                "--arm",
                "nixl_dynamic_gds_mt",
                "--attribution",
                "gds",
                "--backend",
                "lmcache_nixl_dynamic_gds_mt",
                "--filler-key-policy",
                "reuse",
                "--output",
                "/tmp/out.json",
            ],
        ):
            args = CLIENT.parse_args()

        self.assertEqual(args.arm, "nixl_dynamic_gds_mt")
        self.assertEqual(args.attribution, "gds")
        self.assertEqual(args.backend, "lmcache_nixl_dynamic_gds_mt")
        self.assertEqual(args.filler_key_policy, "reuse")


class PromptProfileTest(unittest.TestCase):
    """The transport and semantic-retrieval workloads coexist."""

    def test_transport_profile_is_deterministic_and_english(self) -> None:
        """The default profile is reproducible and contains no localized text."""
        text, expected, metadata = CLIENT.prompt_text(
            "transport", 3, "TARGET-512", 99, "late"
        )

        self.assertEqual(
            text,
            "TARGET-512 " + CLIENT.SENTENCE * 3 + CLIENT.TAIL,
        )
        self.assertEqual(expected, "STAGE1_D6_OK")
        self.assertEqual(
            metadata,
            {"profile": "transport", "version": "english-v1"},
        )
        self.assertNotRegex(text, r"[\uac00-\ud7a3]")

    def test_retrieval_profile_is_deterministic_and_english(self) -> None:
        """A fixed seed produces repeatable records and a grounded answer."""
        first = CLIENT.prompt_text("retrieval", 12, "CASE-A", 41, "early")
        second = CLIENT.prompt_text("retrieval", 12, "CASE-A", 41, "early")
        text, expected, metadata = first

        self.assertEqual(first, second)
        self.assertNotRegex(text, r"[\uac00-\ud7a3]")
        self.assertIn(f"Verification code: {expected}", text)
        self.assertIn(metadata["needle_record"], text.rsplit("Question:", 1)[1])
        self.assertRegex(expected, r"^\d{6}$")

    def test_needle_positions_cover_the_context(self) -> None:
        """Early, middle, and late select ordered records in the same corpus."""
        selected = []
        for position in ("early", "middle", "late"):
            _, _, metadata = CLIENT.prompt_text(
                "retrieval", 101, "CASE-B", 17, position
            )
            selected.append(int(metadata["needle_record"][1:]))

        self.assertLess(selected[0], selected[1])
        self.assertLess(selected[1], selected[2])

    def test_retrieval_case_records_reproducibility_metadata(self) -> None:
        """The sized case carries enough data to reproduce and audit its prompt."""
        def synthetic_token_count(_port: int, text: str) -> int:
            return 100 + 100 * text.count("[Record R")

        with patch.object(CLIENT, "token_count", side_effect=synthetic_token_count):
            body, count, expected, metadata = CLIENT.build_prompt_case(
                8102, 512, "CASE-C", "retrieval", 23, "middle"
            )

        self.assertEqual(count, 600)
        self.assertEqual(metadata["cacheable_tokens"], 512)
        self.assertEqual(metadata["prompt_tokens"], count)
        self.assertEqual(metadata["expected_output"], expected)
        self.assertEqual(len(metadata["prompt_sha256"]), 64)
        self.assertIn(f"Verification code: {expected}", body["messages"][0]["content"])


class PromptPayloadLanguageTest(unittest.TestCase):
    """Measurement runners must not reintroduce localized prompt literals."""

    def test_measurement_runners_contain_no_hangul_literals(self) -> None:
        """Every script that constructs a prompt remains language-portable."""
        runners = (
            SCRIPTS_ROOT / "g2_context_sweep.py",
            SCRIPTS_ROOT / "g2_context_sweep.sh",
            SCRIPTS_ROOT / "g3_admission_cost.py",
            SCRIPTS_ROOT / "g4_capacity_sweep.py",
            SCRIPTS_ROOT / "g5_controlled_kv_experiment.py",
            SCRIPTS_ROOT / "tools" / "nixl_matched.sh",
            SCRIPTS_ROOT / "tools" / "nixl_mode_matrix.sh",
        )
        for runner in runners:
            with self.subTest(runner=runner.name):
                self.assertNotRegex(
                    runner.read_text(encoding="utf-8"),
                    r"[\uac00-\ud7a3]",
                )


if __name__ == "__main__":
    unittest.main()


class TierDeviceScopeTest(unittest.TestCase):
    """The cgroup counter is read for the tier's device, not every device.

    Summing ``io.stat`` counted the OS disk and any other volume the container
    touched as tier traffic. On the reference host that was 11.3% of the
    reading. The counter now selects one device, which is the difference
    between a corroborating total and evidence about the tier.
    """

    # st_dev packing differs between platforms, so the device numbers are
    # supplied through os.major/os.minor rather than round-tripped through
    # os.makedev. What is under test is the sysfs decision, not that encoding.

    def test_a_whole_disk_path_resolves_to_its_own_key(self) -> None:
        """A tier on a whole disk is reported under that disk."""
        with patch.object(CLIENT.os, "stat"), patch.object(
            CLIENT.os, "major", return_value=259
        ), patch.object(CLIENT.os, "minor", return_value=7), patch.object(
            CLIENT.Path, "exists", autospec=True
        ) as exists:
            # /sys/dev/block/259:7 exists; the partition attribute does not.
            exists.side_effect = lambda self: self.name != "partition"
            self.assertEqual(
                CLIENT.tier_io_device("/tier/reference-cache"), ("259:7", "whole_disk")
            )

    def test_a_partition_widens_to_the_parent_disk_and_says_so(self) -> None:
        """io.stat has no partition line, so matching st_dev alone reads zero.

        Widening is recorded rather than hidden: the counter then also covers
        the disk's other partitions, which the operator has to weigh.
        """
        with patch.object(CLIENT.os, "stat"), patch.object(
            CLIENT.os, "major", return_value=259
        ), patch.object(CLIENT.os, "minor", return_value=6), patch.object(
            CLIENT.Path, "exists", autospec=True, side_effect=lambda self: True
        ), patch.object(
            CLIENT.Path,
            "resolve",
            autospec=True,
            side_effect=lambda self: Path("/sys/block/disk0/disk0p1"),
        ), patch.object(
            CLIENT.Path, "read_text", autospec=True, return_value="259:5\n"
        ):
            self.assertEqual(
                CLIENT.tier_io_device("/tier/reference-cache"), ("259:5", "widened_to_parent_disk")
            )

    def test_an_unresolvable_path_yields_no_device(self) -> None:
        """No device means the caller must withhold, not read a total."""
        self.assertIsNone(CLIENT.tier_io_device("/nonexistent-tier-path"))

    def test_a_readable_cgroup_without_the_device_reads_zero(self) -> None:
        """io.stat lists a device only after the cgroup moved bytes on it.

        Verified on the reference host: a container that has not touched the
        tier has an empty io.stat, and the tier line appears with the exact
        byte count after one direct read. So a readable file without the device
        is a measured zero. The arm must be able to report that: zero device
        bytes is the phantom-tier signature, and a counter that can never read
        zero can never expose one.
        """
        for stat in ("", "259:5 rbytes=8192 wbytes=0\n"):
            with self.subTest(io_stat=stat or "<empty>"):
                with patch.object(CLIENT, "run_text", return_value="1234"), patch.object(
                    CLIENT.Path, "read_text", autospec=True
                ) as read_text:
                    read_text.side_effect = ["0::/scope\n", stat]
                    self.assertEqual(CLIENT.cgroup_read_bytes("ctnr", "259:7"), 0)

    def test_an_unreadable_cgroup_withholds_the_observation(self) -> None:
        """Unknown is not zero: an unreadable cgroup yields no measurement."""
        with patch.object(CLIENT, "run_text", return_value="1234"), patch.object(
            CLIENT.Path, "read_text", autospec=True, side_effect=OSError
        ):
            self.assertEqual(CLIENT.cgroup_read_bytes("ctnr", "259:7"), -1)

    def test_a_missing_container_withholds_the_observation(self) -> None:
        """No container to inspect means no reading, not zero bytes."""
        with patch.object(CLIENT, "run_text", return_value="not-a-pid"):
            self.assertEqual(CLIENT.cgroup_read_bytes("ctnr", "259:7"), -1)

    def test_only_the_named_device_is_counted(self) -> None:
        """The selected line is returned, not the sum over devices."""
        stat = "259:7 rbytes=4096 wbytes=0\n259:5 rbytes=8192 wbytes=0\n"
        with patch.object(CLIENT, "run_text", return_value="1234"), patch.object(
            CLIENT.Path, "read_text", autospec=True
        ) as read_text:
            read_text.side_effect = ["0::/scope\n", stat]
            self.assertEqual(CLIENT.cgroup_read_bytes("ctnr", "259:7"), 4096)

    def test_no_device_reads_nothing(self) -> None:
        """An unresolved tier device cannot produce a reading."""
        self.assertEqual(CLIENT.cgroup_read_bytes("ctnr", ""), -1)


class ArmLauncherTierMountTest(unittest.TestCase):
    """Every cache-arm launcher hands the client the tier's host mount."""

    def test_every_arm_launcher_exports_the_tier_mount(self) -> None:
        """Without PROBE_TIER_MOUNT the client records no device for that arm."""
        for launcher in (
            SCRIPTS_ROOT / "g5_controlled_kv_experiment.sh",
            SCRIPTS_ROOT / "tools" / "nixl_matched.sh",
        ):
            with self.subTest(launcher=launcher.name):
                self.assertRegex(
                    launcher.read_text(encoding="utf-8"),
                    r"(?m)^export PROBE_TIER_MOUNT=",
                )

    def test_every_arm_launcher_accepts_an_explicit_model_directory(self) -> None:
        """A host without a reference container can still name its model directory."""
        for launcher in (
            SCRIPTS_ROOT / "g5_controlled_kv_experiment.sh",
            SCRIPTS_ROOT / "tools" / "nixl_matched.sh",
        ):
            with self.subTest(launcher=launcher.name):
                body = launcher.read_text(encoding="utf-8").split("model_mount() {", 1)[1]
                self.assertIn("MODEL_HOST_DIR", body.split("\n}\n", 1)[0])

    def test_every_client_call_passes_the_filler_size_with_the_count(self) -> None:
        """A filler count without its size silently falls back to the client default."""
        for launcher in (
            SCRIPTS_ROOT / "g5_controlled_kv_experiment.sh",
            SCRIPTS_ROOT / "g3_admission_cost.sh",
            SCRIPTS_ROOT / "tools" / "nixl_matched.sh",
        ):
            text = launcher.read_text(encoding="utf-8")
            with self.subTest(launcher=launcher.name):
                self.assertEqual(text.count("--fillers "), text.count("--filler-tokens "))

