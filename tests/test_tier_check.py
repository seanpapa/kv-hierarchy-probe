# SPDX-License-Identifier: Apache-2.0
"""The tier preflight reports what a local NVMe run can establish, before the run.

Every host read goes through a fake host built from a fixture layout, so no
test touches the real ``/sys`` or ``/proc``. The reference layout is a tier on
its own whole NVMe disk with the root filesystem on a partition of another.
"""

# Standard
import importlib.util
import io
import json
import unittest
from types import SimpleNamespace
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

# First Party
from kvprobe import cli
from kvprobe.tier_check import (
    FAIL,
    NOT_READY,
    PASS,
    READY,
    READY_WITH_WARNINGS,
    SKIP,
    WARN,
    PoolSizes,
    TierCheckRequest,
    mount_for,
    parse_mountinfo,
    render_tier_check,
    resolve_block_device,
    run_tier_check,
)

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "schemas" / "tier-check-v0.1.0.schema.json"
SCRIPT = ROOT / "scripts" / "g5_controlled_kv_experiment.py"
GDSCHECK = "/usr/local/cuda/gds/tools/gdscheck"

MOUNTINFO = """\
22 1 259:2 / / rw,relatime shared:1 - ext4 /dev/nvme0n1p2 rw
23 22 259:1 / /boot/efi rw,relatime shared:2 - vfat /dev/nvme0n1p1 rw
24 22 0:21 / /proc rw,nosuid shared:3 - proc proc rw
25 22 0:25 / /sys/fs/cgroup rw shared:4 - cgroup2 cgroup2 rw
40 22 259:7 / /mnt/tier rw,relatime shared:20 - xfs /dev/nvme1n1 rw,attr2,inode64
"""


class FakeHost:
    """A host made of dictionaries; anything not listed does not exist."""

    def __init__(self) -> None:
        """Build the reference layout."""
        self.files: dict[str, str] = {
            "/proc/self/mountinfo": MOUNTINFO,
            "/sys/dev/block/259:7/size": "",
            "/sys/dev/block/259:2/partition": "2",
            "/sys/dev/block/259:1/partition": "1",
            "/sys/devices/pci0/nvme/nvme0/nvme0n1/dev": "259:0\n",
            "/sys/block/nvme1n1/queue/rotational": "0\n",
            "/sys/block/nvme0n1/queue/rotational": "0\n",
            "/sys/module/nvme_core/parameters/multipath": "N\n",
            "/sys/fs/cgroup/cgroup.controllers": "cpuset cpu io memory pids\n",
            "/proc/driver/nvidia-fs/stats": "GDS Version: 1.0\n",
            "/sys/module/nvidia_fs/parameters/rw_stats_enabled": "1\n",
            "/sys/module/nvidia_fs/parameters/dbg_enabled": "0\n",
            "/etc/cufile.json": "{}\n",
        }
        self.dirs: dict[str, list[str]] = {
            "/sys/dev/block/259:7": ["size"],
            "/sys/dev/block/259:2": ["partition"],
            "/sys/dev/block/259:1": ["partition"],
            "/sys/block/nvme1n1/slaves": [],
            "/sys/block/nvme1n1/holders": [],
            "/sys/block/nvme0n1/slaves": [],
            "/sys/block/nvme0n1/holders": [],
            "/dev": ["null", "nvidia-fs0", "nvidia-fs1"],
            "/run/udev": ["data"],
        }
        self.links: dict[str, str] = {
            "/sys/dev/block/259:7": "/sys/devices/pci0/nvme/nvme1/nvme1n1",
            "/sys/dev/block/259:2": "/sys/devices/pci0/nvme/nvme0/nvme0n1/nvme0n1p2",
            "/sys/dev/block/259:1": "/sys/devices/pci0/nvme/nvme0/nvme0n1/nvme0n1p1",
        }
        self.devices: dict[str, tuple[int, int]] = {
            "/mnt/tier": (259, 7),
            "/": (259, 2),
            "/var/lib/docker": (259, 2),
            "/models": (259, 2),
        }
        self.commands: dict[tuple[str, ...], tuple[int, str]] = {
            (GDSCHECK, "-p"): (
                0,
                " NVMe               : Supported\n properties.use_compat_mode : false\n",
            ),
            ("docker", "info", "--format", "{{.DockerRootDir}}"): (0, "/var/lib/docker\n"),
        }
        self.calls: list[tuple[str, ...]] = []

    def read_text(self, path: str) -> str | None:
        """Return a fixture file."""
        return self.files.get(path)

    def list_dir(self, path: str) -> list[str] | None:
        """Return a fixture directory listing."""
        return self.dirs.get(path)

    def exists(self, path: str) -> bool:
        """Return whether the fixture holds the path."""
        return path in self.files or path in self.dirs or path in self.links

    def is_dir(self, path: str) -> bool:
        """Return whether the fixture holds the path as a directory."""
        return path in self.dirs

    def realpath(self, path: str) -> str:
        """Resolve a fixture link."""
        return self.links.get(path, path)

    def device_numbers(self, path: str) -> tuple[int, int] | None:
        """Return the fixture device of a path."""
        return self.devices.get(path)

    def run(self, command: list[str]) -> tuple[int, str]:
        """Return a fixture command result and record the call."""
        self.calls.append(tuple(command))
        return self.commands.get(tuple(command), (127, "not found"))


