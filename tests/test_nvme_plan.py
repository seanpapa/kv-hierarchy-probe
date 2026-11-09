# SPDX-License-Identifier: Apache-2.0
"""Synthetic Linux preflight tests; do not escalate privileges or attach probes."""

# Standard
import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any, TextIO
from unittest.mock import patch

# First Party
from kvprobe.cli import main
from kvprobe.nvme_plan import TracePlanError, build_trace_plan


class NvmePlanTest(unittest.TestCase):
    """Exercise literal argument validation and read-only preflight decisions."""

    def setUp(self) -> None:
        """Build a synthetic namespace and explicit compatible tracepoint format."""
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.sys = self.root / "sys"
        self.proc = self.root / "proc"
        self.trace = self.root / "trace"
        self.save(self.sys / "class/block/nvme0n1/dev", "259:0")
        self.save(self.sys / "class/block/nvme0n1/queue/logical_block_size", "4096")
        self.save(self.sys / "class/block/nvme0n1/queue/max_sectors_kb", "1280")
        self.save(self.sys / "class/block/nvme0n1/queue/max_hw_sectors_kb", "1280")
        self.save(self.sys / "class/nvme/nvme0/transport", "pcie")
        self.save(
            self.proc / "self/mountinfo",
            "1 0 259:0 / /tier/cache rw - ext4 /dev/nvme0n1 rw\n",
        )
        self.setup_format = (
            "field:char disk[32]; offset:8; size:32; signed:1;\n"
            "field:int ctrl_id; offset:40; size:4; signed:1;\n"
            "field:int qid; offset:44; size:4; signed:1;\n"
            "field:u16 cid; offset:52; size:2; signed:0;\n"
            "field:u8 opcode; offset:48; size:1; signed:0;\n"
            "field:u8 cdw10[24]; offset:61; size:24; signed:0;\n"
        )
        self.complete_format = (
            "field:char disk[32]; offset:8; size:32; signed:0;\n"
            "field:int ctrl_id; offset:40; size:4; signed:1;\n"
            "field:int qid; offset:44; size:4; signed:1;\n"
            "field:int cid; offset:48; size:4; signed:1;\n"
            "field:u16 status; offset:66; size:2; signed:0;\n"
        )
        self.setup_path = self.trace / "events/nvme/nvme_setup_cmd/format"
        self.save(self.setup_path, self.setup_format)
        self.save(
            self.trace / "events/nvme/nvme_complete_rq/format", self.complete_format
        )
        for target, value in (
            ("platform.system", "Linux"),
            ("shutil.which", "/tools/bpftrace"),
        ):
            mocked = patch("kvprobe.nvme_plan." + target, return_value=value)
            mocked.start()
            self.addCleanup(mocked.stop)
        mocked = patch(
            "kvprobe.nvme_plan.subprocess.run",
            return_value=subprocess.CompletedProcess(
                ["bpftrace", "--version"], 0, "bpftrace vTEST\n", ""
            ),
        )
        self.command = mocked.start()
        self.addCleanup(mocked.stop)

    def save(self, path: Path, content: str) -> None:
        """Write a synthetic capability file inside this test directory."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def plan(
        self, disk: str = "nvme0n1", roots: tuple[Path, ...] | None = None
    ) -> dict:
        """Invoke the public preflight against isolated filesystem fixtures."""
        return build_trace_plan(
            disk,
            sys_root=self.sys,
            proc_root=self.proc,
            trace_roots=roots if roots is not None else (self.trace,),
        )

    def test_eligible_preflight_never_claims_capture_ready(self) -> None:
        """Compatible metadata is not permission to claim capture or serving."""
        result = self.plan("/dev/nvme0n1")
        self.assertEqual(result["preflight_status"], "eligible")
        self.assertEqual(result["logical_block_size"]["value"], 4096)
        self.assertEqual(result["mounts"]["entries"][0]["filesystem"], "ext4")
        self.assertEqual(result["capture_status"], "experimental_unvalidated")
        self.assertIsNone(result["capture_command"])
        self.assertEqual(result["serving_verification"], "not_assessed")
        self.command.assert_called_once_with(
            ["/tools/bpftrace", "--version"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )

    def test_refuse_partitions_aliases_and_shell_arguments(self) -> None:
        """Only whole literal namespace names are accepted."""
        for disk in (
            "nvme0n1p1",
            "/dev/disk/by-id/anything",
            "nvme0n1; id",
            "../nvme0n1",
            "nvme0c0n1",
            "nvme0n0",
            "$(id)",
        ):
            with self.subTest(disk=disk), self.assertRaises(TracePlanError):
                self.plan(disk)
        self.command.assert_not_called()

    def test_non_linux_host_is_unsupported_without_commands(self) -> None:
        """macOS does not attempt Linux file discovery or execute tools."""
        with patch("kvprobe.nvme_plan.platform.system", return_value="Darwin"):
            result = self.plan()
        self.assertEqual(result["reasons"], ["linux_required"])
        self.command.assert_not_called()

    def test_missing_lbs_is_not_defaulted(self) -> None:
        """Unknown block size remains unknown rather than becoming 512."""
        (self.sys / "class/block/nvme0n1/queue/logical_block_size").unlink()
        result = self.plan()
        self.assertEqual(result["logical_block_size"]["status"], "not_found")
        self.assertEqual(result["preflight_status"], "incomplete")

    def test_non_pcie_transport_is_out_of_initial_scope(self) -> None:
        """NVMe-oF is explicitly outside this initial local-PCIe profile."""
        self.save(self.sys / "class/nvme/nvme0/transport", "tcp")
        self.assertEqual(self.plan()["preflight_status"], "unsupported")

    def test_incompatible_fields_are_unsupported(self) -> None:
        """Dynamic disk and non-byte cdw10 arrays cannot use the chosen profile."""
        for source, replacement in (
            ("char disk[32]", "__data_loc char[] disk"),
            ("u8 cdw10[24]", "u32 cdw10[6]"),
            ("size:2; signed:0", "size:4; signed:0"),
        ):
            with self.subTest(source=source):
                self.save(
                    self.setup_path, self.setup_format.replace(source, replacement)
                )
                self.assertEqual(self.plan()["preflight_status"], "unsupported")

    def test_tracefs_fallback_retains_attempts(self) -> None:
        """A missing primary root may fall back to another compatible root."""
        result = self.plan(roots=(self.root / "missing", self.trace))
        self.assertEqual(result["tracepoints"]["selected_root"], str(self.trace))
        self.assertEqual(len(result["tracepoints"]["attempts"]), 2)

    def test_permission_denied_is_not_missing(self) -> None:
        """Unreadable tracefs preserves the permission diagnosis."""
        original = Path.open

        def open_path(path: Path, *args: Any, **kwargs: Any) -> TextIO:
            """Deny format reads only, independently of the test user's UID."""
            if path.name == "format":
                raise PermissionError("synthetic permission denial")
            return original(path, *args, **kwargs)

        with patch("pathlib.Path.open", open_path):
            result = self.plan()
        self.assertEqual(result["tracepoints"]["status"], "permission_denied")
        self.assertEqual(result["preflight_status"], "incomplete")

    def test_partition_mounts_and_escape_decoding(self) -> None:
        """Mount inventory includes partition devices and decoded mount paths."""
        self.save(self.sys / "class/block/nvme0n1/nvme0n1p1/dev", "259:1")
        self.save(
            self.proc / "self/mountinfo",
            "1 0 259:1 / /tier/cache\\040data rw - ext4 /dev/nvme0n1p1 rw\n",
        )
        self.assertEqual(
            self.plan()["mounts"]["entries"][0]["mountpoint"], "/tier/cache data"
        )

    def test_tool_missing_or_timeout_is_incomplete(self) -> None:
        """Tool availability failures do not become capture capability."""
        with patch("kvprobe.nvme_plan.shutil.which", return_value=None):
            self.assertEqual(self.plan()["bpftrace"]["status"], "not_found")
        self.command.side_effect = subprocess.TimeoutExpired("bpftrace", 5)
        self.assertEqual(self.plan()["preflight_status"], "incomplete")

    def test_invalid_numbers_and_mounts_are_not_guessed(self) -> None:
        """Malformed block geometry and mount rows remain unverified."""
        self.save(self.sys / "class/block/nvme0n1/queue/logical_block_size", "3")
        self.save(self.proc / "self/mountinfo", "broken row")
        result = self.plan()
        self.assertEqual(result["logical_block_size"]["status"], "invalid")
        self.assertEqual(result["mounts"]["status"], "incomplete")
        self.assertEqual(result["preflight_status"], "incomplete")

    def test_cli_json_and_statuses(self) -> None:
        """CLI separates preflight statuses from invalid user arguments."""
        output = self.root / "plan.json"
        with patch("kvprobe.cli.build_trace_plan", return_value=self.plan()):
            with (
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                self.assertEqual(
                    main(
                        [
                            "gate1",
                            "plan",
                            "--disk",
                            "nvme0n1",
                            "--json",
                            "--output",
                            str(output),
                        ]
                    ),
                    0,
                )
        self.assertIsNone(json.loads(output.read_text())["capture_command"])
        unavailable = self.plan()
        unavailable["preflight_status"] = "incomplete"
        with patch("kvprobe.cli.build_trace_plan", return_value=unavailable):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(["gate1", "plan", "--disk", "nvme0n1"]), 1)
        with (
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(main(["gate1", "plan", "--disk", "nvme0n1p1"]), 2)

    def test_cli_refuses_kernel_or_device_output_targets(self) -> None:
        """Explicit output must not overwrite sysfs, procfs or a device."""
        for destination in (
            "/sys/example.json",
            "/proc/example.json",
            "/dev/example.json",
        ):
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(
                    main(
                        ["gate1", "plan", "--disk", "nvme0n1", "--output", destination]
                    ),
                    2,
                )
        self.command.assert_not_called()

    def test_complete_cid_width_is_checked_independently(self) -> None:
        """Completion CID width is not assumed equal to setup CID width."""
        self.save(
            self.trace / "events/nvme/nvme_complete_rq/format",
            self.complete_format.replace("int cid", "u16 cid").replace(
                "offset:48; size:4; signed:1", "offset:48; size:2; signed:0"
            ),
        )
        self.assertEqual(self.plan()["preflight_status"], "unsupported")
