# Derive model KV-cache geometry

`kvprobe geometry` resolves a Hugging Face-style model `config.json` and combines it with explicit
serving-runtime settings to derive exact KV bytes per token and per cache chunk. Use it before
writing `kv_geometry.bytes_per_chunk` into an experiment configuration. Model weights are never
needed.

The model source can be a public Hugging Face model ID, a local model directory,
or a copied `config.json`. The command derives geometry when that configuration
exposes a supported standard-attention layout. Unsupported layouts are refused
rather than approximated; see [fail-closed boundaries](#fail-closed-boundaries).

## Five-minute path

The command below is a worked example using Qwen2.5-32B. Replace
`MODEL_SOURCE` with your public model ID or local model directory, replace the
runtime values with those used by the serving process, then retain
`geometry.json`.

```bash
MODEL_SOURCE="Qwen/Qwen2.5-32B-Instruct"  # Example; replace for your run

python3 -m kvprobe geometry \
  --model "$MODEL_SOURCE" \
  --chunk-tokens 256 \
  --dtype bfloat16 \
  --tensor-parallel-size 1 \
  --output geometry.json
```

For this remote model-ID path:

- the machine needs network access to Hugging Face;
- the repository and `config.json` must be public and ungated;
- only `config.json` is fetched — no model weights or local model installation
  are required;
- for a gated, private or offline model, provide an approved local
  `config.json` with `--model-config` instead;
- an unavailable model/revision or unsupported layout exits with status 2 and
  writes no geometry result.

Before using the result, verify these four items in the printed summary:

- model source and resolved revision;
- runtime dtype and chunk size;
- TP/PP settings and rank placement;
- whether the external counter covers one device or the full TP group.

Use `selection.per_device_counter_bytes_per_chunk` for a per-device counter, or
`selection.tp_group_counter_bytes_per_chunk` only for a counter covering the
whole TP group. If those choices are clear, return to [Gate 1](GATE1.md#fast-path-produce-four-reviewable-items).
The remaining sections are reference and troubleshooting material.

## Reference — public model source

For a public Hugging Face model, name the model directly:

```bash
python3 -m kvprobe geometry \
  --model ORG/MODEL_NAME \
  --chunk-tokens 256 \
  --tensor-parallel-size 1 \
  --output geometry.json
```

This downloads only `config.json`, not model weights. A branch or tag defaults to `main`; the
result records both that requested revision and the immutable commit returned by the Hub. Pin a
known revision when preparing a repeatable run:

```bash
python3 -m kvprobe geometry \
  --model ORG/MODEL_NAME \
  --revision <full-commit-sha> \
  --chunk-tokens 256 \
  --tensor-parallel-size 1 \
  --output geometry.json
```

The output also records the downloaded config's SHA-256 and `weights_downloaded: false`. Gated or
private models are not authenticated by this command: obtain their approved `config.json` and use
`--model-config` instead.

For a model already present on the machine, pass its directory. The CLI reads only the
`config.json` inside it and records the directory's final name rather than the full local path:

```bash
python3 -m kvprobe geometry \
  --model /path/to/local/model-directory \
  --chunk-tokens 256 \
  --dtype bfloat16 \
  --tensor-parallel-size 1 \
  --output geometry.json
```

Use the model directory and runtime settings from the serving process. `chunk_tokens`, dtype, TP,
and PP are runtime choices rather than reliable model properties. If `--dtype` is omitted, the
command uses `dtype` or the older `torch_dtype` from `config.json` and records that source; an
explicit runtime override is preferable when the serving engine changes it.

## Reference — other model sources

`--model` is the normal path:

- an existing directory means `<directory>/config.json`;
- an existing file means that exact local config file;
- another value is treated as a public Hugging Face model ID and resolves only `config.json`.

`--model-config` remains available when a deployment exposes only a copied config, when access to
the Hub is unavailable, or when a gated/private model requires separate authentication. The path
must be readable in the shell running `kvprobe`. If it exists only inside a container, use the
corresponding host mount or copy only that file:

```bash
docker cp <serving-container>:/actual/model/path/config.json ./model-config.json

python3 -m kvprobe geometry \
  --model-config ./model-config.json \
  --chunk-tokens 256 \
  --dtype bfloat16 \
  --tensor-parallel-size 1 \
  --output geometry.json
```

A missing local file, invalid model ID, inaccessible Hub response, unresolvable commit, or invalid
JSON returns exit status 2 and writes no geometry result. A supplied `--revision` is accepted only
for a Hugging Face model ID.

## Reference — without a model file

When you know the model's numbers but have no `config.json` at hand, enter them directly:

```bash
python3 -m kvprobe geometry --layers 64 --kv-heads 8 --head-dim 128 --dtype bfloat16 --chunk-tokens 256
```

`--layers`, `--kv-heads` (the global key-value head count) and `--head-dim` replace a model source;
all three and `--dtype` are required because there is no file to read. The result records
`"input_source": "dimensions"` and omits `num_attention_heads`, which these numbers do not need.
Prefer `--model` for a real run: its resolved revision and config hash establish which model
configuration the bytes belong to.

The command reads `num_hidden_layers`, `num_attention_heads`, `num_key_value_heads`, and either
`head_dim` or `hidden_size` from the resolved config.

## Worked output — Qwen2.5-32B reference configuration

A successful command prints a concise explanation. `--output geometry.json` also preserves the
machine-readable result. The following output is a **worked example only**, using
the recorded Qwen2.5-32B BF16, chunk=256, TP=1 configuration. It is not a
default or a restriction on supported models:

```text
kvprobe geometry — derived result
  Saved JSON: geometry.json
  Model config: Qwen/Qwen2.5-32B-Instruct@<resolved-commit> (Hugging Face config.json only; no weights)
  Config SHA-256: <config-sha256>
  Runtime inputs: bfloat16 (argument), chunk=256 tokens, TP=1, PP=1
  Full model: 262,144 bytes/token; 67,108,864 bytes/chunk (64 MiB)
  Per TP rank: 262,144 bytes/token; 67,108,864 bytes/chunk (64 MiB)
  TP-group aggregate: 262,144 bytes/token; 67,108,864 bytes/chunk (64 MiB)
  Counter selection (bytes/chunk): per-device=67,108,864; TP-group=67,108,864
  Derived from supplied config/settings; live serving settings and rank placement were not observed.
  Next: verify model revision, dtype, chunk size and TP/PP against the serving process.
  Select bytes_per_chunk matching the counter scope and record it in your experiment configuration.
  This prepares expected bytes for Gate 1; it does not establish tier service or a gate PASS.
  See docs/GEOMETRY.md and docs/CONFIGURATION.md.
```

Here, one token's logical KV representation is 256 KiB and a 256-token chunk
is 64 MiB. At TP=1 the three scopes agree. At TP>1 they can differ; select the
scope that matches the external counter rather than assuming these values are
interchangeable. The calculation describes KV payload, not all allocator,
metadata, filesystem or device-I/O overhead.

Before copying a selected value into `kv_geometry.bytes_per_chunk`, compare the
model revision and runtime inputs with the serving process and confirm rank
placement and counter coverage. Follow [configuration identity](CONFIGURATION.md)
for the parameter file, then [Serving Tier Verification](CHECKLIST.md#gate-1--serving-tier-verification--does-this-measure-the-tier-at-all)
for the actual byte/correctness/fallback evidence. Geometry alone is preparation,
not a gate verdict. Other task choices are in
[What you can do next](FIRST-VISIT.md#6-what-you-can-do-next).

By default the command prints the summary only. `--output` also saves the JSON to a file. To send
the JSON to standard output instead — for a script — add `--json`; the summary then goes to
standard error:

```bash
python3 -m kvprobe geometry \
  --model ORG/MODEL_NAME \
  --chunk-tokens 256 \
  --dtype bfloat16 \
  --tensor-parallel-size 1 --json > geometry.json
```

`kvprobe pool-plan` follows the same rule.

## Formula and output scopes

For standard multi-head or grouped-query attention:

```text
bytes_per_token = 2 * layers * kv_heads * head_dim * dtype_bytes
bytes_per_chunk = bytes_per_token * chunk_tokens
```

The factor two is K plus V. The output keeps three scopes separate:

| Output | Meaning |
|---|---|
| `geometry.full_model` | One logical full-model KV representation before TP placement |
| `geometry.per_tp_rank` | Bytes owned by one TP rank under the derived head placement |
| `geometry.tp_group_aggregate` | Sum across all TP ranks, including replication if present |

Use `selection.per_device_counter_bytes_per_chunk` with a counter scoped to one accelerator. Use
`selection.tp_group_counter_bytes_per_chunk` only when the observed counter really covers the
whole TP group. The command prints both because choosing the wrong counter scope can make a correct
transfer look partial or make duplicated data look like excess I/O. The corresponding MiB values
are also emitted for the current reference scripts' integer `PROBE_MIB_PER_CHUNK` input. A MiB
field is `null` when the exact byte count is not an integer number of MiB; do not round it to fit
the current script. Retain the byte value as the configuration fingerprint's exact identity.

The reported KV-head placement is derived from the head counts and requested TP size, not observed
from the serving workers. Confirm it against the engine's rank placement before selecting a counter
scope.

For the recorded Qwen2.5-32B BF16 TP=1 configuration, the result is 262,144 bytes per token and
67,108,864 bytes per 256-token chunk. At TP=2 with evenly sharded KV heads, each rank carries
33,554,432 bytes per chunk while the TP-group aggregate remains 67,108,864 bytes.

## Fail-closed boundaries

The command deliberately refuses cases where the standard formula would invent a result:

- MLA configurations identified by `kv_lora_rank`;
- pipeline parallelism greater than one without exact layer placement;
- TP/KV-head combinations that are neither evenly sharded nor evenly replicated;
- unknown dtypes or model fields that are absent, non-integral, or inconsistent.

It does not connect to the serving endpoint and cannot prove that the live runtime used the supplied
dtype, chunk size, TP size, or model file. Preserve `geometry.json` with the run, compare the inputs
with the live command line and engine logs, and record the selected exact byte value in the
configuration fingerprint. This converts geometry from an undocumented constant into a reviewable
derivation; it does not turn supplied runtime settings into observed facts.
