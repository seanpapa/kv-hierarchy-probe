# Versioning

This repository versions two kinds of thing separately, and they change independently:

- the **package** — the installable `kv-hierarchy-probe`, versioned per release;
- each **data contract** — a JSON or CSV format that a command writes or reads, versioned on its own.

A `0.3.0` in one contract says nothing about a `0.3.0` in another. The product terms **V1 candidate**
and **V2** name a claim scope, not a number in any file; see [PROFILES.md](PROFILES.md).

## Rules

| Rule | |
|---|---|
| Format | `MAJOR.MINOR.PATCH`. The package follows PEP 440 (`0.1.0` is the first public release). |
| Before 1.0 | An **incompatible** change bumps MINOR (`0.2.x` → `0.3.0`). A compatible addition or a correction bumps PATCH. |
| From 1.0 | Semantic versioning: incompatible → MAJOR, compatible addition → MINOR, correction → PATCH. |
| `-experimental` | Marks a contract whose producer has not passed acceptance. Removing the suffix is itself a recorded change. |
| Readers | Every reader states the versions it accepts and refuses others with a message naming the version. |
| Dropping a version | Recorded in the history below, in the same change that drops it. |
| One registry | The table below is the only list of version fields. Other documents link here instead of restating it. |
| Checked | `tests/test_versioning.py` compares this table with the code: every version field the code writes must be listed, and the versions written must match. |
| Releases | A release bumps the package version and lists the contract versions it ships. |

## Registry

| Field | Written | Also read | What it versions | Defined in |
|---|---|---|---|---|
| `version` | `0.1.0` | — | The package | `pyproject.toml` |
| `schema_version` | `0.3.1` | `0.3.0`, `0.3.1` | The evidence document that `report` judges | `kvprobe/ingest.py`, `kvprobe/report.py`, [EVIDENCE.md](EVIDENCE.md#versioned-contracts) |
| `assessment_version` | `0.6.2` | — | The JSON that `report` emits | `kvprobe/report.py` (`ASSESSMENT_VERSION`) |
| `normalized_by.version` | `0.4.2` | — | The ingest adapter recorded in evidence provenance | `kvprobe/ingest.py` (`ADAPTER_VERSION`) |
| `manifest_version` | `0.2.1` | — | The `doctor` capability manifest | `kvprobe/doctor.py` |
| `tier_check_version` | `0.1.0` | — | The `tier-check` result: read-only host conditions for a local PCIe NVMe run; each check records `required_for_ready`, and a required `SKIP` blocks readiness. Not Gate 1 evidence | `kvprobe/tier_check.py`, `schemas/tier-check-v0.1.0.schema.json` |
| `geometry_version` | `0.1.1` | — | The `geometry` output | `kvprobe/geometry.py` |
| `trace_plan_version` | `0.1.0` | — | The `gate1 plan` output | `kvprobe/nvme_plan.py` |
| `collector_version` | `0.3.0-experimental` | — | The `gate1 capture` collector | `kvprobe/nvme_capture.py` |
| `trace_contract_version` | `0.2.0` | `0.2.0` | The capture files `gate1 analyze` reads | `kvprobe/nvme_capture.py`, `kvprobe/nvme_trace.py` |
| `trace_observation_version` | `0.2.0`, `0.3.0` | — | The `gate1 analyze` output: `0.2.0` under strict ordering, `0.3.0` under opt-in causal ordering | `kvprobe/nvme_trace.py` |
| `verification_contract_version` | — | `0.1.0` | The operator review JSON that `gate1 verify` reads; written by the operator, not the code | `kvprobe/serving_verification.py` |
| `serving_verification_version` | `0.1.0-experimental` | — | The `gate1 verify` output | `kvprobe/serving_verification.py` |
| `phase_export_version` | `0.1.0-experimental` | — | The g5 client's phase export metadata | `scripts/g5_controlled_kv_experiment.py` |
| `baseline_version` | `0.1.0` | — | The regression-summary format produced by `scripts/tools/summarize_runs.py` and compared by `scripts/tools/verify_against_baseline.py`; separate from normalized evidence | `examples/baselines/`, `scripts/tools/summarize_runs.py` |

## Versions at first public release

The registry above describes the first public release, `0.1.0`. Its independent data-contract
numbers are retained; a package release does not reset them. The public changelog summarizes the
implemented behavior rather than replaying unpublished development releases.

- **Evidence:** ingest writes `0.3.1`; report reads `0.3.0` and `0.3.1`. Both bind measured evidence
  to configuration fingerprints. `0.3.1` can name Gate 5's cacheable tokens per request. Unsupported
  evidence versions are refused with guidance to normalize measurement outputs using `kvprobe ingest`.
- **Assessment and adapter:** assessment `0.6.2` reports Gate 5 at batch scope and does not read a
  Gate 4 store write as proof that the entry reached the target device. Adapter `0.4.2`
  credits a batch only when its byte check and every response's correctness agree; input size is
  retained when consistently recorded. Missing evidence still withholds performance claims.
- **Geometry and capability inventory:** geometry `0.1.1` records config provenance and derived KV
  bytes; doctor `0.2.1` records capability observations. Neither is a serving verification.
- **NVMe observation:** input `0.2.0` requires CPU-bearing setup and completion records. Inputs with
  no CPU field are refused, not upgraded by guessing CPU provenance. The bundled trace is synthetic.
  Strict observation output is `0.2.0`; opt-in causal output is `0.3.0`. The collector, phase exporter
  and serving review remain experimental under the registry's explicit labels and scope limits.
- **Regression summaries:** `baseline_version: "0.1.0"` identifies the summary format and is separate
  from the evidence document's `schema_version`. It does not change recorded measurement values.

`schemas/evidence-v0.2.0.schema.json` supplies shared definitions referenced by the accepted evidence
schemas; its presence does not make standalone `0.2.0` evidence a supported report input.
