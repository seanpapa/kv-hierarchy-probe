# Prompt construction — causing and verifying KV offload

The probe does not create a KV payload by writing a file directly. It sends a deterministic
prompt through the serving endpoint, lets the model produce KV state, and then repeats the same
logical request after applying memory pressure. This document explains how the prompt controls
the amount and identity of KV data under test.

> A target token count is an experiment input. A lower-tier hit is a measured outcome, not a
> consequence that may be assumed from the prompt alone.

## 1. From a context target to expected KV bytes

The controlled scripts use three related values:

| Field | Meaning |
|---|---|
| `context_target` | Requested cacheable-prefix size. It must be a positive multiple of the configured chunk size. |
| `prompt_tokens` | Token count returned by the vLLM `/tokenize` request for the generated prompt text. |
| `cacheable_tokens` | Complete chunks represented by that count: `floor(prompt_tokens / chunk_tokens) * chunk_tokens`. |

The prompt builder changes the number of repeated sizing units and queries `/tokenize` until the
result lands in the requested chunk interval. With the reference configuration:

```text
chunk_tokens = 256
context_target = 4,096 tokens
cacheable_tokens = 16 chunks * 256 tokens
```

Expected KV bytes are derived separately from model geometry:

```text
bytes_per_token = 2 * layers * kv_heads * head_dim * dtype_bytes
expected_KV_bytes = cacheable_tokens * bytes_per_token
```

For Qwen2.5-32B in the recorded BF16 configuration, one 256-token chunk is 64 MiB, so 4,096
cacheable tokens represent 1,024 MiB. These values are model- and configuration-specific. Set
`PROBE_MIB_PER_CHUNK` for another geometry and record the geometry in the configuration
fingerprint; do not carry the 64 MiB value to another model by assumption.

Use [`kvprobe geometry`](GEOMETRY.md) to derive the exact value from a model `config.json` and the
runtime dtype, chunk size, and TP size. Select the per-device or TP-group value to match the scope
of the external counter; the command refuses layouts it cannot derive without guessing.

When `LMCACHE_SAVE_UNFULL_CHUNK=False`, the trailing partial chunk is not part of the expected
stored payload. That is why the probe uses `cacheable_tokens`, rather than the unaligned prompt
count, to calculate expected device bytes.

## 2. How a prompt produces a lower-tier retrieval

The controlled sequence is:

```text
deterministic prompt
  -> first request creates KV and stores complete chunks
  -> filler requests apply upper-tier pressure
  -> targeted POSIX page-cache eviction, when testing buffered POSIX
  -> byte-identical target request looks up the same cache keys
  -> lower-tier read is checked with path-specific counters
  -> returned text is checked for correctness
```

The first request is the store phase. Repeating the target prompt is necessary for a hit, but it
is not sufficient evidence that the lower tier served the request. The measurement passes only
when the expected output is correct and the external counter for the path under test reports the
expected byte movement. LMCache `Stored` and `Retrieved` log lines are supporting evidence; they
do not replace device-path evidence.

The recompute arm receives the same prompt shape but runs without a cache retrieval. This keeps
the input work matched while establishing the floor against which a cache tier competes.

## 3. Prompt identity and the `unique` value

Each target and filler carries a stable experiment identifier such as `TARGET-4096` or
`FILLER-3`. It is part of the prompt text, not an out-of-band label. Its purposes are to:

- prevent accidental reuse of an entry created for a different experimental case;
- make store and retrieval requests for one case byte-identical;
- make the generated prompt reproducible from the recorded parameters; and
- separate filler entries from the target entry.

LMCache keys are prefix-dependent. Changing an early identifier is therefore intended to create
a different key chain even when later sizing text is repeated. The probe still verifies the
result from observed store/retrieve behavior and external bytes rather than treating the string
change itself as proof of cache identity.

The controlled client records `prompt_sha256` with the profile parameters. Runs may be compared
only when prompt profile, seed, needle position, target sizes, and the other controlled inputs
match.

