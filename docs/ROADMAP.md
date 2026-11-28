# Roadmap and known limitations

> Reviewed: 2026-11-28. This page states what the probe does not verify yet and what is planned.
> It is not a schedule. Items move or close through pull requests that update this page.

**Verified today:** local NVMe only, on the paths listed in
[Scope — what is verified today](../README.md#scope--what-is-verified-today). The sections below separate
known limitations in those paths from work beyond the verified scope. Open questions and planned work
do not extend that scope.

A valid measurement that shows a tier is not worth it is a useful result. Reports from other hardware
and stacks are welcome as GitHub issues, including results that do not pass; please leave out host
names and internal paths.

## Next

**NFS/RDMA Gate 1 evidence (in development).** Experimental raw capture/replay, process/path
binding, reference-originals ingest and Gate 1-only report assessment are present on this branch.
Qualified retained serving originals can establish Gate 1 for their measured requests. Live producer
setup guidance and integrated client-cache-negative coverage remain follow-up work. The established
operational reference remains local NVMe. See [experimental APIs and remaining work](NFS-RDMA-EXPERIMENTAL.md).

- **Closes when:** qualified serving originals produce the expected Gate 1 verdicts through the
  normalized adapter and report, client-cache hits cannot masquerade as remote service, and local
  NVMe regression results remain intact. Gate 1-only evidence keeps the overall report `PARTIAL`.
  A new positive run is required for missing evidence or changed collection/measurement conditions,
  rather than solely because an offline normalization CLI was added.

### Follow-up: TP=1 on a multi-GPU host

**Check TP=1 on a multi-GPU host.** The reference runs place the measured TP=1 endpoint on one
selected GPU of a host with several. Run that endpoint on one GPU and then on another, and confirm that the GPU the configuration names is the one
the process and container actually use, and that controlled work on the other GPU stays out of the
request evidence.

- **Closes when:** recorded runs on two GPU choices show that the configured GPU, the device in use
  and the configuration record agree, and that changing the GPU changes the record; the effect of
  controlled work on another GPU on attribution and timing is recorded; the gates keep valid
  measurements, observed violations and missing evidence apart as documented; and any code fix comes
  with a regression test. A TP>1 worker group stays out of scope.

## Recently completed on main

The rows merged in #28–#31 ship in `0.2.1`, the rows before them in `0.2.0`; no completed change is
unreleased. See [CHANGELOG — 0.2.1](../CHANGELOG.md#021--2026-11-26) and
[CHANGELOG — 0.2.0](../CHANGELOG.md#020--2026-11-19) for details.

| Change | What improved | Merged in |
|---|---|---|
| Gate 1 next steps | A `FAIL` or `UNPROVEN` report names each finding, why it matters and what to check next ([GATE1](GATE1.md#next-steps-in-the-report)) | #18 |
| Gate 4 fallback kinds | Complete per-request records let the report split fallbacks beyond capacity by earlier observed store writes of the same entry and suggest where to look first ([METHOD](METHOD.md#gate-4--capacity-crossover)) | #19 |
| Evidence-version field checks | The reporter refuses version-specific fields in documents whose declared version does not define them, covering Gate 4's split and Gate 5's input size ([EVIDENCE](EVIDENCE.md#versioned-contracts)) | #19, #20 |
| GDS staging-pool cause investigation | Source review and a controlled CPU-cache-on/off comparison support the explanation for pool space held after a completed load ([FAILURE-CATALOG §13](FAILURE-CATALOG.md#staging-buffer-full)). This closed the cause-finding item only; the pass-condition review is the next row | #22 |
| Gate 3 and Gate 4 pass-condition review | Retaining every attempt and the classification counts is enough to prevent a false `PASS`; evidence without them was not covered. A Gate 3 block of successful samples only and a Gate 4 overflow with target-tier service but no counts are now `UNPROVEN`; overflow with no service remains `FAIL`, and evidence with every attempt and the counts is judged as before ([METHOD](METHOD.md#gate-3--admission-cost)) | #23 |
| Staging-pool cause in the Gate 4 text | Fallbacks beyond capacity now carry a note that a held staging pool can cause them, so the counts alone do not show a device fault, unless every fallback points to the store path; METHOD states that the same cause can raise Gate 3's reuses needed. No verdict changed ([METHOD](METHOD.md#gate-4--capacity-crossover)) | #24 |
| `geometry` layout refusal | Explicit `layer_types` must declare full attention for every layer; mixed, uniform non-full-attention and malformed declarations and known hybrid markers are refused instead of answered with the standard formula ([GEOMETRY](GEOMETRY.md#fail-closed-boundaries)) | #25 |
| `tier-check` Docker access | T13 checks that this user reaches a Docker daemon on this host with the NVIDIA runtime registered; a host without Docker no longer reads ready ([TIER-CHECK](TIER-CHECK.md#4-the-checks)) | #26 |
| Gate 5 without GDS | The controlled launcher's `concurrency-posix` sweep runs the POSIX arm alone, so a host without `nvidia-fs` can produce Gate 5; `preflight posix` and `preflight recompute` skip `gdscheck` ([runbook step 5](../scripts/README.md#minimal-runbook--all-five-gates-one-configuration)) | #28 |
| Preparing a host | The runbook says what to install and check for each arm, which gates a host with one GPU and no GDS produces and how to size the fillers; its image check now loads torch first ([scripts/README](../scripts/README.md#preparing-a-host)) | #29 |
| Gate 1 walkthrough | [GATE1](GATE1.md) goes from three synthetic verdicts through preparation, collection, `ingest` and `report`, and explains why a Gate 1/2-only run is `PARTIAL` | #30 |
| Licence metadata | The package declares `Apache-2.0` as an SPDX expression, which clears setuptools' deprecation warning; the shipped LICENSE and NOTICE are unchanged | #31 |

The Gate 4 write counter is host-wide; an observed write does not place the entry on the target
device. A Gate 3 `fallback` remains unsplit, as a documented limit
([METHOD, Gate 3](METHOD.md#gate-3--admission-cost)); its break-even result is unchanged.

## Known limitations in the verified path

### Gate 4: the one-entry staging rule conflicts with backends that need headroom

Gate 4 needs a staging buffer no larger than one entry; otherwise a hit served from the GPU-resident
buffer reads zero device bytes and is misclassified as an upper-tier hit. The GDS backend in
[FAILURE-CATALOG §13](FAILURE-CATALOG.md#staging-buffer-full) needs headroom above one entry to move
entries reliably, consistent with the source-supported retention mechanism documented there. Until
this is resolved, record both the pool size and the entry size and read the Gate 4 counts, not only
the crossover.

- **Open question:** a way to classify GPU-buffer hits that does not depend on a one-entry pool.
- **Closes when:** a method is proposed, tested on synthetic cases, and either adopted with a hardware
  check or rejected with the reason written down.

## Beyond the verified scope

### Other stacks, other machines, multi-GPU hosts

The gates have passed on hardware only for the configurations in
[Scope](../README.md#scope--what-is-verified-today). [EXTERNAL-RUNS](EXTERNAL-RUNS.md) describes how
to produce evidence on a stack without reference scripts. Open: another serving stack (for example
Dynamo KVBM), other NIXL modes, other GPUs or SSDs — recording what breaks, not only what passes.

A host with several GPUs is not the same as a request using several GPUs:

1. **TP=1 on a multi-GPU host** (the [Next](#next) item): the measured GPU must match the selected
   one inside the container, and work on another GPU must stay out of the evidence.
2. **TP>1:** the evidence does not encode a worker group's placement or collectives
   ([multi-GPU worker groups](CONFIGURATION.md#multi-gpu-worker-groups)). A TP=1 result does not cover it.

### An evidence source for a CPU-memory (DRAM) tier

DRAM is the upper tier that Gates 3 and 4 compare against, but its service **cannot be established by
Gate 1 in the current reference implementation**: the probe has no supported per-request external
evidence source for that memory tier, so a DRAM hit is only inferred. Gate 1 needs evidence from outside
the serving framework that the intended memory allocation served a given request in a controlled interval. A
framework hit count, memory-capacity usage or host-wide memory bandwidth alone is not enough. On
unified-memory hosts, a logical CPU cache is not necessarily separate physical memory
([unified-memory lessons](reference/UNIFIED-MEMORY-LESSONS.md)).

- **Direction:** observation methods that could attribute a memory-tier hit to a request — for example
  tracing the copy from the CPU cache allocation to the GPU — with their limits.
- **Closes when:** a method separates a memory-tier hit from recompute and from a GPU-resident hit on a
  controlled run and is expressed as an ingest provider with tests. Until then DRAM stays inferred.

### REMOTE block attachment: NVMe-oF hardware check

An NVMe-oF namespace mounted on the GPU server is a local block device, so in principle the local-NVMe
counters apply. It is **not hardware-verified**; every Gate 1 pass so far is single-path local NVMe.

- Native GDS over the fabric depends on platform, filesystem and transport; the POSIX arm does not need it.
- With fabric multipath, it is unverified on which device number cgroup `io.stat` records the bytes.
- Initiator bytes show transfer from the target, not whether the target served them from cache or media.
- **Plan:** run the five gates on an NVMe-oF namespace over RDMA and over TCP, single path and multipath,
  with the device mapping recorded.
- **Closes when:** those runs are documented and the scope table states what they established and what
  they did not.

### REMOTE file/object attachment: an evidence adapter

NFS over RDMA, object storage and network KV stores do not expose the target storage as a local block
device on the GPU server. The current block-device counter cannot establish their KV payload transfer,
so it cannot by itself establish Gate 1. A remote attachment can still be evaluated as a component tier
at one serving endpoint if suitable evidence is provided. Shared-cache or prefill/decode handoff
validation requires additional correlation and path evidence
([PROFILES, V2](PROFILES.md#v2-disaggregated-path-qualification)).

- **Needed:** an evidence source that counts per-request bytes at the client, the connection or the
  store, with an identity that ties them to the request. NIC totals include other traffic and protocol
  overhead; logical KV bytes, encoded payload and wire bytes can differ and need their own expected values.
- **In development:** the NFS/RDMA [raw APIs and draft scope](NFS-RDMA-EXPERIMENTAL.md) address
  mount-scoped observations with explicit reference ingest and G1-only assessment. Qualified
  retained requests and broader live workflow qualification are separate scopes; object storage
  and network KV stores still need their own evidence providers.
- **Closes when:** an adapter turns such evidence into the normalized format, with synthetic tests for
  complete, missing and partial transfers, and one controlled hardware run shows the attachment's limits.
  Keep the result scoped to the tier and path actually measured.

## Keeping this page current

A pull request that changes the status of an item updates this page in the same change: it moves the
item, states what was established, or removes it with the reason. Work in progress may get its own
GitHub issue; this page links to it while it is open.