def _request(**changes: object) -> TierCheckRequest:
    """Return the reference request with fields replaced."""
    fields: dict[str, object] = {"tier_mount": "/mnt/tier", "arm": "gds", "gdscheck": GDSCHECK}
    fields.update(changes)
    return TierCheckRequest(**fields)  # type: ignore[arg-type]


def _check(document: dict[str, object], check_id: str) -> dict[str, str]:
    """Return one check from a result document."""
    checks = document["checks"]
    assert isinstance(checks, list)
    return next(check for check in checks if check["id"] == check_id)


def _make_partition_tier(host: FakeHost) -> None:
    """Move the tier onto partition 3 of the root disk."""
    host.devices["/mnt/tier"] = (259, 3)
    host.files["/sys/dev/block/259:3/partition"] = "3"
    host.dirs["/sys/dev/block/259:3"] = ["partition"]
    host.links["/sys/dev/block/259:3"] = "/sys/devices/pci0/nvme/nvme0/nvme0n1/nvme0n1p3"
    host.files["/proc/self/mountinfo"] = MOUNTINFO.replace(
        "40 22 259:7 / /mnt/tier rw,relatime shared:20 - xfs /dev/nvme1n1",
        "40 22 259:3 / /mnt/tier rw,relatime shared:20 - xfs /dev/nvme0n1p3",
    )


class ReferenceHostTests(unittest.TestCase):
    """A prepared host is READY, and only unrequested checks are skipped."""

    def test_reference_host_is_ready_with_two_unrequested_checks(self) -> None:
        """T11 and T12 need inputs; everything else passes."""
        document = run_tier_check(_request(), FakeHost())
        self.assertEqual(document["overall"], READY)
        self.assertEqual(document["counts"], {PASS: 10, WARN: 0, FAIL: 0, SKIP: 2})
        self.assertEqual(
            document["device"], {"key": "259:7", "scope": "whole_disk", "disk": "nvme1n1"}
        )
        self.assertEqual(_check(document, "T11")["status"], SKIP)
        self.assertEqual(_check(document, "T12")["status"], SKIP)

    def test_checks_run_in_order_and_say_nothing_extra_on_pass(self) -> None:
        """PASS carries no Why or Fix; the order is T1 to T12."""
        document = run_tier_check(_request(), FakeHost())
        checks = document["checks"]
        assert isinstance(checks, list)
        self.assertEqual([check["id"] for check in checks], [f"T{n}" for n in range(1, 13)])
        for check in checks:
            if check["status"] == PASS:
                self.assertEqual((check["why"], check["fix"]), ("", ""))

    def test_only_read_only_commands_are_run(self) -> None:
        """The check runs gdscheck -p and docker info, nothing else."""
        host = FakeHost()
        run_tier_check(_request(), host)
        self.assertEqual(
            sorted(host.calls),
            sorted([(GDSCHECK, "-p"), ("docker", "info", "--format", "{{.DockerRootDir}}")]),
        )

    def test_unknown_arm_is_refused(self) -> None:
        """An arm the check does not know is a caller error, not a result."""
        with self.assertRaises(ValueError):
            run_tier_check(_request(arm="nvme-of"), FakeHost())


