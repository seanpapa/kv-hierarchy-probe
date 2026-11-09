# Record the exact run setup

A configuration JSON records the conditions that must stay fixed while results
are compared. Its fingerprint prevents measurements from different models,
builds, devices, workloads, or storage paths from being silently pooled.

The fingerprint describes the values in the JSON. It does **not** inspect the
serving process, discover the active backend, or prove that the declared mount
was used. The operator must compare the file with the live command, container,
mounts, and logs before measurement.

## Five-minute path

Start after [`kvprobe geometry`](GEOMETRY.md#five-minute-path) has produced a
reviewed `geometry.json`.

### 1. Copy the configuration example

From the repository root:

```bash
cp examples/reference-configuration.json configuration.json
```

The example is a template, not a detected host configuration.

### 2. Replace the example values

Edit these six objects in `configuration.json`:

| Object | Record | Verify against |
|---|---|---|
| `model` | Model root or ID, exact revision, served name | Serving command and model-load log |
| `kv_geometry` | Chunk tokens, selected bytes per chunk, dtype | Reviewed `geometry.json` and live runtime settings |
| `cache` | Upper-tier capacity in bytes | Backend configuration |
| `runtime` | Immutable image/build identity, stable device ID, optional GPU index | Running container/process and device inventory |
| `measurement` | Contexts, concurrency, samples, warm-ups | Planned commands |
| `arms` | Recompute and cache arms: backend, tier path, counter provider, staging capacity, eviction controls | Container configuration, host/container mounts, and counter scope |

One configuration normally contains both the recompute arm and every cache arm
being compared. Differences between those arms are intentional treatments;
drift outside those fields creates a different experiment.

Use exact bytes, not rounded MiB. For `kv_geometry.bytes_per_chunk`, select the
per-device or TP-group value that matches the external counter's scope.

### 3. Check the file against the live process

Before generating the fingerprint, confirm:

- model revision, dtype, chunk size, TP and PP match the serving process;
- the image/build identity names the software that is actually running;
- `device_id` identifies the measured accelerator, not only its enumeration index;
- each host path and container path resolves to the intended storage tier;
- the attribution provider measures the device and scope named by the arm;
- sample counts, contexts, concurrency and warm-ups match the planned commands.

If any item cannot be verified, keep the run at Gate 1 and record the gap rather
than treating the fingerprint as proof.

### 4. Generate the fingerprint

```bash
python3 -m kvprobe fingerprint configuration.json \
  > configuration.sha256
```

The output is one SHA-256 digest. Key order and JSON formatting do not change
it; changing a controlled value does. Re-run the command after every edit.

### 5. Retain the reviewable set

Keep these files together with the raw run:

```text
geometry.json
configuration.json
configuration.sha256
serving command and relevant logs
container image/build identity
device and mount inventory
```

This set lets a reviewer check both the declared setup and how its fingerprint
was derived. Continue with [Gate 1 evidence collection](GATE1.md#3-collect-evidence)
only after the JSON matches the live process.

If these checks pass and the files are retained, return to Gate 1. The
remaining sections are quick reference; the detailed rationale is kept in
[`reference/`](reference/CONFIGURATION-IDENTITY.md).

## Reference backend names

Each arm's `backend` is one opaque string, so the name has to carry every choice
that changes the path: native or NIXL, static or dynamic, POSIX or `GDS_MT`. A
native cuFile GDS arm records `lmcache_native_gds`, for example. These are
reference-experiment conventions, not a schema enum: another stack may use its
own non-empty identity when its run and configuration agree. The machine-readable
registry is `kvprobe/reference_backends.py`; the full human-readable table is in
[the identity reference](reference/CONFIGURATION-IDENTITY.md#backend-identity-for-nixl-storage-modes),
and CI checks every bundled view against the registry.

Keep mode-specific controls such as pool size, direct I/O, buffer device,
presence cache, filler-key policy, and eviction settings in the raw bundle and
the arm's `eviction_parameters` where applicable.

## What the fingerprint proves

A valid fingerprint proves that the supplied JSON is structurally valid and
has a stable canonical digest. During ingest, recorded arm and identity fields
can be checked against that configuration. It does not independently observe
the running model, image, device, backend, or mount.

Evidence v0.3 also binds each measured request or aggregate to a configuration
ID. Missing, duplicate, unknown, mixed, or stale bindings prevent the results
from being treated as one comparable experiment. See
[Evidence](EVIDENCE.md#configuration-identity-in-v03) for normalization.

## Why the serving software is part of the identity

A backend default, assertion, mode, or data path can change between builds even
when hardware and command-line parameters look identical. Record an immutable
image digest when available, or a stable site-resolvable build label when it is
not. Detailed rules and trade-offs are in the
[configuration identity reference](reference/CONFIGURATION-IDENTITY.md#why-the-serving-software-is-part-of-the-identity).

## Why the device is part of the identity

Different accelerators on one host can reach the same NVMe device through
different paths. Prefer the accelerator's PCI address where the platform
exposes one; `gpu_index` is optional and is not a stable identity by itself.
See the [device identity reference](reference/CONFIGURATION-IDENTITY.md#why-the-device-is-part-of-the-identity).

### Multi-GPU worker groups

Evidence v0.3 identifies one measured accelerator. It does not encode a full
TP worker group, rank placement, collective backend, or observed inter-GPU
path. Keep those claims outside the current fingerprint unless separately
measured. See the [multi-GPU boundary](reference/CONFIGURATION-IDENTITY.md#multi-gpu-worker-groups).

## Detailed reference

The longer [configuration identity reference](reference/CONFIGURATION-IDENTITY.md)
contains backend-mode details, binding examples, image/device rationale,
multi-GPU limits, and schema design decisions. It is supporting material, not a
required top-to-bottom step in the Gate 1 visitor path.
