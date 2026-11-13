# Documentation

This directory contains the method, reproducibility notes, and release controls for
`kv-hierarchy-probe`.

## What each document is for

| Document | Purpose | Governs |
|---|---|---|
| [`FIRST-VISIT.md`](FIRST-VISIT.md) | Guided discovery: see the five decisions, explore a synthetic end-to-end result and two traps, classify the host, then continue to Gate 1. **Start here on a new environment.** | `python -m kvprobe first-visit` |
| [`GATE1.md`](GATE1.md) | Establish measurement validity: prepare the configuration, choose an evidence path, collect and review evidence before performance evaluation. **Continue here after first-visit.** | Gate 1 workflow and repeated validation in Gates 2–5 |
| [`GATE1-NVME-TRACE.md`](GATE1-NVME-TRACE.md) | Optional experimental NVMe observation, trace contracts, quality checks and interpretation limits. | `python -m kvprobe gate1 plan`, `capture`, `analyze` |
| [`GATE1-SERVING-REVIEW.md`](GATE1-SERVING-REVIEW.md) | Experimental scoped serving review for a serial native cuFile GDS run; operator evidence and automatic checks must agree. | `python -m kvprobe gate1 verify` |
| [`VERSIONING.md`](VERSIONING.md) | Versioning rules and the one registry of package and data-contract versions. | `python -m unittest tests.test_versioning` |
| [`RELEASING.md`](RELEASING.md) | How a release is versioned, checked, tagged and published; what happens after a defective release. | — |
| [`ROADMAP.md`](ROADMAP.md) | What is not verified yet, known limitations in the verified path, and planned work, with the condition that closes each item. | scope boundary and next steps |
| [`PROFILES.md`](PROFILES.md) | V1 component-tier claim boundary, v1 generality work, and the v2 disaggregated-path extension. | product, evidence, and adapter direction |
| [`METHOD.md`](METHOD.md) | The procedure in full — the five gates, what counts as evidence, canonical terms. **Read this to understand.** | measurement and claim rules across evidence paths |
| [`CHECKLIST.md`](CHECKLIST.md) | Common measurement checks and gate-specific criteria, with implementation coverage and operator responsibilities. | Gate 1 evidence review and every Gate 2–5 experiment, including reference scripts and external producers |
| [`FAILURE-CATALOG.md`](FAILURE-CATALOG.md) | How a tier reports a plausible number instead of an error, and **which check catches each one** — including the ones no script catches. | all, plus operator preconditions |
| [`EVIDENCE.md`](EVIDENCE.md) | Normalized evidence contract, exit status, and claim-suppression behavior. | `python -m kvprobe report` |
| [`EXTERNAL-RUNS.md`](EXTERNAL-RUNS.md) | Context-run shape for stacks without reference scripts (e.g. Dynamo KVBM), and what a phantom tier looks like. | `python -m kvprobe ingest` |
| [`CONFIGURATION.md`](CONFIGURATION.md) | Short workflow to record the exact run setup and generate its fingerprint. | `python -m kvprobe fingerprint` |
| [`GEOMETRY.md`](GEOMETRY.md) | Resolve a model config and derive standard-attention KV bytes from it plus explicit runtime dtype, chunk, and TP settings. | `python -m kvprobe geometry` |
| [`PORTABILITY.md`](PORTABILITY.md) | Capability-first validation sequence for a new platform, with DGX Spark lessons and the validation that remains. | `python -m kvprobe doctor` |
| [`NIXL-STATIC-GDS-MATCHED.md`](NIXL-STATIC-GDS-MATCHED.md) | Historical v0.4.1 reproduction for native `GdsBackend` vs LMCache NIXL static + `GDS_MT`. | `tools/nixl_matched.sh`, `tools/compare_matched.py` |
| [`NIXL-MODE-MATRIX.md`](NIXL-MODE-MATRIX.md) | Newer-upstream POSIX/GDS_MT × static/dynamic experiment, gates, and identity rules. | `tools/nixl_mode_matrix.sh`, `tools/compare_matched.py` |
| [`NIXL-POOL-SIZING.md`](NIXL-POOL-SIZING.md) | Policy-free static-pool capacity arithmetic, required observations, and the sizing sweep contract. | `python -m kvprobe pool-plan` |
| [`PROMPT-CONSTRUCTION.md`](PROMPT-CONSTRUCTION.md) | How target tokens become cacheable chunks, how store/filler/retrieval requests induce offload, and which evidence verifies the result. | `g2_`–`g5_` prompt workloads |
| [`REVALIDATION.md`](REVALIDATION.md) | The regression merge gate: compare a run with a compatible workload/configuration reference. Also defines cross-check vs independent reproduction. `tools/summarize_runs.py`, `tools/verify_against_baseline.py` |
| [`reference/`](reference/README.md) | Implementation anatomy: responsibility boundaries, cache granularity, logical I/O, and disk layouts. These notes explain results but do not add measurement gates. | public source and version-scoped observations |

