# Gate 1: establish what your measurement can claim

Start here after [first-visit](FIRST-VISIT.md): **read an example → prepare your run → collect → report**.
Gate 1 asks whether the intended tier served the KV data, with correct output and external byte
evidence. Repeat that validation at every measured point in Gates 2–5.

The local NVMe reference path below uses `g5_controlled_kv_experiment.sh one` to collect matched
context runs
for Gates 1 and 2. There is no additional `g1_*` benchmark: `ingest` derives expected bytes from the
recorded configuration and normalizes observations; `report` judges them. The optional
`kvprobe gate1` commands are a separate [NVMe observation route](#optional-nvme-walkthrough).

For the NFS/RDMA Gate 1-only adapter, use the [reference path](NFS-RDMA-EXPERIMENTAL.md) to ingest
qualified retained originals and report the verdict. It is available on main and unreleased;
live producer setup guidance remains follow-up work. It uses NFS-service evidence rather than
the local block-device counter used in this walkthrough.

## 1. Read a verdict without a GPU

From the repository root, run these three synthetic examples, one command at a time:

```bash
python3 -m kvprobe report examples/evidence/gate1-pass-filtered-counter.json
python3 -m kvprobe report examples/evidence/attribution-unfiltered-counter.json
python3 -m kvprobe report examples/evidence/phantom-tier-framework-hit.json
```

Read the `Gate 1 Serving Tier Verification` row before any timing result.

| Example | Gate 1 | Overall | What the row establishes | Next action |
|---|---|---|---|---|
| `gate1-pass-filtered-counter` | `PASS` | `PARTIAL` | 10/10 target-tier requests have device-filtered evidence and match expected bytes | Read Gate 2, then collect the missing Gates 3–5 |
| `attribution-unfiltered-counter` | `UNPROVEN` | `UNPROVEN` | Expected bytes match 10/10, but device-filtered evidence is 0/10 | Collect a counter filtered to the tier's device |
| `phantom-tier-framework-hit` | `FAIL` | `FAIL` | All 3/3 requests classified as target-tier hits moved zero bytes | Check whether the entry was stored and whether an upper buffer answered |

The first example prints these selected lines:

```text
Overall: PARTIAL
Gate 1 Serving Tier Verification: PASS — 10/10 target-tier requests have per-request, target-device-filtered byte evidence (corroborated); 10/10 request intervals have qualifying per-request observations; 10/10 target-tier requests match expected bytes; 0/10 moved no bytes from the tier
Gate 2 Single-hit retrieval break-even: PASS — retrieval and recompute were measured on tier-verified requests
```

**All three commands exit 1.** In the first example, Gates 3–5 were not supplied, so the whole
report remains `PARTIAL` despite Gate 1 passing. The other examples exit 1 for their actual
`UNPROVEN` or `FAIL` verdict. These are report verdicts, not CLI crashes. Synthetic examples
demonstrate the contract; your host needs its own controlled run.

<a name="fast-path-produce-four-reviewable-items"></a>

## 2. Prepare the measurement

This walkthrough uses the reference stack. If first-visit's three questions do not all have a yes
answer yet (a matched recompute arm, an external tier-byte counter aligned to each controlled
request, a way to detect or disable fallback), stay at Gate 1: record the gap and make no
tier-performance claim. Geometry and the configuration review below still prepare a later run. If
first-visit reported `ADAPTER_REQUIRED`, collect with your own producer through
[other evidence routes](#other-evidence-routes-and-diagnostics).

Choose the tier arm for this walkthrough in the same shell used for the runbook:

```bash
TIER_ARM=posix  # Use gds for the native cuFile GDS path
```

Both context runs below execute sequentially on the selected GPU. The separate GPU 1 recompute
service in the all-five runbook is needed by Gates 3 and 4, not by this pair.

| Prepare | Do this | Keep / check |
|---|---|---|
| Model and expected bytes | Run the [five-minute geometry path](GEOMETRY.md#five-minute-path) for your model and runtime. The reference launcher uses 256-token chunks and TP=1; match dtype to the serving process | `geometry.json`; use the per-device bytes for the device-filtered counter. Set the launcher's `PROBE_MIB_PER_CHUNK` accordingly |
| Host and environment | Follow [Preparing a host](../scripts/README.md#preparing-a-host), then runbook steps 1 and 2 with the chosen arm's preflight block | Keep `IMAGE`, `MODEL_HOST_DIR`, `MODEL`, `PROBE_GPU_INDEX`, `OUT`, `RUN`, context/sample/filler settings in that shell. A `NOT READY` preflight stops preparation |
| Exact run setup | Create `$OUT/configuration.json` from runbook step 9 now, before collecting. Use the POSIX arm replacement there when `TIER_ARM=posix`; follow the [configuration review](CONFIGURATION.md#five-minute-path) | Model, KV geometry, image, GPU identity, cache capacities, backend, paths, counter scope and workload must agree with the planned commands and the saved runtime records |

Retain the reviewed geometry with the run, then fingerprint the configuration:

```bash
cp geometry.json "$OUT/geometry.json"
python3 -m kvprobe fingerprint "$OUT/configuration.json" \
  > "$OUT/configuration.sha256"
```

The fingerprint detects changes to the declared setup; it does not observe the serving process.
Review the container inspection and startup logs after collection too. Keep an unknown setting or
counter scope as a limitation, and resolve it before claiming tier performance.

Apply the [common measurement checklist](CHECKLIST.md): correctness, counter scope, residency,
isolation, fallback and evidence retention. `first-visit` capability candidates and `tier-check`
readiness are preparation results, not serving verification.

## 3. Collect evidence

Every tier request needs its own observation: the counter is read at the start and end of each
request, so the bytes can be attributed to that request. A batch or run total cannot show which
request read them. The reference launcher below records per-request observations; for another
serving stack, see [other evidence routes](#other-evidence-routes-and-diagnostics).

On the prepared reference stack, from the repository root and the same shell:

```bash
EVIDENCE="$OUT/g5-$RUN" bash scripts/g5_controlled_kv_experiment.sh one recompute 1
EVIDENCE="$OUT/g5-$RUN" bash scripts/g5_controlled_kv_experiment.sh one "$TIER_ARM" 1
```

The first command starts the recompute arm; the second replaces it with the chosen tier arm.
They create/remove launcher-labelled containers and initialize their cache directories. Reserve
the selected GPU and port, and review the [runbook effects and cleanup](../scripts/README.md#minimal-runbook--all-five-gates-one-configuration)
before executing them. A failed request stays in the raw run; it is not removed to obtain a pass.

| Command | Main output under `$OUT/g5-$RUN/` |
|---|---|
| `one recompute 1` | `randomized-run1-recompute.json` |
| `one posix 1` | `randomized-run1-posix.json` |
| `one gds 1` | `randomized-run1-gds.json` |

The JSON records request outcomes, timings, byte observations and run identity. Container
inspection and logs are retained alongside it. In a tier run, inspect correctness,
`cgroup_read_bytes` and the counter's recorded block-device identity. This counter is filtered
to that device; host-wide `nvidia-fs` observations alone do not establish that scope.

Stop the measured arm and bundle the retained run using the same evidence directory:

```bash
EVIDENCE="$OUT/g5-$RUN" bash scripts/g5_controlled_kv_experiment.sh stop
EVIDENCE="$OUT/g5-$RUN" bash scripts/g5_controlled_kv_experiment.sh collect
```

The raw directory remains, and `collect` prints the archive path under `$HOME`. Keep raw bundles
outside the public repository.

## 4. Normalize, read Gate 1 and continue

Normalize the two raw files against the reviewed configuration:

```bash
python3 -m kvprobe ingest \
  "$OUT/g5-$RUN/randomized-run1-${TIER_ARM}.json" \
  "$OUT/g5-$RUN/randomized-run1-recompute.json" \
  --configuration "$OUT/configuration.json" \
  --run-id "kvprobe-$RUN" \
  --output "$OUT/evidence.json"
```

`ingest` writes `evidence.json` and exits 0, or exits 2 with the field or identity it refused.
It derives expected bytes from configuration KV geometry and recorded cacheable tokens, retaining
observed bytes and attribution scope for the report to judge.

```bash
python3 -m kvprobe report "$OUT/evidence.json" --output "$OUT/report.txt"
```

Read `$OUT/report.txt`, starting with `Gate 1 Serving Tier Verification`. With valid evidence,
this two-run path can give Gate 1 and Gate 2 `PASS`; Gates 3–5 are `SKIP_UNSUPPORTED` because
their measurements were not supplied. Overall is then `PARTIAL`, `report` exits 1 and performance
readings remain withheld. A violation or missing evidence can instead produce `FAIL` or `UNPROVEN`;
read the actual verdict rather than expecting a pass.

Retain four reviewable groups: geometry; configuration and fingerprint; raw runs and runtime
records; normalized evidence and report.

| Gate 1 result | What to do next |
|---|---|
| `PASS` | Read Gate 2's finding, then follow the remaining [runbook measurements](../scripts/README.md#minimal-runbook--all-five-gates-one-configuration) for Gates 3–5 supported by your host. Repeat Gate 1 validation at every measured point |
| `UNPROVEN` | Collect the missing scope or observations named by [Next steps](#next-steps-in-the-report); matching byte totals on an unfiltered counter are insufficient |
| `FAIL` | Inspect each measured violation and any additional missing evidence in [Next steps](#next-steps-in-the-report). Zero bytes can mean no stored entry or another buffer answering; the result alone does not choose the cause |

On the single-GPU POSIX runbook path, Gate 5 is also available. Gates 3 and 4 need the reference
GDS/second-GPU setup, so that path still ends `PARTIAL`; see [host shapes](../scripts/README.md#preparing-a-host).

## Other evidence routes and diagnostics

The reference `g2`, `g3` and `g4` producers use host-wide `nvidia-fs` counters. Preserve their
declared scope; the controlled g5 path above supplies the device-filtered request observations.
For another serving stack, provide controlled intervals, correctness, independent expected bytes
and external observations through the [producer contract](EVIDENCE.md#producing-evidence-from-a-run).
Missing evidence stays unproven.

NVMe tracing is optional. It adds command observations around controlled phases and has its own
quality and serving-review limits; it does not replace the counter-based report contract.

### Optional NVMe walkthrough

Use this route only when local NVMe command observation is relevant; it is not
required for the counter-based Gate 1 path. Start with the read-only `gate1 plan`,
then use a bounded privileged `capture` only after reviewing its ready signal.
Capture generates no I/O and does not attach to the serving client automatically.
The current serial phase producer is the direct Python g5 client with
`--phase-export-dir`; preserve the capture and producer originals, prepare a
separate analysis view, and run strict offline `gate1 analyze`. Keep trace-on
latency separate from performance measurements. The complete command sequence,
privilege boundary, file contract, loss/order policies and current acceptance
limits are in [NVMe command observation](GATE1-NVME-TRACE.md).

### Next steps in the report

When Gate 1 is `FAIL` or `UNPROVEN`, the text and Markdown reports end with a *Next steps* block, and
`first-visit` adds it to the bundled reports it writes. Each step names one finding with its count,
why it keeps Gate 1 from passing, what to check or collect next, what the finding does not show, and
where to read more. The block follows the optional READINGS section, so it remains the last section
with either the default options or `--no-readings`. Measured violations come first, then missing
evidence. Every finding that applies is listed, so one problem does not hide another.

The block explains the verdict; it does not change it. The verdict, the claim state and the exit code
stay as they are. The JSON report (`--format json`) and its `assessment_version` are unchanged, and a
`PASS`, `SKIP_UNSUPPORTED` or `INVALID` Gate 1 gets no block. A step never names a cause the evidence
cannot show: zero bytes, for example, can mean the entry was never written or that another buffer
answered, and the step says both.

| Gate 1 | Step | Finding |
|---|---|---|
| `FAIL` | Wrong output | A request, from any source, returned a wrong answer |
| `FAIL` | Tier device read nothing | A target-tier request with known expected bytes read zero bytes |
| `FAIL` | Fewer bytes than expected | A target-tier request read fewer bytes than expected in its own interval, beyond the tolerance, on any counter |
| `FAIL` | More bytes than expected on the tier's device | A target-tier request read more bytes than expected in its own interval on a counter filtered to the tier's device |
| `FAIL` | No target-tier request | No request is attributed to the target tier |
| `FAIL` or `UNPROVEN` | Counter not filtered to the tier's device | A target-tier request's counter is not filtered to the tier's device |
| `FAIL` or `UNPROVEN` | Not observed per request | A request is observed per batch or per run, not in its own interval |
| `FAIL` or `UNPROVEN` | Heuristic attribution | A request observed in its own interval rests on `heuristic` confidence |
| `FAIL` or `UNPROVEN` | No expected bytes | A target-tier request has no expected byte count |

The first four are measured violations. Every recorded request must return a correct answer,
including recompute and upper-tier requests; an incorrect answer alone does not identify its cause.

The last five describe missing evidence. A run with no target-tier requests keeps the existing
`FAIL` verdict because it contains no tier service to verify. Missing findings follow any measured
violations, and remain visible after a `FAIL` so they can be collected once a violation is fixed.

Optional [`gate1 verify`](GATE1-SERVING-REVIEW.md) records a reviewed companion
for serial native cuFile GDS when all automatic/operator checks hold. Actual
whole serving acceptance is not established. It is not a mandatory public-use
step, does not modify trace observations and is separate from evidence v0.3.
Observation JSON is not serving evidence accepted directly by ingest.

Continue with [Gate 2 retrieval break-even](METHOD.md#gate-2--single-hit-retrieval-break-even),
then admission cost, capacity justification and operating envelope. Gate 1
remains a common validation condition, not permission to skip later checks.
