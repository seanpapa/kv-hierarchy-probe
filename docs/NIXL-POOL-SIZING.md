# NIXL static-pool sizing and eviction probe

## Quick start

Two commands tell you which pool sizes to test for the working set you intend to keep. Neither
starts a serving workload.

```bash
# 1. Exact KV bytes per chunk, from the model source and the live runtime settings
#    (--model accepts a public Hugging Face model ID or a local model directory/config file)
python3 -m kvprobe geometry \
  --model Qwen/Qwen2.5-32B-Instruct \
  --chunk-tokens 256 --output geometry.json

# 2. Pool sizes that bracket the live set: three contexts of 4,096 tokens must stay retrievable
python3 -m kvprobe pool-plan --sweep --geometry geometry.json \
  --concurrent-contexts 3 --context-tokens 4096
```

Both commands print a summary by default. `--output` also saves the JSON; `--json` sends the JSON
to stdout for a script and moves the summary to stderr. For a 64 MiB chunk the sweep summary is:

```text
kvprobe pool-plan — derived result
  Live set: 3 contexts × 16 chunks (4,096 tokens / 256 per chunk) = 48 chunks
    Assumption: distinct chunks per context; no prefix shared between contexts
  Intended live set: 48 chunks × 64 MiB = 3 GiB (12,288 tokens)
  Pool 24 slots = 1.5 GiB (6,144 tokens): headroom -24 (below the live set), forced evictions >= 24
  Pool 48 slots = 3 GiB (12,288 tokens): headroom 0 (at the live set), forced evictions >= 0
  Pool 96 slots = 6 GiB (24,576 tokens): headroom 48 (above the live set), forced evictions >= 0
  Pool 192 slots = 12 GiB (49,152 tokens): headroom 144 (above the live set), forced evictions >= 0
  Sweep values bracket the live set; none is a recommendation.
  Bytes per chunk from: geometry:all_scopes_equal
  Capacity arithmetic only; not a performance prediction.
```

To check one pool size instead, give `--pool-slots 16` in place of `--sweep`.

