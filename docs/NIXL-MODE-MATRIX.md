# LMCache NIXL FILE mode matrix: POSIX/GDS_MT × static/dynamic

## Question and scope

LMCache, not NIXL itself, chooses when storage descriptors are created and how long they live.
The historical matched run measured only LMCache v0.4.1
`NixlStaticStorageBackend + GDS_MT`. This
experiment adds the dynamic FILE implementation introduced later upstream and separates two
questions:

1. What changes when native storage access is wrapped by the NIXL storage path?
2. Within NIXL, what changes when a pre-registered bounded pool becomes key-addressable,
   on-demand storage?

The result is still component-scope evidence from one endpoint and cache domain. It is not a
claim about a disaggregated P→D path or all NIXL deployments.

## Matrix

Run every measured arm from the **same image digest and LMCache commit**. Compare a dynamic arm
with a static arm only when both use that matched software identity; keep the historical v0.4.1
static result separate from a newer image.

| I/O family | Native reference | NIXL static | NIXL dynamic |
|---|---|---|---|
| POSIX | `lmcache_native_posix` | `lmcache_nixl_static_posix` | `lmcache_nixl_dynamic_posix` |
| GDS | `lmcache_native_gds` | `lmcache_nixl_static_gds_mt` | `lmcache_nixl_dynamic_gds_mt` |

`scripts/tools/nixl_mode_matrix.sh` runs the four NIXL arms. Use the existing controlled native
runner for the two native references, but record the reference backend IDs shown above. This table
is a CI-checked view of the machine-readable registry in `kvprobe/reference_backends.py`; the
[identity reference](reference/CONFIGURATION-IDENTITY.md#backend-identity-for-nixl-storage-modes)
explains the naming boundary.

## Why NIXL `GDS` is not in the primary matrix

On the reference v0.4.1 host, NIXL `GDS` failed for both a single object and a batch with
cuFile `Error in setting up Batch` → `NIXL_ERR_BACKEND`; it never reached device I/O. `GDS_MT`
passed both the write and read gates. The primary matrix therefore uses `GDS_MT` only.

After building the newer image, one non-blocking smoke test may check whether `GDS` changed:

```bash
NIXL_BACKEND=GDS NIXL_STORAGE_MODE=dynamic \
  bash scripts/tools/nixl_mode_matrix.sh smoke-gds
```

Record success or failure separately. The smoke test never enters the timing matrix and does not
block `GDS_MT`. Even if it succeeds, do not attribute a `GDS`↔`GDS_MT` difference to the NIXL
abstraction; the plug-ins use different cuFile threading paths.

## Preconditions

1. The image contains LMCache dynamic FILE support (PR #2936 or later) and the NIXL package.
2. The exact image digest and LMCache/NIXL versions are recorded.
3. `GDS_MOUNT` is the same filesystem for every arm.
4. For GDS_MT, `/dev/nvidia-fs*` is present and `gdscheck -p` reports
   `use_compat_mode:false`.
5. `nixl_presence_cache=false`, direct-I/O setting, chunk geometry, L1 capacity, model, payload,
   client revision, and request schedule remain fixed.
6. Arms run sequentially: `nvidia-fs` is host-wide, and the cgroup counter sees this container's GDS
   reads as well as its POSIX ones, so concurrent arms would mix in both sources.
7. Use `filler_key_policy=reuse` for every arm. Dynamic FILE has no pool eviction; unique filler
   keys would accumulate hundreds of GiB during 3 × n=10 and make its directory grow while the
   static arm reuses bounded slots.

The runner defaults to `use_direct_io=false` for NIXL POSIX so it matches the buffered native
POSIX family, and `use_direct_io=true` for GDS_MT. An explicit `NIXL_USE_DIRECT_IO` override is a
different treatment and must be applied consistently to the arms being compared.

## Run the NIXL arms

Set common values first:

```bash
export IMAGE=<new-image-digest-or-immutable-tag>
export GDS_MOUNT=<gds-capable-mount>
export CONTEXTS=4096
export FILLER_KEY_POLICY=reuse  # also use this for both native reference arms
```

The launcher runs the client from `scripts/` (set `CLIENT` only to use another copy).

For each of the four combinations, run `preflight`, `setup`, `matched`, and `collect`:

```bash
NIXL_BACKEND=POSIX NIXL_STORAGE_MODE=static \
  EVIDENCE=/tmp/nixl-static-posix bash scripts/tools/nixl_mode_matrix.sh setup
NIXL_BACKEND=POSIX NIXL_STORAGE_MODE=static \
  EVIDENCE=/tmp/nixl-static-posix bash scripts/tools/nixl_mode_matrix.sh matched
NIXL_BACKEND=POSIX NIXL_STORAGE_MODE=static \
  EVIDENCE=/tmp/nixl-static-posix bash scripts/tools/nixl_mode_matrix.sh collect
```

Repeat with:

```text
POSIX  + dynamic
GDS_MT + static
GDS_MT + dynamic
```

Use a distinct `EVIDENCE` directory for every arm. The script derives a distinct container,
storage directory, raw arm ID, and reference backend ID from the mode and plug-in.

## Mode-specific gates

| Arm | File-lifecycle gate | I/O attribution gate |
|---|---|---|
| NIXL static POSIX | P zero-length pool files exist before PUT; slot bytes grow | container block-write/read evidence plus file bytes |
| NIXL dynamic POSIX | directory starts empty; key-derived files appear after PUT | container block-write/read evidence plus file bytes |
| NIXL static GDS_MT | P pool files exist before PUT; slot bytes grow | `nvidia-fs` write/read bytes; no register/transfer error |
| NIXL dynamic GDS_MT | directory starts empty; key-derived files appear after PUT | `nvidia-fs` write/read bytes; no register/transfer error |

The POSIX client counter reads the container cgroup's `io.stat` line for the tier's device only and
records that device, so Gate 1 can pass on it; see
[Reaching Gate 1 `PASS`](EXTERNAL-RUNS.md#reaching-gate-1-pass). `nvidia-fs` bytes are host-wide: on
their own they leave Gate 1 `UNPROVEN`. The cgroup counter also sees GDS reads, so record it on the
GDS arms too.

## Compare

The comparator now separates exact arm identity from the counter provider. Example, NIXL static
versus dynamic GDS_MT:

```bash
python3 scripts/tools/compare_matched.py --context 4096 --provider gds \
  --a-arm nixl_static_gds_mt --a-backend lmcache_nixl_static_gds_mt \
  --a /tmp/nixl-static-gds-mt/nixl_static_gds_mt-run{1,2,3}.json \
  --b-arm nixl_dynamic_gds_mt --b-backend lmcache_nixl_dynamic_gds_mt \
  --b /tmp/nixl-dynamic-gds-mt/nixl_dynamic_gds_mt-run{1,2,3}.json \
  --provenance-verified
```

For POSIX use `--provider posix` and the corresponding exact arm/backend IDs. The comparator's
provider choice does not upgrade cgroup evidence to target-device-filtered evidence.

## Preserve with every arm

- raw request rows and console output;
- runtime config including pool size, presence cache, buffer device, and direct-I/O setting;
- container inspect and complete compressed logs;
- image identity and installed software versions;
- startup and write-gate records;
- file counts/bytes and open-FD observations;
- `nvidia-fs` or block evidence;
- checksums over the complete evidence directory.

Run a separate restart test after timing: stop the dynamic container without deleting its files,
restart with the same configuration, and verify that the same key is found without rebuilding a
key→slot table. Keep that operational result separate from the latency samples.
