# Experimental NFS/RDMA evidence APIs

This development branch adds raw capture/replay and a draft scope contract for an NFS service.
It does **not** yet provide an NFS `ingest` adapter, an NFS Gate 1 verdict, or hardware-verified
NFS support. The existing local NVMe workflow and CLI defaults remain the operational path.

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
`report` deliberately refuses draft evidence 0.4.0 until the serving adapter and assessment exist.
See [VERSIONING](VERSIONING.md) for raw format versions and compatibility boundaries.

## Measurement evidence and stack health

Reference-client replay separates `measurement_evidence` availability from `stack_health`.
A completed request checkpoint can remain readable after a later teardown error or without a
shutdown receipt. Actual parent-observed exit/signal/timeout outcomes remain visible. An
unfinished request or corrupt required artifact cannot supply valid evidence.

Availability, matching output and a normal exit are each insufficient for Gate 1. Source/runtime
qualification and competing-I/O review remain required; adjacent quiet intervals alone do not
prove absence of other I/O during the request. Cleanup patches are separate operational work.

## Remaining work before NFS Gate 1 support

1. Qualify actual serving-client input/model/runtime/cache conditions and counter isolation.
2. Implement normalization, explicit CLI selection and versioned PASS/FAIL/UNPROVEN/INVALID
   assessment from the retained originals, with no legacy default changes.
3. Complete integration regression and positive/negative serving acceptance, including a
   client-cache hit with zero remote payload. Until then all raw replay remains
   `observation_only`, `serving_verification=not_assessed`, `performance_claims=suppressed`.

A future Gate 1-only result leaves Gates 2–5 unmeasured: the overall report must remain `PARTIAL`
with performance claims suppressed. This branch adds no new performance result or verified scope.
