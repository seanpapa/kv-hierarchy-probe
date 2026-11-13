# Roadmap and known limitations

> Reviewed: 2026-11-13. This page states what the probe does not verify yet and what is planned.
> It is not a schedule. Items move or close through pull requests that update this page.

**Verified today:** local NVMe only, on the paths listed in
[Scope — what is verified today](../README.md#scope--what-is-verified-today). The sections below separate
known limitations in those paths from work beyond the verified scope. Open questions and planned work
do not extend that scope.

A valid measurement that shows a tier is not worth it is a useful result. Reports from other hardware
and stacks are welcome as GitHub issues, including results that do not pass; please leave out host
names and internal paths.

## Next

**Gate 1 output: missing evidence, why it matters, what to collect next.** The report already states
why a Gate 1 result is `UNPROVEN` or `FAIL`, but the reason, its rationale and the next step are spread
across the report output, [GATE1](GATE1.md) and [EVIDENCE](EVIDENCE.md). The change keeps every
verdict, claim allowance and exit code as they are.

## Known limitations in the verified path

### GDS backend: staging-pool space appears to be held after a completed load

[FAILURE-CATALOG §13](FAILURE-CATALOG.md#staging-buffer-full) documents a full cuFile staging pool
turning stores and retrievals into quiet no-ops on LMCache's GDS backend. In the runs behind that
entry, failures followed a load that succeeded: a completed load appears to keep its pool space for a
while, so the next transfer is the one that fails. A larger pool moved the limit without removing it.

- **Not known:** why the space is held after a load, and what releases it.
- **Closes when:** a debug-level run shows the allocation failures next to the transfer that preceded
  them, and §13 states the mechanism. The Gate 3 and Gate 4 pass conditions are then reviewed against it.

### Gates 3 and 4: the kinds of `fallback` are reported together

Both gates classify an outcome that did not come from the target tier as `fallback`. Every attempt stays
in the calculation, but cases that need different diagnoses share one count.

- **Gate 4:** a `fallback` beyond capacity can follow a store with no observed write, or one with
  write activity observed but retrieval not established. The producer keeps these observations in
  per-request `stores` and `retrievals` records, but the report reads only the counts
  ([METHOD, Gate 4](METHOD.md#gate-4--capacity-crossover)). The write counters are host-wide:
  observed write activity alone does not prove that the entry reached the target device.
- **Gate 3:** a reuse attempt classified as `fallback` can be a recompute or a hit served by an upper
  tier. Neither receives positive saving credit, so the break-even result does not change
  ([METHOD, Gate 3](METHOD.md#gate-3--admission-cost)).
- **Closes when:** the Gate 4 report distinguishes "no observed store write" from "store write
  observed, retrieval not established", retaining the counter's scope and any uncertainty, with tests,
  an assessment version bump and no change to existing verdicts; and the Gate 3 question is either
  implemented the same way or written down as a limit.

### Gate 4: the one-entry staging rule conflicts with backends that need headroom

Gate 4 needs a staging buffer no larger than one entry; otherwise a hit served from the GPU-resident
buffer reads zero device bytes and is misclassified as an upper-tier hit. The GDS backend in
[FAILURE-CATALOG §13](FAILURE-CATALOG.md#staging-buffer-full) needs headroom above one entry to move
entries reliably. Until this is resolved, record both the pool size and the entry size and read the
Gate 4 counts, not only the crossover.

- **Open question:** a way to classify GPU-buffer hits that does not depend on a one-entry pool.
- **Closes when:** a method is proposed, tested on synthetic cases, and either adopted with a hardware
  check or rejected with the reason written down. The staging-pool item above may change the answer.

## Beyond the verified scope

### Other stacks, other machines, multi-GPU hosts

The gates have passed on hardware only for the configurations in
[Scope](../README.md#scope--what-is-verified-today). [EXTERNAL-RUNS](EXTERNAL-RUNS.md) describes how
to produce evidence on a stack without reference scripts. Open: another serving stack (for example
Dynamo KVBM), other NIXL modes, other GPUs or SSDs — recording what breaks, not only what passes.

A host with several GPUs is not the same as a request using several GPUs:

1. **TP=1 on a multi-GPU host:** the measured GPU must match the selected one inside the container,
   and work on another GPU must stay out of the evidence.
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
- **Closes when:** an adapter turns such evidence into the normalized format, with synthetic tests for
  complete, missing and partial transfers, and one controlled hardware run shows the attachment's limits.
  Keep the result scoped to the tier and path actually measured.

## Keeping this page current

A pull request that changes the status of an item updates this page in the same change: it moves the
item, states what was established, or removes it with the reason. Work in progress may get its own
GitHub issue; this page links to it while it is open.
