# Experimental NFS/RDMA evidence APIs

This development branch adds raw capture/replay, an explicit reference-originals ingest adapter,
and NFS-service Gate 1 assessment. The path is experimental and limited to a trusted, controlled
tiny BF16 TP=1 reference producer and one serialized chunk retrieval. Generalized NFS support and
live positive/negative integration qualification are not declared complete. Existing CLI defaults and
the operational local NVMe workflow are unchanged.

## Why a separate scope is needed

A local block-device filter and an NFS mount counter answer different questions. NFS evidence
must bind a request to the intended service/export/client filesystem and distinguish server-read
payload from RPC receive bytes and client-cache hits. A response from the server's page cache
can establish remote NFS service; it does not establish a server SSD read or GPU-direct transport.
Mount-scoped intervals are not request-ID hardware traces.

## Implemented APIs

| Module | What it does | Boundary |
|---|---|---|
| `kvprobe.nfs_observation` | Parse Linux mountstats/mountinfo and derive counter deltas | Missing fields and resets are errors, not zero-byte observations |
| `kvprobe.nfs_bundle` | Capture snapshots/file operations and replay retained artifact bundles | File I/O remains observation-only, not serving proof |
| `kvprobe.nfs_serving` | Bracket a caller-supplied synchronous callback with request/quiet snapshots; replay response, geometry and counter facts | The caller supplies the client; the callback and matching bytes do not qualify its provenance or isolation |
| `kvprobe.nfs_path_binding` | Capture/replay process lifetime and kernel-resolved path-to-counter-filesystem metadata | A matching filesystem does not prove payload access, export-subtree identity or exclusive use |
| `kvprobe.nfs_client` | Cross-check the experimental tiny reference-client receipt format, baseline/output/KV originals and path/counter bindings | A narrow receipt reader, not a general vLLM or LMCache adapter |
| `kvprobe.nfs_client_process` | Preserve pre-teardown checkpoints and parent-observed process outcomes | Trusted POSIX child commands only; not a sandbox or an authenticity attestation |
| `kvprobe.nfs_ingest` | Normalize the explicit trusted reference format into NFS Gate 1-only evidence | Default ingest remains legacy; no live access or output overwrite |
| `kvprobe.nfs_qualification` | Recompute source-build identity, recognize raw-layout AST shapes, compare physical originals and bound namespace facts | Unknown source/layout stays unqualified; hashes are not execution authentication |
| `kvprobe.nfs_policy` | Classify typed NFS request observations | No stack-exit condition; missing, contradicted and invalid facts remain distinct |
| `kvprobe.tier_scope`, `kvprobe.scope_contract` | Validate draft NFS identities and their configuration/request bindings | Scope validation is not full evidence assessment |

Capture writes artifacts to an empty directory outside the measured mount; replay reads retained
originals without consulting live replay-host proc data. Hash checks protect consistency, not the
truth of a producer's declarations. Live capture needs Linux proc sources and the appropriate
process-access permissions. It does not install dependencies or create NFS mounts.

Run the portable synthetic checks without a GPU, NFS server or model download:

```bash
python3 -m pip install -e '.[test]'
python3 -m unittest discover -s tests -p 'test_nfs_*.py'
```

Fixtures use synthetic counters, model receipts and reserved `.invalid` endpoints. Process tests
launch bounded synthetic POSIX children, including deliberate signals/timeouts; they are not
serving experiments. Captured proc files, source inventories and raw bundles can contain local
paths or process details and belong outside the public source tree.

## Draft configuration contract

The NFS provider is `nfs_mountstats`. Its arm has a `tier_identity` with opaque labels:

```json
{
  "kind": "nfs_service",
  "id": "scope-a",
  "service_id": "service-a",
  "export_id": "export-a",
  "client_id": "client-a",
  "transport": "rdma",
  "io_mode": "direct"
}
```

Use the explicit API version for a configuration that contains this identity:

```python
from kvprobe.configuration import fingerprint_configuration

# parameters is the complete frozen configuration, not just the identity above.
fingerprint = fingerprint_configuration(parameters, evidence_version="0.4.0")
```

All identity fields participate in the fingerprint. `io_mode` may be `direct` or `buffered`;
declaring buffered mode does not prove a remote read. Transport is restricted to RDMA in this
draft. `target_device_filtered` stays false for an NFS service. Existing 0.3.x configuration
fingerprints remain unchanged; placing NFS scope declarations in legacy evidence is rejected.
`report` judges the explicit NFS Gate 1-only evidence contract; a scope declaration alone
cannot pass. Required request/source/file/namespace/counter/correctness roles must be present.
See [VERSIONING](VERSIONING.md) for raw format versions and compatibility boundaries.

