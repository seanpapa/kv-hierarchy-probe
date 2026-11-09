# Configuration identity: detailed contract

This reference explains the design boundaries behind the shorter
[configuration workflow](../CONFIGURATION.md). Use the workflow while preparing
a run; return here when reviewing backend names, image and device identity,
measurement bindings, or multi-GPU limitations.

Evidence schema v0.3 prevents measurements collected under different conditions from being pooled
into one curve or ratio. Each normalized document contains a registry of complete experiment
configurations plus bindings from measured objects to configuration IDs.

## What the fingerprint covers

The `parameters` object is deliberately strict and versioned. It records:

| Object | Required identity |
|---|---|
| `model` | Actual model root, revision, and served name |
| `kv_geometry` | Chunk tokens, exact bytes per chunk, and dtype |
| `cache` | Upper-tier capacity in bytes |
| `runtime` | The serving software the run used (`image_digest`), and the device it measured (`device_id`, optionally `gpu_index`) |
| `measurement` | Contexts, concurrency levels, samples, and warm-ups |
| `arms` | Backend, tier path, attribution, staging capacity, and eviction policy |

The complete example is
[`../../examples/reference-configuration.json`](../../examples/reference-configuration.json).

Use bytes rather than rounded MiB for capacities and KV geometry. A served-name alias does not
replace the actual model root or revision. Use an immutable image digest rather than a mutable tag.
Record filler count and token size, cache-control mode, or other eviction knobs under each arm's
`eviction_parameters`.

Derive `bytes_per_chunk` from the model and live runtime inputs with
[`kvprobe geometry`](../GEOMETRY.md). Preserve that output with the run. Its per-device and TP-group
values are different counter scopes, not interchangeable estimates.

### Backend identity for NIXL storage modes

The v0.3 `backend` field is an opaque string, so it must carry every backend choice that changes
the path. The schema does not restrict external stacks to the names below. For this repository's
bundled LMCache experiments, record each NIXL FILE mode with the reference identity below rather
than collapsing every run into `nixl` or `gds`.

`kvprobe/reference_backends.py` is the machine-readable registry for these six reference paths.
This table and the [NIXL mode matrix](../NIXL-MODE-MATRIX.md) are human-readable views; CI compares
both, the bundled runners, and examples with the registry so a duplicated value cannot drift
silently.

| Path | `backend` |
|---|---|
| Native buffered POSIX | `lmcache_native_posix` |
| LMCache NIXL static + POSIX | `lmcache_nixl_static_posix` |
| LMCache NIXL dynamic + POSIX | `lmcache_nixl_dynamic_posix` |
| Native cuFile GDS | `lmcache_native_gds` |
| LMCache NIXL static + GDS_MT | `lmcache_nixl_static_gds_mt` |
| LMCache NIXL dynamic + GDS_MT | `lmcache_nixl_dynamic_gds_mt` |

The raw run must record the same backend identity; `kvprobe ingest` already refuses a recorded
backend that contradicts the configuration. Preserve `nixl_pool_size`, presence-cache setting,
buffer device, direct-I/O setting, LMCache/NIXL versions, and image digest in the checksummed raw
bundle. Static/dynamic mode is part of the treatment, not an interchangeable implementation detail.
For a static/dynamic comparison, also record the filler-key policy. Reusing a bounded filler set
prevents an eviction-free dynamic backend from accumulating a new set of files for every sample;
the comparator rejects arms that record different policies.

For static-pool sizing work, fingerprint `pool_mode`, `nixl_pool_size`, exact `slot_bytes`
(the same value `kvprobe pool-plan` takes as bytes per chunk), `required_live_chunks`, and `selection_intent` under the arm's `eviction_parameters`. The v0.3
object is intentionally open-ended, so these controls can be bound now without pretending that
configuration proves observed eviction. Preserve realized slot/FD counts and observed
eviction/reuse as raw measurement artifacts. See
[NIXL static-pool sizing and eviction probe](../NIXL-POOL-SIZING.md).

## Compute a fingerprint

```bash
python3 -m kvprobe fingerprint examples/reference-configuration.json
```

The command validates the configuration, serializes it as UTF-8 JSON with sorted keys and no
insignificant whitespace, and prints its SHA-256 digest. Object key order and formatting do not
change the result; any controlled parameter does.

## Bind measurements

The v0.3 `bindings` object covers every measured unit:

```json
{
  "attribution_requests": [
    {
      "request_id": "request-1",
      "configuration_id": "config-a"
    }
  ],
  "break_even": "config-a",
  "admission": "config-a",
  "capacity": "config-a",
  "envelope_levels": [
    {
      "level_index": 0,
      "configuration_id": "config-a"
    }
  ]
}
```

Supported sections require complete bindings. Unsupported sections use `null` for their aggregate
binding and an empty list for request or level bindings. Unknown IDs, duplicate bindings, missing
bindings, and stale fingerprints are malformed evidence.

If valid bindings name two configurations, the document remains evidence but its overall verdict is
`INVALID`. This distinction matters: the JSON is intact, but the measurements are not comparable.
See
[`../../examples/evidence/mixed-configuration-v0.3.json`](../../examples/evidence/mixed-configuration-v0.3.json).

## Why the serving software is part of the identity

A configuration is not only hardware and parameters. Two runs on the same device, with every
parameter equal, still differ if the serving image changed under them — a cache backend gains a
mode, an assertion moves, a default changes. A fingerprint that did not bind the build would merge
those runs into one curve and report the result as a single configuration.

This is not hypothetical. A backend that requires a buffer size in one release and derives it in
the next behaves differently under identical settings; the run that fails and the run that succeeds
are not two samples of one configuration.

### What to record

