# Gate 1 — NVMe observations (experimental)

For **first-visit → Gate 1 → Gates 2–5**, start with [the Gate 1 user guide](GATE1.md).
This page is the optional NVMe path's contract, not a mandatory tracing prerequisite.

Gate 1 asks whether a measurement supports the claimed serving tier. This first
implementation adds **offline namespace observations** to that investigation.
It also provides **read-only capture preflight** and an opt-in experimental
root-only collector. These observations do not grant a Gate 1 serving PASS;
the separate reviewed companion is described at the end of this page.

## Experimental live collector — acceptance pending

Collector **0.3.0-experimental** emits input contract **0.2.0**, appending the
probe's `cpu` ID to every S/C record. This CPU is the tracepoint execution CPU,
not a request owner, GPU identity or NVMe queue owner. Supported-host Linux
smokes have exercised compilation, CPU fields and scoped record-delivery accounting.
Independent filter/clock and whole serving acceptance remain open; this is not
broad kernel/device qualification. Observations from other collector revisions do not qualify this source.

The public analyzer accepts CPU-bearing input contract 0.2.0 only. It never
invents CPU identity for captures. Widths are strict: S/9 and C/7 fields; inputs
without CPU fields are refused.
New collector manifests mark missing CPU-bearing records as an incomplete
capture even when scoped record-delivery loss happens to be zero.

```bash
# Explicit root execution, trusted checkout/interpreter, new root-owned output.
sudo python3 -m kvprobe gate1 capture --disk nvme0n1 \
  --run-dir /tmp/kvprobe-capture-new --duration-seconds 30
```

Do not run this example until the selected device and trusted source are reviewed.
It attaches probes but generates no I/O, clears no cache and controls no serving
process. Initial scope requires bpftrace **v0.20.2**, eligible local PCIe preflight,
and a new absolute output directory under root-owned, non-symlink parents.
Root-owned sticky `/tmp` is allowed. An untrusted/writable executable is refused.
Output is private (0700 directory); export requires an explicit privileged copy.
For remote runs, wrap the command in `nohup`, retaining its log and exit code.