class DeviceTests(unittest.TestCase):
    """T1 and T2: the tier resolves to its own counted disk."""

    def test_missing_tier_path_fails_t1_and_skips_the_device_checks(self) -> None:
        """No device means Gate 1 would be UNPROVEN; T2 to T5 cannot be judged."""
        host = FakeHost()
        del host.devices["/mnt/tier"]
        document = run_tier_check(_request(), host)
        self.assertEqual(_check(document, "T1")["status"], FAIL)
        self.assertIn("UNPROVEN", _check(document, "T1")["why"])
        for check_id in ("T2", "T3", "T4", "T5"):
            self.assertEqual(_check(document, check_id)["status"], SKIP)
        self.assertEqual(document["overall"], NOT_READY)
        self.assertEqual(document["device"]["scope"], "unresolved")  # type: ignore[index]

    def test_a_filesystem_without_a_block_device_fails_t1(self) -> None:
        """tmpfs and overlay have device numbers but no /sys/dev/block entry."""
        host = FakeHost()
        host.devices["/mnt/tier"] = (0, 45)
        t1 = _check(run_tier_check(_request(), host), "T1")
        self.assertEqual(t1["status"], FAIL)
        self.assertIn("0:45 has no block device", t1["finding"])

    def test_a_partition_warns_and_names_the_other_mounts_on_its_disk(self) -> None:
        """Widening is reported with what else the counter now covers."""
        host = FakeHost()
        _make_partition_tier(host)
        document = run_tier_check(_request(), host)
        t1 = _check(document, "T1")
        self.assertEqual(t1["status"], WARN)
        self.assertIn("whole disk nvme0n1 (259:0)", t1["finding"])
        self.assertIn("also mounted from that disk: /, /boot/efi", t1["finding"])
        self.assertEqual(document["device"]["scope"], "widened_to_parent_disk")  # type: ignore[index]

    def test_a_disk_shared_with_root_and_docker_warns(self) -> None:
        """The container's other reads on the same disk cannot be told apart."""
        host = FakeHost()
        _make_partition_tier(host)
        t2 = _check(run_tier_check(_request(model_dir="/models"), host), "T2")
        self.assertEqual(t2["status"], WARN)
        self.assertIn("root filesystem /", t2["finding"])
        self.assertIn("Docker root /var/lib/docker", t2["finding"])
        self.assertIn("model directory /models", t2["finding"])

    def test_a_tier_path_on_the_root_filesystem_fails(self) -> None:
        """A missing mount leaves the directory on the boot disk (catalog §9)."""
        host = FakeHost()
        host.devices["/mnt/tier"] = (259, 2)
        host.files["/proc/self/mountinfo"] = MOUNTINFO.replace(
            "40 22 259:7 / /mnt/tier rw,relatime shared:20 - xfs /dev/nvme1n1 rw,attr2,inode64\n", ""
        )
        t2 = _check(run_tier_check(_request(), host), "T2")
        self.assertEqual(t2["status"], FAIL)
        self.assertIn("root filesystem", t2["finding"])
        self.assertIn("§9", t2["fix"])

    def test_docker_root_not_compared_is_said_and_does_not_fail(self) -> None:
        """Without docker info the comparison is partial and says so."""
        host = FakeHost()
        del host.commands[("docker", "info", "--format", "{{.DockerRootDir}}")]
        t2 = _check(run_tier_check(_request(), host), "T2")
        self.assertEqual(t2["status"], PASS)
        self.assertIn("not compared: Docker root (docker info unavailable)", t2["finding"])

    def test_a_stacked_root_on_the_tier_disk_is_found_through_the_mapper(self) -> None:
        """LVM on the tier disk still shares it, even under another device name."""
        host = FakeHost()
        host.devices["/"] = (253, 0)
        host.dirs["/sys/dev/block/253:0"] = ["dm"]
        host.links["/sys/dev/block/253:0"] = "/sys/devices/virtual/block/dm-0"
        host.dirs["/sys/block/dm-0/slaves"] = ["nvme1n1"]
        t2 = _check(run_tier_check(_request(), host), "T2")
        self.assertEqual(t2["status"], WARN)
        self.assertIn("root filesystem /", t2["finding"])


