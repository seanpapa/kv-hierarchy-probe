# Gate 1: establish what your measurement can claim

This is the next step after [first-visit](FIRST-VISIT.md).
The visitor path is **first-visit → Gate 1 → Gates 2–5**. Gate 1 establishes
measurement validity, not a benchmark score or a one-time host certificate.
Repeat its checks at every measured point in Gates 2–5.

## Fast path: produce four reviewable items

Use the four items below as the working path. Each linked reference provides
additional detail when that step needs it.

| Step | Do this | Keep this output | Stop when |
|---|---|---|---|
| 1. Expected bytes | Run [`geometry`](GEOMETRY.md#five-minute-path) for **your model and live runtime settings**. It is not Qwen-specific. | `geometry.json` | The model layout is refused, or dtype/chunk/TP/PP cannot be matched to the serving process. |
| 2. Exact run setup | Follow the short [configuration workflow](CONFIGURATION.md#five-minute-path): replace the example with values from the live process and `geometry.json`, then generate a hash that detects setup drift. | `configuration.json` and `configuration.sha256` | Any model, backend, build, device, counter scope or mount value cannot be verified against the live process. |
| 3. Evidence route | Choose the reference counter path, another-stack producer, or optional NVMe observation from [Collect evidence](#3-collect-evidence). | Raw counters, request intervals, correctness and fallback evidence | No external signal can identify the intended tier for each controlled interval. |
| 4. Verdict | Normalize and run `report`; read Gate 1 before any timing result. | Evidence JSON and report | Proceed to a tier-performance claim only after Gate 1 passes. |

For a first visit, do **Step 1 only**, then return here. The optional NVMe
walkthrough is not required unless NVMe command observation is relevant to your
evidence route.

## 1. Carry forward the first-visit result

Keep the capability manifest and demo reports. A `CANDIDATE` adapter means
prerequisites were detected, not that GDS or the serving stack works.
`ADAPTER_REQUIRED` means the reference evidence source does not fit; another
stack can still apply the method with its own controlled producer and counters.

Before a tier-performance experiment, require a matched recompute arm, external
bytes aligned to controlled intervals, and a way to detect or disable fallback.
If one is missing, stay at Gate 1: record the gap, prepare geometry or inspect
existing evidence. Treat a diagnostic observation as preparation and reserve
performance claims for complete evidence.

## 2. Prepare the measurement

1. Derive expected bytes with [geometry](GEOMETRY.md), using actual model,
   dtype, chunk and parallelism inputs. Match the scope to the counter.
2. Follow the [five-minute configuration path](CONFIGURATION.md#five-minute-path):
   record the exact run setup, check it against the live process, and generate
   its fingerprint (the SHA-256 used to detect setup drift).
3. Apply the [common checklist](CHECKLIST.md): counter availability/scope,
   residency/isolation, correctness, fallback and evidence retention.
4. Choose the path below. Neither path automatically completes all checks.

## 3. Collect evidence

| Path | What it does | Start with |
|---|---|---|
| Reference counter-based experiment | g2–g5 collect external deltas and workload results. Gate 1 validation applies within each experiment; there is no additional standalone benchmark. | [Scripts prerequisites and commands](../scripts/README.md) |
| Another serving stack | Your producer supplies controlled intervals, independent expected bytes, external deltas and correctness. Missing evidence stays unsupported. | [Evidence producer contract](EVIDENCE.md#producing-evidence-from-a-run) |
| Optional local NVMe observation | `gate1 plan → capture → analyze` adds namespace command observations around controlled phases. It does not replace serving review or the report contract. | [NVMe collector and input contract](GATE1-NVME-TRACE.md) |

NVMe tracing is optional; **Gate 1 validation is not**. A tier without NVMe
tracing can use suitable external counters. Reference runners can create/remove
containers and initialize cache paths: review their effects before running them.

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

## 4. Review and continue

| Result | Next action |
|---|---|
| Missing control, unknown residency/fallback or insufficient scope | Keep the limitation; collect missing evidence or stop the unsupported claim. |
| NVMe analyzer `usable` | Inspect bytes, commands, outside-phase I/O, loss and pairing. This is observation quality, not serving PASS. |
| NVMe analyzer `incomplete` | Retain reasons and raw artifacts, keeping them visible without filtering or replacement. |
| `report` Gate 1 `PASS` | Every request correct, with expected bytes on a counter filtered to the tier's device. Proceed to Gates 2–5, repeating Gate 1 checks at every point. |
| `report` Gate 1 `UNPROVEN` | No violation measured, but the evidence cannot establish the tier served: usually a counter not filtered to the tier's device. Re-measure with the [per-device counter](EXTERNAL-RUNS.md#reaching-gate-1-pass). |
| `report` Gate 1 `FAIL` | A measured violation: wrong output, zero or too few bytes, or too many on a filtered counter. Fix the tier or the measurement before Gates 2–5. |

Optional [`gate1 verify`](GATE1-SERVING-REVIEW.md) records a reviewed companion
for serial native cuFile GDS when all automatic/operator checks hold. Actual
whole serving acceptance is not established. It is not a mandatory public-use
step, does not modify trace observations and is separate from evidence v0.3.
Observation JSON is not serving evidence accepted directly by ingest.

Continue with [Gate 2 retrieval break-even](METHOD.md#gate-2--single-hit-retrieval-break-even),
then admission cost, capacity justification and operating envelope. Gate 1
remains a common validation condition, not permission to skip later checks.
