# Matched native cuFile GDS vs LMCache NIXL static + GDS_MT

> **Scope.** This is the historical LMCache v0.4.1 reproduction. It exercises
> `NixlStaticStorageBackend` with a pre-created descriptor/file pool. It does not cover the
> dynamic FILE mode added later upstream. Use [NIXL-MODE-MATRIX.md](NIXL-MODE-MATRIX.md) for the
> static/dynamic POSIX/GDS_MT experiment.

**Question.** Over the *same device and the same cuFile*, does adding the NIXL storage plug-in
change the serving tier or its cost, versus LMCache's own native `GdsBackend`? The matched arm
holds the host, device, payload, model, and client fixed. It does **not** isolate the NIXL
abstraction alone: native key-derived files/on-demand handles are compared with LMCache's NIXL
static slot pool and, on the measured host, a multi-threaded `GDS_MT` plug-in (§7).

This document is the reproduction recipe. It names no host: set `IMAGE` and `GDS_MOUNT` (as every
script here does) and it runs anywhere the preconditions in §2 hold. It is not a benchmark
report: the one result it quotes is a historical, host-scoped record, and raw artifacts are not kept in
this repository. It states the **gate criteria** and
the **shape** a valid result must have, so a reader can tell a real measurement from a phantom one.

Scripts used: [`../scripts/g5_controlled_kv_experiment.sh`](../scripts/g5_controlled_kv_experiment.sh)
(+`g5_controlled_kv_experiment.py`), [`../scripts/tools/nixl_matched.sh`](../scripts/tools/nixl_matched.sh),
[`../scripts/tools/compare_matched.py`](../scripts/tools/compare_matched.py).

## 1. What is held constant, what changes

| Held constant (both arms) | Changed (only this) |
|---|---|
| GPU, SSD device, filesystem/mount, model, prompt payload | The L2 storage path: |
| `LMCACHE_CHUNK_SIZE=256`, `SAVE_UNFULL_CHUNK=False` | — native LMCache **`GdsBackend`** (`LMCACHE_GDS_PATH` + `LMCACHE_CUFILE_BUFFER_SIZE`), or |
| L1 CPU size (`MAX_LOCAL_CPU_SIZE`), ~3 GiB staging buffer | — LMCache **NIXL static storage mode** (`nixl_pool_size=512`) + `GDS_MT` |
| client, context length, eviction, per-request `nvidia-fs` attribution | over the same cuFile to the same mount |

Because the buffer sizes, chunk size, L1, model, payload and client are identical, the two arms read
the **same KV size from the device per request** — that byte-parity is one of the gates (§5).

## 2. Preconditions (the test host)

1. One vLLM+LMCache container image (this procedure was written against **LMCache v0.4.1** with
   **`nixl-cu13` 0.10.1**). `nixl_matched.sh preflight` checks `import nixl._api`.
2. `GDS_MOUNT` on a filesystem where **native GDS works**: `gdscheck -p` must report
   `use_compat_mode : false`. In compat mode you are not measuring the device tier.
3. `/dev/nvidia-fs*` present, `/etc/cufile.json` readable, `/run/udev` available to the container.
4. A recompute arm already serving (the scripts discover the host model directory from it with
   `docker inspect`, and it is the recompute reference).
5. Required environment for every command below: `IMAGE`, `GDS_MOUNT`. GPU0 must be free of leftover
   experiment containers before you start (see §8, gotcha 1).

## 3. Procedure

Run the two arms **sequentially**, not concurrently: both use GPU0, and the `nvidia-fs` counter is
host-global, so an overlapping arm would corrupt per-request attribution.

### Step 0 — stage the files
Copy `g5_controlled_kv_experiment.py` to the client path the scripts expect (`/tmp/` by default) and the
three scripts to a working directory. Confirm GPU0 is free (`nvidia-smi -i 0`).

