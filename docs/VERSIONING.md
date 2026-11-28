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
| Verdicts | A change that alters the verdict or exit status of a document the report already accepts is incompatible. A change to the wording of a reason or result alone is a correction. |
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
| `version` | `0.3.0.dev0` | — | The package | `pyproject.toml` |
| `schema_version` | `0.3.2`, `0.4.0` | `0.3.0`, `0.3.1`, `0.3.2`, `0.4.0` | Report judges legacy local evidence and the experimental NFS Gate 1-only contract | `kvprobe/ingest.py`, `kvprobe/report.py`, `kvprobe/scope_contract.py`, [EVIDENCE.md](EVIDENCE.md#versioned-contracts) |
| `assessment_version` | `0.8.0` | — | The JSON that `report` emits | `kvprobe/report.py` (`ASSESSMENT_VERSION`) |
| `normalized_by.version` | `0.4.3`, `0.1.0-experimental` | — | Legacy context adapter / experimental NFS reference adapter | `kvprobe/ingest.py` (`ADAPTER_VERSION`), `kvprobe/nfs_ingest.py` (`NFS_ADAPTER_VERSION`) |
| `manifest_version` | `0.2.1` | — | The `doctor` capability manifest | `kvprobe/doctor.py` |
| `tier_check_version` | `0.2.0` | — | The `tier-check` result: read-only host conditions for a local PCIe NVMe run; each check records `required_for_ready`, and a required `SKIP` blocks readiness. Not Gate 1 evidence | `kvprobe/tier_check.py`, `schemas/tier-check-v0.2.0.schema.json` |
| `geometry_version` | `0.2.0` | — | The `geometry` output | `kvprobe/geometry.py` |
| `nfs_client_version` | `0.2.0-experimental` (workload checkpoint) | `0.1.0-experimental` (complete client), `0.2.0-experimental` (parent-bound workload) | Request originals can be evaluated before/without stack shutdown; lifecycle health is separate | `kvprobe/nfs_client.py`, `kvprobe/nfs_client_process.py` |
| `nfs_client_observation_version` | `0.1.0-experimental` | — | Raw client observation view separates measurement availability from stack health; teardown is not a Gate 1 condition | `kvprobe/nfs_client.py` |
| `nfs_client_process_version` | `0.1.0-experimental` | `0.1.0-experimental` | Parent-observed wait/signal/timeout, logs and artifact seal; actual stack outcome is independent of measurement availability | `kvprobe/nfs_client_process.py` |
| `scope_inventory_version` | — | `0.1.0-experimental` | Root-side trusted namespace/membership originals read by the experimental NFS reference adapter | `kvprobe/nfs_qualification.py` |
| `nfs_path_binding_version` | `0.1.0-experimental` | `0.1.0-experimental` | Read-only process/path-to-counter-filesystem metadata; matched is not serving verification or exclusive/export-subtree proof | `kvprobe/nfs_path_binding.py` |
| `nfs_serving_capture_version` | `0.1.0-experimental` | `0.1.0-experimental` | Raw serving-callback candidate capture and replay; client/baseline provenance and competing I/O remain unqualified, no serving verdict | `kvprobe/nfs_serving.py` |
| `nfs_capture_version` | `0.1.0-experimental` | `0.1.0-experimental` | Raw NFS snapshot, operation and file-I/O bundle manifests; observation only, not normalized serving evidence | `kvprobe/nfs_bundle.py` |
| `diagnostic_version` | — | `0.1.0-experimental` | Legacy NFS file-I/O diagnostic input; missing snapshot provenance stays unavailable | `kvprobe/nfs_bundle.py` |
| `trace_plan_version` | `0.1.0` | — | The `gate1 plan` output | `kvprobe/nvme_plan.py` |
| `collector_version` | `0.3.0-experimental` | — | The `gate1 capture` collector | `kvprobe/nvme_capture.py` |
| `trace_contract_version` | `0.2.0` | `0.2.0` | The capture files `gate1 analyze` reads | `kvprobe/nvme_capture.py`, `kvprobe/nvme_trace.py` |
| `trace_observation_version` | `0.2.0`, `0.3.0` | — | The `gate1 analyze` output: `0.2.0` under strict ordering, `0.3.0` under opt-in causal ordering | `kvprobe/nvme_trace.py` |
| `verification_contract_version` | — | `0.1.0` | The operator review JSON that `gate1 verify` reads; written by the operator, not the code | `kvprobe/serving_verification.py` |
| `serving_verification_version` | `0.1.0-experimental` | — | The `gate1 verify` output | `kvprobe/serving_verification.py` |
| `phase_export_version` | `0.1.0-experimental` | — | The g5 client's phase export metadata | `scripts/g5_controlled_kv_experiment.py` |
| `baseline_version` | `0.1.0` | — | The regression-summary format produced by `scripts/tools/summarize_runs.py` and compared by `scripts/tools/verify_against_baseline.py`; separate from normalized evidence | `examples/baselines/`, `scripts/tools/summarize_runs.py` |

## History

### Unreleased (0.3.0.dev0)

- Development checkpoint, not a published release or generalized hardware support claim.
  Explicit NFS reference ingest writes 0.4.0 and report judges Gate 1; Gates 2–5 remain
  unmeasured. G1-only success is overall PARTIAL with performance claims suppressed.
- The raw NFS client reader accepts buffered mode only when the recorded actual runtime and
  configured NFS mode agree; local reference controls keep their original direct-I/O requirement.
- Incompatible assessment boundary: new `target_scope`, NFS provider or tier identity declarations
  inside 0.3.x now raise an input error, including fatal runs. Previously accepted legacy examples
  keep their complete assessments apart from the assessment version field. This refusal includes
  scope-mixing documents that the old reader accepted despite violating their old schema.
- `assessment_version` advances to `0.8.0`; package targets 0.3.0 as a PEP 440 development version.
  No existing schema file, canonical legacy configuration bytes or default ingest output changes.
- Explicit configuration APIs accept `evidence_version="0.4.0"`; `fingerprint --evidence-version`
  and `ingest --adapter nfs-reference` select the new path. Existing defaults remain legacy.
  New NFS arm identity binds opaque scope/service/export/client IDs, RDMA transport and I/O mode.
  Explicit draft 0.4.0 permits zero upper-tier reusable capacity for a controlled
  cache-disabled setup; 0.3.x validation and schema files keep requiring a positive capacity.


The registry above describes the current checkout; an Unreleased entry is not a release. Each release below lists the contract versions it
ships; a package release does not reset them. The [changelog](../CHANGELOG.md) describes what changed
for a user.

### 0.2.1 — 2026-11-26

- **Unchanged:** every contract keeps the version it had in `0.2.0`; only the package version moves.

### 0.2.0 — 2026-11-19

- **Changed, incompatible:** `assessment_version` `0.6.2` → `0.7.1` (two Gate 3 and Gate 4 verdicts
  changed for documents the report already accepted; `0.7.1` is a wording correction on top);
  `geometry_version` `0.1.1` → `0.2.0` (unsupported declared layouts now exit 2);
  `tier_check_version` `0.1.0` → `0.2.0` (thirteen checks, new schema file
  `schemas/tier-check-v0.2.0.schema.json`; `tier-check-v0.1.0.schema.json` stays for earlier
  results).
- **Changed, compatible:** `schema_version` written `0.3.1` → `0.3.2` (an optional pair of Gate 4
  counts), read `0.3.0`, `0.3.1` and `0.3.2`; `normalized_by.version` `0.4.2` → `0.4.3`.
- **Unchanged:** every other contract keeps the version it had in `0.1.1`.

### 0.1.1 — 2026-11-15

- **New:** `tier_check_version` `0.1.0`, the `tier-check` result
  ([TIER-CHECK](TIER-CHECK.md#5-output-and-exit-codes)).
- **Unchanged:** every other contract keeps the version it had in `0.1.0`.

### 0.1.0 — 2026-11-13 (first public release)

The data-contract numbers below were reached in private development and kept at release. The public
changelog summarizes the implemented behavior rather than replaying unpublished development releases.

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
