# First visit: see what the probe can reveal

The first visit has two outcomes: understand the five decisions this repository can produce, and
classify the current host as **usable with the reference adapter**, **usable after an adapter is
added**, or **not currently measurable**. It does not run a hardware benchmark and it cannot
establish Serving Tier Verification for a new host.

**Next: [Gate 1](GATE1.md).** It connects this discovery result to measurement
preparation, evidence collection and review, then to Gates 2–5. Gate 1 is common
validation throughout those experiments; NVMe tracing inside it is optional.

## 1. Run the guided path

From the repository root with Python 3.10 or newer:

```bash
python3 -m kvprobe first-visit
```

To preserve a reading guide, the capability manifest and four bundled Markdown reports:

```bash
python3 -m kvprobe first-visit --output-dir /tmp/kvprobe-first-visit
```

Start with `/tmp/kvprobe-first-visit/README.md`. It orders the four reports,
explains what each verdict demonstrates and marks `doctor.json` as optional
host-capability detail rather than a hardware result.

Use `--verbose` when you also want the full host inventory and every bundled reporter case. The
default output stays focused on the decisions and the route into Gate 1.

The command performs four steps in order:

1. **See the five decisions.** It states what each gate can reveal and why Gate 1 controls whether
   the later performance results can be trusted.
2. **Explore one result and two traps.** It uses bundled evidence to show the five outputs, then
   contrasts host-wide bytes and a phantom framework hit with valid serving-tier evidence.
3. **Classify this host.** It checks conservative capabilities without changing the host and
   distinguishes adapter prerequisites from a hardware measurement that has not happened.
4. **Choose what to unlock next.** It separates capacity arithmetic available now from the Gate 1
   checkpoint and the Gate 2–5 decisions that follow it.

`first-visit` returns zero when the bundled examples produce their expected verdicts. Missing GPU,
GDS, LMCache, or NIXL capabilities do not make it fail: absence is a routing result, not a broken
tool. A detected reference adapter is only a **candidate**. Host discovery never proves that the
target filesystem served a request.