Recommended visitor path: **FIRST-VISIT → GATE1 → Gates 2–5**. Each gate's purpose, pass condition and
result are in one table: [the five gates at a glance](../README.md#the-five-gates-at-a-glance).
Use `METHOD` to understand the method, `CHECKLIST` throughout measurement,
`PROFILES` to define claim scope, and `FAILURE-CATALOG` to investigate misleading results.

The [`reference/`](reference/README.md) section is deliberately outside that operational path.
Read it when you need to understand why a backend produced a particular object, descriptor, file,
or counter pattern.

## Start here

- [`FIRST-VISIT.md`](FIRST-VISIT.md) runs one guided command and carries the capability result
  into Gate 1 preparation, whether the host is a reference candidate, needs an adapter,
  or must resolve missing prerequisites. Its
  [output walkthrough](FIRST-VISIT.md#5-reading-the-output-decisions-first-host-route-second)
  explains every field and the difference between a demo PASS and hardware validation.
  [What you can do next](FIRST-VISIT.md#6-what-you-can-do-next) maps each task to
  its inputs, outputs, role in Gates 1–5 and detailed instructions.
- [`GATE1.md`](GATE1.md) is the next step: prepare geometry and configuration, select an
  evidence path and review measurement validity. Repeat these checks in Gates 2–5.
  NVMe tracing is optional; Gate 1 validation is not. Diagnostic completion alone is not
  serving verification.
- [`PROFILES.md`](PROFILES.md) separates the current component-tier claim from the planned
  disaggregated-path extension and defines how topology and cache scope remain separate dimensions.
- [`METHOD.md`](METHOD.md) defines the five gates, evidence rules, and canonical terminology.
- [`../scripts/README.md`](../scripts/README.md) lists the reference measurement scripts for
  Gates 2–5 and the host prerequisites. Each one also produces the Gate 1 evidence for the points it
  measures; the Gate 1 guide covers preparing and reviewing that evidence.
- [`CHECKLIST.md`](CHECKLIST.md) is the execution companion: what each gate passes on, who or what
  verifies it, and the trap that most often fools it. Use it during Gate 1 preparation and
  evidence review, then throughout Gates 2–5; it is not limited to `g2_`–`g5_` scripts.
- [`FAILURE-CATALOG.md`](FAILURE-CATALOG.md) catalogues the ways a tier reports a plausible number
  while doing something else, with the checks that expose each one — read it before trusting any
  tier measurement, yours or ours.
- [`EVIDENCE.md`](EVIDENCE.md) defines the normalized input and explains why a `FAIL` or
  `UNPROVEN` gate suppresses performance claims. It also shows graded readings, unjudged,
  without relaxing those gates.
- [`CONFIGURATION.md`](CONFIGURATION.md) gives the short workflow for copying the example,
  recording the live run setup, checking it, and generating its fingerprint. Detailed design
  rationale is kept in [`reference/`](reference/CONFIGURATION-IDENTITY.md).
- [`PORTABILITY.md`](PORTABILITY.md) defines the capability-first validation sequence and records
  what DGX Spark validation taught and what remains to verify.
- [`NIXL-STATIC-GDS-MATCHED.md`](NIXL-STATIC-GDS-MATCHED.md) gives the matched native
  `GdsBackend` versus NIXL `GDS_MT` reproduction procedure and its historical 2026-09-08 strict-gate result.
- [`NIXL-POOL-SIZING.md`](NIXL-POOL-SIZING.md) defines how configured slots, intended live
  working set, forced eviction, and observed device behavior must be kept separate.
- [`PROMPT-CONSTRUCTION.md`](PROMPT-CONSTRUCTION.md) explains how the scripts turn context targets
  into deterministic prompt identities, complete cache chunks, filler pressure, and verified
  lower-tier reads.

## Validation and release controls

- [`REVALIDATION.md`](REVALIDATION.md) describes comparison with the bundled English-v1 reference.

## Version numbers in this repository

The package and each data contract carry their own version and change independently. The registry,
the rules and the history are in [`VERSIONING.md`](VERSIONING.md); `tests/test_versioning.py` keeps it
in step with the code.

## Keeping this current

This block is the index of **status claims** — what is validated as of today. Those are what go
stale, which makes this the first thing to check. Two rules keep it honest:

1. **Update it in the same change that produces the result.** A run that changes what has been
   validated — a new gate result, a cross-check completed, a script merged to `main`, a bundle
   published — updates this block and its date in the same commit. Not afterwards.
2. **Its date is never older than the newest status claim anywhere in the repository.** That is a
   checkable invariant: if `METHOD.md` or `NIXL-STATIC-GDS-MATCHED.md` says something is validated
   and this block does not mention it, one of them is wrong.

**A dated observation is not a status claim.** The failures in
[`FAILURE-CATALOG.md`](FAILURE-CATALOG.md) carry dates because a reader needs to know when we saw
them, but "on 2026-08-25 a metric reported the same throughput whether every write failed or
succeeded" does not age — the date pins it. Those stay out of this index; what they owe the reader
instead is a label saying they are historical observations rather than current results. Only
sentences of the form "X *is* validated" belong here.

If a claim here can no longer be stated accurately, **delete it rather than letting it age.** An
absent line costs a reader nothing; a stale one costs them trust in every other line.

## Current status — 2026-11-13

- The hardware-verified scope is the one in the
  [README scope table](../README.md#scope--what-is-verified-today): local NVMe on three named paths.
  What is not verified yet, known limitations in that path and planned work are listed in
  [`ROADMAP.md`](ROADMAP.md).
- The GPU-free first-visit path is implemented: one guided `first-visit` command, `doctor`, four
  bundled demo cases, automatic five-gate reports, claim suppression, and CPU-only CI. DGX Spark
  validation produced the measurement lessons in
  [`reference/UNIFIED-MEMORY-LESSONS.md`](reference/UNIFIED-MEMORY-LESSONS.md), not a
  hardware-verified tier; [`PORTABILITY.md`](PORTABILITY.md) lists the validation that remains.
  This repository does not claim turnkey cross-platform support or distributed performance.
- The documented visitor path now continues from first-visit to Gate 1 preparation and
  evidence review, then Gates 2–5. Experimental `gate1 plan`, `capture`, `analyze` and `verify`
  commands exist. The separate NVMe `gate1 verify` review is limited to serial native
  cuFile GDS; independent trace filter/clock acceptance and an actual `verified` serving
  run under that contract remain unestablished. Further NVMe feature expansion is paused;
  completion of that verification track was not a prerequisite for the public baseline. It does not
  replace common validation or the evidence report's Gate 1 verdict.
- On 2026-10-08 all five gates were measured in one run of one declared configuration, on the
  host and model of the 2026-10-05 re-measurement: a native cuFile GDS arm with its recompute arm,
  4,096-token entries of 1 GiB, and a 1 GiB staging pool so that Gate 4 can classify hits. The
  report under the current assessment (`0.6.2`) is `PASS` with performance claims allowed:
  - Gate 1: 10/10 requests moved the expected bytes on the tier's own disk, per the per-device
    cgroup counter (`corroborated`).
  - Gate 2: request TTFT p50 0.18 s on retrieval versus 0.77 s on recompute, 10 samples each.
  - Gate 3: `ECONOMICAL`, 0.23 reuses to repay admission against an expected 1, counting all 10
    attempts: 5 served by the tier and 5 `store_unattributed` (no observed write during the store),
    although the engine logged all 10 stores as complete. Counting only the successful attempts
    would give 0.12, so counting every attempt nearly doubles the reuses needed.
  - Gate 4: in working sets beyond the upper tier's capacity, the tier served 4 of 15 retrievals;
    3 were classified as upper-tier hits, and 8 fell back or missed. The tier was needed there
    but did not serve all the overflow.
  - Gate 5, at 2,048 tokens per request: every batch met its byte and correctness checks through
    concurrency 2; the batches above it did not and are not credited to the tier.

  A gate passes when its measurement is valid, not when the tier serves every request. The Gate 3
  and Gate 4 limits match the staging-pool behaviour in
  [`FAILURE-CATALOG.md` §13](FAILURE-CATALOG.md#staging-buffer-full), and the pool was sized for
  Gate 4's one-entry rule, not for throughput. This is one host, one model, one configuration and
  one run, not cross-platform validation. Its evidence bundle and report are retained in
  access-controlled storage; no document from this run is bundled in `examples/`.
- A 2026-10-05 re-measurement at 4,096 context tokens produced report Gate 1 and Gate 2
  `PASS` in all nine cache-arm evidence documents (buffered POSIX, native cuFile GDS and
  NIXL `GDS_MT`, three runs each). All 90 measured cache-arm requests recorded the expected
  1 GiB through the per-device cgroup counter. The grade is `corroborated`, not request-ID
  hardware tracing. Gates 3–5 were not supplied, so every report remains `PARTIAL` with
  performance claims suppressed. This is one host, one model and concurrency 1, not
  cross-platform validation or acceptance of the separate NVMe verification track.
  Raw bundles and the other result reports are retained in access-controlled storage; one of the
  nine documents is bundled, with identifying fields substituted, as
  [`../examples/evidence/gate1-pass-filtered-counter.json`](../examples/evidence/gate1-pass-filtered-counter.json).
- A 2026-09-25 four-arm reference capture covers the current portable English
  `transport/english-v1` workload: 600/600 rows passed the correctness check
  (expected value present in the completion) and
  controlled-interval expected-byte corroboration, with at most 1.8% p50 drift across the 20
  context/arm cells versus the historical run. Those runs predate the per-device counter reader, so their
  Gate 1 reads `UNPROVEN`, not PASS; see [`REVALIDATION.md`](REVALIDATION.md).
- On 2026-09-21 the TP=2 rank-local KV geometry was measured on one host: a 256-token chunk was
  64 MiB per rank at TP=1 and 32 MiB at TP=2, node-aggregate bytes unchanged. That is V2 preparation,
  not a disaggregated-path result; see [`PROFILES.md`](PROFILES.md).
- Historical Gate 3 producer cross-check on 2026-09-10, before the all-attempt contract: reversing
  context order produced 10/10 observed stores and retrievals at both entry sizes and the same
  sub-one-reuse conclusion. It is not a validation of adapter `0.4.0`; re-ingesting that run keeps
  the legacy calculation, so the all-attempt path requires a new run. Gate 4
  only through the clean crossover: one through three entries were DRAM-only and the
  fourth was three DRAM plus one NVMe, with no fallback or miss. Behavior well above the crossover
  remains out of scope. Independent reproduction by someone else is a separate, later bar.
- On 2026-09-09 the whole gate procedure was re-run on a second model whose KV bytes per token are
  half the first's. Gates 1, 2 and 5 passed for both, at different break-even and concurrency
  coordinates; that bundle has no Gate 3 or Gate 4 run. It shows the procedure transfers, not that
  any number does.
- Reference closure exposed two usability failures now guarded here: `doctor` searches common CUDA
  toolkit locations when `gdscheck` is absent from PATH, and the controlled runner rejects an API
  model root that differs from the requested `MODEL` path.
- Historical, 2026-09-08: a strict native `GdsBackend` versus NIXL `GDS_MT` matched rerun passed the
  matched comparator's own gates, on the former localized payload. Its evidence is `nvidia-fs` only, so
  under the current verdict contract its Gate 1 would read `UNPROVEN` and its TTFT is not a current claim; see
  [`NIXL-STATIC-GDS-MATCHED.md`](NIXL-STATIC-GDS-MATCHED.md).
- The sanitized English-v1 comparison fixture is
  [`../examples/baselines/transport-english-v1-20260925.json`](../examples/baselines/transport-english-v1-20260925.json).
  The closure decision and checksummed raw bundles are kept in access-controlled storage; see
  [`EVIDENCE.md`](EVIDENCE.md).