class DeviceClassTests(unittest.TestCase):
    """T3 to T5: only a plain local NVMe namespace is verified."""

    def _with_tier_disk(self, name: str, rotational: str) -> FakeHost:
        """Return the reference host with the tier on another disk."""
        host = FakeHost()
        host.links["/sys/dev/block/259:7"] = f"/sys/devices/pci0/ata/{name}"
        host.files[f"/sys/block/{name}/queue/rotational"] = rotational
        return host

    def test_a_sata_disk_warns_and_multipath_does_not_apply(self) -> None:
        """Another device class is unverified; NVMe multipath is irrelevant."""
        document = run_tier_check(_request(), self._with_tier_disk("sdb", "1\n"))
        self.assertEqual(_check(document, "T3")["status"], WARN)
        self.assertIn("sdb is not an NVMe namespace", _check(document, "T3")["finding"])
        self.assertEqual(_check(document, "T4")["status"], SKIP)

    def test_native_multipath_on_warns(self) -> None:
        """Attribution with multipath on is unverified, not a measured failure."""
        host = FakeHost()
        host.files["/sys/module/nvme_core/parameters/multipath"] = "Y\n"
        t4 = _check(run_tier_check(_request(), host), "T4")
        self.assertEqual(t4["status"], WARN)
        self.assertIn("multipath=Y", t4["finding"])

    def test_unreadable_multipath_parameter_is_skipped(self) -> None:
        """A built-in nvme_core without the parameter is not guessed."""
        host = FakeHost()
        del host.files["/sys/module/nvme_core/parameters/multipath"]
        self.assertEqual(_check(run_tier_check(_request(), host), "T4")["status"], SKIP)

    def test_a_device_mapper_tier_warns_but_its_nvme_disk_counts_for_t3(self) -> None:
        """LVM over NVMe is stacked (T5) on an NVMe device (T3)."""
        host = FakeHost()
        host.devices["/mnt/tier"] = (253, 1)
        host.dirs["/sys/dev/block/253:1"] = ["dm"]
        host.links["/sys/dev/block/253:1"] = "/sys/devices/virtual/block/dm-1"
        host.dirs["/sys/block/dm-1/slaves"] = ["nvme1n1p1"]
        host.dirs["/sys/block/dm-1/holders"] = []
        host.files["/sys/class/block/nvme1n1p1/partition"] = "1"
        host.links["/sys/class/block/nvme1n1p1"] = "/sys/devices/pci0/nvme/nvme1/nvme1n1/nvme1n1p1"
        document = run_tier_check(_request(), host)
        self.assertEqual(_check(document, "T3")["status"], PASS)
        t5 = _check(document, "T5")
        self.assertEqual(t5["status"], WARN)
        self.assertIn("dm-1 is stacked on nvme1n1", t5["finding"])

    def test_a_held_disk_warns(self) -> None:
        """A disk claimed by a mapper is not a plain namespace."""
        host = FakeHost()
        host.dirs["/sys/block/nvme1n1/holders"] = ["dm-3"]
        t5 = _check(run_tier_check(_request(), host), "T5")
        self.assertEqual(t5["status"], WARN)
        self.assertIn("held by dm-3", t5["finding"])