For a complete example and field-by-field interpretation, see
[Reading the output](#5-reading-the-output-decisions-first-host-route-second).

## 2. Apply the three-question stop rule

Before running a measurement script, answer all three questions:

- Can you run a matched recompute arm with the cache disabled?
- Can you align an external tier byte counter to each controlled request interval?
- Can you detect or disable silent fallback?

Use the result as follows:

| Result | Meaning | Continue with |
|---|---|---|
| Three yes answers and `reference adapter: CANDIDATE` | The reference stack may fit; filesystem and intervals remain unverified. | [Gate 1](GATE1.md), reference counter path |
| Three yes answers but `ADAPTER_REQUIRED` | The method fits but the reference evidence source does not. | [Gate 1](GATE1.md), another-stack path |
| Any no answer | A defensible tier-performance claim is not yet supported. | [Gate 1 preparation](GATE1.md#2-prepare-the-measurement); record the gap and stop the unsupported claim. |

Use external device-path evidence for this decision. Framework logs, an engine
hit counter, and stable TTFT are insufficient substitutes because they can all
remain plausible while the target tier serves zero bytes.

## 3. Know what the repository does next

`first-visit` is the preflight, not the product boundary. It prevents a long run on a host where
the intended tier cannot be observed. Once the stop rule passes, the rest of the repository:

- runs matched recompute and retrieval arms across context sizes;
- proves the serving source with request-aligned external counters and expected bytes;
- measures retrieval break-even, admission cost, capacity justification, and operating envelope;
- normalizes script output into a versioned evidence document; and
- permits performance claims only when all five report gates pass. `report` also
  displays graded readings without judging them or changing claim permission, and
  withholds the performance readings when a gate fails.

Immediately after `first-visit`, this narrower statement is supported:

> The CLI runs, the five decisions and their Gate 1 dependency are visible, host capabilities were
> recorded, and the bundled reports fail closed as expected.

The command then hands each unproven claim to a concrete repository path:

| Claim not established by `first-visit` | How this repository establishes it | Reviewable output |
|---|---|---|
| The host passes Gate 1 | Apply Common Measurement Validation during the Gate 2–5 experiments. | Request/batch counter intervals, expected-byte checks, correctness, and fallback/error review |
| GDS works on the target filesystem | Run the reference setup and preflight, then require actual target-device byte movement. | Backend initialization, `gdscheck`, `nvidia-fs` deltas, and stored files |
| A request was served from the intended tier | Match its controlled interval to external bytes and the independently derived payload. | Classified rows retained in the raw run and normalized evidence |
| The reference scripts work on this serving stack | Execute the documented preconditions and revalidation procedure on that stack. | Capability manifest, configuration identity, run output, and comparison result |
| A performance number is reportable | Complete the applicable gates, normalize with `kvprobe ingest`, and assess with `kvprobe report`. | `performance_claims: allowed` only on a passing report |

If the bundled adapter does not fit, the repository still supplies the method, evidence contract,
and fail-closed reporter; implementing the stack-specific evidence source is the remaining work.
It does not turn an unobservable tier into an observable one.

## 4. Continue in one order

First choose a goal from [What you can do next](#6-what-you-can-do-next).
Geometry, pool arithmetic and existing-evidence review do not require a new
hardware measurement. For a new hardware experiment, use the sequence below.

If the three-question stop rule passes, continue without skipping ahead:

1. Open [Gate 1](GATE1.md): review geometry/configuration and the common checklist.
2. Inside Gate 1, choose reference counters, another adapter or optional NVMe observations.
   Review setup/permissions and controlled interval collection before execution.
3. Inspect the resulting quality and serving-source evidence, including unknown conditions.
4. Continue to Gates 2–5 only for supported measurements, repeating Gate 1 checks at each point.
5. Retain raw evidence, normalize supported records with ingest and judge with report.

The stopping rule is intentional: if Serving Tier Verification cannot be established, further
latency measurements may be interesting diagnostics, but they are not tier-performance evidence.


## 5. Reading the output: decisions first, host route second

The default output is intentionally short enough to scan. It first tells a visitor why the five
gates matter, then shows a synthetic end-to-end result, two attribution traps, the current host's
route and the next checkpoint. The output below was copied from an actual run on a CPU-only
development host; Step 3 changes with host capabilities.

```text
kvprobe first-visit

STEP 1/4 — See the five decisions this probe can unlock
  Gate 1 · REALITY    Did the intended tier actually serve each request?
  Gate 2 · BENEFIT    At what context does retrieval beat recompute?
  Gate 3 · ECONOMICS  How many realized reuses repay one admission?
  Gate 4 · NECESSITY  When does upper-tier capacity make this tier useful?
  Gate 5 · LIMIT      Up to what load does the tier keep serving every request?
  Gate 1 evidence is required before Gate 2-5 performance results can be trusted.

STEP 2/4 — Explore one end-to-end result and two traps
  SYNTHETIC EXAMPLE — these values do not describe this host

  Gate 1 · REALITY    PASS - target-device bytes matched the expected KV payload.
  Gate 2 · BENEFIT    retrieval beats recompute on a single hit: offloading pays at this context
                      (retrieval 0.1700 s vs recompute 0.3000 s).
  Gate 3 · ECONOMICS  decision: ECONOMICAL — admission is repaid within the expected reuses
                      (expected reuses 2.00; reuses to repay 0.67); all 10 attempts included: 6
                      target-tier, 1 fallback, 3 store-unattributed.
  Gate 4 · NECESSITY  the upper tier overflowed and the target tier served the overflow (4 of 4
                      retrievals beyond its capacity; the rest stayed in the upper tier): the tier
                      is needed beyond the upper tier's capacity.
  Gate 5 · LIMIT      every measured batch met its tier-byte and correctness checks through
                      concurrency 2; the envelope's upper bound was not reached.

  Reporter self-check: 4/4 expected verdicts matched.
  WHY GATE 1 MATTERS
    UNPROVEN - matching host-wide bytes cannot identify the tier's device.
    FAIL     - a framework hit with zero device bytes is a phantom tier.
  Rerun with --verbose to inspect all four bundled cases.

STEP 3/4 — Classify this host
  GPU: not_detected
  docker: available
  nvidia_fs: not_detected
  reporter self-check: PASS
  reference adapter: ADAPTER_REQUIRED
  detail: host prerequisites for the bundled adapter (GPU, docker, nvidia-fs) are not all present;
          the method may still apply with another evidence source.
  hardware measurement: NOT_RUN
  Gate 1 serving-tier verification: NOT_ESTABLISHED
  Full capability inventory: python3 -m kvprobe doctor or rerun with --verbose.

STEP 4/4 — Choose what to unlock next
  AVAILABLE NOW — capacity arithmetic, without a GPU or model weights
    Any stack — expected bytes per chunk (Gate 1 preparation):
      Example model source: replace ORG/MODEL_NAME with your own
      public model ID or use a local model directory.
      python3 -m kvprobe geometry --model ORG/MODEL_NAME --chunk-tokens 256 --output geometry.json
      Only config.json is fetched; model weights are not downloaded.
    Optional — fixed-slot pool planning; documented reference: LMCache NIXL static (Gate 4/5 preparation):
      python3 -m kvprobe pool-plan --sweep --geometry geometry.json --concurrent-contexts N --context-tokens T

  MEASUREMENT ROUTE
    reference adapter: ADAPTER_REQUIRED
    Gate 1: NOT_ESTABLISHED — prove the serving source before timing it.
    Gates 2-5 (listed in STEP 1) become meaningful once Gate 1 holds.

  NEXT CHECKPOINT — answer all three before a hardware run
    [ ] Can you run a matched recompute arm with the cache disabled?
    [ ] Can you align an external tier byte counter to each controlled request interval?
    [ ] Can you detect or disable silent fallback?
  If yes to all three, continue with docs/GATE1.md and use docs/CHECKLIST.md during every
  experiment.
  Before freezing configuration: rerun python3 -m kvprobe geometry with the selected model and the
  live dtype/chunk/TP settings; it gives the expected bytes a measurement is judged against.
  If the current adapter is CANDIDATE: inspect scripts/README.md; capability discovery alone is not
  a hardware PASS.
  If any answer is no: implement an adapter/evidence source or stop; do not report a
  tier-performance claim.
```

The example values are bundled synthetic data, not measurements of the current host. The Gate 3
line shows the current all-attempt decision: every admission attempt counts, including fallbacks
and stores with no observed write. Run `python3 -m kvprobe report examples/evidence/pass-all-attempts.json`
to inspect the complete synthetic report.

### Full host inventory with `--verbose`

The default output prints the three reference-adapter prerequisites: GPU, Docker and `nvidia-fs`.
With `--verbose`, the checks below describe the current implementation in
[`doctor.py`](../kvprobe/doctor.py). `available` is scoped to each check; it is
not a general declaration that the corresponding subsystem works.

| Output | Check and meaning | Still unproven |
|---|---|---|
| `GPU: available (1 devices: NVIDIA GB10)` | `nvidia-smi --query-gpu=name` returned one GPU name. | Model loading, serving health, cache configuration and runtime TP size. |
| `inter-GPU topology: not_applicable` | Fewer than two GPUs were detected on this host, so the local GPU-pair topology query was skipped. | Connections to another host, NIC/RDMA behavior and distributed serving. |
| `runtime collective: not_observed` | Host discovery deliberately makes no claim about the serving process's collective path. | Rank binding, collective backend and selected communication path. |
| `memory architecture: unified (high)` | The GPU name contains `GB10`; the probe classifies its memory architecture as unified with high confidence. `high` describes classification confidence. | Free memory, usable KV-cache capacity, or how a serving engine manages CPU/GPU cache pools. Treat this as a classification, not as independent CPU DRAM and GPU HBM capacity measurements. |
| `gpu_memory_usage_query: available` | The `nvidia-smi memory.used` query succeeded without a `Not Supported` marker. | KV-cache occupancy, reclaimable cache capacity and request-level memory changes. The manifest records query capability, not a memory measurement. |
| `docker: available` | A `docker` executable was found on `PATH`. | Daemon access, image availability, GPU-container support and successful startup. |
| `nvidia_fs: available` | `/proc/driver/nvidia-fs/stats` exists. | Read permission, enabled counters, nonzero request deltas, target-device filtering and actual storage service. |
| `gds: not_tested` | An executable `gdscheck` was found on `PATH` or in a common CUDA toolkit location. The command was not run. | GDS support on the intended filesystem, backend initialization and whether retrieval uses a native path or fallback. |
| `nixl`, `lmcache`, `kvbm`: `not_detected` | The package name was not found by `importlib.util.find_spec` in the Python environment running this command. | Whether the package is installed in another virtual environment or serving container, and whether that server uses it. Even `available` would mean discoverable, not successful initialization. |
| `attribution adapter: available` | The same `nvidia-fs` stats path exists, so the reference counter source is a candidate. | A working adapter run, usable counter granularity and attribution to the target device. |
| `measurement ready: true` | GPU presence, the Docker executable and the `nvidia-fs` stats path were all detected. | All measurement prerequisites beyond these three existence checks. Neither engine-package detection nor GDS validation is part of this boolean. |
| `note: capability discovery ...` | Explicit boundary: discovery does not establish Serving Tier Verification. | Whether any request was served by the intended tier. |

In particular, the current `measurement ready` predicate is only:

```text
GPU detected AND docker executable found AND nvidia-fs stats path exists
```

Read `true` as **reference-adapter candidate**, then apply the three-question
stop rule. It does not mean “the serving stack is ready” or “GDS passed.” The
combination of `measurement ready: true` and `lmcache: not_detected` is therefore
possible: the checks have different scopes. For a container deployment, inspect
the engine environment and configuration inside the serving container separately.

**A loaded `nvidia-fs` does not validate a native path.** In earlier GB10
validation, discovery found the statistics path, but the tested local-storage
configuration did not establish native GDS access. That observation does not
settle support for another filesystem, remote transport or future stack.
`available` names a candidate counter source. Check the exact path with
`gdscheck -p` and a controlled run; missing evidence stays unsupported. See the
[unified-memory lessons](reference/UNIFIED-MEMORY-LESSONS.md) for the distinction
between discovery, transport and verified tier service.

### Evidence map

| Evidence class | What is public | What it can support |
|---|---|---|
| Synthetic | Hand-written evidence and ingested synthetic fixtures, including the all-five `PASS` demo | Reporter, schema and fail-closed behavior only; no host-performance claim |
| Sanitized measured | Selected normalized documents such as the filtered-counter Gate 1/2 run and capacity-envelope runs | The stated result for that run shape and configuration; identifying fields are replaced and unsupported gates remain unsupported |
| Raw evidence retained outside the repository | No raw bundle; the public tree keeps scoped summaries, contracts and revalidation notes | The documented claim boundary only; the repository does not offer independent raw-artifact reproduction |

See [the example inventory](../examples/README.md) for the file-by-file origin,
verdict and sanitization boundary. A verdict does not change class: a synthetic
`PASS` remains synthetic, while a measured `PARTIAL` can still be a valid result
for the gate that run was designed to observe.

### Detailed bundled cases with `--verbose`

What each gate checks is summarized in [the five gates at a glance](../README.md#the-five-gates-at-a-glance).

The four bundled cases are independent examples, not four phases or outputs of
one run. Reports saved by `--output-dir` repeat their data origin and current-host
disclaimer so that a report copied out of the directory cannot be mistaken for a
measurement of the machine that generated it.

The marker at the start of each line is the **demo test result**. `expected` and
`actual` are the **evidence verdicts**. `[PASS] ... expected=FAIL actual=FAIL`
means the reporter correctly rejected that example; it does not mean the host
passed a storage test.

| Example | Expected evidence verdict | Interpretation |
|---|---|---|
| `pass-all-attempts.json` | `PASS`, claims allowed | The synthetic fully passing example exercises the five-gate reporting path, with Gate 3 under the current all-attempt contract. Its values are not measurements of this host. |
| `attribution-unfiltered-counter.json` | `UNPROVEN`, claims suppressed | The bytes match, but the counter covers the whole host rather than the tier's device, so Gate 1 cannot be established; Gates 2–5 were not measured. |
| `envelope-collapse-small-pool.json` | `PARTIAL`, claims suppressed | The concurrency sweep is a valid measurement, so Gate 5 passes; its result is the envelope — every measured batch met its tier-byte and correctness checks through concurrency 4; at 8 none did, so its 80 requests are not credited to the tier. Gates 1–4 were not measured. |
| `phantom-tier-framework-hit.json` | `FAIL`, claims suppressed | Synthetic: the framework reported hits, but the tier's device read nothing on any request — Gate 1 records a measured violation. |

`gates not passed` lists failures **and** unsupported gates. `SKIP_UNSUPPORTED`
means the example lacks supported evidence for that gate, not that a test of
this host was skipped. `[MISMATCH]` would mean the demo's actual verdict differed
from its expected verdict and would make `first-visit` return a nonzero status.
See [evidence and verdict semantics](EVIDENCE.md) for the reporting contract.

### The host result to carry forward

| Output | Meaning |
|---|---|
| `reporter self-check: PASS` | Every bundled case returned its expected verdict. |
| `reference adapter: CANDIDATE` | The three host checks above passed; filesystem and counter alignment still need validation. If they do not pass, the command prints `ADAPTER_REQUIRED`. |
| `detail` | Explains that candidate status leaves target-filesystem and request-alignment work open. It does not attest that vLLM or LMCache is installed or healthy. |
| `hardware measurement: NOT_RUN` | No serving workload or hardware benchmark was run by this command. |
| `Gate 1 ... NOT_ESTABLISHED` | No request-level serving-tier claim is supported by this first visit. |

The supported conclusion for this example is: **the CLI works, a GB10 host and
potential counter source were detected, and the reporter handles its bundled
cases correctly. The actual serving tier and its performance remain unmeasured.**

### Turn discovery into a measurement decision

The first block is the path that pays off on any host, including one without a GPU. `geometry`
works for any stack: give it the model config (or the layer count, global KV-head count, head
dimension and dtype) and it returns the expected bytes per chunk that Gate 1 compares against.

`pool-plan` is optional and narrower. It is Gate 4/5 preparation for LMCache's NIXL static
storage pool (`nixl_pool_size`): give it the number of contexts that must stay retrievable at once
and their length, and it returns the live set in chunks, bytes and tokens and the eviction pressure
of pool sizes around it. For any other fixed-slot pool you must map slots and chunk size yourself
with `--pool-slots` and `--bytes-per-chunk`. That is capacity arithmetic, not a performance
prediction ([pool sizing](NIXL-POOL-SIZING.md)).

The empty checkboxes are questions for the operator, not automatically verified
results. Each one supplies a control needed to interpret later numbers:

| Question | Why it matters | What to establish next |
|---|---|---|
| Matched recompute with cache disabled? | Retrieval latency needs a comparable baseline. | Same model, prompt workload and relevant runtime settings, with caching disabled for the recompute arm. |
| External bytes aligned to each controlled request interval? | A quick response or framework hit can occur without target-tier I/O. | Readable counter, independent expected-byte geometry, matching interval and documented device/granularity scope. |
| Silent fallback detectable or disabled? | A configured backend can serve through a different path. | Backend initialization and fallback settings/logs, together with external path evidence. |

The remaining lines route the operator: read the checklist if all three answers
are yes, use `geometry` to match model and live runtime inputs before configuration
freeze, and inspect the reference scripts if the adapter is a candidate. If an
answer is no, add the missing control/evidence source or stop the tier-performance
claim. A successful first visit never answers these questions for you.

For diagnostic readings after evidence has been collected, use
[`report`](EVIDENCE.md#efficiency-readings), which shows them by default. It consumes an
evidence document; `first-visit` does not attach to a live serving endpoint or
produce efficiency readings for this host.


## 6. What you can do next

After interpreting `first-visit`, choose the question you want to answer. The
probe supports preparation, controlled measurement and assessment of evidence
already collected. You do not need to run every task below, and candidate status
alone does not qualify a host for a measurement script.

### How the next tasks connect to Gates 1–5

The work flows from **preparation → controlled experiments → normalization and
assessment**. A task is not automatically a gate PASS. Geometry and pool sizing
are calculations; a fingerprint identifies supplied settings; ingest organizes
recorded evidence; report applies the gate contract to that evidence.

| Gate | Question | Where the evidence comes from |
|---|---|---|
| 1 — Serving Tier Verification | Did the intended tier serve the request at the claimed scope? | Configuration/counter checks, correctness, external bytes, request linkage and fallback review within every experiment. |
| 2 — Single-hit retrieval break-even | Does guaranteed-hit retrieval beat matched recompute? | Context/retrieval and recompute measurements with Gate 1 validation. The reporter compares recorded TTFT medians; a context-sweep crossing requires the separate sweep analysis. |
| 3 — Admission cost | Do expected reuses repay admission, counting every reuse attempt? | Admission cost and realized saving over every classified store/reuse attempt; Gate 2 establishes the verified-hit benefit. |
| 4 — Capacity justification | Was the target tier needed beyond the upper tier's capacity? | Working-set/overflow observations and evidence of target-tier service. |
| 5 — Operating envelope | Through which tested concurrency does target-tier service hold? | Correctness and serving-tier evidence across the load sweep. |

**Gate 1 is common validation throughout Gates 2–5**, not a one-time experiment
completed before them. Gate 5 repeats serving-tier checks along a load axis.
Each experiment must preserve its evidence scope; a host-wide corroborating
counter must not be reported as target-device filtered.

The task tables below distinguish preparation, evidence collection, display and
assessment. Missing gates remain unsupported; a matched backend comparison,
calculator result or efficiency reading cannot replace them. See the
[five-gate method](METHOD.md) and [execution checklist](CHECKLIST.md) for the
full experimental requirements.

### Prepare or inspect existing evidence

These tasks use supplied files or numbers. They do not start a serving workload
or establish that those inputs match a live deployment.

| I want to… | Related gates and role | Inputs to prepare | Command and result | Detailed instructions |
|---|---|---|---|---|
| Calculate how many KV bytes a model should produce | Gate 1 preparation; expected-byte input for later experiments | Model `config.json`, runtime dtype, chunk tokens, TP/PP settings and intended counter scope | `kvprobe geometry`: derived bytes per token/chunk for the full model, TP rank and TP group. Review against live runtime settings before using the selected value. | [KV geometry](GEOMETRY.md) |
| Check whether a NIXL static pool can hold an intended live set | Gates 4/5 preparation; no measured capacity or envelope verdict | Pool slots (or `--sweep`), exact bytes per chunk (or a `kvprobe geometry` file) and peak simultaneously live chunks (or concurrent contexts and context tokens) | `kvprobe pool-plan`: logical capacity, headroom and a lower bound on forced evictions, with a GiB summary and, for `--sweep`, pool sizes that bracket the live set. It does not predict throughput or select an optimal pool size. | [Static-pool sizing](NIXL-POOL-SIZING.md) |
| Record the exact run setup | Gates 1–5 configuration consistency; no live-setting discovery | A configuration JSON checked against the live model, geometry, runtime and arm settings | `kvprobe fingerprint`: canonical SHA-256 identity. It does not discover the live configuration; the operator verifies the supplied values. | [Configuration workflow](CONFIGURATION.md#five-minute-path) |
| Turn measurement runs into evidence | Gates 1–5 evidence normalization, according to supplied run shapes | Supported run JSON files, a matching configuration JSON, a run ID and expected reuses if measured/known | `kvprobe ingest`: configuration-bound evidence JSON, or a refusal when inputs cannot be reconciled. It does not collect missing measurements. | [Producing evidence from a run](EVIDENCE.md#producing-evidence-from-a-run) |
| Decide which claims an evidence document supports | Gates 1–5 assessment; claim permission requires all five PASS | Evidence JSON version 0.2 or 0.3 | `kvprobe report`: five gate verdicts, claim permission and validated envelope. Unsupported gates remain unsupported. | [Verdict and exit status](EVIDENCE.md#verdict-and-exit-status), [claim profiles](PROFILES.md) |
| Inspect efficiency observations even when claims are suppressed | Gates 1/2/3/5-related observations; display only, no new verdict | The same evidence document, with supported byte/timing/admission/envelope sections | `kvprobe report`: read amplification, TTFT-bounded bandwidth, tier-served ratios and reuse economics with grades. Missing inputs remain unmeasured; displayed observations do not authorize a claim. | [Efficiency readings](EVIDENCE.md#efficiency-readings) |

To inspect arguments without running a measurement:

```bash
python3 -m kvprobe --help
python3 -m kvprobe geometry --help
python3 -m kvprobe pool-plan --help
python3 -m kvprobe fingerprint --help
python3 -m kvprobe ingest --help
python3 -m kvprobe report --help
```

To explore the reporter immediately using a bundled example:

```bash
python3 -m kvprobe report examples/evidence/pass-all-attempts.json
```

That document is synthetic. Its readings describe the example, not your server.
For your own capture, follow the linked ingest instructions to produce evidence,
then run `report` on that evidence file.

### Run a controlled hardware experiment

Start only after the [three-question stop rule](#2-apply-the-three-question-stop-rule)
passes and the [reference-script prerequisites](../scripts/README.md#required-environment)
fit the host and serving stack. The reference runners can create/remove containers
and initialize tier paths; review their setup, privileges and cleanup before use.
They do not attach to an arbitrary existing endpoint. `first-visit` does not
choose an image, mount, backend or safe cleanup path for you.

| I want to… | Related gates and role | What must be available | Run and result | Detailed instructions |
|---|---|---|---|---|
| Establish whether requests actually use the intended tier | Gate 1 within every experiment | Verified geometry, controlled intervals, suitable external counters, correctness and fallback controls | Follow the Gate 1 guide; collect and review scoped evidence. Existing CLI observations do not automatically complete validation. | [Gate 1 guide](GATE1.md), [common checklist](CHECKLIST.md), [script coverage](../scripts/README.md) |
| Find where retrieval beats recompute | Gates 1 + 2 | Matched recompute/retrieval arms and a context sweep, with serving-tier checks | `g2_context_sweep.sh` and its client: context/latency observations and retrieval-versus-recompute analysis. This is a workload experiment; `report` alone does not calculate a token-count crossing. | [Gate 2 checklist](CHECKLIST.md), [script commands](../scripts/README.md) |
| Determine how many realized reuses repay storing an entry | Gates 1 + 3; Gate 2 establishes verified hit benefit | The configured arms, admission cost, every classified reuse outcome and a workload-supplied expected reuse count | `g3_admission_cost.sh`: all-attempt store/reuse observations and an economical, not-economical, or unproven result. | [Gate 3 checklist](CHECKLIST.md), [script commands](../scripts/README.md) |
| Determine when a lower tier becomes necessary | Gates 1 + 4 | Upper-tier capacity, a controlled growing working set and external byte evidence | `g4_capacity_sweep.sh`: retrieval classifications across the resident/device crossover. | [Gate 4 checklist](CHECKLIST.md), [script commands](../scripts/README.md) |
| Find the load range that preserves target-tier service | Gates 1 + 5 | A fixed configuration, request concurrency schedule, correctness and byte checks | `g5_controlled_kv_experiment.sh`: concurrency observations and tier/correctness outcomes. Request concurrency is not a data-parallel deployment measurement. | [Gate 5 checklist](CHECKLIST.md), [script commands](../scripts/README.md) |
| Compare matched native GDS and NIXL paths, or investigate NIXL modes | Gate 1/path validation plus matched latency comparison; not full five-gate closure | Documented software versions, matched schedules, backend identity and verified provenance | The matched/mode-matrix runners collect captures; `compare_matched.py` checks and compares them. These results do not by themselves complete all five report gates. | [Matched static GDS procedure](NIXL-STATIC-GDS-MATCHED.md), [NIXL mode matrix](NIXL-MODE-MATRIX.md) |

After collection, retain the raw run, logs and configuration. Use `ingest` to
normalize supported run shapes, then `report` to assess evidence. Keep
unsupported sections explicit rather than filling them with example values.

### Use another stack, investigate a failure, or revalidate a change

| I want to… | Related gates and role | Work to do and expected result | Detailed instructions |
|---|---|---|---|
| Apply the gates to a stack without the reference runners | Gate 1/2 context inputs; Gates 3–5 require additional evidence | Implement a stack-specific harness that records the supported run shape, independent expected bytes, external deltas and correctness. `ingest` can then normalize those records. Context runs supply Gate 1/2 inputs; Gates 3–5 need their own evidence. | [External runs](EXTERNAL-RUNS.md), [evidence run shapes](EVIDENCE.md#producing-evidence-from-a-run) |
| Explain a plausible but false result | Any gate, especially Gate 1; investigation, not automatic PASS | Match the symptom to failure cases, inspect the required counters/logs/configuration, and collect the missing discriminating evidence. There is no universal automatic failure-diagnosis command. | [Failure catalog](FAILURE-CATALOG.md) |
| Check whether a new run still matches a reference | Gate 1 validity and compatible latency comparison; no replacement for other gates | Capture a compatible workload/configuration and use the baseline comparator. Preserve comparison output; distinguish a cross-check from independent reproduction. | [Revalidation](REVALIDATION.md) |
| Understand whether a component-tier result applies to multi-GPU or distributed serving | Claim boundary for all gates; requires evidence at the intended deployment scope | Compare the intended claim with the supported profile and evidence bindings before designing further measurements. A component-tier report cannot establish an unmeasured end-to-end distributed path. | [Measurement profiles](PROFILES.md), [multi-GPU configuration boundary](CONFIGURATION.md#multi-gpu-worker-groups) |

For a host classified `ADAPTER_REQUIRED`, geometry and existing-evidence tasks
remain available. Hardware work requires a suitable adapter/evidence source.
For `CANDIDATE`, validate the filesystem, engine deployment and counter scope
before selecting a reference runner. Neither classification proves a serving
path or gives a hardware-specific performance recommendation.