- **The model source.** `--model` accepts a public Hugging Face model ID, a local model directory,
  or a local Hugging Face-style `config.json`; model weights are not read or downloaded. For a
  public model ID, `kvprobe geometry` fetches only `config.json` and records the resolved commit and
  content hash. Use the model and revision the serving process actually loads, not a similarly
  named model. If the model exists only inside a container, copy just its config to the host with
  `docker cp <serving-container>:/actual/model/path/config.json ./model-config.json`.
  `kvprobe geometry` reads `num_hidden_layers`, `num_attention_heads`, `num_key_value_heads`
  (defaults to the attention-head count when absent), `head_dim` or else `hidden_size`, and
  `torch_dtype` unless you pass `--dtype`. It refuses MLA models (`kv_lora_rank` present) and
  pipeline parallelism above one. `--chunk-tokens`, `--dtype` and the TP size are runtime
  settings that the file does not contain: take them from the serving command, because a copied
  config does not verify them. Details: [Choose the model source](GEOMETRY.md#five-minute-path).
  With no config file at hand, `--layers --kv-heads --head-dim --dtype` give the same bytes
  ([Without a model file](GEOMETRY.md#reference--without-a-model-file)).
- **Bytes per chunk.** `--geometry` reads the file written by `kvprobe geometry --output`. With
  tensor parallelism above one the per-rank and TP-group values differ, so choose
  `--geometry-scope per_tp_rank` or `tp_group_aggregate`; the command refuses to guess. Use
  `--bytes-per-chunk` instead to supply a number you measured yourself.
- **Tokens.** When the chunk size is known, from `--geometry` or `--chunk-tokens` (which also works
  with `--bytes-per-chunk`), the output states every capacity in tokens as well as bytes. It prints
  no token figure when the chunk size is unknown, and rejects a `--chunk-tokens` that disagrees
  with the geometry file.
- **Live chunks.** Either give the peak number of distinct chunks that must stay retrievable at
  once (`--required-live-chunks`, see [Capacity model](#capacity-model)) or let the command derive
  it from `--concurrent-contexts` and `--context-tokens`. The chunk size comes from `--geometry`,
  or `--chunk-tokens`. When the context length is not a multiple of the chunk size, say whether a
  trailing partial chunk is stored with `--partial-chunk drop` or `keep`; the command does not
  assume the backend's behavior. The output records every input it used. Whether slots are counted
  per rank or per TP group is yours to state; the calculator cannot check it.
- **When contexts share a prefix, do not use the derivation.** It assumes every context needs its
  own distinct chunks, so it counts a shared prefix once per context and overstates the live set.
  For a workload where contexts share a prefix, work out the distinct chunks yourself and pass
  `--required-live-chunks`. As an illustration only, if the backend keys a chunk by its prefix so
  identical chunks are stored once, then 3 contexts of 4,096 tokens that share a 2,048-token prefix
  (256-token chunks) hold `2,048 / 256 + 3 × (4,096 − 2,048) / 256 = 8 + 24 = 32` distinct chunks,
  not the 48 the derivation reports. Whether a backend shares chunks that way, and at what
  granularity, is a property you must confirm for your build; the calculator does not know it.
  Record how you derived the number next to the result.
- **Sweep values.** `--sweep` plans `ceil(live/2)`, `live`, `2 × live` and `4 × live` slots, so one
  value sits below the live set, one at it and two above (see [Sweep design](#sweep-design)). They
  span the region a sweep must cover. None is a recommendation, and a live set of one chunk has no
  value below it.
- **Static pools only.** A dynamic FILE backend creates files on demand and has no pool to size or
  evict from; see [NIXL-MODE-MATRIX.md](NIXL-MODE-MATRIX.md).

The rest of this page defines what a pool size does and does not mean, what to record, and how to
run the sweep.

## Reading the result

The figures below use the Quick start example: Qwen2.5-32B-Instruct with 64 layers, 8 KV heads,
`head_dim` 128 and bf16. They are arithmetic for that geometry, not measurements.

**Bytes per token drive everything.** `bytes_per_token = 2 × layers × KV heads × head_dim × dtype
bytes` (derivation in [GEOMETRY.md](GEOMETRY.md)). Here that is `2 × 64 × 8 × 128 × 2 = 262,144`
bytes, 256 KiB per token, so 1 GiB holds 4,096 tokens and one 128K-token (131,072) context needs
32 GiB. The KV head count, not the attention head count, sets this: the same model storing 40 KV
heads instead of 8 would need five times as much per token.

**Slots are tokens.** A pool holds `slots × chunk_tokens` tokens. For this geometry:

| Pool slots | Logical capacity | Tokens held |
|---:|---:|---:|
| 24 | 1.5 GiB | 6,144 |
| 48 | 3 GiB | 12,288 |
| 64 | 4 GiB | 16,384 |
| 96 | 6 GiB | 24,576 |
| 192 | 12 GiB | 49,152 |

The 64-slot value that the public examples cited in the claim boundary below use therefore holds 16,384 tokens, four
contexts of 4,096, for this geometry. Judge any slot count by the tokens it holds. One 128K-token
context needs 512 slots, and the largest sweep value above holds less than half of one.

**Slots are cheap next to a device.** The slot count is bounded by the free space on the
filesystem, `free bytes / slot_bytes`. If a terabyte-class volume had 1 TiB free, it would hold
16,384 slots of 64 MiB, about 4.2 million tokens; the 12 GiB sweep maximum is about 1.2% of that.
Whether the pool is large enough is usually a configuration question, not a device limit. Check
the free space you actually have and share.

**The pool only matters beyond the tier above it.** A lower tier is read for what the tier above
cannot hold. Compare the live set in bytes with the upper tier's capacity (recorded as
`upper_tier_capacity_bytes` in the evidence configuration). If the live set fits above, a pool
sized for it may never be read, which Gate 4 reports; if it does not, roughly the excess is what
the pool must hold. See [Capacity justification in METHOD.md](METHOD.md#gate-4--capacity-justification).

**What to look for in a sweep (expectations to test, not results).** Pools below the live set must
evict and reuse slots; at the boundary any further entry evicts; above it, extra headroom only
changes whether eviction happens. Because a slot count is not I/O parallelism (see the claim
boundary below), expect eviction and reuse counts to change at the boundary. A gradual change in
latency across cells that all hold the live set would point to a factor other than pool size,
which the sweep holds constant.

## Claim boundary

`nixl_pool_size` is an LMCache static-storage setting: it determines how many file or object
descriptors are created and reused. It is not the NIXL `GDS_MT` thread count, an NVMe queue-depth
setting, or a value that NVIDIA hardware selects automatically.

No public NVIDIA or LMCache source establishes one optimal pool size for every workload. NVIDIA's
CloudAI LMCache configuration and LMCache's public configuration example both use 64 slots, but
neither presents 64 as a benchmark-derived recommendation. NIXLBench exposes a separate
`gds_mt_num_threads` control for the GDS_MT plug-in. NVIDIA's GDS guide also states that one cuFile
handle can be shared by multiple threads. Increasing the LMCache file count therefore must not be
described as increasing I/O parallelism by itself.

Primary references:

- [NVIDIA CloudAI LMCache arguments](https://docs.nvidia.com/networking/display/cloudai161/_modules/cloudai/workloads/ai_dynamo/ai_dynamo.html)
- [NVIDIA GPUDirect Storage best practices](https://docs.nvidia.com/gpudirect-storage/best-practices-guide/)
- [NIXLBench storage and GDS_MT options](https://github.com/ai-dynamo/nixl/blob/d5c127e558fd4ddb12e47127243057339c2c2b72/benchmark/nixlbench/README.md)
- [LMCache configuration reference](https://github.com/LMCache/LMCache/blob/1d8716508371ab5c95de0d963160e1e64de467ef/docs/source/api_reference/configurations.rst)

The probe may establish an operating range for one measured configuration. It must not turn a
sample value, framework default, or single sweep winner into a vendor recommendation.

## Capacity model

For a fixed KV geometry and one backend instance:

```text
logical pool capacity = pool slots × exact bytes per chunk
minimum slots         = peak chunks intended to remain live simultaneously
slot headroom         = pool slots − minimum slots
```

If no entry is explicitly removed before the peak, a negative headroom gives a lower bound on
forced evictions and slot reuses. This is capacity arithmetic, not a performance prediction. The
`classification` field names only where the capacity stands relative to the live set
(`below_live_set`, `at_live_set` or `above_live_set`); it does not rate the value.

The calculator adds no undocumented safety factor and calls no value optimal.

`required_live_chunks` is not total writes over an arbitrary interval. It is the maximum number of
distinct chunks the workload expects to remain retrievable at the same time. A churn experiment
may deliberately set that expectation above the pool size; a production-capacity experiment must
derive it from the request lifecycle and retention objective.

`--bytes-per-chunk` is the exact stored bytes of one slot. The evidence key `slot_bytes` (see
[Evidence-version direction](#evidence-version-direction)) names the same quantity, so record one
number under both names.

## Required observations

Preserve these facts for every arm:

| Category | Required observation |
|---|---|
| Configuration | backend and mode, `nixl_pool_size`, exact chunk bytes, direct-I/O setting, buffer device, image and NIXL/LMCache revisions |
| Intended demand | peak live chunks and bytes, how the peak was derived, request concurrency and retention interval |
| Pool realization | slot-file count before the first PUT, registered/open descriptor count, logical bytes after writes |
| Lifecycle | successful PUTs, explicit removals, evictions, slot reuses, retrieval hits and misses |
| Integrity | retrieved-key identity and payload correctness after reuse; an evicted key must not resolve to a newer occupant |
| Device path | target-device-filtered bytes where available; buffered and direct I/O are separate treatments |
| Cost | startup/registration time, open FDs, steady-state PUT/GET latency, TTFT, device bandwidth and command latency |

File size alone is not current occupancy: a reused static slot may retain old allocated blocks after
its key mapping changes. Framework logs alone are also insufficient to prove device I/O.

## Sweep design

1. Derive the peak live working set before selecting values (`--concurrent-contexts` records the
   derivation, or state your own).
2. Include at least one value below the working set, one at the boundary, and two above it;
   `pool-plan --sweep` lists such a bracket.
3. Hold model, TP, chunk geometry, request trace, storage device, direct-I/O mode, GDS_MT thread
   count, software build, and eviction policy constant.
4. Restart the backend between cells and verify the realized slot/FD count before sending traffic.
5. Run a forced-reuse integrity cell separately from latency cells.
6. Report the smallest pool that satisfies the declared retention objective and stays inside the
   validated operating envelope. Call it a measured candidate, not a universal recommendation.

`pool-plan --sweep` gives one bracket. A power-of-two list such as `16, 64, 128, 256, 512` is also
reasonable, but only when it brackets the derived working set. Neither list is a recommendation.

## Related material

| I want to… | Read or run |
|---|---|
| Compare static and dynamic NIXL file modes | [NIXL-MODE-MATRIX.md](NIXL-MODE-MATRIX.md), `scripts/tools/nixl_mode_matrix.sh` |
| Reproduce the matched native vs NIXL static `GDS_MT` run, including pool-file counts after init | [NIXL-STATIC-GDS-MATCHED.md](NIXL-STATIC-GDS-MATCHED.md), `scripts/tools/nixl_matched.sh` |
| See how filler and store requests create the pressure that fills a pool | [PROMPT-CONSTRUCTION.md](PROMPT-CONSTRUCTION.md) |
| Fingerprint the pool controls with the rest of the configuration | [CONFIGURATION.md](CONFIGURATION.md) |
| Derive exact bytes per chunk | [GEOMETRY.md](GEOMETRY.md) |

## Evidence-version direction

Evidence v0.3 can fingerprint these controls today under each arm's open-ended
`eviction_parameters` object. Record at least:

```json
{
  "pool_mode": "static",
  "nixl_pool_size": 16,
  "slot_bytes": 67108864,
  "required_live_chunks": 36,
  "selection_intent": "forced_eviction"
}
```

The next evidence version should promote pool mode, configured slots, realized slots, and exact
slot bytes to typed fields. Observed evictions, reuses, integrity results, and peak live occupancy
belong in measurement evidence rather than configuration identity. Until that contract exists,
the calculator is planning evidence only and does not change a five-gate verdict.