class CounterTests(unittest.TestCase):
    """T6 and T9: the counters Gate 1 and Gates 3–4 read exist and count."""

    def test_no_cgroup_v2_fails(self) -> None:
        """The per-device counter would read -1."""
        host = FakeHost()
        del host.files["/sys/fs/cgroup/cgroup.controllers"]
        t6 = _check(run_tier_check(_request(), host), "T6")
        self.assertEqual(t6["status"], FAIL)
        self.assertIn("UNPROVEN", t6["why"])

    def test_cgroup_v2_without_io_fails(self) -> None:
        """A mounted v2 hierarchy without io is still unreadable for the tier."""
        host = FakeHost()
        host.files["/sys/fs/cgroup/cgroup.controllers"] = "cpu memory pids\n"
        self.assertEqual(_check(run_tier_check(_request(), host), "T6")["status"], FAIL)

    def test_missing_nvidia_fs_fails(self) -> None:
        """Without the driver there is no native path and no corroboration."""
        host = FakeHost()
        del host.files["/proc/driver/nvidia-fs/stats"]
        host.dirs["/dev"] = ["null"]
        t9 = _check(run_tier_check(_request(), host), "T9")
        self.assertEqual(t9["status"], FAIL)
        self.assertIn("/proc/driver/nvidia-fs/stats and no /dev/nvidia-fs*", t9["finding"])

    def test_counters_off_fail(self) -> None:
        """rw_stats_enabled=0 makes native runs look like compat (catalog §9)."""
        host = FakeHost()
        host.files["/sys/module/nvidia_fs/parameters/rw_stats_enabled"] = "0\n"
        t9 = _check(run_tier_check(_request(), host), "T9")
        self.assertEqual(t9["status"], FAIL)
        self.assertIn("§9", t9["fix"])
        self.assertIn("returns to its default after a reboot", t9["fix"])
        self.assertIn("tee /sys/module/nvidia_fs/parameters/rw_stats_enabled", t9["fix"])

    def test_debug_logging_warns(self) -> None:
        """Debug on depresses throughput but does not invalidate attribution."""
        host = FakeHost()
        host.files["/sys/module/nvidia_fs/parameters/dbg_enabled"] = "1\n"
        document = run_tier_check(_request(), host)
        self.assertEqual(_check(document, "T9")["status"], WARN)
        self.assertEqual(document["overall"], READY_WITH_WARNINGS)

    def test_unreadable_counter_parameter_is_skipped(self) -> None:
        """A parameter this user cannot read is not reported as on or off."""
        host = FakeHost()
        del host.files["/sys/module/nvidia_fs/parameters/rw_stats_enabled"]
        self.assertEqual(_check(run_tier_check(_request(), host), "T9")["status"], SKIP)


class CuFileTests(unittest.TestCase):
    """T7, T8 and T10: cuFile can take the native path on this mount."""

    def _with_tier_fs(self, fstype: str, options: str) -> FakeHost:
        """Return the reference host with the tier on another filesystem."""
        host = FakeHost()
        host.files["/proc/self/mountinfo"] = MOUNTINFO.replace(
            "- xfs /dev/nvme1n1 rw,attr2,inode64", f"- {fstype} /dev/nvme1n1 {options}"
        )
        return host

    def test_ext4_without_data_ordered_fails(self) -> None:
        """cuFile needs the option as a string (catalog §4)."""
        t7 = _check(run_tier_check(_request(), self._with_tier_fs("ext4", "rw")), "T7")
        self.assertEqual(t7["status"], FAIL)
        self.assertIn("§4", t7["fix"])

    def test_ext4_with_data_ordered_passes(self) -> None:
        """The superblock options carry data=ordered."""
        host = self._with_tier_fs("ext4", "rw,data=ordered")
        self.assertEqual(_check(run_tier_check(_request(), host), "T7")["status"], PASS)

    def test_another_filesystem_fails(self) -> None:
        """Local native GDS supports XFS and ordered ext4."""
        host = self._with_tier_fs("btrfs", "rw")
        self.assertEqual(_check(run_tier_check(_request(), host), "T7")["status"], FAIL)

    def test_compat_mode_fails_and_reports_the_nvme_line(self) -> None:
        """Compat is a different path at the same speed (catalog §3)."""
        host = FakeHost()
        host.commands[(GDSCHECK, "-p")] = (
            0,
            " NVMe : Supported\n properties.use_compat_mode : true\n",
        )
        t8 = _check(run_tier_check(_request(), host), "T8")
        self.assertEqual(t8["status"], FAIL)
        self.assertIn("NVMe: Supported", t8["finding"])

    def test_gdscheck_missing_or_silent_is_skipped(self) -> None:
        """No answer is not a pass."""
        host = FakeHost()
        self.assertEqual(
            _check(run_tier_check(_request(gdscheck=""), host), "T8")["status"], SKIP
        )
        host.commands[(GDSCHECK, "-p")] = (0, "GDS release version: 1.0\n")
        t8 = _check(run_tier_check(_request(), host), "T8")
        self.assertEqual(t8["status"], SKIP)
        self.assertIn("no use_compat_mode line", t8["finding"])

    def test_missing_udev_fails(self) -> None:
        """The container's 5008 has this cause (catalog §5)."""
        host = FakeHost()
        del host.dirs["/run/udev"]
        t10 = _check(run_tier_check(_request(), host), "T10")
        self.assertEqual(t10["status"], FAIL)
        self.assertIn("/run/udev", t10["finding"])

    def test_posix_arm_skips_every_cufile_check(self) -> None:
        """The POSIX arm is judged on the device and counter checks only."""
        host = FakeHost()
        del host.files["/proc/driver/nvidia-fs/stats"]
        document = run_tier_check(
            _request(arm="posix", pool=PoolSizes(cufile_buf_mib=8192, mib_per_chunk=64)), host
        )
        for check_id in ("T7", "T8", "T9", "T10", "T11"):
            self.assertEqual(_check(document, check_id)["status"], SKIP, check_id)
        self.assertEqual(document["overall"], READY)
        self.assertNotIn((GDSCHECK, "-p"), host.calls)