### Step 1 — native `GdsBackend` arm, 3 × n = 10
```bash
export IMAGE=<image> GDS_MOUNT=<mount>
EVIDENCE=/tmp/nixl-static-native bash g5_controlled_kv_experiment.sh matched-gds
EVIDENCE=/tmp/nixl-static-native bash g5_controlled_kv_experiment.sh collect
```
This yields the native side as three runs (total n = 30 at context 4096) and, as a by-product,
re-checks the recompute/POSIX anchors on the current client. **Gate:** every `gds` run must be
`correct N/N` and `tier N/N` (§5).

### Step 2 — NIXL arm, 3 × n = 10
```bash
export IMAGE=<image> GDS_MOUNT=<mount>
EVIDENCE=/tmp/nixl-static-gds-mt NIXL_BACKEND=GDS_MT bash nixl_matched.sh setup
EVIDENCE=/tmp/nixl-static-gds-mt NIXL_BACKEND=GDS_MT CONTEXTS=4096 bash nixl_matched.sh matched
EVIDENCE=/tmp/nixl-static-gds-mt bash nixl_matched.sh collect
```
- `setup` frees GPU0 of known experiment arms (keeps the recompute arm) and stands up the NIXL arm.
- **Do not mount the diagnostic override for a timing run** (leave `NIXL_DIAG_BACKEND_PY` unset); it
  adds logging. It is only for diagnosing a failed write gate (§6).
- `GDS_MT` is the primary matched arm. `GDS` remains available only to reproduce or diagnose its
  host-specific failure; do not mix the two plug-ins in one result (§7).

### Step 3 — compare
```bash
python3 compare_matched.py --context 4096 \
  --a-backend lmcache_native_gds --b-backend lmcache_nixl_static_gds_mt \
  --a-label native-GdsBackend \
  --a /tmp/nixl-static-native/randomized-run{1,2,3}-gds.json \
  --b-label NIXL-GDS_MT \
  --b /tmp/nixl-static-gds-mt/nixl-run{1,2,3}.json \
  --provenance-verified
```