The first post-attachment interval tick emits `ready.json`; `BEGIN` is not used
as readiness. Every event uses `nsecs(monotonic)`, not the version's default
BOOTTIME clock. Source: [bpftrace v0.20.2 reference](https://github.com/bpftrace/bpftrace/blob/v0.20.2/docs/reference_guide.md).
The collector retains `capture.bt`, `preflight.json`, raw `bpftrace.out`, normalized
`trace.csv`, `bpftrace.err`
and `manifest.json`. Controller ID comes from matching trace records, not the
namespace's numeric name. A no-I/O run leaves it unknown. Records are not sorted.

Duration is 1–3600 seconds after readiness, with a duration+60 second watchdog.
A polled 128 MiB combined stdout/stderr ceiling stops capture; this is a soft
limit, not a disk quota. SIGINT/timeout/size-stop records remain incomplete.
Do not interrupt with SIGKILL and expect a finalized manifest.

Scoped loss accounting was introduced in **0.2.0-experimental** and exercised
in supported-host Linux smokes. CLI exit 0 requires clean capture,
one observed controller and scoped loss zero; 1 means incomplete/loss/no observed
identity, and 2 is an invocation/prerequisite failure. No status is a Gate 1 PASS.
Do not change old unknown loss to zero manually. Trace clock/filter acceptance
and a serial producer's `phases.csv` remain required. A raw capture alone is
not directly analyzable without valid phase
records; missing ready/end/controller identity is not filled with invented values.
Performance measurements with tracing enabled must stay separate from trace-off
results. Evidence v0.3 and serving confidence are unchanged.

### Scoped loss accounting

- Each matched setup/completion probe increments a separate per-CPU `count()`
  map before emitting its CSV record. A BEGIN sentinel initializes each map;
  it is not readiness and is subtracted from the final count.
- After detachment, bpftrace's normal final map output supplies both counts.
  Their names/decimal values are parsed strictly. Missing, duplicate, invalid
  or smaller-than-retained counters block zero.
- `Lost N events` notices are read from stdout and stderr. The text form comes
  from [bpftrace 0.20.2 output source](https://github.com/bpftrace/bpftrace/blob/v0.20.2/src/output.cpp).
  Original stdout is immutable; only validated S/C and marker lines enter the
  normalized CSV. Unknown stdout/stderr diagnostics block zero.
- Zero requires ready/end, a clean tool exit, no forced stop, both final counts,
  matching retained record counts and no loss notices. Positive loss is a
  **lower bound**: max(reported lost events, setup+completion record deficit),
  not their sum because the signals may refer to the same losses.
- Scope is **matched probe invocations → retained records**, not all NVMe
  hardware commands. This does not validate filtering, clock alignment, probe
  coverage or per-request source attribution. Perfect pairing alone is not a
  zero-loss test: a missing setup/completion pair can leave retained pairing clean.

Manifest additions record the counter/record counts, method/reasons and raw
stdout SHA-256. Analyzer input hashes include raw stdout when present. Missing
counter output leaves loss unknown; no migration fills it in. Collector
0.3.0-experimental uses input contract 0.2.0 with CPU fields. The normalized
evidence document is a separate contract; see [VERSIONING](VERSIONING.md).

## Opt-in serial producer phase export (experimental)

The controlled g5 client accepts `--phase-export-dir <new-directory>` in Linux
`context` mode for cache arms. Run from a trusted checkout with the matching
`kvprobe` package importable (installed, or explicitly set `PYTHONPATH` to that
checkout). A copied standalone client without the package still works when this
option is absent. Existing JSON sample fields and legacy behavior are unchanged.

This is a direct Python-client option. Shell runners do not forward it or attach
capture automatically. See the [Gate 1 walkthrough](GATE1.md#optional-nvme-walkthrough)
for readiness, phase export and separate analysis-view preparation.

The new directory contains:

- `lifecycle.csv`: target-prime, store-settle and eviction-pressure intervals.
- `request.csv`: the target HTTP call only, excluding snapshots and log lookup.
- `envelope.csv`: snapshots, target HTTP call and subsequent audit collection.
- `producer.json`: producer source hash, clock and intended request evidence path.

The CSV files contain normalized phase columns plus `outcome` and `error_type`.
Completed means the wrapped call returned, **not** correct output or complete
cache transfer. Exceptions are recorded and re-raised; only the class name is
retained, never its potentially sensitive message. Raw request JSON/logs still
require separate audit. Warmup and measurement are distinguished in unique labels.

Request and envelope views are nested in time but kept in **separate files**.
Select one retrieval view as the analyzer's `phases.csv`; never concatenate these
views or sum their bytes. Lifecycle export does not prove store completion or
eviction: a fixed wait and filler pressure are only recorded actions.

Existing directories are refused. This option does not attach probes, verify
collector readiness/coverage or grant a serving PASS. Start no integrated run
until capture/filter/clock and producer acceptance have been reviewed. Expected
bytes still use the client's reviewed Qwen runtime constants; this is not portable
model geometry discovery. Error outcomes must be checked separately: the trace
analyzer's usable quality is not an audit of CSV outcome/correctness columns.

## Read-only Linux preflight

```bash
python3 -m kvprobe gate1 plan --disk nvme0n1
python3 -m kvprobe gate1 plan --disk /dev/nvme0n1 \
  --json --output /tmp/kvprobe-trace-plan.json
```

Choose the actual whole namespace before running; `nvme0n1` is an example, not
an automatically selected experiment disk. The command does not open the block
device, run sudo, mount tracefs, attach probes, drop caches or stop containers.
It reads sysfs/procfs/tracefs metadata and, when found, runs only
`bpftrace --version` with a five-second timeout. `--output` is the sole explicit
file write; targets under `/sys`, `/proc` and `/dev` are refused.

| Inspect | Meaning and boundary |
|---|---|
| Namespace identity | Whole `nvmeNnM` or literal `/dev/nvmeNnM`, sysfs major:minor. Partitions, aliases, multipath names and shell expressions are refused |
| Transport | Initial scope is local `pcie`; NVMe-oF is unsupported, unknown transport is incomplete |
| Geometry | Actual sysfs logical block size, not an assumed 512/4096 default |
| Queue limits | `max_sectors_kb` and `max_hw_sectors_kb` are recorded when readable; they are not deterministic command counts |
| Tracepoint formats | Both `nvme_setup_cmd` and `nvme_complete_rq`; explicit field widths/arrays checked, text fingerprints recorded |
| Tracefs access | `/sys/kernel/tracing` first, `/sys/kernel/debug/tracing` fallback. Missing, permission-denied and incompatible formats remain distinct in JSON |
| bpftrace | Executable and bounded version output; not a test of privilege, probe compilation or clock semantics |
| Mounts | Match namespace and partition major:minor in `/proc/self/mountinfo`; mount paths may contain sensitive information |

The initial `nvme-byte-array-v1` profile requires fixed `char disk[32]` in both
events, signed 4-byte controller/queue IDs, unsigned 2-byte setup CID, signed
4-byte completion CID, unsigned 1-byte opcode and unsigned 2-byte status.
Setup also requires the 24-byte `u8`/`__u8`/`unsigned char` `cdw10` array.
Setup and completion CID types differ in the [Linux NVMe tracepoint source](https://github.com/torvalds/linux/blob/v6.8/drivers/nvme/host/trace.h#L57-L101);
the planner checks live format files instead of guessing from a kernel version.
This is a deliberately narrow prospective collector profile, not universal
kernel support. A different layout is refused until implemented and tested.

`preflight_status` is **eligible**, **incomplete**, or **unsupported**. CLI exit
0 means eligible prerequisites, 1 means incomplete/unsupported prerequisites,
and 2 means invalid arguments/output failure. A non-Linux host returns unsupported
without attempting Linux discovery or tool execution.

**Even eligible output has `capture_status: experimental_unvalidated` and
`capture_command: null`.** The next development stage must validate the
collector, including ready/end/loss records and clock/filter semantics, before
an executable capture command can be emitted. Preflight is not a ready signal
and its JSON is not the analyzer's completed-capture manifest.

Mount inventory describes this process's mount namespace only. Empty inventory
does not establish a dedicated device or absence of competing I/O. Controller
names are inventory; they do not fill in an observed trace `controller_id`.
Retain plan JSON privately and review device paths, mount paths and tool paths
before sharing it. No automatic privacy scrubbing is implied.

## What you can run now

```bash
python3 -m kvprobe gate1 analyze examples/gate1-trace
python3 -m kvprobe gate1 analyze examples/gate1-trace \
  --json --output /tmp/kvprobe-trace-observation.json
```

The bundled input is entirely synthetic. A successful command demonstrates the
analyzer, not NVMe access on your machine. No GPU, Linux, root, Docker or bpftrace
is needed for offline analysis. The CLI never invokes sudo or changes a cache.

Exit statuses: **0** = usable observation under the recorded capture assertions;
**1** = incomplete observation (JSON is still available); **2** = unreadable,
malformed, unsupported contract or resource-limit violation. None means a
serving-tier PASS. Malformed inputs have no observation verdict.

## Input contract 0.2.0

A run directory contains:

| File | Contract |
|---|---|
| `manifest.json` | Version, namespace/controller, actual logical block size, clock and capture assertions |
| `phases.csv` | Header `request_id,phase,start_ns,end_ns`; optional `expected_read_bytes` |
| `trace.csv` | Normalized setup/completion records below, preserving retained delivery order; timestamp inversions make quality incomplete |
| `bpftrace.err` | Capture stderr, including an empty file when there were no diagnostics |

See [the synthetic manifest](../examples/gate1-trace/manifest.json). Required fields:

```json
{
  "trace_contract_version": "0.2.0",
  "namespace": "nvme0n1",
  "controller_id": 0,
  "logical_block_size": 4096,
  "clock": "CLOCK_MONOTONIC",
  "capture_start_ns": 0,
  "capture_end_ns": 100,
  "capture_ready": true,
  "capture_exit_code": 0,
  "loss_events": 0
}
```

Capture start/end bracket the recording. Phases must be fully inside that window.
`capture_ready` records that probes were attached before the producer began;
analyzer input alone cannot verify that assertion. `capture_exit_code` and
`loss_events` may be null/omitted, but then quality is incomplete. **Do not fill
unknown loss with zero**, infer successful capture from a CSV file, or mark ready
without a ready signal. The experimental collector is not yet Linux-accepted.
This version is a normalized input contract, not a
promise that arbitrary historical bpftrace output can be read unchanged.

```text
S,timestamp_ns,controller_id,queue_id,command_id,opcode,slba,block_count,cpu_id
C,timestamp_ns,controller_id,queue_id,command_id,status,cpu_id
```

Numeric fields are decimal integers. `block_count` is the **decoded count**
(`NLB + 1`), not raw zero-based NLB. Read/write payload bytes are block_count ×
recorded logical block size. Other opcodes are counted as commands, not as
read/write payload. Queue 0 is outside scope. Events must match the declared
controller. Blank lines and `#` comments are permitted. Out-of-order events are
flagged incomplete, not silently sorted into a fabricated causal order.

The collector filters one whole namespace; Linux acceptance must verify clock and
tracepoint field semantics. The current analyzer cannot independently confirm
namespace filtering from these event rows: controller identity is not namespace
identity. A partition or an unknown clock is refused, not guessed.

## Event ordering contract

### Experimental opt-in causal consistency

Strict remains the default and historical observations are not migrated.
Explicitly selecting `--ordering-policy causal` produces observation output
0.3.0 with policy `retained_delivery_causal_v1`:

```bash
python3 -m kvprobe gate1 analyze /path/to/cpu-bearing-run \
  --ordering-policy causal --output /tmp/new-causal-observation.json
```

The output file must not already exist. Keep the original strict observation.
This is offline experimental analysis, not a Gate 1 serving PASS or independent
Linux clock/filter certification. It never sorts or rewrites events.

- CPU-bearing input contract 0.2.0 is required under both ordering policies. Raw stdout/stderr, recorded raw digest, ready/end/controller and
  recomputed scoped loss must agree. Raw event records must match normalized
  records exactly in retained order. Missing/contradictory evidence is incomplete.
- Per-CPU timestamps must be nondecreasing. Commands must arrive setup before
  completion, with nonnegative lifetime, no pending-ID collision, no unmatched
  events and zero completion status. CPU is not a pairing key.
- The last completion timestamp for every command key is retained for the whole
  capture. A reused key's setup must not precede that completion. Equal timestamps
  retain delivery order; ties never resolve pending collisions.
- Global adjacent inversions between different CPUs and different command keys
  remain visible diagnostics, but alone do not make this policy incomplete.
  Same-CPU/same-key adjacent inversions and per-CPU inversions do. No ns tolerance
  is used. Loss, capture, phase-boundary and overlap failures are not waived.
- Pending state and full-capture history each have a 65,536-key limit; history
  is never evicted to certify an unchecked reuse. Exhaustion is a resource error,
  not a usable observation. Existing CPU/line/phase limits remain.

Output records history scope/count/limit and assertion-level validation scope.
Usable means consistency under this explicitly selected policy, not that the
physical command chronology was reconstructed. Completion-first delivery is
not repaired. Correctness, runtime, residency, store and fallback remain separate
serving checks. The recorded clock/filter assertions still need independent
acceptance. CLI statuses retain 0/1/2 meanings under the selected policy.

For CPU-bearing input 0.2.0, ordering diagnostics also report observed CPU IDs,
adjacent same-CPU/cross-CPU inversions and inversions in each CPU's retained
subsequence. At most 4096 distinct CPUs are tracked, with CPU IDs 0–65535;
exceeding either bound is refused. These classifications do not establish the
cause of an inversion or permit sorting. Setup and completion may execute on
different CPUs, so pairing still uses controller/queue/command ID, not CPU.

CPU-bearing normalized records:

```text
S,timestamp_ns,controller_id,queue_id,command_id,opcode,slba,block_count,cpu_id
C,timestamp_ns,controller_id,queue_id,command_id,status,cpu_id
```

Observation output 0.2.0 adds an `ordering` diagnostic object; input contract
0.1.0 and quality/pairing rules are unchanged. It records adjacent inversion
count, maximum adjacent backward step and maximum lag behind the largest prior
timestamp. The first 16 inversion examples identify physical CSV line numbers
and command keys; additional inversions are counted but not retained as examples.
Storage is bounded independently of trace length. These are delivery/timestamp
diagnostics, not a causal explanation or an accepted reorder tolerance. CPU
provenance is explicitly `not_recorded`, and `reordered` remains false.

Both input contracts distinguish three orders:

- **Delivery order:** the order of retained records in `trace.csv`. Analysis does
  not sort, rewrite or repair the input. Input hashes identify these exact bytes.
- **Timestamp order:** timestamps must be globally nondecreasing across the whole
  capture. Equal timestamps are allowed and retain delivery order. Any inversion,
  including one nanosecond or an event outside a request phase, adds
  `event_order_not_monotonic` and makes the observation incomplete.
- **Command causality:** each `(controller_id, queue_id, command_id)` must have
  an unambiguous setup followed by completion. A completion delivered first is
  unmatched; a completion timestamp before setup has a negative lifetime and
  contributes no successful bytes. Pending ID reuse is ambiguous. Equal IDs on
  different queues are independent.

Exact phase bytes, successful pairing and zero scoped record loss do not override
an ordering failure. Aggregates retained in an incomplete observation are
diagnostic, not a certified phase or serving result. Sequential ID reuse in
delivery order with overlapping timestamps also remains incomplete.

Historical input contract 0.1.0 has no CPU identifier. Cross-CPU delivery is a
possible explanation for an inversion, not a demonstrated cause. Do not apply
an arbitrary timestamp tolerance or silently sort historical captures.

A future ordering policy requires an explicit versioned contract: provenance and
clock checks, bounded buffering and watermark rules, equal-timestamp and ID-reuse
handling, late-event/overflow failures, and Linux acceptance. Such a policy is
not implemented here; it must not retroactively upgrade existing observations.

## Aggregation and quality

- **Submitted bytes** count read/write setup records, including failed or
  unmatched commands. **Successful completed bytes** count safely paired commands
  with status zero. They are different observations, not interchangeable metrics.
- Pairing uses controller + queue + command ID. An ID reused after completion is
  valid. Reuse while still pending makes pairing ambiguous; those completions are
  excluded from successful bytes. Unmatched events and errors remain visible.
- Phase ownership uses setup time in **[start, end)**. A completion at/after the
  end is marked boundary-crossing. Its successful bytes stay with the setup phase;
  quality is incomplete. Bytes are never prorated across time windows.
  Commands set up before a phase and completed inside/after it are also flagged
  as boundary-crossing; their bytes remain with their setup owner (or outside).
- Overlapping phases receive their overlapping setup observations but quality is
  incomplete; **do not sum their rows or treat them as unique request attribution**.
  Global totals count each setup once. Unowned setups go to `outside_phases`.
- Host command lifetime uses paired setup/completion timestamps, including failed
  pairs. Count/total/min/max are provided; no latency percentiles are implemented.
  It is not application latency or SSD-internal execution time.
- Missing stderr, any nonempty diagnostic, unknown/nonzero loss, capture failure,
  unmatched/ambiguous events or time-boundary/order problems yield `incomplete`.
  Diagnostics require operator review even when they are only warnings.
- Empty trace can be a usable **recorded zero-I/O observation** only when capture
  assertions are complete. It is not independently certified capture success.

Each input is SHA-256 fingerprinted. Input files are never changed, and an output
path aliasing a raw input is rejected. Keep original logs: an observation JSON
alone cannot reconstruct the measurement.

### Synthetic resource acceptance tool

Run the explicit tool from the repository root on Linux or macOS; no GPU, sudo,
serving process or target-device workload is required:

```bash
PYTHONPATH=. python3 scripts/tools/benchmark_gate1_trace.py \
  --output-dir /tmp/gate1-resource-review-v1
```

The output directory must be new. Two fresh worker processes retain synthetic
fixtures, observations and `report.json`: 500,000 sequential commands (1,000,000
events), and the same workload with its final completion deliberately absent.
Both use one phase, four CPU IDs and 32,768 reused command keys. This checks
complete-byte aggregation and fail-closed pairing despite zero scoped delivery
loss. It does not exercise maximum phase overlap, pending-command pressure,
real event delivery, or kernel capture.

Default acceptance budgets are analyzer time ≤120 seconds and process peak RSS
≤256 MiB per worker; wall-clock timeout is 180 seconds including fixture
generation. Peak RSS includes imports/generation, while analyzer time does not.
Budgets are diagnostic acceptance targets, not enforcement of a process memory
limit or performance guarantees on other machines. CLI exit 0 means these
selected resource/correctness checks passed; exit 1 means a budget/check/worker
failure, and exit 2 means invalid arguments or an existing output directory.
No status means a Gate 1 serving PASS. Results must name the platform, input
scope and source hashes; a local run does not complete Linux acceptance.

Resource limits: manifest ≤64 KiB, each UTF-8 record ≤4 KiB, ≤256 phases,
≤65,536 pending IDs. The parser streams events and stores pending commands and
per-phase counters, not a per-LBA block set or all completed commands. Violations
are explicit errors, not truncation. Phase matching currently takes O(events ×
phases). Resource acceptance is profile-specific; the tool above does not
establish the worst-case time/memory bound for every allowed input.

## How to interpret it with Gates 2–5

| Gate | Trace contribution | Additional evidence still needed |
|---|---|---|
| 1 — serving verification | Namespace I/O and controlled interval consistency | Correctness, actual runtime/geometry, residency/eviction, fallback and competing-I/O control |
| 2 — retrieval break-even | Read observations alongside a context sweep | Matched recompute/retrieve timing and valid measurement conditions |
| 3 — admission cost | Write observations alongside store intervals | Incremental store cost, retrieval saving and expected reuse |
| 4 — capacity justification | Device activity as upper-tier capacity is exceeded | Upper-tier residency, controlled working set and serving classification |
| 5 — operating envelope | Batch device activity and capture quality under load | Correctness and tier validation at every concurrency; unique request attribution if claimed |

`expected_read_bytes` is an optional **supplied** logical-payload estimate. Its
ratio to successful device bytes is descriptive: headers, metadata, competing
activity and filesystem behavior can change it. Ratio 1 is not a gate PASS.
The analyzer does not derive model geometry, assess correctness, prove KV identity,
measure LBA reuse, or upgrade confidence. Output always says
`serving_verification: not_assessed` and is **not evidence schema v0.3 input**.

Do not pool trace-on diagnostic TTFT with trace-off performance measurements.
See [METHOD](METHOD.md) and [CHECKLIST](CHECKLIST.md) for common validation.

## Current scope and remaining acceptance

An experimental [reviewed serving companion API](GATE1-SERVING-REVIEW.md) now
exists separately from these trace observations. It reanalyzes retained inputs
and requires all automatic/reviewed checks; it does not change analyzer verdicts
or evidence v0.3. `gate1 verify` connects the companion with explicit review
inputs and new-output-only semantics. Actual Linux serving acceptance is pending.

What exists is described above: read-only preflight, an experimental root-only collector with
scoped loss accounting, opt-in serial phase export from the g5 client, the offline analyzer with
strict and opt-in causal ordering, and the reviewed serving companion (`gate1 verify`).
Required work is independent filter/clock acceptance, documentation/audit and
defect fixes. Further Gate 1 feature development is paused. Broader kernel/device
support and automatic trace integration into Gate 2–5 are not promised at launch.
Actual whole serving acceptance remains optional and unestablished.

Strict can leave real KV captures incomplete because of cross-CPU delivery order;
explicit causal can be usable only under its documented constraints. Profile-specific
large-input tests do not qualify every supported layout. These limits and bpftrace
v0.20.2 pinning remain explicit; zero exit status does not remove them.
