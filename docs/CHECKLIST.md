# Measurement checklist

Start with [`FIRST-VISIT.md`](FIRST-VISIT.md), then [Gate 1](GATE1.md).
Use this checklist during evidence collection and every Gate 2–5 experiment.
This is the execution companion to the long-form
[`METHOD.md`](METHOD.md); [`FAILURE-CATALOG.md`](FAILURE-CATALOG.md) explains how each check can
produce a plausible but false result.

Each gate states **what must be true** — that part is stack-agnostic — and then names the script
that carries it **here**. Preconditions, environment variables and commands live in
[`../scripts/README.md`](../scripts/README.md) and are not repeated in this document.

Run in order. **Each gate passes when its measurement is valid; whether the tier belongs is read
from what Gates 2–5 report as results** ([METHOD §3](METHOD.md#3-the-five-gates)). The order is deliberate: the
cheapest disqualifier runs first, and a tier you cannot attribute cannot be characterized.
Gate 1 is passed by your measurement rather than by the tier, and gate 5 is gate 1 checked along
a load axis — [METHOD §3.6](METHOD.md).

---

### Before you measure anything

What must be true, whatever your stack. **How to satisfy each one here — the commands, the
environment variables, the container flags — is in
[`../scripts/README.md`](../scripts/README.md), which is the single source for that.**

- [ ] A **recompute arm** exists — same model, same host, cache disabled. This is the floor, not a control.
- [ ] Reads are **cold**: page cache dropped, resident buffers below working-set size, filler
      **unique per iteration** (a repeated filler becomes a hit itself and applies no pressure).
- [ ] Transparent fallback is disabled where supported, or its detection and
      rejection criteria are documented. Unknown fallback remains unverifiable;
      a successful response or absence of log keywords is not proof of no fallback.
- [ ] Arms are plumbed identically, and diagnostic runs are separate from performance runs.
- [ ] Nothing is left from the previous run.
- [ ] Whatever counters attribution depends on are **enabled** — a disabled counter reads exactly
      like a tier that was never used.

For a local NVMe tier, `python3 -m kvprobe tier-check` checks part of this list read-only before the
run: the device the counter reads, counters enabled, compat mode, the staging pool against the
working set, and leftovers in a directory you name ([runbook step 2](../scripts/README.md#minimal-runbook--all-five-gates-one-configuration)).
It does not check cold reads, fillers or arm plumbing, and `READY` is not Gate 1 evidence.
Unreadable required checks block `READY`; unrequested optional checks and inapplicable checks do
not. Exit 0 means `READY`, while warnings or an incomplete required check return exit 1.

---

### Gate 1 — Serving Tier Verification · *does this measure the tier at all?*

**Role:** the serving-tier core of **Common Measurement Validation** for every Gate 2–5
experiment. The [Gate 1 guide](GATE1.md) is the next step after first-visit;
its observation CLI is not a separate benchmark or automatic checklist PASS.
The guided `first-visit` command runs
`doctor` and the bundled demo, but deliberately leaves hardware measurement `NOT_RUN` and Gate 1
`NOT_ESTABLISHED`. Numbers, evidence keys, and automated report rules are unchanged.

**Experimental NVMe observations:** the [Gate 1 NVMe layer](GATE1-NVME-TRACE.md) has a read-only
preflight (`gate1 plan`), an explicit root-only collector (`gate1 capture`, Linux acceptance of the
current version pending) and an offline analyzer (`gate1 analyze`); the g5 client can export serial
phase intervals for it. None of them automates this checklist, establishes serving verification or
replaces the existing report. A separate experimental [`gate1 verify`](GATE1-SERVING-REVIEW.md) can
return `verified` for one serial native cuFile GDS run, and only when every check in an explicit
operator review passes; it records that review rather than replacing this checklist, and its Linux
acceptance is pending. Review the preflight's blockers and device/mount scope: an eligible
preflight is not a capture-ready signal or a completed checklist.

**Before execution**

- [ ] **Configuration:** compare actual model, dtype, chunk size, buffers, backend and mounts
      with the recorded conditions. **Operator check.** `kvprobe fingerprint` fingerprints
      supplied configurations and the v0.3 report checks bindings; neither discovers whether
      the supplied values match the live deployment.
- [ ] **Expected payload:** derive expected bytes independently of observed bytes.
      **Script/ingest support:** measurement clients and `kvprobe/ingest.py`; review the
      supplied KV geometry rather than assuming it is automatically detected.
- [ ] **Counter scope:** verify the chosen counter is enabled and covers the intended
      device/path. **Producer and operator responsibility:** `kvprobe ingest` records provider
      evidence and `kvprobe/report.py` checks eligibility; a counter not filtered to the
      tier's device leaves Gate 1 `UNPROVEN`. The per-device cgroup counter is filtered.
- [ ] **Isolation:** check for competing activity that could contaminate the interval.
      **Operator/setup responsibility:** not a universal automated isolation guarantee.

**During/after requests**

- [ ] **Request linkage:** record external counter intervals and their request/batch scope.
      **Script support:** `g2`/`g5` collect request deltas; `g3` observes store/retrieve;
      `g4` classifies retrievals. Aggregate-only evidence must not become per-request proof.
- [ ] **Tier and bytes:** distinguish device-served, resident, missing and partial transfers.
      **Script/report support:** `g2`/`g5` tier checks, `g3` write checks, `g4` DRAM/device
      classifications; the normalized report checks source, external evidence and byte
      tolerance. Not every script emits identical classification fields.
- [ ] **Correctness:** check successful completion and the workload's defined output test.
      **Automatic where recorded:** `g5` correctness checks and report validation of request
      `correct` flags. Equivalent coverage is not guaranteed for every script/workload.
      In the reference client `correct` means the expected value appears in the completion; it
      does not compare cold and cached completions — see
      [FAILURE-CATALOG §12](FAILURE-CATALOG.md#cold-hit-completions).
- [ ] **Errors/fallback:** review engine logs and recovery/partial-retrieval evidence.
      **Manual log audit plus report support for recorded fatal observations.** There is no
      universal automatic log parser proving absence of fallback.

**Before aggregation**

- [ ] **Claim eligibility:** keep unverifiable/invalid rows out of performance claims.
      **Script/report support:** script-specific tier filtering and report gate decisions.
      Honor report-wide suppression; do not bypass a failed gate by cherry-picking rows.
- [ ] **Auditability:** retain raw rows, logs, configuration, exclusions and checksums.
      **Collect/ingest support plus operator verification:** presence of artifact references
      alone does not prove that all original files were preserved.

**Pass:** the evidence supports the claimed serving source and expected bytes at the stated
scope, and applicable report checks pass. A device-retrieval claim needs device evidence;
a capacity experiment may legitimately contain DRAM-served points or a recompute reference.
Zero device bytes must be classified, not automatically treated as a device hit or failure.
**Trap:** the engine's own "stored"/throughput counter reports the same number whether every
write succeeded or every write failed.

### Gate 2 — Single-hit retrieval break-even · *when is retrieval worth it?*

**Run:** `bash scripts/g2_context_sweep.sh setup` — read `gate.txt` and `backend-init.txt` before measuring — then `measure`, `collect`. Order-randomized repeats: `scripts/g5_controlled_kv_experiment.sh randomized`.

- [ ] Both arms fitted as lines: fixed cost + per-token cost, across a context sweep.
- [ ] The crossing with the recompute line is reported **with its concurrency and hit assumption.**

**Pass:** both arms measured under one configuration on Gate 1-verified requests.
**Result:** where your workload's context lengths sit against the crossing — above it offloading
pays on a single hit; below it offloading efficiency is low. A crossing above your contexts is a
finding, not a failed gate.
**Trap:** calling this an admission rule. It assumes a 100% hit and ignores the cost of writing.

### Gate 3 — Admission cost · *how many reuses repay the write?*

**Run:** with a GDS arm serving on `PORT` and a recompute arm on `RECO_PORT` — `g2_context_sweep.sh setup`
brings both up, or `g5_controlled_kv_experiment.sh serve gds` starts the tier arm alone —
`bash scripts/g3_admission_cost.sh measure`, then `bash scripts/g3_admission_cost.sh collect`.
The host-wide `nvidia-fs` write/read counters classify each attempt; they do not
select which latency samples survive.

- [ ] Per-entry **store cost** measured on the path under test, under Gate 1 discipline.
- [ ] Recompute and GDS store/reuse paths both warmed before retained attempts.
- [ ] Every requested admission/reuse pair retained as target-tier, fallback,
      store-unattributed, or unattributed.
- [ ] No `unattributed` attempt remains; otherwise Gate 3 is `UNPROVEN`.
- [ ] **Break-even reuse count** = mean admission cost ÷ mean realized reuse saving
      across all attempts. Positive saving is credited only to target-tier hits;
      negative cost from slow non-target outcomes remains.

**Pass:** Gate 2's target-tier saving is established and every Gate 3 attempt is classified.
Fallback and absent-store observations are valid economic outcomes, not reasons to drop a row.
Host-wide counters still prove presence, not target-device exclusivity.
**Result:** `ECONOMICAL` or `NOT_ECONOMICAL` from the declared expected reuse count;
`UNPROVEN` leaves the economic decision indeterminate.
**Trap:** computing repayment from successful hits only. That answers the optimistic case, not the
realized admission decision.

### Gate 4 — Capacity justification · *is this tier ever the best choice?*

**Run:** with the same two arms, the tier arm's cuFile buffer no larger than one entry,
`bash scripts/g4_capacity_sweep.sh measure`, then `bash scripts/g4_capacity_sweep.sh collect`. The crossover
where retrievals stop being DRAM-resident approximates usable L1 capacity; read the beyond-capacity counts
too ([FAILURE-CATALOG §13](FAILURE-CATALOG.md#staging-buffer-full)). For each `fallback`, the row's `stores`
and `retrievals` records show whether that entry's store wrote anything.

- [ ] Compared against the tier above **while that tier still has room.**
- [ ] Working set swept **past** the upper tier's capacity.

**Pass:** every retrieval in the sweep classified (tier above, this tier, or miss). **Fail:** the tier
above overflowed and this tier served nothing.
**Result:** whether there is a region where the tier above cannot hold the working set and this
tier serves it — or its necessity is not shown for this workload.
**Trap:** judging a storage tier against DRAM speed. Past the capacity boundary it competes with
recompute, not with the faster tier. Its axis is capacity × reuse, not bandwidth.

### Gate 5 — Operating envelope · *up to what load does the tier exist?*

**Run:** `bash scripts/g5_controlled_kv_experiment.sh concurrency` (and `concurrency-gds`). Per-request correctness and per-batch tier-byte checks run at every level.

- [ ] Concurrency swept at **one** per-request input size, with correctness and tier checks **running at every level.**
- [ ] Report the highest load at which **every** measured batch met its tier-byte check with all responses correct — and the input size.

**Pass:** every declared level measured with per-level tier evidence. **Fail:** not even the lowest
level was fully served.
**Result:** the envelope you can state — the last level at which every batch was credited, at the
measured input size — with what happened at its edge described. The tier counters cover a batch, so
a failed batch does not tell you how many of its requests the tier missed.
**Trap:** reporting a saturation point. Past the envelope the tier does not slow down — it stops
serving some requests while the responses still look normal, and the number is a mixture of two
tiers. Check the engine log too: it may name the requests it rescheduled.

---

### Before you report

- [ ] Every number states **what it is against** (`N% lower than buffered POSIX`, `break-even at
      T tokens against recompute`) and under what conditions (context, concurrency, host). Your
      numbers go here; this repository's are host- and model-scoped and do not transfer.
- [ ] Discarded runs are reported too, with why.
- [ ] No claim extends past the measured range — no extrapolation, no other hardware's multiples.
- [ ] The tier list you conclude with is stated as a property of **this machine**, not of the
      framework.
