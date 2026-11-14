# scripts/

## Minimal runbook — all five gates, one configuration

The shortest path from a prepared host to a five-gate report. This order ran end to end on hardware
(LMCache v0.4.1 native GDS, one host, two GPUs); measurement takes about 30 minutes. Run every block
from the repository root, in one `bash` shell that stays open (the launchers keep a `sudo` timestamp).
Background for each gate is in [`../docs/CHECKLIST.md`](../docs/CHECKLIST.md); this section is only
the commands.

**Prerequisites.** Two NVIDIA GPUs — GPU 0 is measured, GPU 1 serves the recompute reference that
Gates 3 and 4 compare against. A GDS-capable filesystem on a **whole** NVMe disk (the per-device
counter cannot attribute a partition), with `gdscheck -p` reporting `use_compat_mode : false`.
cgroup v2, Docker with the NVIDIA runtime, a vLLM + LMCache image ([Getting an image](#getting-an-image)), the model directory on the host,
`sudo`, Python 3.10 or newer. Loopback ports 8104 and 8110 free. Nothing else using either GPU.

### Getting an image

The runbook needs one image that serves vLLM with LMCache's `LMCacheConnectorV1` and can reach the
GDS path. Only one combination has hardware results here:

| Component | Tested version |
|---|---|
| Base image | NVIDIA Dynamo `nvcr.io/nvidia/ai-dynamo/vllm-runtime:1.0.1-cuda13` — vLLM 0.16.0, torch 2.9.1 + CUDA 13.0, NIXL 0.10.1 |
| LMCache | v0.4.1, **built from source inside that image** |
| Host | NVIDIA driver with GDS installed (`nvidia-fs` loaded, `gdscheck -p` not in compat mode); the launchers pass the devices, `/run/udev` and the cuFile configuration into the container |

Why from source: the prebuilt LMCache 0.4.1 wheel ships a `c_ops` extension built for CUDA 12, which
does not load against this image's CUDA 13 torch, and its GPU architecture list stops at compute
capability 10.0. Newer LMCache releases on PyPI require a `transformers` version that vLLM 0.16.0
does not accept. A source build against the image's own torch avoids both:

```dockerfile
FROM nvcr.io/nvidia/ai-dynamo/vllm-runtime:1.0.1-cuda13
USER root
RUN pip uninstall -y lmcache || true
RUN pip install --no-cache-dir ninja "packaging>=24.2" "setuptools>=77.0.3,<81.0.0" "setuptools_scm>=8" wheel
COPY lmcache-src /opt/lmcache-src
ARG TORCH_CUDA_ARCH_LIST
ENV TORCH_CUDA_ARCH_LIST=${TORCH_CUDA_ARCH_LIST} ENABLE_CXX11_ABI=1
RUN rm -rf /opt/lmcache-src/.git && cd /opt/lmcache-src \
 && SETUPTOOLS_SCM_PRETEND_VERSION=0.4.1 pip install . --no-build-isolation --no-cache-dir
```

```bash
git clone --branch v0.4.1 --depth 1 https://github.com/LMCache/LMCache.git lmcache-src
docker build --build-arg TORCH_CUDA_ARCH_LIST=<your GPU's compute capability, e.g. 9.0> -t vllm-lmcache:0.4.1-src .
```

`USER root` is needed because the base image builds as a non-root user; removing `.git` lets
`setuptools_scm` take the pinned version instead of inspecting a repository owned by another user.
Check the result with `docker run --rm --gpus all <image> python3 -c "import lmcache.c_ops"` before
starting the runbook. The tested image was produced by the same steps in two stages (prebuilt wheel
first, then replaced by this source build); this single-file form has not itself been rebuilt and
re-measured. Any other image works if it passes the runbook's preflight and the `c_ops` import, but it
is a different configuration: record its digest, and expect your numbers to describe it, not ours.
Pulling from `nvcr.io` is subject to NVIDIA's container terms.

<!-- runbook:start -->

**1. Environment.** The `:?` entries have no default — set them first.
`PROBE_CUFILE_BUF_MIB` is the staging pool: keep it at one entry or less, or Gate 4 misreads
GPU-buffer hits ([FAILURE-CATALOG §13](../docs/FAILURE-CATALOG.md#staging-buffer-full)).
`PROBE_MIB_PER_CHUNK` is the KV size of a 256-token chunk of *your* model (64 for Qwen2.5-32B,
see [below](#set-probe_mib_per_chunk-for-the-model-you-are-serving)).

```bash
export IMAGE=${IMAGE:?your vLLM+LMCache image} GDS_MOUNT=${GDS_MOUNT:?the GDS filesystem mount}
export MODEL_HOST_DIR=${MODEL_HOST_DIR:?host directory that holds the model directory}
export MODEL=${MODEL:?model path inside the container, e.g. /models/Qwen2.5-32B-Instruct}
export PROBE_MIB_PER_CHUNK=${PROBE_MIB_PER_CHUNK:?KV MiB per 256-token chunk of this model}
export GDSCHECK=${GDSCHECK:-/usr/local/cuda/gds/tools/gdscheck}
export PROBE_GPU_INDEX=0 PROBE_CUFILE_BUF_MIB=1024
export FILLERS=6 FILLER_TOKENS=4096 REP_CONTEXT=4096 SAMPLES=10 BATCHES=10
export RUN=$(date +%Y%m%d-%H%M%S)
export OUT=$HOME/kvprobe-$RUN
mkdir -p "$OUT"
```

**2. Preflight** (seconds). `tier-check` checks, read-only, the host conditions that decide whether
this run can establish Gate 1: the device the counter reads, a dedicated disk, multipath, cgroup v2,
the filesystem, compat mode, the `nvidia-fs` counters, the container prerequisites and the staging
pool. **Continue only with `READY`** (exit 0). Warnings, failures and required checks that could not
be completed return exit 1; their output names the next check or fix. Optional inputs that were
not supplied and checks that do not apply to the selected arm may be skipped without blocking
`READY`. `doctor` lists what the host can observe; `preflight` prints the mount, `gdscheck`, the
GPUs and the image id.

```bash
(
set -e
python3 -m kvprobe tier-check --tier-mount "$GDS_MOUNT" --model-dir "$MODEL_HOST_DIR" --cufile-buf-mib "$PROBE_CUFILE_BUF_MIB" --mib-per-chunk "$PROBE_MIB_PER_CHUNK" --context-tokens "$REP_CONTEXT" --fillers "$FILLERS"
python3 -m kvprobe doctor
sudo -v
EVIDENCE=$OUT/g5-$RUN bash scripts/g5_controlled_kv_experiment.sh preflight
)
```

The subshell stops the block if a command fails, without closing your interactive shell. If it
stops, resolve the reported condition and rerun step 2 before starting step 3. T3 checks the
controller transport: NVMe/TCP and NVMe/RDMA are outside this local PCIe baseline. T8 checks
reported compat settings, including forced compat, rather than proving a native transfer; that
still requires the controlled run's evidence.

**3. Recompute reference on GPU 1** (about 2 min). It serves until step 8.

```bash
PROBE_GPU_INDEX=1 PORT=8110 CONTAINER=kvprobe-recompute-gpu1 EVIDENCE=$OUT/reference-$RUN bash scripts/g5_controlled_kv_experiment.sh serve recompute
```

**4. Gates 1 and 2** — one context run per arm (about 4 min each).

```bash
EVIDENCE=$OUT/g5-$RUN bash scripts/g5_controlled_kv_experiment.sh one recompute 1
EVIDENCE=$OUT/g5-$RUN bash scripts/g5_controlled_kv_experiment.sh one gds 1
```

**5. Gate 5** — concurrency 1, 2, 4, 8 (about 10 min).

```bash
EVIDENCE=$OUT/g5-$RUN bash scripts/g5_controlled_kv_experiment.sh concurrency-gds
```

**6. Gate 3** — ten admission/reuse attempts on a fresh tier arm (about 5 min).

```bash
EVIDENCE=$OUT/g5-$RUN bash scripts/g5_controlled_kv_experiment.sh serve gds
PORT=8104 RECO_PORT=8110 CONTEXTS=$REP_CONTEXT N=$SAMPLES WARMUP=2 EVIDENCE=$OUT/admission-$RUN bash scripts/g3_admission_cost.sh measure
EVIDENCE=$OUT/admission-$RUN bash scripts/g3_admission_cost.sh collect
```

**7. Gate 4** — a working-set sweep past the upper tier (about 4 min). The launcher's L1 holds three
1 GiB entries, so the sweep runs to six.

```bash
EVIDENCE=$OUT/g5-$RUN bash scripts/g5_controlled_kv_experiment.sh serve gds
PORT=8104 RECO_PORT=8110 ENTRY_TOKENS=$REP_CONTEXT WORKING_SETS="1 2 3 4 5 6" EVIDENCE=$OUT/capacity-$RUN bash scripts/g4_capacity_sweep.sh measure
EVIDENCE=$OUT/capacity-$RUN bash scripts/g4_capacity_sweep.sh collect
```

**8. Cleanup** — stop both arms (only containers this tree labelled are removed) and bundle the runs.

```bash
EVIDENCE=$OUT/g5-$RUN bash scripts/g5_controlled_kv_experiment.sh stop
PROBE_GPU_INDEX=1 PORT=8110 CONTAINER=kvprobe-recompute-gpu1 EVIDENCE=$OUT/reference-$RUN bash scripts/g5_controlled_kv_experiment.sh stop
EVIDENCE=$OUT/g5-$RUN bash scripts/g5_controlled_kv_experiment.sh collect
```

**9. Configuration** — what the runs cannot record themselves. The image id and the GPU's PCI
address are read from this host; the rest restates step 1 and the launcher's fixed L1 (3 GiB).

```bash
IMAGE_ID=$(docker image inspect "$IMAGE" --format '{{.Id}}')
DEVICE_ID=$(nvidia-smi -i "$PROBE_GPU_INDEX" --query-gpu=pci.bus_id --format=csv,noheader | tr -d ' ')
cat > "$OUT/configuration.json" <<EOF
{
  "model": {"root": "$MODEL", "revision": "unrecorded", "served_name": "$(basename "$MODEL")"},
  "kv_geometry": {"chunk_tokens": 256, "bytes_per_chunk": $((PROBE_MIB_PER_CHUNK * 1048576)), "dtype": "bfloat16"},
  "cache": {"upper_tier_capacity_bytes": 3221225472},
  "runtime": {"image_digest": "$IMAGE_ID", "device_id": "$DEVICE_ID", "gpu_index": $PROBE_GPU_INDEX},
  "measurement": {"contexts": [$REP_CONTEXT], "concurrency": [1, 2, 4, 8], "samples": $SAMPLES, "warmup": 2},
  "arms": [
    {"name": "recompute", "backend": "disabled", "tier_path": null, "attribution_provider": "serving_log",
     "staging_capacity_bytes": 0, "eviction_policy": "none", "eviction_parameters": {}},
    {"name": "gds", "backend": "lmcache_native_gds", "tier_path": "/gds/lmcache-controlled-gds",
     "attribution_provider": "cgroup_blk", "staging_capacity_bytes": $((PROBE_CUFILE_BUF_MIB * 1048576)),
     "eviction_policy": "targeted", "eviction_parameters": {"fillers": $FILLERS, "filler_tokens": $FILLER_TOKENS}}
  ]
}
EOF
```

**10. Ingest and report.** `--expected-reuses` is your workload's reuse count per admitted entry,
stated before you look at the result (1 is the most conservative).

```bash
python3 -m kvprobe ingest "$OUT/g5-$RUN/randomized-run1-gds.json" "$OUT/g5-$RUN/randomized-run1-recompute.json" "$OUT/g5-$RUN/concurrency-gds.json" "$OUT/admission-$RUN/admission-cost.json" "$OUT/capacity-$RUN/capacity.json" --configuration "$OUT/configuration.json" --run-id "kvprobe-$RUN" --expected-reuses 1 --output "$OUT/evidence.json"
python3 -m kvprobe report "$OUT/evidence.json" --output "$OUT/report.txt" || echo "report exit $? (1 = overall not PASS; read $OUT/report.txt)"
cat "$OUT/report.txt"
```

<!-- runbook:end -->

**What you get.** In `$OUT`: `g5-$RUN/` (context and concurrency runs, container logs),
`admission-$RUN/`, `capacity-$RUN/` and `reference-$RUN/`, the first three with `SHA256SUMS`;
`configuration.json`, `evidence.json`, `report.txt`. The `collect` steps also leave
`g5-$RUN.tar.gz`, `admission-$RUN.tar.gz` and `capacity-$RUN.tar.gz` in `$HOME`.

**Exit codes.** `ingest`: 0 written, 2 refused — the message names the field that did not match.
`report`: 0 overall `PASS`, 1 overall not `PASS` (each gate's verdict and reason are printed), 2 the
evidence could not be read. A gate that is not `PASS` is a finding about the measurement, not a crash;
`report` says so on stderr when it exits 1.

**If a step fails.** The labelled arms stay up; run step 8's two `stop` lines before retrying. A
port or GPU already in use, a model directory the container cannot see, or `use_compat_mode : true`
are the usual causes. Results never need tuning to pass: a gate passes when its measurement is
valid, and what it found is a separate result ([`../docs/METHOD.md`](../docs/METHOD.md)).

## Status

> **Status (current reference 2026-09-25).** The current `transport/english-v1` workload has a
> sanitized four-arm reference in
> [`../examples/baselines/transport-english-v1-20260925.json`](../examples/baselines/transport-english-v1-20260925.json).
> All 600 measured rows in its source capture passed the correctness check
> (expected value present in the completion) and controlled-interval
> expected-byte corroboration. Use it only with the same workload identity, configuration scope,
> and aggregation shape; see [`../docs/REVALIDATION.md`](../docs/REVALIDATION.md).
>
> **That reference reads Gate 1 `UNPROVEN`.** It predates the per-device counter, so its byte
> evidence is a controlled-interval corroboration that does not identify the tier's device. It is an
> audit record of correctness and expected bytes, not a serving-tier pass, and its latencies are not
> a performance result. For a measured Gate 1 `PASS (corroborated)` replay
> [`../examples/evidence/gate1-pass-filtered-counter.json`](../examples/evidence/gate1-pass-filtered-counter.json)
> with `kvprobe report`.
>
> ⚠️ **`g3_admission_cost.*` (gate 3), `g4_capacity_sweep.*` (gate 4), and `nixl_matched.sh` are newer.**
> Both were cross-checked on 2026-09-10: Gate 3 with context order reversed, Gate 4 through the
> clean DRAM→NVMe crossover with a one-entry cuFile pool; larger working sets remain out of scope because
> the current store-all-then-read design does not separate cache-capacity failure from genuine tier
> placement (see [`../docs/REVALIDATION.md`](../docs/REVALIDATION.md)). Nothing here is a benchmark score; raw measurement
> artifacts are not kept in this repository.

## Naming

Measurement scripts are prefixed with the gate they establish, so the directory listing reads in
the order the procedure runs:

```
g2_context_sweep.*            gate 2 — retrieval break-even against recompute
g3_admission_cost.*           gate 3 — store cost and break-even reuse count
g4_capacity_sweep.*           gate 4 — capacity justification against the tier above
g5_controlled_kv_experiment.* gate 5 — operating envelope (and randomized repeats for gate 2)
tools/                        not measurements: comparators, the merge gate, the NIXL arm
```

**Visitor order: first-visit → [Gate 1](../docs/GATE1.md) → Gates 2–5.**
There is no `g1_*` benchmark runner: common validation applies inside every
measurement. The separate experimental `kvprobe gate1` CLI adds observations
and an optional reviewed companion; it does not automatically validate these runners.

Gate 1 is the serving-tier core of **Common Measurement Validation**, not another experiment.
Apply configuration/counter prechecks before execution, request checks during/after execution,
and eligibility/audit checks before reporting. The current implementation is distributed
across scripts, ingest/report code, and operator checks—not one universally called validator.
See the [common checklist](../docs/CHECKLIST.md) for coverage and manual responsibilities.

## What each script measures

| Script | Gates | Produces |
|---|---|---|
| `g2_context_sweep.sh` + `g2_context_sweep.py` | 1, 2 | Each arm as a line — fixed cost plus per-token cost — across a context sweep, with the crossing against recompute. Per-request `nvidia-fs` read bytes and a tier-purity verdict for every measurement |
| `g5_controlled_kv_experiment.sh` + `g5_controlled_kv_experiment.py` | 1, 5 | Order-randomized repeats of the arms at one context, and a concurrency sweep with per-request correctness and tier gates |
| `g3_admission_cost.sh` + `g3_admission_cost.py` | 1, 3 | Per-entry admission cost and realized break-even reuse count. Every target-tier, fallback, store-unattributed, or unattributed attempt is retained; host-wide `nvidia-fs` counters classify rather than filter samples |
| `g4_capacity_sweep.sh` + `g4_capacity_sweep.py` | 1, 4 | Working-set sweep that finds the DRAM→NVMe crossover, classifying each retrieval as DRAM-resident or device-served by `nvidia-fs` read bytes; keeps a write/read record per store and retrieval |
| `tools/nixl_matched.sh` (+ `g5_controlled_kv_experiment.py`) | 1 | Preserves the LMCache v0.4.1 **NIXL static + `GDS_MT`** matched reproduction. It is not a dynamic-mode runner. **Full recipe: [`../docs/NIXL-STATIC-GDS-MATCHED.md`](../docs/NIXL-STATIC-GDS-MATCHED.md)** |
| `tools/nixl_mode_matrix.sh` (+ `g5_controlled_kv_experiment.py`) | 1 | Runs the newer LMCache FILE matrix: NIXL POSIX/GDS_MT × static/dynamic, with mode-specific file/write gates and explicit arm/backend identity. NIXL `GDS` is smoke-only. **Plan and commands: [`../docs/NIXL-MODE-MATRIX.md`](../docs/NIXL-MODE-MATRIX.md)** |
| `tools/summarize_runs.py` | — | Not a measurement: pools context runs into the reference-summary shape `verify_against_baseline.py` compares (see `../docs/REVALIDATION.md`) |
| `tools/verify_against_baseline.py` | — | Not a measurement: compares a candidate summary against a recorded baseline within tolerance (the merge gate; see `../docs/REVALIDATION.md`) |
| `tools/compare_matched.py` | 1 | Not a measurement: compares two matched storage arms at one context. It validates exact arm/backend identities, selects GDS or POSIX read evidence, and rejects reused sources/containers, mismatched schedules or payload shapes, and rows without plausible device-read bytes before reporting TTFT p50/p95. The drift-band result is descriptive, not a statistical equivalence test |

Gate 1 requirements apply within every measurement; the optional NVMe observation CLI is
separate. Producers record path-specific request evidence, `ingest` normalizes it, and `report`
applies the current verdict contract. Failed requests remain in raw evidence for auditability; a Gate 1 `FAIL`
or `UNPROVEN` suppresses the whole normalized performance claim rather than merely filtering bad
rows. `g3_admission_cost.sh` and
`g4_capacity_sweep.sh` drive their measurement against the arms `g2_context_sweep.sh setup` already
brought up and gated — one source of container configuration.

Run the matched comparator only after separately confirming that both captures used the same
host, GPU, SSD/filesystem, model, client revision, and controlled schedule. Record that operator
attestation explicitly:

```bash
python3 compare_matched.py \
  --context 4096 \
  --a-backend lmcache_native_gds \
  --b-backend lmcache_nixl_static_gds_mt \
  --a /path/to/native-gds-run.json \
  --b /path/to/nixl-gds-mt-run.json \
  --provenance-verified
```

The flag is an attestation, not automatic proof of environmental equality. The JSON hard gates
verify the caller-selected exact arm/backend identities, counter provider, distinct sources and
containers, identical schedules and prompt/cacheable-token shapes, correctness/tier success, and
expected read volume for every measured row. Legacy invocations default to `arm=gds` and the
`nvidia-fs` provider.

## Required environment

Both shell scripts refuse to start without these, because they name your site rather than
anything portable:

| Variable | What it is |
|---|---|
| `IMAGE` | Container image with vLLM and LMCache built for this host (one image serves every arm) |
| `GDS_MOUNT` | The GDS-capable filesystem mount used for the cache tier. Both scripts read `GDS_MOUNT`; `g5_controlled_kv_experiment.sh` also still accepts it as `MOUNT` |

`PROBE_GPU_INDEX` (default `0`) selects which GPU every arm runs on; the scripts pass it to
`docker --gpus`, read occupancy and telemetry with `nvidia-smi -i`, and record `device_id` — the
PCI address on this stack — plus `gpu_index` in the run envelope and in `provenance.txt`. Put the
same values in the configuration's `runtime` block, where `device_id` is required and `gpu_index` is
optional; `kvprobe ingest` refuses a configuration that names a device the run did not use. The
storage counters do not identify which accelerator issued the I/O: `nvidia-fs` is host-wide, while
the current cgroup reader is filtered to the recorded tier block device. Without the runtime identity,
the accelerator is not recoverable from the evidence. The contract itself takes any stable identifier, not just a PCI address (see
[`../docs/CONFIGURATION.md`](../docs/CONFIGURATION.md)).

Optional, with defaults: `MODEL` (path inside the container), `PORT`, `RECO_PORT`, `CTNR`,
`RECO_CTNR`, `L1_GB`, `CUFILE_BUF_MIB`, `EVIDENCE`, `SEED`, `GDSCHECK`, and — for the gate 3/4
scripts — `CONTEXTS`, `ENTRY_TOKENS`, `WORKING_SETS`, `N`, `WARMUP`. `PROBE_TIER_MOUNT` is exported
from `MOUNT` by `g5_controlled_kv_experiment.sh`; set it directly only when driving the client by
hand.

### Set `PROBE_MIB_PER_CHUNK` for the model you are serving

`PROBE_MIB_PER_CHUNK` defaults to `64`, which is the KV size of a 256-token chunk **for the model
these scripts were calibrated on**. It is not derived from the model you point them at, and nothing
in the run notices the mismatch.

Serving a different model with the default therefore fails every tier gate while the counters are
correct. Observed: an 8B model at a 4,096-token context moved exactly `536,870,912` bytes, the
counter read exactly that, and all four requests still reported `tier=FAIL`, because the default
expected 1 GiB. The measurement was right and the constant was wrong.

Derive the value before a run and pass it:

```bash
kvprobe geometry --model-config /path/to/config.json --dtype bfloat16 --chunk-tokens 256
#   Full model: 131,072 bytes/token; 33,554,432 bytes/chunk (32 MiB)
export PROBE_MIB_PER_CHUNK=32
```

Match the scope to the counter: a per-device counter wants the per-rank figure under tensor
parallelism, not the TP-group aggregate. See [`../docs/GEOMETRY.md`](../docs/GEOMETRY.md).

### The POSIX arm's cgroup counter reads one device

`g5_controlled_kv_experiment.sh` exports `PROBE_TIER_MOUNT="$MOUNT"` so the client can resolve the
host tier mount to a single block device and read `io.stat` for that device only. Summing every
`io.stat` line counted the OS disk and any other volume the container touched as tier traffic — on
the reference host that was **11.3%** of the reading.

Two behaviours matter when reading the evidence:

- `io.stat` accounts I/O against **whole disks.** A tier on a partition carries the partition's
  `st_dev`, which appears in no `io.stat` line, so the client resolves it to the parent disk and
  records the scope as `widened_to_parent_disk`. The counter then also covers that disk's other
  partitions, which weakens what the number can attribute. A tier on its own disk records
  `whole_disk`.
- `io.stat` lists a device only **after** the cgroup has moved bytes on it, so a container that has
  not touched the tier has an empty file. A readable file without the device is therefore a measured
  **zero**; only an unreadable cgroup, a missing container, or an unresolved device yields -1 and
  withholds the observation. The arm has to be able to report zero: zero device bytes is the
  phantom-tier signature, and a counter that can never read zero can never expose one.

**Confirmed against a real retrieval.** On the reference host, an LMCache POSIX arm serving a
4,096-token context recorded `536,870,912` bytes on the tier device for every request — exactly
16 chunks × 32 MiB, a ratio of 1.0000 against the derived geometry — with `nvfs_read_mib` at 0, as
a POSIX arm should be. Page cache did not hide the read, because `evict_posix_file_pages` evicts the
tier's own pages before each request with `POSIX_FADV_DONTNEED` rather than a host-wide
`drop_caches`.

That run also showed the device-scoped and summed readings agreeing exactly, which is the point:
the summed value was right by circumstance on a quiet arm, and is right by construction now.

The client records `cgroup_io_device` and `cgroup_io_device_scope` for every cache arm, so `ingest`
marks the counter target-device filtered and Gate 1 can pass on it. Keep the eviction: a retrieval
served from the DRAM tier above the disk moves no device bytes and reads the same as a tier that did
not serve, so without eviction the request fails Gate 1 rather than passing it. A run captured before
the per-device reader records neither field; its Gate 1 reads `UNPROVEN`.

`FILLER_TOKENS` sets the size of each filler request (default 8,192 tokens, the size every recorded run used).
Keep one filler within the staging pool: with a small pool, a filler larger than the pool exercises a store path
the recorded runs never measured. `FILLER_KEY_POLICY` defaults to `unique`, preserving the historical native/static procedure. Set
it to `reuse` for every arm in a static/dynamic matrix: dynamic FILE has no bounded slot eviction,
so unique filler keys would grow the backing directory on every sample. The raw run records the
policy and the matched comparator rejects a mismatch.

### Prompt profiles for the controlled experiment

`g5_controlled_kv_experiment.py` has two prompt profiles. `transport` is the default portable
English movement workload. Its metadata records `version=english-v1`; do not compare it with
performance evidence produced by the former localized payload.
`retrieval` builds deterministic English synthetic experiment records and asks for the six-digit
verification code from one record. The selected record can be early, middle, or late in the
context. The raw evidence records the profile arguments, selected record, seed, and prompt SHA-256.
See [`../docs/PROMPT-CONSTRUCTION.md`](../docs/PROMPT-CONSTRUCTION.md) for the full path from a
context target through token alignment, cache identity, filler pressure, expected KV bytes, and
lower-tier verification.

Use the shell wrapper to run a semantic early-context retrieval workload across the same arms:

```bash
PROMPT_PROFILE=retrieval PROMPT_SEED=17 NEEDLE_POSITION=early \
  bash g5_controlled_kv_experiment.sh context-sweep
```

Repeat with `NEEDLE_POSITION=middle` and `late` to separate retrieval-distance effects. Compare
results only when profile, seed, needle position, context targets, and all other controlled inputs
match. A retrieval run is a new workload and must not be treated as a continuation of a historical
transport baseline.

Set `MODEL_HOST_DIR` to the host directory that holds the model directory; it is mounted at
`/models`. Without it, both launchers read the `/models` mount of an existing reference container
with `docker inspect`, and stop when there is none.

`nixl_matched.sh` also accepts an **opt-in diagnostic override** (empty by default, no effect):
`NIXL_DIAG_BACKEND_PY` bind-mounts a host copy of LMCache's NIXL backend module — patched with
`nixl_v0.4.1_background_transfer_diag.patch` to surface the otherwise-swallowed background transfer
exception — over the installed in-container module. The script discovers that module path from the
image; `NIXL_BACKEND_CONTAINER_PATH` can override it for unusual layouts. An optional
`NIXL_LD_LIBRARY_PATH`. This is investigation scaffolding pinned to **LMCache v0.4.1** and is how
the `GDS` "Error in setting up Batch" failure was diagnosed and `GDS_MT` confirmed. `NIXL_BACKEND`
selects `GDS` or `GDS_MT`. Keep the override opt-in and preserve its diagnostic output separately
from performance evidence.

## Preconditions on the host

1. A recompute arm already serving on `RECO_PORT` with the model mounted.
2. `GDS_MOUNT` mounted on a filesystem where native GDS works — `gdscheck -p` must report
   `use_compat_mode: false`. **In compat mode this is not the tier you think you are measuring.**
3. `/dev/nvidia-fs*` present, `/run/udev` available to the container, `/etc/cufile.json` readable.
4. `cufile_buffer_size` set deliberately. **If it exceeds the working set it becomes a
   GPU-resident tier and the run never touches the device** — a phantom tier, which gate 1 exists
   to catch.
5. **Nothing left from the previous run.** Container-created files are root-owned, and a failed
   cleanup leaves chunks that are served as cache hits:

   ```bash
   sudo rm -rf <tier_path> && sudo mkdir -p <tier_path> && sudo chown "$(id -u):$(id -g)" <tier_path>
   find <tier_path> -type f | wc -l    # must be 0 before starting
   ```
6. **Arms plumbed identically.** `g2_context_sweep.sh setup` copies the network mode from the
   recompute arm for this reason — publishing a port on one arm only adds docker-proxy latency to
   every request on that arm, and the comparison breaks while the numbers still look fine.
7. **Logging off for performance runs.** cuFile `TRACE` and `nvidia_fs` debug depress throughput;
   keep diagnostic runs separate (`cat /sys/module/nvidia_fs/parameters/dbg_enabled` → 0), and
   confirm counters are on (`rw_stats_enabled` → 1) or attribution silently reports nothing.
   **A reboot turns the counters off again.** `rw_stats_enabled` is a module parameter, and on the
   reference host it was back at 0 after a reboot although the last run had counted reads. No
   launcher turns it on. Enable it before each session:

   ```bash
   echo 1 | sudo tee /sys/module/nvidia_fs/parameters/rw_stats_enabled
   ```

   `tier-check` reports it as a `FAIL` (T9) when it is off.

> These preconditions are the stack-specific way to satisfy the method-level requirements in
> [`../docs/CHECKLIST.md`](../docs/CHECKLIST.md). That document states *what must be true*; this
> one is *how it is true here*.

## What the launchers do to the host

The launchers start containers, empty tier directories and use `sudo`. On a shared server that
matters, so these are their defaults:

| Concern | Default | Change it with |
|---|---|---|
| Serving API | vLLM listens on `127.0.0.1` only — the API has no authentication. In a bridge network the container listens on all its interfaces and the host-side `--publish` is bound to `127.0.0.1` | `SERVE_HOST` — only on a network you control |
| Containers | Every container a launcher starts carries the label `kvprobe.owner=kv-hierarchy-probe`. A launcher removes only labelled containers; if a same-named container someone else started exists, it stops and prints the `docker rm -f` command for you to decide | — |
| Tier directories | Emptied before an arm starts. A directory is reset as root only when `GDS_MOUNT` is a mount point and the directory is not a symlink; the POSIX arm's `PROBE_POSIX_TIER_DIR` must be one directory under `/gds` | `PROBE_POSIX_TIER_DIR`, within that rule |
| Measurement client | The `.py` next to the launcher (or in `scripts/` for the `tools/` launchers), never a fixed path in `/tmp` | `CLIENT` |
| Evidence directory | Created if missing; refused if it is a symlink or not owned by you | `EVIDENCE` (g5 and the NIXL tools) |
| Host page cache | Never dropped by default (native GDS reads bypass it) | `PROBE_DROP_HOST_CACHE=1` for g2/g3, e.g. to study a compat-mode path |

**Privileges the native GDS arms need**, and why: `--device /dev/nvidia-fs*` (the cuFile driver),
`--cap-add IPC_LOCK` and `--ulimit memlock=-1` (pinned buffers), `--volume /run/udev:/run/udev:ro`
(cuFile reads the filesystem type from udev — [FAILURE-CATALOG §5](../docs/FAILURE-CATALOG.md)),
`--volume /etc/cufile.json:/etc/cufile.json:ro`, and `--ipc host` (vLLM shared memory). Nothing runs
`--privileged`. The POSIX arm evicts the tier's own pages per file; the host-wide page cache is dropped only
when you set `PROBE_DROP_HOST_CACHE=1` for the g2/g3 clients — a native GDS read does not go through it, so the default
leaves other users' cache alone.

## Re-validation before merging to `main`

The full procedure is in [`../docs/REVALIDATION.md`](../docs/REVALIDATION.md). In short: set
`IMAGE` and `GDS_MOUNT`, run each script's `setup`/`measure`/`collect`, and confirm with
`verify_against_baseline.py` that the fresh output matches an applicable baseline with the same
prompt profile, profile version, and prompt identity — latency within tolerance, and every gate
count (correctness, serving tier verification, tier purity) exactly equal. The former localized
baseline is not applicable to `transport/english-v1`; use the bundled English-v1 reference with a
matching summary and producer as described in [`../docs/REVALIDATION.md`](../docs/REVALIDATION.md).

## A note on language

Comments, console output, and measurement payloads are English. The default `transport` payload is
versioned as `english-v1`; changing its text creates another workload even when the builder lands
on the same cacheable-token target. The opt-in `retrieval` profile is also English by design.
