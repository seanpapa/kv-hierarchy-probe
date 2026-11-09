# Gate 1 reviewed serving companion (experimental)

This API is separate from `gate1 analyze`. It reconciles retained trace bytes and
explicit operator reviews for one serial native-cuFile-GDS component-tier run.
The API and CLI exist, but there is no Linux whole-Gate acceptance result yet.
Synthetic tests returning `verified` are not actual serving measurements.

```python
from pathlib import Path
from kvprobe.serving_verification import verify_serving

result = verify_serving(Path("run"), Path("review.json"))  # strict default
# Explicit opt-in; review.json must also declare "ordering_policy": "causal".
# result = verify_serving(Path("run"), Path("review.json"), ordering_policy="causal")
print(result["status"])
```

## CLI

```bash
python3 -m kvprobe gate1 verify run --review review.json --json \
  --output /tmp/new-serving-companion.json
```

Strict is the default. Add `--ordering-policy causal` only when explicitly opting
into that policy; the review must agree. Every output must be a new file, under
either policy. Existing files, raw/review destinations and output symlinks are
refused; exclusive creation also protects a file created by another writer.
The output is separate from trace observations and evidence reports.

| CLI exit | Meaning |
|---|---|
| 0 | `verified` under the declared automatic/operator-reviewed contract |
| 1 | valid `incomplete` or `failed` result; read JSON `status` to distinguish |
| 2 | arguments, contract, input/read/write or output-preservation error; no new verdict |

Default output is an English summary. `--json` gives JSON-only stdout and sends
the summary to stderr. A synthetic verified fixture is still only a tool test.

The API only reads files. Use an operator-controlled, quiescent artifact root.
It does not modify raw inputs, saved observations, caches or serving processes.
Existing trace `serving_verification: not_assessed`, evidence v0.3 and confidence
remain unchanged. Saved `observation.json` is never trusted: analysis is rerun.

## Review contract 0.1.0

The JSON object has exactly these fields:

| Field | Contract |
|---|---|
| `verification_contract_version` | `"0.1.0"` |
| `ordering_policy` | `"strict"` or explicitly selected `"causal"`, matching the API argument |
| `scope` | namespace, integer controller_id, request_id, phase, backend=`"native_cufile_gds"`, endpoint_id, cache_domain_id, integer concurrency=1 |
| `reviewer` | nonempty id and timezone-bearing ISO-8601 reviewed_at |
| `artifacts` | list of `{ "path": "relative/file", "sha256": "64 lowercase hex characters", "roles": ["runtime"] }` |
| `input_sha256` | exact analyzer input hash map for this capture, not just a subset |
| `checks` | list of `{ "id": "runtime_identity", "status": "unresolved", "reason": "Runtime not yet reviewed", "artifacts": [] }` |
| `byte_policy` | null when unresolved; otherwise the declaration below |

Missing checks or referenced files remain unresolved. Unknown fields/check IDs,
duplicate JSON keys/IDs/paths/roles, undeclared references and malformed types
are contract errors, not reviewed results. Each supplied check needs nonempty
reason text. `satisfied` without the required artifact roles is unresolved.

Review files are bounded to 64 KiB; at most 256 artifacts, 256 MiB per artifact
and 512 MiB total, including unreviewed primary capture inputs. Paths must be
canonical relative POSIX paths; absolute paths, traversal, symlinks (including
parents) and nonregular artifacts are rejected. Hashes are checked before and
after analysis. This does not secure a hostile concurrently modified directory.

## Required checks and artifact roles

Every check supports `satisfied / unresolved / violated` and artifact references.
The roles below are explicit reviewed declarations about content, not facts
inferred from a filename. A single artifact may declare multiple roles.

| Check ID | Required referenced roles | Review responsibility |
|---|---|---|
| `capture_integrity` | manifest, trace, phases, stdout, stderr | supported collector/environment and covered intervals |
| `filter_clock_acceptance` | acceptance | matching source/kernel/tracepoint, filter controls and clock alignment |
| `runtime_identity` | runtime, configuration | actual model/dtype/TP/chunk/backend/buffers/mounts versus declarations |
| `admission_complete` | lifecycle, framework_log | complete target store settled before pressure, with write corroboration |
| `residency_isolation` | residency, runtime, framework_log | prefix-cache bypass, upper-tier state, competing activity |
| `retrieval_correctness_path` | request, framework_log, counters | correct completed output, partial/fallback/error review and counter scope |
| `byte_policy` | geometry, byte_policy | independent payload geometry and predeclared path-specific allowance |
| `audit_binding` | manifest, trace, phases, request, configuration | retained evidence linkage and trace-on latency separation |

Capture references must include the actual `manifest.json`, `trace.csv`,
`phases.csv`, `bpftrace.out` and `bpftrace.err`, with the corresponding roles.
Calling arbitrary notes a `trace` does not substitute for the primary input.
The byte-policy check must reference the specific declaration artifact.

## Automatic checks versus review

The companion automatically reruns the selected analyzer and requires usable
quality; selects exactly one matching request/phase; binds namespace/controller;
checks artifact/input hashes; recomputes scoped record-delivery accounting and
raw-to-normalized equality even under strict analysis; and compares successful
read/write bytes against the policy when capture quality is usable.

The operator must still review runtime, admission, residency, fallback, isolation,
geometry and filter/clock acceptance. Neither hashes nor a loss-zero assertion
prove those facts. Roles and reasons do not replace reading the evidence.
The result discloses automatic checks, per-check review modes/reasons, source
hashes, missing artifacts and unresolved conditions.

## Predeclared byte policy

Example structure only, **not a universal allowance**:

```json
{
  "expected_read_bytes": 67108864,
  "read_min_bytes": 67108864,
  "read_max_bytes": 67108864,
  "write_max_bytes": 0,
  "declared_before_ns": 100,
  "artifact": "policy.json"
}
```

The referenced policy file must contain the same object without `artifact`.
All numeric fields are nonnegative integers; read_min must be positive and
bracket expected payload with read_max. Expected bytes must match the selected
phase. Declaration time must precede the earliest analyzed phase. The timestamp
and geometry provenance still require human review: JSON alone cannot prove
the declaration was actually made beforehand. Allowance must come from the
advance declaration and geometry; observed bytes or ratio/LBA overlap alone
cannot approve serving.

## Verdicts

- `verified`: all automatic and reviewed requirements satisfied; **reviewed,
  scoped component-tier measurement only**, not request-ID hardware proof.
- `incomplete`: missing/unknown evidence, unusable capture or unresolved checks.
- `failed`: demonstrated criterion or retained-byte integrity violation. Failure
  takes precedence, but missing evidence remains visible in the result.

`VerificationError`, `TraceError`, file/encoding/JSON errors return no verdict.
Missing required capture files yield incomplete; malformed capture contracts do
not yield a false PASS. The API does not automatically inspect arbitrary
framework-log formats or assign a hardware request ID to a command.

See [NVMe trace workflow](GATE1-NVME-TRACE.md). Supported-Linux acceptance
and actual first-path review remain subsequent milestones.