## Measurement evidence and stack health

Reference-client replay separates `measurement_evidence` availability from `stack_health`.
A completed request checkpoint can remain readable after a later teardown error or without a
shutdown receipt. Actual parent-observed exit/signal/timeout outcomes remain visible. An
unfinished request or corrupt required artifact cannot supply valid evidence.
An error in the request callback or either quiet-control callback invalidates the capture.
Known optional shutdown/cleanup diagnostic JSON is retained and decoded as stack health;
unreadable diagnostic JSON does not veto otherwise valid measurement evidence. Required
measurement JSON and all artifact containment/hash checks remain enforced.

Availability, matching output and a normal exit are each insufficient for Gate 1. Source/runtime
qualification and competing-I/O review remain required; adjacent quiet intervals alone do not
prove absence of other I/O during the request. Cleanup patches are separate operational work.

## Normalize retained reference originals

Use an extracted run directory, with `candidate/client/` originals, bracketing path/counter
captures, and root-side `scope-before.json`, `scope-after.json`, and
`outer-mount-namespace.txt`. The source and format requirements are in the module docstrings.
No archive extraction, mount, sudo, model loading or live process access occurs during ingest.
This checkpoint exposes offline ingest; live producer setup is not a supported public runbook yet.

```bash
python3 -m kvprobe ingest --adapter nfs-reference /path/to/extracted-run \
  --configuration /path/to/extracted-run/candidate/client/serving/configuration.json \
  --run-id nfs-reference-check --output nfs-evidence.json
python3 -m kvprobe report nfs-evidence.json
```

Ingest returns 0 when it writes evidence; that is not a verdict. The NFS adapter refuses to
overwrite an existing output file. `report` returns **1** for a G1-only `PARTIAL`, `FAIL`,
`UNPROVEN` or `INVALID` report, and **2** for malformed normalized evidence. Performance claims
remain suppressed. `fingerprint --evidence-version 0.4.0` handles the explicit NFS configuration.

The adapter replays client/baseline and counter originals, compares actual source-build identity,
recognizes the reviewed raw-layout method shapes without executing retained source, and checks
key/file/range/physical buffer copies and namespace membership/propagation. Unknown source or
missing originals stays unproven; malformed input is refused. A valid scoped 0/partial/excess
payload or wrong output/KV is a failure; reset, scope/time instability or RPC errors invalidate
measurement. RPC receive bytes are never substituted for NFS payload. The identity mapping uses
zero byte tolerance; aggregate counters, retry, mixed I/O, shared/unknown scope and heuristic
attribution cannot pass.

This adapter requires a **trusted, independently reviewed reference producer**. It checks
consistency and recognized I/O layouts, not the arbitrary semantics of every retained Python
module or execution authenticity. Artifact hashes and a normalized report cannot authenticate a
fabricated bundle. Choosing the explicit adapter is not a substitute for common measurement
validation. A stored `reviewed=true`, raw pending-reason list, shutdown receipt or successful
wrapper exit is never used as a qualification shortcut.

## Remaining work and when a new run is needed

- Review the integration regression and Linux CLI replay before merging the experimental path.
- Finish live producer setup guidance and optional wrapper integration for repeatable collection.
- Live client-cache-negative coverage and broader stack/layout qualification remain follow-up work;
  synthetic negative tests establish policy behavior, not hardware behavior.

A qualified retained controlled capture can establish Gate 1 for its measured request. Adding an
offline adapter or CLI does not by itself require repeating the positive workload. A new run is
needed when required originals are absent or the producer, counter path or measured conditions
change. Additional live acceptance expands the supported workflow; it is not a condition for
recognizing an already qualified request. No performance or generality claim follows from one
retained request.

The general Gate 1 requirements are correctness, intended-tier counter scope, expected payload
agreement and sufficient control of competing explanations. This reference adapter's AST layout
recognition, physical-original comparisons and namespace inventory implement that contract for
one producer; they are not universal setup requirements for every tier or adapter.

A Gate 1-only result leaves Gates 2–5 unmeasured: the overall report remains `PARTIAL` with
performance claims suppressed. The normalized NFS policy has no stack-exit/shutdown condition;
raw stack-health diagnostics remain separately available. Source helpers, unknown layouts,
concurrency, multiple chunks and other serving stacks need their own qualification.