## 4. Filler pressure: `unique` versus `reuse`

Filler requests are not measured as target results. They exist to push the target out of a
resident upper tier so that its next use must reach the tier under test.

`FILLER_KEY_POLICY=unique` adds a new identifier on every sample. This is the historical
native/static procedure and prevents a repeated filler from becoming a hit that applies no new
pressure.

`FILLER_KEY_POLICY=reuse` reuses a bounded filler set. Use this policy for **every arm** in the
static/dynamic NIXL mode matrix: dynamic FILE storage does not have the static backend's bounded
slot eviction, so unique filler keys would grow the backing directory on every sample. Reusing
fillers changes the workload and is therefore not interchangeable with a historical `unique`
run. The matched comparator rejects different filler policies.

Filler count and filler size must exceed the resident capacity that is meant to be displaced.
Even then, pressure is not proof of eviction. A valid target sample still needs the tier-specific
read-byte and correctness checks.

## 5. Prompt profiles

The controlled experiment provides two profiles.

### `transport`

This is the default portable movement workload. It repeats a deterministic English sizing
sentence and asks the model to return the fixed canary `STAGE1_D6_OK`. Evidence records the
profile version as `english-v1`. Its purpose is controlled KV movement and output correctness,
not semantic long-context retrieval quality.

The English payload replaced an earlier localized payload and is intentionally a
new workload. Compare it only with runs using the same workload profile;
matching `context_target` or `cacheable_tokens` alone is insufficient.

### `retrieval`

This profile creates deterministic synthetic records and asks for the six-digit verification code
from a selected record. `PROMPT_SEED` fixes record contents, while `NEEDLE_POSITION` selects an
early, middle, or late record. It tests semantic retrieval at different positions while retaining
a compact correctness answer.

A retrieval-profile run is a different workload. Keep it separate from a
transport-profile baseline, and interpret a position comparison as a
storage-path comparison only when all other inputs are held fixed.

## 6. Token-count boundary

The current builder calls vLLM `/tokenize` with the generated prompt text, while the measured
request uses the chat-completions API. A model's chat template can add control tokens around that
text. Therefore:

- `prompt_tokens` is the count returned by the builder's `/tokenize` call;
- it must not be described as an unconditional trace of the engine's final templated token stream;
- the aligned value is the probe's sizing convention; and
- actual LMCache `Stored`/`Retrieved` token counts and expected external byte movement must agree
  before a run supports a performance claim.

Keep enough margin below the next chunk boundary that template overhead cannot silently move the
request into another expected chunk count. If the engine-reported token count or device bytes do
not match the intended geometry, stop the run and correct the sizing procedure rather than
changing the expected value after measurement.

## 7. What the evidence should retain

For each measured prompt shape, retain at least:

- prompt profile and profile arguments;
- seed and needle position where applicable;
- `context_target`, `prompt_tokens`, and `cacheable_tokens`;
- `prompt_sha256`;
- chunk size, dtype, and bytes per chunk;
- filler count, filler token size, and filler-key policy;
- LMCache store/retrieve lines;
- path-specific external counter deltas; and
- output-correctness result.

These fields let a reviewer distinguish three questions that must not be collapsed: what prompt
was sent, how much KV the configuration predicts, and which tier actually served the repeated
request.

## 8. Source of truth

- [`METHOD.md`](METHOD.md) defines the gates that decide whether the resulting measurement may
  support a claim.
- [`../scripts/README.md`](../scripts/README.md) lists command-line controls and execution
  procedures.
- [`../scripts/g2_context_sweep.py`](../scripts/g2_context_sweep.py) implements the historical
  context sweep.
- [`../scripts/g3_admission_cost.py`](../scripts/g3_admission_cost.py) creates fresh admission
  entries and measures store plus later retrieval.
- [`../scripts/g5_controlled_kv_experiment.py`](../scripts/g5_controlled_kv_experiment.py)
  implements the two prompt profiles, exact sizing loop, expected answer, and evidence metadata.