**A content digest of the image is the preferred value** — `docker image inspect --format '{{index
.RepoDigests 0}}'`, or the `Id` for an image built locally and never pushed. It is the strongest
form of the identity because it changes whenever anything in the image changes, including changes
nobody labelled.

`image_digest` is nevertheless **opaque to the validator**, exactly as `device_id` is. What the
method needs is a value that differs when the software differs and stays stable across the
campaign, and a digest is one way to get that, not the only way. Requiring a digest would mean an
operator who publishes evidence must disclose a private build to stay inside the contract — and the
predictable result is a fabricated digest, which reads as authentic where a label reads as what it
is. `reference-build-discrete-01` and `vllm-0.16.0+lmcache-0.4.1` are both valid; the second is
resolvable at the site that wrote it, which is the point.

Record it **verbatim**, on the same terms as the device identity below: the fingerprint is taken
over the text, and surrounding or control whitespace is refused rather than trimmed.

A valid fingerprint proves that the recorded configuration is internally consistent. It does not
independently verify the image identity — see [EVIDENCE.md](../EVIDENCE.md), "Configuration checking
is bounded by what was recorded."

## Why the device is part of the identity

The counter sources have different storage-device scopes. `/proc/driver/nvidia-fs/stats` is a
driver-level total for the node. Linux cgroup `io.stat` reports totals per cgroup and block device;
the earlier reference reader summed every device the container touched, while the current reader
selects the recorded tier device (or its parent whole disk). Neither source identifies which
accelerator issued the I/O, so **once a run is normalized, the accelerator that produced it is no
longer recoverable unless the run records it.**

On a multi-device host that matters. Two accelerators can sit behind different root complexes, so
the path from a device to the same NVMe is not the same path, and the cost of reaching the tier is
not the same cost. Without `device_id` in the runtime block, a run on one device and a run on
another produce the **same fingerprint** and merge into one document — the exact silent mismatch
the fingerprint exists to prevent.

### Use the PCI address

**Where the platform exposes a PCI address, use it.** It is stable across reboots, it distinguishes
devices on the same host, and it is the same string for every operator on that host — which is what
makes two campaigns comparable rather than merely self-consistent. The measurement scripts read it
and print it at the end of a run, ready to copy.

| Platform | `device_id` |
|---|---|
| NVIDIA, discrete | **`nvidia-smi --query-gpu=pci.bus_id`**, e.g. `00000000:65:00.0` |
| Any PCI accelerator | **The platform's own PCI address**, as its tool prints it, e.g. `0000:a1:00.0` |
| Unified-memory or integrated part | Whatever names the device there, e.g. `gb10-0` |
| No stable address exposed | Any identifier the operator can keep stable, e.g. `node2-accel-a` |

The first two rows are the recommendation; the last two are why it is a recommendation and not a
rule.

### Why the schema does not enforce it

`device_id` is **opaque to the validator on purpose.** What the method needs is a value that differs
between devices and stays stable across the campaign; it does not need any particular vendor's
address format, and the two differ. An earlier revision of this contract required an eight-digit PCI
domain because that is what one vendor's tool prints. It rejected `0000:65:00.0` — an ordinary Linux
form — and put every platform without PCI addressing outside the contract entirely. That is the
opposite of what [portability](../PORTABILITY.md) means here, so the rule now lives in this document
and in the scripts' output, where it can guide without excluding.

Record it **verbatim**: the fingerprint is taken over the text, so a change of case or spacing is a
change of configuration. Surrounding and control whitespace is refused rather than trimmed, because
silent trimming would hide exactly that drift. `gpu_index` is **optional and never the identity**:
enumeration order is not stable across reboots or driver changes, so it narrows nothing on its own.

The measurement scripts select the device with `PROBE_GPU_INDEX` (default `0`), and the gate 3, 4
and 5 producers record `device_id` — the PCI address on this stack — plus `gpu_index` in the run
envelope itself. `kvprobe ingest` then holds the configuration to what the run recorded: a
configuration naming a device the run did not use is refused, the same way a contradicting image
digest or chunk size is. A field the configuration omits is not compared, so omitting the optional
index does not turn into a mismatch against whatever the run happened to record.

A run that does not name a device — anything produced before this, or a host where the platform's
query tool cannot answer — is ingested without that check. The field is left unrecorded rather
than guessed, so the configuration's claim about the device stands unverified. That is visible in
the run, not hidden by a placeholder.

### Multi-GPU worker groups

Evidence v0.3 identifies one measured accelerator. It does not encode tensor-parallel size, the
complete rank-to-device binding, collective backend, or the inter-GPU path actually selected by a
serving process. On a multi-GPU endpoint, keep the `kvprobe doctor` manifest and serving logs with
the raw evidence, but do not treat the current `runtime.device_id` as identity for the full worker
group.

`doctor` records host topology capability only. `NV#`, `PIX`, `PHB`, or `SYS` in that manifest
describes a possible physical route, not proof that NCCL or another collective used it. A future
schema must fingerprint the worker group and bind TP size, rank placement, collective backend, and
runtime communication evidence before reports compare multi-GPU runs automatically. This is an
additive requirement for component-tier measurements and is separate from the disaggregated P→D
transfer fields described in [PROFILES.md](../PROFILES.md).

## Scope

One configuration describes the complete comparison, including every intentionally different arm.
For example, recompute and retrieval backends belong in the same `arms` list. The fingerprint
therefore changes when a controlled value drifts, while preserving the treatment difference the
experiment was designed to compare.

`kvprobe ingest` binds runs to an operator-supplied configuration and checks recorded arm,
schedule, and explicit identity fields against it (see [Evidence](../EVIDENCE.md)). It does not
independently discover metadata absent from the run. Operators must derive those values from
captured metadata; a fingerprint is not proof of the actual runtime's identity. Keep hostnames,
usernames, and private host paths out of the normalized document.