> **Strict-gate result (2026-09-08):** the earlier native 3 × n=10 versus NIXL 1 × n=30 pool remains
> an exploratory audit record. The rerun used 3 × n=10 with two warm-ups on both arms and passed all
> hard gates: native p50 0.1807 s versus NIXL `GDS_MT` 0.1700 s, or 5.9% lower relative to native.
> That result used the former localized transport payload. It is historical evidence, not a
> performance baseline for the current `transport/english-v1` workload.
>
> **The gates below are this procedure's, not `report`'s Gate 1.** Its byte evidence is `nvidia-fs`
> only, and that counter is host-wide, so under the current verdict contract this run's Gate 1 reads
> `UNPROVEN` — insufficient evidence, with no violation observed. Passing every gate in §5 therefore
> does not make the 5.9% a performance claim; see
> [Reaching Gate 1 `PASS`](EXTERNAL-RUNS.md#reaching-gate-1-pass).
> Evidence: the matched-rerun record is kept with the checksummed raw artifacts outside this
> repository — see [`EVIDENCE.md`](EVIDENCE.md).

## 4. What `compare_matched.py` reports

- **Match gate first:** both envelopes must be native-GDS context captures with distinct sources and
  containers, identical schedules and prompt/cacheable-token shapes, and equal measured-row counts.
- **Per-request validity:** every row must be `correct=true`, `tier_ok=true`, and show the expected
  `nvidia-fs` device-read volume. One bad row makes the comparison fail.
- **Provenance attestation:** `--provenance-verified` records the operator's independent check of the
  host, GPU, SSD/filesystem, model, client revision, and controlled schedule.
- **Descriptive verdict:** the p50 drift band (`--tolerance`, default 5%) is not a statistical
  equivalence or significance test.

## 5. Gate criteria — what makes a run valid

| Gate | Where | Pass condition |
|---|---|---|
| Write (NIXL only) | `nixl_matched.sh setup` | `nvfs_writes_delta > 0` **and** `gds_register_error_5027 = 0`. Pool files grew by `chunks × chunk-bytes`. (`nvfs=0 & pool bytes>0` = silent compat/POSIX fallback → **fail**; both 0 = no write → **fail**.) |
| Read / tier | inside every measurement | Per-request device read (`nvidia-fs`) ≥ 95% of the expected KV size, and the correctness token present. A failing row remains in raw evidence and rejects the matched comparison; it is not averaged into a result. |
| Match | `compare_matched.py` | Distinct evidence/container identities; identical schedule, payload shape, and measured-row count. |
| Validity | `compare_matched.py` | Every row is correct, tier-attributed, and reads the expected KV bytes from the device. |

## 6. Diagnosing a failed write gate (optional, v0.4.1-only)

NIXL puts are asynchronous fire-and-forget: `batched_submit_put_task` schedules the transfer on a
background loop and does not await it, so a failed transfer raises into an unretrieved future and is
**swallowed silently** (no error, no bytes). To surface it, apply
[`../scripts/tools/nixl_v0.4.1_background_transfer_diag.patch`](../scripts/tools/nixl_v0.4.1_background_transfer_diag.patch)
to a host copy of LMCache's `nixl_storage_backend.py` and bind-mount it via `NIXL_DIAG_BACKEND_PY`
(see `nixl_matched.sh`). The callback logs the transfer's completion or the exception
(e.g. a cuFile "Error in setting up Batch" → `NIXL_ERR_BACKEND`). This is investigation
scaffolding, not part of a measurement.

## 7. Which backend reaches the device is not portable — and the attribution caveat

NIXL exposes `GDS` (single-threaded cuFile batch path) and `GDS_MT` (multi-threaded). **Which one
actually reaches the device is host-specific**: one host may see `GDS` fail at cuFile Batch setup
while `GDS_MT` writes natively; another may see `GDS_MT` fail at file registration
(`error=5027`) and fall back to POSIX. Always run the §5 write gate per host and per backend.

**Attribution caveat for the comparison.** If, on a given host, only `GDS_MT` reaches the device
(single-threaded `GDS` fails), then the matched comparison is *the native backend's own I/O
dispatch* vs *NIXL's multi-threaded plug-in* — two things differ, not one. Be precise about the
native side rather than calling it single-threaded: in LMCache v0.4.1 the blocking retrieval path
is a serial loop over keys, each a synchronous read on the caller's thread, because the backend
does not override `batched_get_blocking` (`GdsBackend` enables its thread pool only for `wekafs`).
The store path is not serial — `GdsBackend` writes through `asyncio.to_thread`, which uses the
event loop's default executor. So "serial retrieval" is a property of the backend's dispatch on
this filesystem, not of cuFile, and it does not hold for stores or on Weka. In that case the defensible statement is that the
abstraction **added no cost** (and which backend reaches the device is not portable), **not** that
NIXL is faster: a TTFT difference cannot be attributed to the abstraction layer alone. Attributing
any gain to multi-threaded I/O needs an isolated reproducer.

## 8. Gotchas (each cost us a run)

1. **A leftover arm holding GPU0.** `g5_controlled_kv_experiment.sh` only tears down its own container;
   a NIXL container left running from a previous session keeps GPU0 and aborts the run at the first
   arm (`GPU0 is occupied`). Remove it first. `nixl_matched.sh setup` does auto-free known arms.
2. **Gate probe overflowing the context window.** The setup write-gate prompt must stay well under
   the model's context window, or it returns an error instead of storing (fixed in `nixl_matched.sh`).
3. **Async write vs a fixed sleep.** The write gate polls the counter (and inspects pool bytes) for
   up to ~30 s, because the NIXL put completes asynchronously; a fixed short sleep can race it.
4. **Long, detached runs.** For an unattended run, launch under `nohup ... &` and read the logfile;
   an interrupted SSH session kills a foreground run.

## 9. Artifacts

Each arm's `collect` writes a checksummed `tar.gz` (with a `SHA256SUMS`). Preserve both bundles: the
native `randomized-run{1,2,3}-gds.json` and NIXL `nixl-run{1,2,3}.json` files are the inputs to
`compare_matched.py`, and the checksummed raw bundles stay in access-controlled storage.
