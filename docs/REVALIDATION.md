# Regression validation — comparing a run with a compatible reference

Measurement code can keep running while silently changing what it measures. Regression validation
compares a candidate run with a reviewed reference that has the **same workload identity and
configuration scope**. It is a merge gate for changes to measurement behavior, not a claim that
two different workloads are interchangeable.

The comparison tool needs no GPU or server:
[`../scripts/tools/verify_against_baseline.py`](../scripts/tools/verify_against_baseline.py) reads
two JSON summaries, checks latency and throughput within a tolerance, and requires correctness and
serving-tier gate counts to match exactly.

## Current reference

The bundled reference is
[`../examples/baselines/transport-english-v1-20260925.json`](../examples/baselines/transport-english-v1-20260925.json).
It records the reviewed 2026-09-25 `transport/english-v1` capture:

- profile `transport`, version `english-v1`, seed 17;
- recompute, buffered POSIX, native GDS, and NIXL GDS_MT;
- 256, 512, 1,024, 2,048, and 4,096 cacheable tokens;
- three arm-order runs and 30 measured requests per arm/context cell;
- 600/600 passed the correctness check (expected value present in the completion) and controlled-interval expected-byte
  corroboration;
- at most 1.8% p50 drift across the 20 cells versus the earlier historical run.

At 4,096 tokens, p50 TTFT was 781.14/290.64/182.52/171.49 ms for
recompute/POSIX/native GDS/NIXL, respectively.

This is a **host- and configuration-scoped regression reference**, not a portable performance
target. Its counters are interval-aligned corroboration, not request-ID hardware traces or direct
target-device-filtered Gate 1 evidence — the capture predates the per-device counter, so its Gate 1
reads `UNPROVEN` and its latencies are a comparison fixture rather than a performance result. That
is enough for a merge gate, which asks whether the same measurement still behaves the same way. The sanitized file contains comparison fields and scope,
not raw logs or deployment identifiers. The closure decision, provenance, and raw checksummed
bundles are kept outside this repository in access-controlled storage; see
[`EVIDENCE.md`](EVIDENCE.md).

## Compatibility rules

Compare two runs only when all of the following match:

- prompt profile, profile version, seed, and prompt identity;
- model and tokenization;
- cacheable-token targets and KV geometry;
- arm semantics and backend mode;
- serving configuration, including TP, cache capacities, and relevant pool sizes;
- counter provider and evidence scope;
- sample aggregation shape.

A mismatch is not performance drift. It is a different experiment and needs a separate reference.
Pool rows only when their workload versions match, and keep old latency values
with their original profile.

## Procedure

1. Freeze the candidate configuration and record its workload metadata.
2. Run the relevant measurement and retain raw output, configuration, and provenance.
3. Produce the candidate summary from the context runs with `summarize_runs.py`. Name each run with
   the arm label the reference uses, and state the scope the runs cannot record — model,
   accelerator, tensor-parallel size, arm-order seed, chunk geometry — in `--scope` (a reference's
   own file works when those match). The tool takes the prompt identity, filler and warm-up
   settings, runs per arm and counter scope from the runs, and refuses a stated value they
   contradict:

   ```bash
   python3 scripts/tools/summarize_runs.py \
     --scope examples/baselines/transport-english-v1-20260925.json \
     --run recompute randomized-run1-recompute.json --run native-gds randomized-run1-gds.json \
     --run posix randomized-run1-posix.json --run nixl-gds-mt nixl-run1.json \
     ...one --run per arm and run file... \
     --source "<what produced these runs>" --output candidate.json
   ```

   Rebuilt from the 2026-09-25 raw runs (60 files), it reproduces the bundled reference exactly:
   the same workload, scope and all 20 cells. A run with a per-device counter states
   `target_device_filtered: true`, so it is not identical in scope to that reference.
4. Compare the candidate with the compatible reference:

   ```bash
   python3 scripts/tools/verify_against_baseline.py \
     --baseline examples/baselines/transport-english-v1-20260925.json \
     --candidate candidate.json
   ```

5. Record the candidate evidence, comparator output, tolerance, and any explained difference in
   the pull request. A gate-count mismatch is a behavioral change, not acceptable drift.

The bundled file can be used to verify the path itself without a GPU:

```bash
python3 scripts/tools/verify_against_baseline.py \
  --baseline examples/baselines/transport-english-v1-20260925.json \
  --candidate examples/baselines/transport-english-v1-20260925.json
```

## What the comparator checks

| Field kind | Examples | Rule |
|---|---|---|
| Workload identity | profile, version, seed, prompt SHA-256 | exact object match |
| Configuration scope | model, accelerator, TP, fillers, aggregation | exact object match |
| Latency / throughput | `ttft_p50_s`, `ttft_p95_s`, `speedup`, `requests_per_s` | within relative tolerance |
| Gate counts / verdicts | `n`, `correct`, `tier_ok`, `tier_purity_ok` | must match exactly |
| Workload geometry | `cacheable_tokens`, `kv_expected_mib` | must match exactly |
| Telemetry, timestamps, raw rows | GPU temperature, clocks, wall-clock | ignored |

The default relative tolerance is 5%. Tighten it with `--tolerance` only when the reference and
candidate share sufficiently similar conditions. Tolerance never applies to exact gate counts.

## Cross-check and independent reproduction

**Cross-check.** The same quantity is obtained with nuisance factors varied, or through a second
signal. This catches analysis and implementation mistakes before a result supports a claim.

**Independent reproduction.** A different person on a different machine applies the procedure and
obtains a consistent result. This is a stronger, later bar. The bundled reference is not evidence
that independent reproduction has occurred.

## Historical note

The project originally checked that replacing site literals with environment variables and
translating comments did not change the former localized workload. That one-time
pre-/post-parameterization result is retained only as provenance for historical measurements.
`transport/english-v1` intentionally changed the payload, so the historical comparison is not a
baseline or merge gate for current runs.
