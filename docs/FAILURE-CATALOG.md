# Failure catalog — misleading KV cache measurements

These cases show how plausible latency or framework metrics can survive failed storage
operations, resident-buffer hits, and fallback. External counters, filesystem observations,
and error logs helped identify the discrepancy in the examined configuration.

This is the companion to [`METHOD.md`](METHOD.md). The method tells you which gates to run; this
tells you what it looks like when a gate is the only thing standing between you and a result you
would have published.

Each entry is tagged:

- **[principle]** — transfers to any stack. The mechanism is not specific to our software.
- **[stack]** — observed on vLLM + LMCache with cuFile / `nvidia-fs`. The shape recurs elsewhere,
  but the commands and strings are ours.

Observed on Linux, ext4, local NVMe, a discrete Blackwell-class GPU. Where a check depends on
that, it says so.

**Every number below is a dated observation, not a current result.** They are kept because the
mechanism recurs, and dated so you know when we met it; none of them is a performance claim, and
the raw artifacts are held outside this repository. What *is* validated as of today is indexed in
the [status block](README.md#keeping-this-current), which is a separate kind of statement.

---

## 0. The judgment rule

**Compare framework metrics with external evidence.** The following setup checks corroborate
storage-path availability and successful writes in the examined stack. Resolve discrepancies
before characterizing retrieval.

```bash
# 1. Kernel counters — increase only on the native path
grep -E '^(Reads|Writes)' /proc/driver/nvidia-fs/stats
# 2. Registration failures
docker logs <container> 2>&1 | grep -c "cuFileHandleRegister failed"
# 3. Logical file size and payload-bearing files — not proof of physical allocation
du -sb <tier_path>; find <tier_path> -type f -size +64k | wc -l
```

Expected setup observations: native write counters rise, registration succeeds, and files have
payload-sized logical lengths. `du -sb` reports apparent size; file size alone does not prove
physical allocation or a later device read. For retrieval, record the selected read counter
around controlled request intervals and compare its delta with expected KV bytes. Host-wide
counters such as `nvidia-fs` corroborate but cannot pass Gate 1; the per-device cgroup counter can.

---

## 1. The engine reports success for writes that all failed **[stack]**

*Checks:* serving-tier validation uses external byte observations alongside backend errors and
file inspection. Coverage varies by script; `g3_admission_cost.py` retains a store with zero
observed write as `store_unattributed` (or `unattributed` when the counters cannot classify it), so
its latency still affects admission economics. See [`CHECKLIST.md`](CHECKLIST.md).

**What you see.** `Stored N out of N tokens … GB/s`, steady and believable, run after run.

**What it is.** The metric counts tokens handed to the storage manager, not bytes the backend
committed. Same host, same workload, before and after the writes were fixed:

| Actually on disk | Engine reported |
|---|---|
| 88 KiB — 22 stub files, **every write failed** | `Stored 5632/5632, 1.3750 GB, 24.5742 GB/s` |
| 1.38 GiB — 22 real files, **complete success** | `Stored 5632/5632, 1.3750 GB, 24.3488 GB/s` |

0% and 100% differ by less than run-to-run noise. Measured 2026-08-25; evidence retained outside
this repository.

**Invalidates.** Every throughput, TTFT and tier claim derived from engine logs alone.

**Generalization [principle].** A metric emitted *before* the operation it describes is a
scheduling record, not a completion record. Ask where in the code the counter increments.

---

## 2. The phantom tier — a run that never touched the device **[principle]**

*Checks:* `g4_capacity_sweep.py` uses zero-read observations plus a recompute-relative latency
heuristic to classify a possible memory hit. `g3_admission_cost.py` retains a zero-read reuse as a
fallback/store-unattributed outcome, so it lowers realized saving rather than disappearing. These
rules prevent such rows from supporting a disk-retrieval claim; they do not directly trace each
request to a particular resident buffer.

**What you see.** A stable, repeatable speedup that is *better than it should be* — in our case
roughly 12× against recompute, with a framework retrieve rate around 612 GiB/s. This is a
historical apparent rate, not measured SSD bandwidth.

**Repeatable is the point.** We first saw it on 2026-08-25 and met it again in a 2026-08-27 context
sweep, where a 1,408 MiB working set sat entirely inside a 3,072 MiB cuFile pool and reported
12.28× with little spread. A number that moved around would have been easier to distrust. Measured
2026-08-25 and 2026-08-27; evidence retained outside this repository.

**Interpretation in the examined run.** The cuFile pool exceeded the target working set, and
repeated fillers did not evict it effectively. The resident-buffer explanation is consistent
with the observations. Zero observed device reads invalidate a disk-service claim for those
intervals; they alone do not identify the exact buffer that supplied the bytes.

**Investigate and check the evidence:**

- **Latency anomaly:** the apparent disk run reported TTFT 0.0952 s while the DRAM arm
  measured 0.1087 s. This motivates investigation; end-to-end latency ordering alone does not
  establish which tier served a request.
- **External read evidence:** compare the selected counter's delta with the expected payload
  during each controlled retrieval interval, accounting for counter scope and competing traffic.
  In the corrected historical run, each of the 20 controlled retrieval intervals recorded 1,408 MiB.
  This is controlled-interval corroboration; it is not a request-ID hardware trace.

**Fix.** Size the buffer below the working set deliberately, and make the filler between
iterations **unique per iteration**; a repeated filler becomes a cache hit itself and applies no
pressure. Verify `Reads` for retrieval — not `Writes`, which stays at `+0` and confirms nothing.

**Invalidates.** Disk-retrieval performance claims from the affected arm. Preserve the failed
observations as diagnostic evidence and repeat the arm after correcting the eviction procedure.

---

## 3. Native and compat are indistinguishable by speed **[principle]**

*Caught by:* `g2_context_sweep.sh setup` — its gate requires a real device write (files over 64 KiB, bytes on disk, an `nvidia-fs` write delta) before measuring. Blocking compat is still an **operator precondition**: no script edits `cufile.json`.

**What you see.** Excellent throughput, so you conclude the accelerated path is active.

**What it is.** The compat (POSIX) path reported an apparently high throughput (2026-08-25;
evidence retained outside this repository). This was not a target-device measurement or an SSD-bandwidth
result. **Speed is not evidence of path.** Nor is `gdscheck`'s `NVMe: nvfs`, which lists available
modes, not the chosen one.

**Confirm.** Block the fallback, then read the counters — **[stack]**:

```bash
# /etc/cufile.json → "allow_compat_mode": false
gdscheck -p | grep -E "NVMe|use_compat_mode"     # expect use_compat_mode: false
grep -c "cuFile IO mode: POSIX" <trace>          # native: 0; compat: 1:1 with I/O count
```

| | native | compat (silent fallback) |
|---|---|---|
| `Reads`/`Writes` | rise with ops | **never rise** |
| `cuFile IO mode: POSIX` | 0 | matches I/O count exactly |
| With compat blocked | succeeds | **registration fails** |

**Generalization [principle].** Any accelerated data path with a transparent software fallback
has this property. Find the switch that *disables* the fallback and run with it off; a path that
only works when it may silently degrade was never demonstrated.

---

## 4. ext4 mounted without an explicit `data=ordered` **[stack]**

*Caught by:* **nothing in this repository.** It is an operator precondition — check `findmnt` before `setup`, per [`../scripts/README.md`](../scripts/README.md). Downstream it surfaces as §3's gate failing.

**What you see.** `gdscheck` says `nvfs`, yet everything goes POSIX. With compat blocked:
`GPUDirect Storage not supported on current file`.

**What it is.** The driver supports "XFS and EXT4 in ordered mode". ext4 is ordered in practice,
but cuFile requires the option to appear **as a string in the mount options**.

```bash
findmnt -no TARGET,SOURCE,FSTYPE,OPTIONS /mnt/<tier>
# ❌ rw,relatime                 ← silent fallback
# ✅ rw,relatime,data=ordered
```

**Fix.** Remount with `data=ordered`, persist by **UUID** in `/etc/fstab` — device numbers
reshuffle across reboots. XFS needs nothing; it has no such journaling option.

**Note.** The registration error appearing after you block compat is **good news**: the problem
became visible. With compat enabled you would never have seen it.

---

## 5. The same error inside a container has a different cause **[stack]**

*Caught by:* `g2_context_sweep.sh setup`, which aborts when `GdsBackend` never appears in `backend-init.txt`. It tells you the backend is absent, not why — this entry is the why.

**What you see.** `cuFileHandleRegister failed (cuFile err=5008)` — identical string to §4, but
the host works and only the container fails.

**What it is.** Missing `/run/udev`. cuFile finds the block device, then reads `ID_FS_USAGE`
from the udev database to decide whether the volume supports direct I/O. No udev DB, no verdict.

The trace shows five consecutive lines starting with
`udev property not found: ID_FS_USAGE <device>`.

**Fix.** `--volume /run/udev:/run/udev:ro`

**Further privilege and mount changes are ruled out here.** Error 5008 persisted
with `--privileged`, seccomp/apparmor unconfined, `-v /sys`, host-identical mount
paths, and `bind-propagation=rshared`. Passing the device nodes alone was also
insufficient.

---

## 6. A missing config value degrades the engine instead of stopping it **[stack]**

*Caught by:* the same `backend-init.txt` check. This is the failure that makes it worth aborting there rather than at measurement time — the service looks healthy either way.

**What you see.** `Failed during post_init: .` — nothing after the colon. Traffic keeps flowing,
every request succeeds, latency merely looks like a cache miss.

**What it is.** A bare `assert config.cufile_buffer_size is not None` with no message: `str(e)`
is the empty string. The handler prints only `str(e)`, so the reason is blank — and the engine
**swallows the exception, drops to recompute, and continues serving normally.**

**Confirm.** The backend never attaches:

```bash
docker logs <container> 2>&1 | grep -E "GDS backend using fstype|Using cufile|Created backend: GdsBackend"
```

**Fix.** Set the buffer size explicitly. Supplying only the tier path raises no objection: the
config is echoed back unchanged, and the required pairing appears only in a class docstring.

**Generalization [principle].** Ask what your engine does when a tier fails to initialize. If
the answer is "serve from recompute and keep going", then **a healthy-looking service is not
evidence that the tier exists.** Degrade-and-continue is correct for production and lethal for
measurement.

---

## <a name="orphaned-future"></a>7. An error trapped in an orphaned future **[principle]**

*Caught by:* **nothing automatically.** Confirmed for NIXL with [`../scripts/tools/nixl_v0.4.1_background_transfer_diag.patch`](../scripts/tools/nixl_v0.4.1_background_transfer_diag.patch), which retrieves the future's result. On any stack this needs instrumentation you add.

**What you see.** No error at all. Writes are submitted, nothing lands.

**What it is.** The put path submits with `asyncio.run_coroutine_threadsafe(...)` and never
awaits or stores the future. A `RuntimeError` raised inside — ours came from a failed
transfer — dies with the future.

**Confirm.** Attach a callback that calls `future.result()`, or collect results synchronously.
Raising the log level is not enough.

> **The absence of `Task exception was never retrieved` is not evidence of success.** Whether
> that warning appears depends on implementation and future lifetime.

**Invalidates.** Any claim that data reached the tier, and every measurement above it.

---

## <a name="tier-stops-under-load"></a>8. The tier stops existing under load instead of slowing down **[principle]**

*Caught by:* Gate 5 — `g5_controlled_kv_experiment.sh concurrency`, which keeps per-request correctness checks and per-batch tier-byte checks running at every concurrency level. The batch check says *that* a batch was not fully served; the count below comes from the engine log and the batch byte shortfall, not from the gate.

**What you see.** Throughput keeps climbing with concurrency, so you record a saturation point.

**What it is.** At concurrency 8, **20 of 80 requests were not served by the tier** — correctness
60/80, tier gate 0/10. Through concurrency 4 the same tier passed every gate. The tier did not get
slower; for a quarter of the requests **it was not there.**

**This one is not silent in the log.** On the measured stack the engine named the request and the
shortfall, then rescheduled it:

```
ERROR ... Request <id>The number of retrieved tokens is less than the expected number of tokens!
          This should not happen! (vllm_v1_adapter.py)
ERROR ... Num retrieved tokens: 0, num expected tokens: 2048
INFO  ... Recovered from KV load failure: 1 request(s) rescheduled (2048 tokens affected).
```

So reserve **silent fallback** for a path change nothing logs, and read this failure as silent *in
the response*: the HTTP reply came back looking like the others. Your stack may log it differently
or not at all — which is why the gate judges per-request attribution and not the log. But if you
run this stack, these lines are a cheap detector, and their absence is not a pass.
Measured 2026-08-27; cause established 2026-09-10 (a staging pool smaller than the aggregate
demand of the batch). Evidence retained outside this repository.

**Invalidates.** The number is not a slow measurement of that tier, it is a mixture of two
tiers. Report the validated envelope and discard the rest — a saturation point you cannot
attribute per request is not a saturation point.

---

## 9. Measurement plumbing that quietly changes what you measure **[principle]**

*Caught by:* partly. `g2_context_sweep.sh setup` copies the network mode from the recompute arm so the arms match. Cleanup, logging level, mount identity and counter enablement are **operator checks**.

| What you did | What it does | How you find out |
|---|---|---|
| Published a port (`-p`) on one arm | docker-proxy/NAT adds latency to every request; the arms are no longer comparable. **The numbers still come out** | Compare `NetworkMode` across containers; match it |
| Left cuFile `TRACE` or `nvidia_fs` debug on | Throughput drops substantially | `cat /sys/module/nvidia_fs/parameters/dbg_enabled` → 0 for performance runs |
| Skipped cleanup because `rm` failed on root-owned files | Leftover chunks from the previous run are served as cache hits | `find <tier_path> -type f \| wc -l` must be 0 before starting |
| Trusted the mount point after a reboot | The mount was gone; the run measured the **boot SSD**. `fstab` had no entry | Resolve to the device and its PCI path, not the directory |
| Read counters that were disabled | `rw_stats_enabled=0` makes native runs look like compat | `cat /sys/module/nvidia_fs/parameters/rw_stats_enabled` → 1. Also: `Ops` is in-flight, not cumulative — compare `Reads`/`Writes` `n=` |

---

## 10. Ruled out — do not spend time here **[stack]**

Checked and rejected while chasing the symptoms above. Same symptoms, none of these were the
cause.

| Hypothesis | Checked with | Result |
|---|---|---|
| `memlock` limit (container default 8 MB) | `--ulimit memlock=-1` | Rejected — failure reproduced at unlimited |
| BAR1 too small | `nvidia-smi -q`, Resizable BAR in `lspci -vv` | Rejected |
| PCIe ACS diverting P2P | `lspci -vv` | Rejected — `ACS-` |
| IOMMU enabled | `gdscheck -p` | Rejected — disabled |
| PCIe topology | `lspci -tv`, sysfs PCI path | Rejected — sibling port of the same switch |
| `O_DIRECT` unsupported | cuFile TRACE | Rejected |
| NVMe metadata / protection information | `nvme id-ns -H` | Rejected — metadata size 0 |
| Container isolation (privileged, seccomp, `/sys`) | Tried each | Rejected — see §5 |

---

## 11. Using this on a different stack

The **[principle]** entries are the transferable part, and they reduce to five questions to ask
of any tier, whatever implements it:

1. Where does the success metric increment — before or after the operation completes?
2. Can this tier be served from somewhere faster than itself? (buffer, page cache, a tier above)
3. Is there a transparent fallback, and what happens when you disable it?
4. What does the engine do when the tier fails — stop, or serve from recompute and keep going?
5. Does every transfer need staging space, and what happens when that space is full — wait, fail
   loudly, or skip? ([§13](#staging-buffer-full))

If you cannot answer all five for your stack, the gates in [`METHOD.md`](METHOD.md) will not
protect you, because the evidence they rely on will not exist.

---

## <a name="cold-hit-completions"></a>12. A cache hit can change the completion — an interpretation note **[principle]**

*Not a failure entry.* No gate judges this. It is here because it is easy to misread in both directions.

**What you see.** The same prompt at `temperature=0` gives one completion when it is computed cold and a
different one when its prefix is served from the cache, while the tier moved exactly the expected bytes.

**What it can be.** A cache-hit prefill does not repeat the cold computation. The cached prefix KV is loaded
unchanged, but the remaining tokens and the first output logits are computed by differently shaped kernels with
a different floating-point reduction order. Where the top two logits are nearly tied, greedy decoding can pick
another token and the completion diverges from there. A partial hit moves the split point again, so it can
differ from both. None of this requires a defect — and a corrupted round trip, a wrong cache key or a mixed-up
restore can produce the same symptom.

**How to read it.**

- A difference alone is not evidence that the tier is broken. Identical completions alone are not evidence that
  it is correct.
- To separate the two: compare a hit from the upper tier with a hit from the lower tier for the same prompt
  (agreement suggests the stored bytes round-trip intact); repeat the cold request with no cache connector (if
  cold requests disagree with each other, the engine is not deterministic on its own); remove model-supplied
  sampling defaults (for vLLM, `--generation-config vllm`) and fix the seed; enable the engine's batch-invariant
  or deterministic mode where it has one. Then find the first diverging token and compare its logit margin: a
  small margin points to numerics, a large one to the restored data.
- The invariant a cache owes you is that the restored KV equals the stored KV, bit for bit. Equal completions
  are a property of the engine's numerical determinism, not of the tier.

**What the reference client checks today.** A request is `correct` when the workload's expected value — a canary
or a retrieved code — appears in a short completion (`max_tokens` 7). That catches gross corruption. It does not
compare cold and cached completions, and the client does not keep the completion text. A later version is planned
to record a per-request completion hash and report a cold/hit difference as an unjudged notice; the notice will
not change any verdict.

---

## <a name="staging-buffer-full"></a>13. A full staging buffer turns stores and retrievals into quiet no-ops **[stack]**

*Checks:* `g3_admission_cost.py` retains a store with no observed device write and includes its
later reuse latency in realized saving; `g4_capacity_sweep.py` classifies a zero-read,
recompute-slow retrieval as `fallback`, keeps per-request store-write and retrieval-read records, and
the Gate 4 result states the beyond-capacity counts. Nothing in the engine's INFO log flags it.

**What you see.** The engine logs every store as complete — `Stored N out of N tokens` — at
plausible throughput. Some of those stores leave no write on the device. Later, an entry the tier
should hold comes back with zero retrieved tokens and the request is recomputed; at INFO level the
only trace is `The cache block is in the storage, but it can't be retrieved` and the scheduler
recovering from the load failure. In the runs behind this entry the failures were not random: they
followed a load that succeeded. Stores interleaved with loads failed every other time; stores with no
load in between all wrote. Loads in a row succeeded about as many times as the pool held whole
entries, then failed — observed with pools of one and two entries, not established as a rule.

**What it is.** On LMCache's GDS backend (v0.4.1, cuFile), every transfer — the write of a stored
entry and the read of a retrieved one — takes its staging space from the cuFile buffer pool. When
the pool cannot fit the whole entry because another transfer still holds part of it, the
allocation returns nothing. The read path logs that at debug level only and gives up, so the
request falls back to recompute; on the write side the engine still reports the store as done.
A pool sized to exactly one entry hits this whenever anything else is in flight. Write times that
split into two groups — a real write and an almost instant one — are the visible symptom.

A completed load appears to keep its pool space for a while, so the next transfer — a store or
another load — is the one that fails. Why the space is held and what releases it is not established
here. Two observations bound it: a failed load does not write the entry again, so the tier keeps an
entry it cannot serve; and in the one comparison made, a pool with room for one more entry moved the
limit by one entry without removing it, so a long enough scan of overflowing entries still failed.

**Confirm.**

- Compare the engine's store log with the device write counter per request: a store the engine
  calls complete but the counter never saw is this case.
- Look for the "in the storage, but it can't be retrieved" line next to zero-token retrievals.
- Read the producers' per-request records — Gate 3 `attempts`, Gate 4 `stores` and `retrievals` —
  to tell a store with no observed write from one whose write activity was observed but whose
  retrieval was not established. The write counters are host-wide, so observed write activity alone
  does not prove that the entry reached the target device. Note which transfer came just before each
  failure.
- Run once at debug log level: the allocation failures appear there and nowhere else.
- Repeat with a pool larger than one entry and watch the drops and fallbacks change.

**It pulls against Gate 4.** Gate 4 needs a staging buffer no larger than one entry, or a hit served
from the GPU-resident buffer reads zero device bytes and is misclassified as an upper-tier hit. This
backend needs headroom above one entry to move entries reliably. Record both the pool size and the
entry size, and read the Gate 4 counts, not only the crossover: a crossover with many fallbacks
beyond it says the tier did not serve what overflowed in that configuration. The records separate
stores with no observed write from stores whose write activity was observed but whose retrieval was
not established; in the runs behind this entry the failures were of the second kind. Because the
write counters are host-wide, that alone does not prove the entries reached the target device.

**Invalidates.** Admission economics computed from successful attempts only; any "the tier served the overflow"
claim without the beyond-capacity counts; engine store throughput as evidence of a write.

**Generalization [principle].** A transfer that needs a fixed staging area and fails softly when the
area is full looks like success to every signal except a counter on the device. Size the area with
headroom over the largest single transfer, and find out what the engine does when it cannot get
space.