class StagingPoolTests(unittest.TestCase):
    """T11: the staging pool against one entry and the working set."""

    def _t11(self, pool_mib: int, fillers: int = 6) -> dict[str, str]:
        """Judge the runbook workload (64 MiB chunks, 4,096 tokens)."""
        pool = PoolSizes(cufile_buf_mib=pool_mib, mib_per_chunk=64, fillers=fillers)
        return _check(run_tier_check(_request(pool=pool), FakeHost()), "T11")

    def test_the_runbook_pool_of_one_entry_passes(self) -> None:
        """1,024 MiB is exactly one 4,096-token entry of 64 MiB chunks."""
        t11 = self._t11(1024)
        self.assertEqual(t11["status"], PASS)
        self.assertIn("entry 1024 MiB", t11["finding"])

    def test_a_pool_above_one_entry_warns(self) -> None:
        """Gate 4's one-entry rule (catalog §13)."""
        t11 = self._t11(3072)
        self.assertEqual(t11["status"], WARN)
        self.assertIn("§13", t11["fix"])

    def test_a_pool_holding_the_working_set_fails(self) -> None:
        """Entry plus six fillers is 7,168 MiB: a phantom tier (catalog §2)."""
        t11 = self._t11(7168)
        self.assertEqual(t11["status"], FAIL)
        self.assertIn("working set 7168 MiB", t11["finding"])

    def test_without_fillers_a_pool_above_one_entry_fails(self) -> None:
        """With no fillers the working set is the entry itself."""
        self.assertEqual(self._t11(2048, fillers=0)["status"], FAIL)

    def test_a_partial_trailing_chunk_counts_as_a_chunk(self) -> None:
        """4,100 tokens need 17 chunks of 256."""
        pool = PoolSizes(cufile_buf_mib=1024, mib_per_chunk=64, context_tokens=4100)
        t11 = _check(run_tier_check(_request(pool=pool), FakeHost()), "T11")
        self.assertIn("entry 1088 MiB", t11["finding"])
        self.assertEqual(t11["status"], PASS)


class TierDirectoryTests(unittest.TestCase):
    """T12: nothing left from an earlier run is served as a hit."""

    def test_leftover_entries_fail(self) -> None:
        """A non-empty directory is reported with its count."""
        host = FakeHost()
        host.dirs["/mnt/tier/cache"] = ["lost+found", "chunk-0", "chunk-1"]
        t12 = _check(run_tier_check(_request(tier_dir="/mnt/tier/cache"), host), "T12")
        self.assertEqual(t12["status"], FAIL)
        self.assertIn("2 entries left", t12["finding"])

    def test_only_lost_and_found_passes(self) -> None:
        """A fresh filesystem root is empty."""
        host = FakeHost()
        host.dirs["/mnt/tier/cache"] = ["lost+found"]
        t12 = _check(run_tier_check(_request(tier_dir="/mnt/tier/cache"), host), "T12")
        self.assertEqual(t12["status"], PASS)

    def test_a_directory_not_created_yet_passes(self) -> None:
        """The launcher will create it."""
        t12 = _check(run_tier_check(_request(tier_dir="/mnt/tier/new"), FakeHost()), "T12")
        self.assertEqual(t12["status"], PASS)

    def test_an_unlistable_directory_is_skipped(self) -> None:
        """Root-owned contents are not guessed."""
        host = FakeHost()
        host.files["/mnt/tier/cache"] = ""
        t12 = _check(run_tier_check(_request(tier_dir="/mnt/tier/cache"), host), "T12")
        self.assertEqual(t12["status"], SKIP)


