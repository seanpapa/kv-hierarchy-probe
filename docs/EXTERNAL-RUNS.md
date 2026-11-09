# External runs — stacks without reference scripts

The reference scripts drive vLLM + LMCache. Another serving stack — Dynamo KVBM, TensorRT-LLM,
SGLang, an in-house cache — can still be judged by the same gates if its harness writes a
**context run** in the shape below and hands it to `kvprobe ingest`. The adapter does not know
which framework produced the run; the arm's `backend` is free text.

This page covers Gate 1 and Gate 2 input (context runs). Gates 3–5 still need the run shapes
listed in [EVIDENCE.md](EVIDENCE.md#producing-evidence-from-a-run).

## Run shape

```json
{
  "mode": "context",
  "arm": "block-tier",
  "arguments": {"contexts": [4096], "samples": 3, "warmup": 1},
  "result": {
    "4096": {
      "rows": [
        {"phase": "measure", "index": 1, "correct": true, "tier_ok": true,
         "nvfs_read_mib": 1024, "cacheable_tokens": 4096, "ttft_ms": 180.0}
      ]
    }
  }
}
```

| Field | Meaning | Rule |
|---|---|---|
| `arm` | Arm name | Must be declared in the configuration's `arms` |
| `phase` | `warmup` or `measure` | Only `measure` rows become requests |
| `correct` | Output matched the reference | From the harness's own check, not the framework |
| `cacheable_tokens` | Tokens the tier should hold for this request | Whole blocks only: `floor(prompt_tokens / block_tokens) * block_tokens` |
| `nvfs_read_mib` or `cgroup_read_bytes` | Bytes read around this one request | The field the arm's `attribution_provider` selects; take a delta around each request at concurrency 1 |
| `tier_ok` | The request met the tier's byte expectation | **Derive it from the counter delta, never from the framework's log** |
| `ttft_ms` | Time to first token | Needed for Gate 2 |

The configuration's `kv_geometry` sets the expected bytes. For a stack that stores fixed-size
blocks rather than chunks, `chunk_tokens` is the block's token count and `bytes_per_chunk` its
byte size. Expected bytes come from this geometry, never from the observation.

## What a phantom tier looks like

A phantom tier is configured and reported as a hit, but did not serve the request. The fixtures in
[`../tests/fixtures/external/block-tier/`](../tests/fixtures/external/block-tier/) are synthetic runs of a
generic disk tier that stores fixed-size blocks (block 16 tokens × 4 MiB, 4,096 tokens → 1 GiB per
request). They describe no particular framework; they fix each outcome of the contract:

| Run | What happened | Outcome |
|---|---|---|
| `served.json` | Every request read 1 GiB from the tier | Gate 1 `UNPROVEN` (host-wide counter, not filtered to the tier's device); reason shows `3/3 … match expected bytes; 0/3 moved no bytes` |
| `phantom-no-writes.json` | Tier never written; requests hit the host tier; counter-derived `tier_ok` false | `ingest` refuses: no bytes and a failed tier gate cannot separate an upper-tier hit from a recompute |
| `phantom-framework-hit.json` | Harness took `tier_ok` from the framework log; device read nothing | Gate 1 `FAIL`; reason shows `3/3 moved no bytes from the tier` |
| `partial.json` | Half of each request came from the tier | Gate 1 `FAIL`; reason shows `0/3 … match expected bytes` |
| `second-use-admission.json` | Tier stores a block only on its second use: the warm-up re-request read nothing and was recomputed; measured requests read 1 GiB | Gate 1 `UNPROVEN` (unfiltered counter); reason shows `3/3 … match expected bytes` and a note `1/1 warm-up requests moved no bytes` |

The byte counts in the Gate 1 reason are reported apart from whether the counter is filtered. A
shortfall or zero on any counter is a measured `FAIL`; matching bytes on an unfiltered counter only
add support, so the run stays `UNPROVEN`.

### Reaching Gate 1 `PASS`

Record a counter filtered to the tier's device for every request. On Linux the cgroup v2 `io.stat`
counter does this without extra drivers or tracing:

- set the arm's `attribution_provider` to `cgroup_blk` and record each request's delta as
  `cgroup_read_bytes`, read from the `io.stat` line of the tier's device only;
- record the device on the run as `cgroup_io_device` (`MAJOR:MINOR`) and its scope as
  `cgroup_io_device_scope`: `whole_disk`, or `widened_to_parent_disk` for a tier on a partition.

`ingest` then marks the request target-device filtered, and Gate 1 passes when every request is
correct and every target-tier request is within tolerance of the expected bytes. The reason shows the
grade, `(corroborated)` for a counter delta. A run without the device fields, or with another scope,
stays `UNPROVEN`. `served.json` with these fields is pinned as a `PASS` in
[`../tests/test_gate1_device_filter.py`](../tests/test_gate1_device_filter.py). The reference client
records both fields for every cache arm.

The producer/operator must verify that this device actually backs the configured cache
path and that the counter reader selected its `io.stat` line. `ingest` trusts the recorded
device/scope rather than inspecting the live mount. Retain that mapping and reader provenance,
and check competing I/O, especially when the scope includes other partitions; see
[device mapping responsibilities](EVIDENCE.md#producing-evidence-from-a-run).

Warm-up requests stay out of the verdict, but ingest counts those that moved no bytes and the
report adds a note to the Gate 1 reason. A tier whose admission policy stores a block only on its
second use (Dynamo KVBM's default host-to-disk frequency filter is one) misses the first re-request;
with the default warm-up that row is a warm-up, and without the note it would vanish. To judge that
first reuse instead of noting it, run with `--warmup 0`: the zero-byte row is then measured and
ingest refuses the run.

## Boundary

- The tests fix the contract with synthetic runs. No measurement from another serving stack ships in
  this repository.
- A store-side check (did the tier receive writes) is not part of Gate 1. A tier that was never
  written shows up here only through the retrieve-side bytes.
