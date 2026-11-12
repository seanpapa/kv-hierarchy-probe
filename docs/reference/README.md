# Implementation reference notes

These notes explain the implementation anatomy behind the measurements in
`kv-hierarchy-probe`. They are **reference material**, not additional gates and not a
substitute for [`METHOD.md`](../METHOD.md).

Use them when a result raises a structural question such as:

- Which memory and transfer assumptions change on a unified-memory host?
- Who owns the cache key, placement, eviction, and byte transfer?
- Why is an LMCache chunk larger than a vLLM KV block?
- Why do buffered POSIX, native cuFile GDS, and LMCache NIXL static storage leave
  different files on disk?

## Reading order

1. [`STORAGE-BACKEND-RESPONSIBILITIES.md`](STORAGE-BACKEND-RESPONSIBILITIES.md) — separate
   the serving engine, cache manager, transfer library, plug-in, and storage device.
2. [`KV-GRANULARITY-AND-IO.md`](KV-GRANULARITY-AND-IO.md) — connect tokens, vLLM blocks,
   LMCache chunks, KVBM blocks, descriptors, and logical I/O.
3. [`STORAGE-LAYOUTS.md`](STORAGE-LAYOUTS.md) — compare file identity, metadata,
   lookup, eviction, and restart behavior for three LMCache disk paths.
4. [`CONFIGURATION-IDENTITY.md`](CONFIGURATION-IDENTITY.md) — review the detailed
   fingerprint contract, measurement bindings, image/device identity rationale,
   and multi-GPU limitations after using the short configuration workflow.

5. [`UNIFIED-MEMORY-LESSONS.md`](UNIFIED-MEMORY-LESSONS.md) — distinguish logical cache
   tiers, shared physical memory, network transport and repeated application validation.

## Evidence labels

Every note uses the same labels:

- **Source-confirmed** — read from the cited public implementation or documentation.
- **Observed** — seen in a controlled run; the observation is configuration-scoped.
- **Inference** — a reasoned interpretation that still needs a dedicated measurement or
  maintainer confirmation.

An unlabeled architectural summary is a condensation of the source-confirmed facts immediately
around it. Performance values are intentionally absent: reference anatomy should not turn a
host-scoped result into a general benchmark claim.

## Version and correction policy

The implementation-anatomy notes were last checked on **2026-09-29**. The
unified-memory lessons were reviewed on **2026-11-12**; each note states its scope.
LMCache, Dynamo KVBM, vLLM, and NIXL change quickly. A class name or lifecycle
described here may move or disappear.

When correcting a note, please include one of the following in the pull request:

- a public source link pinned to a commit;
- a merged upstream pull request and the release that contains it; or
- a minimal reproduction with configuration identity and the counter or trace used to support
  the observation.

Treat behavior as shipped only after the corresponding change is merged into
the reviewed version.