class MountTableTests(unittest.TestCase):
    """The mount table is read the way the kernel writes it."""

    def test_escaped_mount_points_and_super_options(self) -> None:
        """Octal escapes are decoded; superblock options are kept."""
        entries = parse_mountinfo(
            "40 22 259:7 / /mnt/my\\040tier rw,relatime shared:1 - ext4 /dev/x rw,data=ordered\n"
        )
        self.assertEqual(entries[0].mount_point, "/mnt/my tier")
        self.assertIn("data=ordered", entries[0].options)

    def test_the_longest_and_latest_mount_wins(self) -> None:
        """A stacked mount over the tier replaces the one beneath it."""
        entries = parse_mountinfo(
            MOUNTINFO + "41 40 0:50 / /mnt/tier rw shared:21 - tmpfs tmpfs rw\n"
        )
        entry = mount_for("/mnt/tier/cache", entries)
        assert entry is not None
        self.assertEqual(entry.fstype, "tmpfs")


class RenderTests(unittest.TestCase):
    """The text answers the operator; SKIP is never counted as a pass."""

    def test_render_shows_why_and_fix_only_for_warn_and_fail(self) -> None:
        """A failing check carries its reason and the documented fix."""
        host = FakeHost()
        host.files["/sys/module/nvme_core/parameters/multipath"] = "Y\n"
        host.files["/sys/fs/cgroup/cgroup.controllers"] = "cpu\n"
        text = render_tier_check(run_tier_check(_request(), host))
        self.assertIn("FAIL  T6", text)
        self.assertIn("WARN  T4", text)
        self.assertEqual(text.count("Why:"), 2)
        self.assertIn("Overall: NOT READY (2 not checked)", text)
        self.assertIn("It is not Gate 1 evidence", text)


class CommandTests(unittest.TestCase):
    """Exit codes: 0 no FAIL, 1 a FAIL, 2 usage or not Linux."""

    def _run(self, arguments: list[str], host: FakeHost, system: str = "Linux") -> tuple[int, str, str]:
        """Run the command against a fake host."""
        out, err = io.StringIO(), io.StringIO()
        with patch("kvprobe.tier_check.platform.system", return_value=system), patch.object(
            cli, "LocalHost", return_value=host
        ), patch.object(cli, "find_gdscheck", return_value=GDSCHECK), patch.dict(
            "os.environ", {}, clear=True
        ), redirect_stdout(out), redirect_stderr(err):
            code = cli.main(["tier-check", "--tier-mount", "/mnt/tier", *arguments])
        return code, out.getvalue(), err.getvalue()

    def test_ready_exits_zero(self) -> None:
        """No FAIL is exit 0."""
        code, out, _ = self._run([], FakeHost())
        self.assertEqual(code, 0)
        self.assertIn("Overall: READY (2 not checked)", out)

    def test_a_failing_check_exits_one(self) -> None:
        """Any FAIL is exit 1."""
        host = FakeHost()
        del host.dirs["/run/udev"]
        self.assertEqual(self._run([], host)[0], 1)

    def test_not_linux_exits_two_and_checks_nothing(self) -> None:
        """macOS and others get a clear message, not a page of SKIP."""
        host = FakeHost()
        code, out, err = self._run([], host, system="Darwin")
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("nothing was checked", err)
        self.assertEqual(host.calls, [])

    def test_pool_size_without_chunk_size_is_a_usage_error(self) -> None:
        """T11 needs both sizes."""
        code, _, err = self._run(["--cufile-buf-mib", "1024"], FakeHost())
        self.assertEqual(code, 2)
        self.assertIn("given together", err)

    def test_json_goes_to_stdout_and_the_summary_to_stderr(self) -> None:
        """A script reading stdout gets JSON alone."""
        code, out, err = self._run(
            ["--json", "--cufile-buf-mib", "1024", "--mib-per-chunk", "64"], FakeHost()
        )
        self.assertEqual(code, 0)
        document = json.loads(out)
        self.assertEqual(document["tier_check_version"], "0.1.0")
        self.assertEqual(_check(document, "T11")["status"], PASS)
        self.assertIn("Overall: READY", err)

    def test_gdscheck_comes_from_the_environment_before_the_search(self) -> None:
        """GDSCHECK set by the runbook is the one run."""
        host = FakeHost()
        host.commands[("/opt/gds/gdscheck", "-p")] = (0, "use_compat_mode : false\n")
        out, err = io.StringIO(), io.StringIO()
        with patch("kvprobe.tier_check.platform.system", return_value="Linux"), patch.object(
            cli, "LocalHost", return_value=host
        ), patch.object(cli, "find_gdscheck", return_value=GDSCHECK), patch.dict(
            "os.environ", {"GDSCHECK": "/opt/gds/gdscheck"}, clear=True
        ), redirect_stdout(out), redirect_stderr(err):
            cli.main(["tier-check", "--tier-mount", "/mnt/tier"])
        self.assertIn(("/opt/gds/gdscheck", "-p"), host.calls)


