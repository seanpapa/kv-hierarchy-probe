# `tier-check` — what each check reads and decides

`python3 -m kvprobe tier-check` is a read-only preflight for a local NVMe tier. It runs in seconds as
step 2 of the [minimal runbook](../scripts/README.md#minimal-runbook--all-five-gates-one-configuration),
before a run that takes about 30 minutes. This document defines its twelve checks (T1–T12), the
statuses they return, the overall result and the exit codes. The command's output cites these IDs, and
so do [FAILURE-CATALOG](FAILURE-CATALOG.md), [CHECKLIST](CHECKLIST.md) and the
[host preconditions](../scripts/README.md#preconditions-on-the-host).

**`READY` is not Gate 1 evidence.** It says that host conditions which would spoil a run were not
found. Only the run's per-request counters show that the tier served a request
([Gate 1](GATE1.md)).

## 1. Why it exists

Several host conditions decide whether a local NVMe run can establish Gate 1 at all: which device the
per-device counter reads, whether that disk carries other traffic, whether cuFile runs native or compat,
whether the `nvidia-fs` counters are on, whether the staging pool is smaller than the working set, and
whether an earlier run left chunks behind. Each of these used to be checked by hand from prose. A missed
one showed up only after the run: Gate 1 came back `UNPROVEN`, or the run measured something other than
the tier. `tier-check` reads those conditions before the run and reports:

- what it could check;
- what it could not check, and whether that blocks readiness;
- for each problem, which later result it would spoil and where the fix is documented.

## 2. Scope

- **In scope:** a local PCIe NVMe tier on one Linux host, for the arms the runbook runs (`gds`,
  `posix`). NIXL static `GDS_MT` is checked as `gds` (`--arm gds`).
- **Read-only.** It reads `/sys`, `/proc` and the mount table, and runs two commands, each with a 10 s
  timeout: `gdscheck -p` and `docker info --format '{{.DockerRootDir}}'`. It never writes, remounts,
  changes module parameters or starts a container, and it does not need `sudo`.
- **Never guessed.** A value it cannot read becomes `SKIP`. A `SKIP` is never counted as a pass.
- **No verdict changes.** The evidence contract, `ingest` and the report are unaffected by it.
- **Recognised, not supported:** NVMe-oF (TCP, RDMA), native NVMe multipath and device-mapper or RAID
  stacking are reported as `WARN`. The method has not verified byte attribution there; a warning does
  not stop the run.
- **Not checked:** GPU idleness, free ports, free space on the tier, background I/O on the device, cold
  reads, fillers and arm plumbing. These remain with the runbook and [CHECKLIST](CHECKLIST.md).

## 3. Statuses

| Status | Meaning |
|---|---|
| `PASS` | The condition holds. |
| `WARN` | The run can proceed, but what it establishes is weaker or unverified. |
| `FAIL` | The run would not establish Gate 1, or would measure something else. |
| `SKIP` | Could not be checked here. The reason is printed. |

Each check also records `required_for_ready`:

- A **required** `SKIP` blocks `READY`: the condition matters for this arm, but it could not be read.
- An **optional** `SKIP` does not block `READY`. This covers a check that does not apply to the
  selected arm, and an optional input that was not supplied.

When a check finds both a warning and something it could not read, the result depends on the check;
the table in §4 gives each rule. In T3, missing evidence outranks a warning: a transport known to be
outside the baseline together with one that cannot be read gives a required `SKIP` that keeps both
findings. In T2, a disk confirmed as shared gives a `WARN` even when another comparison is missing,
because no further comparison could make the disk dedicated; the finding lists what was not compared.

## 4. The checks

The title column is the title the command prints. When T1 cannot resolve a device, T1 fails and T2–T5
return a required `SKIP` naming T1. No other check depends on another's result.

| ID | Title | Reads | Result | Prevents | Fix documented in |
|---|---|---|---|---|---|
| T1 | Tier resolves to a counted block device | `stat` of the mount → `/sys/dev/block/<major>:<minor>`, `partition` | Unresolved → **FAIL**. Whole disk → **PASS**. Partition → **WARN**: the counter covers the parent disk, and the finding lists that disk's other mounted filesystems | Gate 1 `UNPROVEN` with "no recorded device"; a counter that also counts other partitions | [scripts/README](../scripts/README.md#the-posix-arms-cgroup-counter-reads-one-device) |
| T2 | Tier disk is dedicated | The mount table; the disks behind `/`, Docker's root directory and `--model-dir` (if given), through device-mapper and RAID layers | The tier path is on the root filesystem itself → **FAIL** (the mount is missing). The tier's mount is not in `/proc/self/mountinfo` → required **SKIP**. Shares a disk with a compared path → **WARN** naming it and any comparison it could not make. `docker info` unavailable or a compared path unresolved, with nothing shared → required **SKIP**. Otherwise → **PASS** | Measuring the boot disk after the tier mount disappeared; other traffic on the same disk counted as tier bytes | [FAILURE-CATALOG §9](FAILURE-CATALOG.md#9-measurement-plumbing-that-quietly-changes-what-you-measure-principle); [scripts/README](../scripts/README.md#the-posix-arms-cgroup-counter-reads-one-device) |
| T3 | Tier device is local PCIe NVMe | The disk name and `queue/rotational`; the `transport` of the NVMe controller behind the namespace. For a native multipath head, the transport of each path, or of each controller in its subsystem ([§6](#6-native-nvme-multipath-t3-and-t4)) | Not an NVMe namespace, rotational, or any transport other than `pcie` (for example `tcp` or `rdma`) → **WARN** "outside the local PCIe baseline". A rotational flag or transport that cannot be read, or a head whose paths and controllers cannot be listed → required **SKIP**, even when something else is already known to be outside the baseline. Otherwise → **PASS**; a head also names its paths and points to T4 | Reading a result as covering a tier class the method has not verified, including NVMe-oF | [README scope](../README.md#scope--what-is-verified-today) |
| T4 | Native NVMe multipath is not active for the tier | Where the namespace sits in sysfs ([§6](#6-native-nvme-multipath-t3-and-t4)); `/sys/module/nvme_core/parameters/multipath`, reported only | Multipath head → **WARN**. Plain namespace → **PASS** whatever the parameter says; the finding reports it. Neither found and the parameter not `N`/`0` → required **SKIP**; with `N`/`0` → **PASS**. Tier not on NVMe → optional **SKIP** | Bytes recorded on a device number that Gate 1 does not read | [README scope](../README.md#scope--what-is-verified-today); [ROADMAP](ROADMAP.md) |
| T5 | No device stacking | The disk's `slaves/` and `holders/`; `dm-*` and `md*` names | Stacked on lower disks, a device-mapper or RAID device, or held by another device → **WARN**. `slaves` or `holders` cannot be listed → required **SKIP**. Otherwise → **PASS** | As T4, for dm-multipath, LVM and RAID | [ROADMAP](ROADMAP.md) |
| T6 | cgroup v2 io controller | `/sys/fs/cgroup/cgroup.controllers` | No cgroup v2, or no `io` controller → **FAIL**. Otherwise → **PASS** | The per-device counter cannot be read, and Gate 1 withholds the observation | [runbook prerequisites](../scripts/README.md#minimal-runbook--all-five-gates-one-configuration) |
| T7 | Filesystem accepted by cuFile | `/proc/self/mountinfo` for the mount | `xfs` → **PASS**. `ext4` with `data=ordered` in its options → **PASS**. `ext4` without it, or another filesystem → **FAIL**. Mount not found → required **SKIP**. `posix` arm → optional **SKIP** | A silent POSIX fallback, or `cuFileHandleRegister` error 5008 | [FAILURE-CATALOG §4](FAILURE-CATALOG.md#4-ext4-mounted-without-an-explicit-dataordered-stack) |
| T8 | cuFile compat settings disabled | `gdscheck -p`, found from `--gdscheck`, then `$GDSCHECK`, then the same search `doctor` uses | Reads `use_compat_mode` and `force_compat_mode`. Either `true` → **FAIL**. Both `false` → **PASS**. `gdscheck` not found, a nonzero exit (including the timeout) or either value missing → required **SKIP**. The `NVMe` support line is added to the finding. `posix` arm → optional **SKIP** | Measuring compat, which speed cannot tell apart from native. Passing does not prove a native transfer; that still needs the run's evidence | [FAILURE-CATALOG §3](FAILURE-CATALOG.md#3-native-and-compat-are-indistinguishable-by-speed-principle) |
| T9 | nvidia-fs loaded and counting | `/proc/driver/nvidia-fs/stats`, `/dev/nvidia-fs*`, `/sys/module/nvidia_fs/parameters/{rw_stats_enabled,dbg_enabled}` | Stats file or devices absent → **FAIL**. `rw_stats_enabled` not `1`/`Y` → **FAIL**, with the command that turns it on. `dbg_enabled` not `0`/`N` → **WARN** (debug logging depresses throughput). Either parameter unreadable → required **SKIP**. Otherwise → **PASS**. `posix` arm → optional **SKIP** | Attribution that silently reports nothing; a performance run with debug logging on | [host preconditions 3 and 7](../scripts/README.md#preconditions-on-the-host); [FAILURE-CATALOG §9](FAILURE-CATALOG.md#9-measurement-plumbing-that-quietly-changes-what-you-measure-principle) |
| T10 | Container prerequisites for cuFile | `/run/udev` is a directory; `/etc/cufile.json` is readable | Either missing → **FAIL**. Otherwise → **PASS**. `posix` arm → optional **SKIP** | `cuFileHandleRegister` error 5008, inside the container only | [FAILURE-CATALOG §5](FAILURE-CATALOG.md#5-the-same-error-inside-a-container-has-a-different-cause-stack); [host precondition 3](../scripts/README.md#preconditions-on-the-host) |
| T11 | Staging pool below the working set | `--cufile-buf-mib` and `--mib-per-chunk`, given together; `--context-tokens` (default 4096), `--fillers` (6), `--chunk-tokens` (256) | Entry = `mib_per_chunk × ⌈context / chunk⌉`. Working set = `(1 + fillers) × entry`. Pool ≥ working set → **FAIL**. Entry < pool < working set → **WARN**. Pool ≤ entry → **PASS**. Sizes not given, or `posix` arm → optional **SKIP** | A GPU-resident "tier" that never touches the device; Gate 4 misreading staging-pool hits | [FAILURE-CATALOG §2](FAILURE-CATALOG.md#2-the-phantom-tier--a-run-that-never-touched-the-device-principle) and [§13](FAILURE-CATALOG.md#staging-buffer-full); [host precondition 4](../scripts/README.md#preconditions-on-the-host) |
| T12 | Tier directory is empty | A listing of `--tier-dir` | Directory does not exist yet → **PASS**. Entries other than `lost+found` → **FAIL** with the count. Not listable → required **SKIP**. `--tier-dir` not given → optional **SKIP** | Chunks left by an earlier run served as cache hits | [host precondition 5](../scripts/README.md#preconditions-on-the-host) |

**Why T11 counts `1 + fillers` entries.** For a cold read to reach the device, the measured context and
every filler must be evicted from the staging pool. A pool that holds all of them can serve every request
from GPU memory: the phantom tier. A pool between one entry and the working set does not hide the device,
but Gate 4 needs the pool to hold at most one entry to classify hits, so it warns.

**Why T12 is optional.** The reference launchers clear their own directories before each arm. The check
matters for a launcher that does not.

## 5. Output and exit codes

**Text output** (the default) prints one line per check: status, ID, title and finding. Every `WARN`,
every `FAIL` and every required `SKIP` adds a *Why* line and a *Fix* line.

| Overall | When |
|---|---|
| `READY` | No `FAIL`, no `WARN`, no required `SKIP` |
| `READY WITH WARNINGS` | At least one `WARN`; no `FAIL` and no required `SKIP` |
| `NOT READY` | At least one `FAIL` or required `SKIP` |

When any check was skipped, the overall line says how many, for example `READY (1 not checked)`.
`READY WITH WARNINGS` adds a line saying the run may proceed and that the warnings belong with the run's
results. Every result ends by repeating that `READY` is not Gate 1 evidence.

| Exit | Meaning |
|---|---|
| `0` | `READY` or `READY WITH WARNINGS`. A warning does not stop the run. |
| `1` | `NOT READY` |
| `2` | A usage error (including only one of `--cufile-buf-mib` and `--mib-per-chunk`), a host that is not Linux (nothing was checked), or an output file that cannot be written |

The runbook runs step 2 inside `( set -e … )`, so the preflight block stops on `NOT READY` without
closing your shell and continues on `READY WITH WARNINGS`.

**JSON output.** `--json` prints the result as JSON on standard output and moves the text summary to
standard error. `--output FILE` also saves the JSON. The document holds `tier_mount`, `arm`,
`device` (`key`, `scope`, `disk`), twelve `checks`, `counts` and `overall`. Each check has `id`,
`title`, `status`, `finding`, `why`, `fix` and `required_for_ready`. It is a public data contract,
validated by the schema in [`schemas/`](../schemas/); its version is in
[VERSIONING](VERSIONING.md).

```text
python3 -m kvprobe tier-check --tier-mount PATH [--arm gds|posix] [--tier-dir PATH]
    [--model-dir PATH] [--gdscheck PATH]
    [--cufile-buf-mib N --mib-per-chunk N] [--context-tokens N] [--fillers N] [--chunk-tokens N]
    [--output FILE] [--json]
```

`--context-tokens`, `--cufile-buf-mib`, `--mib-per-chunk` and `--chunk-tokens` must be positive
integers; `--fillers` must be a non-negative integer.

## 6. Native NVMe multipath (T3 and T4)

`nvme_core`'s `multipath` parameter defaults to `Y`, so the parameter alone would warn on nearly every
host. T4 therefore looks at the tier's own device instead:

- The kernel creates a **multipath head** only when the NVMe subsystem reports more than one controller
  (or, from mainline v6.16, when `multipath_always_on` is set). It registers the head under the
  subsystem device, so a head has an `nvme-subsysN` ancestor in sysfs.
- A namespace without a head stays a **plain disk under its controller** (`nvmeN`), with the parameter
  at `Y` as with `N`.

T3 checks the transport of everything that can carry a head's I/O:

- From mainline v6.15, the head's `/sys/block/<disk>/multipath/` directory links its path disks; T3
  reads each path's controller and that controller's `transport`.
- Before v6.15 those links do not exist. T3 then reads every controller linked from
  `/sys/class/nvme-subsystem/nvme-subsysN/` (present in the v5.15 and v6.8 sources). That set can
  include a controller that does not serve this namespace, so a non-PCIe controller there may warn
  conservatively.
- Distribution kernels may backport either feature, so T3 tests for the links, not the kernel version.
  Some distribution kernels show an empty `multipath` directory on plain namespaces as well, so a head
  is recognised by its `nvme-subsysN` ancestor, never by that directory.

## 7. Known limits

- **Multipath heads are fixture-tested only.** Both of T3's head routes are covered by tests, but no
  multipath head has been checked on hardware. A `PASS` for a plain namespace under `multipath=Y`
  follows from the kernel source; the reference runs used `N`. A kernel with neither route gives a
  required `SKIP` that names the head and T4.
- **T8 and older GDS releases.** A `gdscheck` that prints no `force_compat_mode` line gives a required
  `SKIP`, so such a host cannot reach `READY`.
- **Configuration, not behaviour.** T7, T8 and T10 check the settings that make cuFile fall back or
  fail; they do not observe a transfer. The run's evidence decides whether the tier served.
