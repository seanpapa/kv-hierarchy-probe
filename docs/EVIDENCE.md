# Normalized evidence and automatic reports

`kvprobe report` is the claim boundary between measurement and publication. It consumes a small,
stack-independent JSON document, applies the five gates in order, and permits performance claims
only when every gate passes. It also shows efficiency readings with attribution grades; a failed
gate withholds the performance family and leaves the integrity readings, which is where the
failure usually shows. See [efficiency readings](#efficiency-readings).

The current contract is `schema_version: "0.3.1"`; `0.3.0` documents also remain supported
without changing their verdicts. All are intentionally smaller than raw serving-stack
output. An adapter should keep raw evidence in access-controlled storage, then derive a sanitized
normalized document without deleting failed requests. Raw bundles are not kept in this repository.
Portable regression cases that reproduce a gate decision without deployment data live in
[`../tests/fixtures/portability/`](../tests/fixtures/portability/); they use generic host classes and
synthetic identifiers.

## Claim scope of the current contract

Evidence v0.3 can describe a tier measurement within one serving endpoint and cache domain. It
does not contain a `measurement_profile` field, bind a prefiller request to a decoder request,
identify source and destination cache domains, or record P→D/object transfer bytes and complete-path
phases. Its effective *component-tier* claim scope is therefore a documentation-level upper bound
inferred from what the contract can represent, not a value declared by the evidence. V0.3 also
cannot distinguish a colocated execution topology from a component observed inside a disaggregated
service.

The current reporter validates required evidence semantics, configuration bindings, and five gates.
It does not run the full JSON Schema, emit a profile-specific claim scope, or enforce one. In
particular, an added top-level `measurement_profile` is ignored by the current reporter and does not
change its verdict. Human review must therefore prevent a v0.3 PASS from being presented as proof
of an end-to-end disaggregated path.

Product release versions, evidence-schema versions, and measurement profiles are independent. A
future schema for the `disaggregated_path` profile must make `measurement_profile` explicit, encode
execution topology and cache scope as separate fields, and require the corresponding correlation,
transfer, placement, and phase bindings. This belongs in
a new schema version: the v0.3 JSON Schema has `additionalProperties: false` and must remain
stable. That schema rule and the current reporter's partial manual validation are distinct; the
future implementation must test both. See [Measurement profiles and version direction](PROFILES.md).

## Versioned contracts

| Version | Status | Contract |
|---|---|---|
| `0.3.0` | Supported | Configuration fingerprints and bindings for every measured section. `runtime.device_id` is required; `gpu_index` is optional. |
| `0.3.1` | **Current**; written by `kvprobe ingest` | Adds optional `envelope.request_cacheable_tokens`, the per-request cacheable input size shared by the measured concurrency sweep. Inconsistent recorded input sizes are refused. |

Unsupported versions are refused with a message naming `kvprobe ingest`; no configuration or provenance
is guessed to upgrade an old document. Normalize retained measurement outputs using the appropriate
adapter and explicitly reviewed configuration. See [VERSIONING](VERSIONING.md) for all independent contracts.

## Producing evidence from a run

`kvprobe ingest` normalizes the four run shapes this repository produces into `0.3.1`, and takes
as many of them as you have — one document can carry every gate that was measured:

```bash
kvprobe ingest context-tier.json context-recompute.json concurrency.json \
  admission.json capacity.json \
  --configuration CONFIG.json --run-id my-run --expected-reuses 2 --output evidence.json
kvprobe report evidence.json
```

| Run | Gate it supplies |
|---|---|
| `context` (tier arm) | 1 — per-request attribution |
| `context` (tier **and** recompute arms) | 2 — retrieval against recompute, paired by context |
| `admission` (`g3_admission_cost`) | 3 — measured store cost, with `--expected-reuses` from you |
| `capacity` (`g4_capacity_sweep`) | 4 — whether the declared upper-tier capacity was exceeded and the target tier served retrievals beyond it |
| `concurrency` | 5 — served requests per level |

The run supplies what it recorded — per-request correctness, phase, and bytes. The configuration
is supplied by the caller, because a run does not know its own model revision or eviction policy,
and the adapter refuses rather than inventing one. It also refuses when the configuration
contradicts the run about something both describe, and when a request moved no bytes and failed
the tier gate, since a run of that shape cannot separate an upper-tier hit from a recompute.

**Gate 4 keeps the classification accounting.** Adapter `0.3.1` reads the producer's flat
`dram_hits`, `nvme_hits`, `fallback`, and `miss` fields, refuses any row whose buckets do not add up
to its working set, and retains totals for the whole sweep and for rows beyond the declared
`upper_tier_capacity_bytes`. Overflow comes from that declared capacity boundary, not from the
first successful NVMe hit; this preserves the failure case where the working set exceeded the
upper tier but the target tier served nothing. Evidence written by an older adapter remains
readable, but it has no retained classification totals. The report's Gate 4 result states those beyond-capacity counts: how many
retrievals the target tier served and how many fell back or missed.

**Gate 3 keeps every attempted outcome.** Adapter `0.4.0` reads the producer's per-attempt records
and emits `calculation_basis: all_attempts`, signed `realized_reuse_saving_s`, and
`attempt_counts`. Target-tier hits, fallbacks, and store-unattributed attempts all remain in the
mean realized saving. Only verified target-tier hits receive positive credit; a fast upper-tier
outcome contributes zero, while negative saving remains valid. The count buckets must cover every attempt.
`unattributed > 0` makes Gate 3 `UNPROVEN` rather than silently shrinking the sample, and ingest
refuses a row with fewer attempts than the run requested. Older adapter output with `sample_counts`
remains readable through the legacy repayment path; its result is labelled an `optimistic
estimate`, because the attempts the producer dropped are missing from it.

**Gate 5 counts conservatively.** A batch that failed its tier gate, or holds an incorrect response,
cannot be split into served and unserved requests, so none of it counts as served and the envelope
ends at the last level proven clean — the same rule the method states in prose. The result therefore
says those requests are *not credited* to the tier, not that they left it.

**Counter selection is arm-bound.** The recorded arm must appear in `configuration.arms`.
Its `attribution_provider` selects `nvfs_read_mib` for `nvidia_fs`, or `cgroup_read_bytes`
for `cgroup_blk`. A missing, negative, malformed, or non-finite selected counter is refused;
another counter is never substituted, even if it has more bytes.

**Filtering must be evidenced, not inferred.** Every counter delta normalizes as `corroborating`:
it is a counter, not request-ID hardware tracing. `target_device_filtered` is set from what the run
recorded, never from a row-level boolean:

- `cgroup_blk` is filtered when the run records `cgroup_io_device` and a `cgroup_io_device_scope` of
  `whole_disk` or `widened_to_parent_disk`. The reference client's per-device reader records both for
  every cache arm; this counter also counts GPU-direct (cuFile) reads, so GDS and NIXL arms can declare it.
  `widened_to_parent_disk` means the tier sits on a partition and the counter covers that whole disk.
- Runs from the earlier reader, which summed every device the container touched, record neither and
  stay unfiltered.
- `nvidia_fs` reads host-wide `/proc/driver/nvidia-fs/stats` and is never filtered.

**Device mapping is a producer/operator responsibility.** `ingest` trusts the recorded
device and scope; it does not independently resolve the live cache mount, validate the
`MAJOR:MINOR` string, or prove that the counter reader used that device. Retain the host/container
mount mapping, filesystem-to-device resolution and counter-reader provenance. Check for
competing I/O within the same container/device interval; a widened parent-disk scope also
covers other partitions. Matching bytes support a corroborated claim, not request-ID tracing
or automatic proof of isolation.

Missing gates remain `unsupported`.

**Configuration checking is bounded by what was recorded.** Envelope and argument arm names
must agree. Context/sample/warm-up settings are compared where present; concurrency `batches`
is compared with configuration `samples`. Explicit `model_root`, `model_revision`, `served_name`,
`image_digest`, `chunk_tokens`, `bytes_per_chunk`, `dtype`, `upper_tier_capacity_bytes`, and
arm backend/path/provider/staging/eviction fields are compared in the envelope or arguments
when recorded. Legacy admission/capacity runs may omit an arm; any recorded arm must match.
Unrecorded values remain operator-supplied assertions. A valid fingerprint proves canonical
configuration consistency, not independently captured model/image identity.

## Required evidence

| Section | Required input | Gate derived from it |
|---|---|---|
| `attribution.requests` | Correctness, attributed source, external-evidence flag, bytes observed | 1 — Serving Tier Verification |
| `break_even` | Non-empty retrieval and recompute TTFT samples | 2 — Single-hit retrieval break-even |
| `admission` | Store cost, expected reuse count, and (for adapter `0.4.0`) realized saving plus complete attempt-outcome counts | 3 — Admission cost |
| `capacity` | Whether upper-tier overflow and target-tier service were observed | 4 — Capacity justification |
| `envelope.levels` | Requests and target-tier-served requests at each concurrency | 5 — Operating envelope |
| `envelope.request_cacheable_tokens` | Optional (`0.3.1`): cacheable tokens per request, one value for the whole sweep | 5 — named in the result |

See [`../examples/evidence/pass-capabilities.json`](../examples/evidence/pass-capabilities.json) for
synthetic, configuration-bound capability evidence and
[`../examples/evidence/pass-all-attempts.json`](../examples/evidence/pass-all-attempts.json) for the current
configuration-bound shape with an all-attempt Gate 3 block
([`pass-v0.3.json`](../examples/evidence/pass-v0.3.json) keeps the legacy successful-sample block). [`../examples/README.md`](../examples/README.md) records which bundled
examples were produced by `kvprobe ingest` from measurement runs and which are hand-written.

## Configuration identity in v0.3

> **2026-09-16 — `runtime` names the device the run measured: `device_id` is required, `gpu_index`
> is optional.** This narrows `0.3.0` rather than creating `0.4.0`: the public package baseline is `0.1.0`,
> unpublished, and no `0.3.0` document exists outside this repository, so there is no record whose
> meaning changes underneath it. Bundled examples were regenerated and their fingerprints
> recomputed. A document written before this is refused with `runtime keys mismatch`, naming the
> fields — add the device the run measured and recompute the fingerprint. Runs produced by the
> gate 3, 4 and 5 scripts record the device they used, and ingest refuses a configuration that
> contradicts it. The reason is in [Configuration](CONFIGURATION.md): storage counters do not
> identify which accelerator issued the I/O, even when cgroup accounting is filtered to one block
> device, so without the device in the identity, two accelerators on one host share a fingerprint.
>
> `device_id` is an **opaque stable identifier**, not a vendor address format. A PCI address is the
> natural value where the platform prints one, but any identifier that stays stable across the
> campaign and differs between devices is valid, so a platform without PCI addressing is still
> inside the contract.

Version 0.3 adds a configuration registry and explicit bindings from every measured request,
aggregate section, and envelope level to a registry entry. The report recomputes each entry's
SHA-256 fingerprint from canonical JSON. A stale fingerprint is malformed evidence; evidence bound
to more than one verified configuration is a valid record with an `INVALID` verdict and suppressed
performance numbers.

The fingerprint covers model identity, exact KV bytes per chunk, upper-tier capacity, the serving
software and device identities,
measurement schedule, and every arm's backend, tier path, attribution provider, staging capacity,
and eviction policy. See [Configuration identity](CONFIGURATION.md) for the complete contract and
the `kvprobe fingerprint` command.

## Serving tier verification

Every supported attribution request declares its expected and observed byte volume and describes
the evidence source:

```json
{
  "id": "request-1",
  "correct": true,
  "source": "target_tier",
  "expected_bytes": 268435456,
  "observed_bytes": 268435456,
  "attribution": {
    "provider": "native_device_counter",
    "granularity": "per_request",
    "confidence": "direct",
    "target_device_filtered": true,
    "artifact_refs": ["counter-snapshot-1"]
  }
}
```

Gate 1 has three outcomes:

| Outcome | When |
|---|---|
| `PASS` | Every request is correct and observed in its own interval (`per_request`) at `direct` or `corroborating` confidence; at least one request is served by the target tier; and every target-tier request has **target-device-filtered** bytes within the declared tolerance (10% by default). The reason names the grade: `direct` if every proven request is direct, otherwise `corroborated`. |
| `FAIL` | A measured violation: a request with wrong output; a target-tier request that moved no bytes; a per-request interval with fewer bytes than expected on any counter (an unfiltered counter can only add other devices' bytes); or more than expected on a filtered counter. Also when no request was served by the target tier. |
| `UNPROVEN` | No violation, but the evidence cannot establish the criterion: the counter is not filtered to the tier's device, the observation is `per_batch` or `per_run`, or the confidence is `heuristic`. |

The Gate 1 reason also counts target-tier requests whose observed bytes match the expectation and
those that moved no bytes from the tier, apart from the verdict. They separate a corroborated but
unfiltered run (`UNPROVEN`, bytes match) from a phantom tier (`FAIL`, no bytes). See
[EXTERNAL-RUNS.md](EXTERNAL-RUNS.md#what-a-phantom-tier-looks-like).

`attribution.warmup_observations` (optional) counts warm-up requests and those that moved no bytes
from the tier. Warm-ups never change the verdict; when any moved no bytes, the Gate 1 reason carries
a note, so a tier that missed its first reuse is not hidden by the warm-up exclusion.

When the platform cannot provide per-request attribution at all, state that explicitly instead of
inventing a request-level boolean:

```json
{
  "availability": {
    "status": "unsupported",
    "reason_code": "per_request_target_device_counter_unavailable",
    "detail": "Only a run-level aggregate block-I/O counter is available."
  },
  "corroborating_observations": [
    {
      "provider": "cgroup_block",
      "granularity": "per_run",
      "observed_bytes": 1593835520,
      "artifact_refs": ["aggregate-counter-1"]
    }
  ]
}
```

This produces `SKIP_UNSUPPORTED`, not `FAIL`. See
[`../examples/evidence/partial-unsupported.json`](../examples/evidence/partial-unsupported.json).

## Fatal observations

An allocation failure, model mismatch, counter reset, incomplete run, or malformed timing is
evidence that the run is invalid. Record it under `validity.fatal_observations` and link it to a
declared artifact. A fatal run may omit the five evidence sections because their absence is a
consequence of the captured failure.

```json
{
  "validity": {
    "fatal_observations": [
      {
        "code": "allocation_failure",
        "scope": "measurement",
        "artifact_refs": ["server-log-1"]
      }
    ]
  }
}
```

The report returns `overall: "INVALID"` while still treating the JSON as a well-formed evidence
record. See
[`../examples/evidence/invalid-allocation.json`](../examples/evidence/invalid-allocation.json).

## Sanitized provenance

`provenance.normalized_by` identifies the adapter, while `provenance.artifacts` binds evidence to
raw material by an opaque ID, kind, and SHA-256 digest. Do not put hostnames, usernames, absolute
host paths, container IDs, or complete raw logs in the normalized document. Cross-reference checks
ensure every `artifact_refs` entry names a declared artifact.

The supported machine-readable contracts are
[`../schemas/evidence-v0.3.0.schema.json`](../schemas/evidence-v0.3.0.schema.json) and
[`../schemas/evidence-v0.3.1.schema.json`](../schemas/evidence-v0.3.1.schema.json). The reporter also
performs semantic checks that JSON Schema cannot express, including artifact cross-references,
target-tier byte tolerance, configuration fingerprint verification, and binding coverage.

## Verdict and exit status

```bash
python3 -m kvprobe report examples/evidence/pass-all-attempts.json --format markdown
```

The command returns for every supported schema version:

- `0`: valid evidence and all five gates pass;
- `1`: valid evidence with an overall `FAIL`, `UNPROVEN`, `PARTIAL`, or `INVALID` verdict;
- `2`: malformed, incomplete, or unreadable evidence.

A failing report has `performance_claims: "suppressed"` and contains no `performance` object.
Gate reasons and the last fully attributed concurrency remain visible because a failed run is
evidence too. JSON output is suitable for CI; Markdown output is suitable for an evidence bundle.

## What normalization does not prove

The normalized document is not a new source of attribution. The adapter that creates it must tie
each request to evidence outside the serving framework — for example native device, block-layer, or
transport counters plus bytes actually moved. Naming a provider without preserving the referenced
raw evidence violates the method even if the report passes.


## Efficiency readings

```bash
kvprobe report examples/evidence/pass-all-attempts.json
kvprobe report examples/evidence/pass-all-attempts.json --format markdown
kvprobe report examples/evidence/pass-all-attempts.json --format json
kvprobe report examples/evidence/pass-all-attempts.json --no-readings
```

`report` shows readings by default: numbers with an attribution grade. The
readings state what was observed and never judge it; no reading calls a result
good, bad, fast or slow, and a test enforces that. They do not authorize a
performance claim: all five gates still determine claim permission, and exit
statuses are unchanged. `--no-readings` restores the claim-only report. JSON adds
a separate `readings` object; the assessment API and `demo` are unchanged.
`--readings` is accepted as the default.

**Any non-PASS verdict withholds every performance reading.** The core rule reaches the
readings, not only the `performance` object: when the verdict is not `PASS`, the
report heads the block with that verdict and returns no bandwidth, saving,
speedup, repay or margin — in the rendered output and in the `readings` object a
downstream reader parses. Printing a speedup beside a caveat is not a control,
because a number quoted out of a report leaves its caveat behind.

What remains is the **integrity** family: read amplification and tier-served
ratio. These describe whether the measurement holds together rather than how the
tier performed, and they are usually what explains the failure.

A withheld reading is reported as `withheld`, distinct from `not_measured`, and
keeps its sample count and a `withheld_reason`. The quantity was measured; the
record says so and declines to publish the value, rather than claiming the run
never produced one.

- Read amplification: observed / expected bytes per request, shown as min,
  median and max. Below one means the expected byte volume was not observed;
  above one may reflect over-read or unrelated traffic.
- Effective read bandwidth: common expected request bytes / median retrieval
  TTFT / 2^30, in GiB/s. This is a **TTFT-bounded lower bound**, not measured
  device bandwidth. It is omitted for mixed byte sizes, zero TTFT, incorrect
  requests, or evidence of incomplete target-tier service. Byte and TTFT
  samples are not individually paired by the schema.
- Tier-served ratio: target-tier served / requests at each concurrency, using
  the harness's recorded count. Zero-request levels are unmeasured. Comparing
  this with a framework hit ratio requires matching populations and intervals;
  their difference alone does not prove phantom hits.
- Saving: median recompute TTFT minus median retrieval TTFT. Speedup is the
  ratio of these medians. Repay reuses = store cost / positive saving;
  nonpositive saving yields `never`. Margin = expected reuses / repay reuses;
  zero repay leaves margin unmeasured rather than dividing by zero.

Each metric includes a sample count (`n`), status, unit and grade. Timing-based
counts refer to retrieval TTFT samples; amplification counts valid positive-byte
requests; tier ratio counts envelope levels. Unsupported inputs produce
`not measured by this run`; a gate failure produces `withheld` for the
performance family. Values are rounded only for display.

`ATTRIBUTED` requires per-request, direct, target-device-filtered evidence.
Positive byte observations without all three qualify as `CORROBORATED`;
otherwise evidence is `UNVERIFIED`. The weakest request grade labels the run.
Corroborating counters may include other traffic. `OFF-TIER` warns when a source
is not the target tier or observed bytes are below expected bytes; it is a
separate display state. Timing remains a client observation; the grade describes
its tier attribution. Fatal or mixed-configuration evidence produces no readings
and cannot combine configurations.