class ScriptParityTests(unittest.TestCase):
    """kvprobe resolves a tier the way the hardware-verified client does."""

    @classmethod
    def setUpClass(cls) -> None:
        """Load the controlled client script as a module."""
        spec = importlib.util.spec_from_file_location("controlled_client_parity", SCRIPT)
        assert spec is not None and spec.loader is not None
        cls.client = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.client)

    def _script(self, host: FakeHost, path: str) -> tuple[str, str] | None:
        """Run the script's resolver against the same fixture."""
        client = self.client
        numbers = host.devices.get(path)

        def stat(_: str) -> object:
            if numbers is None:
                raise FileNotFoundError(path)
            return SimpleNamespace(st_dev=0)

        with patch.object(client.os, "stat", side_effect=stat), patch.object(
            client.os, "major", return_value=numbers[0] if numbers else 0
        ), patch.object(client.os, "minor", return_value=numbers[1] if numbers else 0), patch.object(
            client.Path, "exists", autospec=True, side_effect=lambda p: host.exists(str(p))
        ), patch.object(
            client.Path, "resolve", autospec=True, side_effect=lambda p: Path(host.realpath(str(p)))
        ), patch.object(
            client.Path,
            "read_text",
            autospec=True,
            side_effect=lambda p, *a, **k: host.files[str(p)],
        ):
            return client.tier_io_device(path)

    def _kvprobe(self, host: FakeHost, path: str) -> tuple[str, str] | None:
        """Run the kvprobe resolver, in the script's return shape."""
        numbers = host.devices.get(path)
        if numbers is None:
            return None
        device = resolve_block_device(host, *numbers)
        return (device.key, device.scope) if device.resolved else None

    def test_whole_disk_partition_unresolved_and_missing_agree(self) -> None:
        """Same key and scope for every layout the fixtures cover."""
        host = FakeHost()
        host.devices["/mnt/tmpfs"] = (0, 45)
        expected = {
            "/mnt/tier": ("259:7", "whole_disk"),
            "/": ("259:0", "widened_to_parent_disk"),
            "/mnt/tmpfs": None,
            "/mnt/missing": None,
        }
        for path, answer in expected.items():
            with self.subTest(path=path):
                self.assertEqual(self._kvprobe(host, path), answer)
                self.assertEqual(self._script(host, path), answer)


def _jsonschema_usable() -> bool:
    """Return whether the test-only validator is installed."""
    try:
        # Third Party
        from jsonschema import Draft202012Validator  # noqa: F401
    except ImportError:
        return False
    return True


@unittest.skipUnless(_jsonschema_usable(), "jsonschema is a test-only dependency")
class SchemaTests(unittest.TestCase):
    """Every result shape satisfies the published schema."""

    def test_results_satisfy_the_schema(self) -> None:
        """Ready, warning, failing, unresolved and POSIX results all validate."""
        # Third Party
        from jsonschema import Draft202012Validator

        validator = Draft202012Validator(json.loads(SCHEMA.read_text(encoding="utf-8")))
        partition = FakeHost()
        _make_partition_tier(partition)
        missing = FakeHost()
        del missing.devices["/mnt/tier"]
        cases = {
            "ready": run_tier_check(_request(), FakeHost()),
            "warnings": run_tier_check(_request(), partition),
            "unresolved": run_tier_check(_request(), missing),
            "posix": run_tier_check(_request(arm="posix"), FakeHost()),
            "pool": run_tier_check(
                _request(pool=PoolSizes(cufile_buf_mib=7168, mib_per_chunk=64)), FakeHost()
            ),
        }
        for name, document in cases.items():
            with self.subTest(case=name):
                errors = [error.message for error in validator.iter_errors(document)]
                self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
