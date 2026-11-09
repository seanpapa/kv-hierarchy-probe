# Measurement profiles and version direction

`kv-hierarchy-probe` keeps one core method. Profiles define the unit of claim covered by a verdict
and the evidence required to support that scope. Execution topology and cache-domain scope are
orthogonal properties, not profile names.

## Terms and decision

- **V1 candidate** is a product-release label for the current pre-1.0 implementation. It qualifies
  a storage tier as observed by one endpoint and is called the *component-tier* profile here. The
  current evidence happens to cover one cache domain, but that cache scope is not the profile name.
- **V2** is the planned product extension whose disaggregated scope qualifies the complete
  prefill/decode path. It reuses the v1 core and five gates while requiring additional evidence.
  `component_tier` and `disaggregated_path` are proposed future profile values, not current schema
  fields.

V2 is not a fork, a second codebase, or a replacement method.

```text
kv-hierarchy-probe core
├── v1: component-tier qualification
│   ├── five-gate assessment
│   ├── normalized evidence and fail-closed report
│   └── backend / serving-stack adapters
└── v2: disaggregated path qualification
    ├── the same five gates
    ├── cross-worker producer/consumer request correlation
    ├── P→D or object-mediated transfer evidence
    └── placement, duplication, and distributed envelope
```

## V1 candidate: component-tier qualification

The unit of claim is a storage tier as observed by one serving endpoint in one cache domain. The
five gates answer whether the measurement proves the tier, retrieval beats recompute, admission
cost is recoverable, capacity makes the tier necessary, and the result holds over a measured load
range.

Before a 1.0 release, generality work should make the existing boundary portable:

1. Define a point-at-an-existing-endpoint adapter contract. Serving-stack bring-up remains outside
   the probe.
2. Extend attribution providers beyond the two implemented (`nvidia_fs`, `cgroup_blk`) to transport/RDMA and object counters.
3. Derive KV geometry from the serving configuration, with a checked manual override.
4. Separate the current run-shape-specific ingest code from a generic run-to-evidence adapter
   contract.
5. Produce the same five-gate report with another backend and, where possible, another serving
   stack.
6. Preserve fail-closed capability handling: unavailable evidence produces `SKIP_UNSUPPORTED`, not
   an inferred PASS.

Generality does not mean one script launches every serving stack. It means environment-specific
adapters produce evidence that the same core judges under the same explicit rules.

## V2: disaggregated path qualification

V2 applies the same questions to a larger unit of claim: the path from prefill compute or cache
retrieval through KV delivery and decoder admission to first token. It adds, at minimum:

- prefiller and decoder topology, role, and opaque instance identity;
- correlation of producer and consumer observations for the same request;
- cache owner, source domain, destination domain, and transfer mode;
- logical KV bytes, L2 reads/writes, P→D payload and wire bytes, and object PUT/GET bytes;
- location lookup, L2 access, transfer completion, decoder admission, and first-token timestamps;
- duplicate physical copies per live unique logical KV;
- an XpYd envelope that includes routing and link queueing.

V2 compares complete matched arms rather than adding an assumed transfer penalty to a v1 result:

```text
miss/direct:
  route + prefill compute + P→D handoff + decode admission

producer-local L2 hit:
  route + location lookup + producer L2 read + P→D handoff + decode admission

decoder-local or shared L2 hit:
  route + location lookup + decoder/shared L2 read + decode admission

object-mediated miss:
  route + prefill compute + object PUT + object GET + decode admission
```

A handoff common to two arms may largely cancel, while overlap, queue position, partial transfer,
or a different route may make it material. Measure the complete arms and use phase timing to
explain the difference.

## Parallelism and v2 readiness

*Moved from the root README on 2026-10-03; the wording is unchanged.*

On a host with multiple NVIDIA GPUs, `doctor` also records the GPU count and pair topology reported
by `nvidia-smi topo -m` (for example `NV#`, `PIX`, `PHB`, or `SYS`). It deliberately prints
`runtime collective: not_observed`: host topology does not prove the serving process's TP size,
rank-to-GPU binding, collective backend, or selected communication path. Preserve the doctor JSON
beside the run, then capture those runtime facts from the serving process. Evidence v0.3 does not
yet fingerprint a multi-GPU worker group, so do not describe its single `device_id` as proof of the
whole TP communication path.

The current evidence contract identifies one measured accelerator; it does not yet fingerprint a
complete multi-GPU worker group. V2 preparation is already tracking tensor parallelism (TP), data
parallelism (DP), rank placement, worker identity, collective communication, and replica/cache
ownership:

- TP=2 rank-local KV geometry was measured on one host (2026-09-21): for a 256-token chunk the
  per-rank file went from 64 MiB at TP=1 to 32 MiB at TP=2 while node-aggregate KV bytes stayed
  constant, and one logical chunk kept a single identity across both ranks. The arithmetic behind
  those figures is in [`GEOMETRY.md`](GEOMETRY.md); the run artifacts are retained outside this
  repository.
- Host GPU topology discovery is implemented, but runtime collective use remains
  `not_observed` until serving-process evidence binds ranks, devices, and the selected path.
- DP is a separate replica axis that may duplicate KV data across cache domains. Its runtime and
  cache-key behavior still requires validation; request concurrency is not a DP measurement.
- A future evidence schema must bind TP/DP configuration, rank-to-device placement, collective
  backend, replica identity, and distributed P-to-D topology before a multi-GPU or
  distributed-scope verdict can pass.

See [Multi-GPU worker groups](CONFIGURATION.md#multi-gpu-worker-groups) and the sections
above for the current boundary and the planned evidence contract.

## Compatibility and verdict rules

Product releases, evidence schemas, and measurement profiles have separate versions:

| Dimension | Example | Meaning |
|---|---|---|
| Product release | `kv-hierarchy-probe 1.x`, `2.x` | Shipped features and claim boundary |
| Evidence schema | current `0.3.1`, later versions | Machine-readable document contract |
| Measurement profile | proposed `component_tier`, `disaggregated_path` | Unit of claim and mandatory evidence |
| Execution topology | proposed `colocated`, `disaggregated` | Whether prefill and decode share a worker group |
| Cache scope | proposed `single_domain`, `multi_domain` | Number and placement of cache domains in scope |

These dimensions must not be collapsed. A disaggregated 1P1D deployment can use one shared cache
domain, while a service can run multiple colocated replicas with multiple cache domains.

### What is enforced today

- Evidence v0.3 has no `measurement_profile` property. Its JSON Schema sets top-level
  `additionalProperties: false`, so a document containing that property violates the schema.
- `kvprobe report` currently performs partial manual validation rather than running the full JSON
  Schema. It accepts and ignores an unknown top-level `measurement_profile`, then assesses
  configuration bindings and the five gates without emitting a profile-specific claim scope.
- Both statements above are pinned by
  [`tests/test_profiles_boundary.py`](../tests/test_profiles_boundary.py): the schema stays closed
  and profile-free, the verdict is identical with and without the property, and no profile value
  reaches the assessment output. If the reporter ever derives a claim scope from the property, that
  test fails before this page goes stale.
- The component-tier boundary is inferred from the information v0.3 can represent. Preventing a
  v0.3 PASS from being described as a complete disaggregated-path PASS is currently an editorial
  and review rule. V0.3 does not encode enough information to assert an execution topology.

### Required when the disaggregated-path schema is designed

1. Add a required `measurement_profile` field in a new schema version, with explicit
   `component_tier` and `disaggregated_path` values.
2. Add execution-topology and cache-scope fields separately. Make `disaggregated_path` require a
   disaggregated execution topology plus producer/consumer correlation, transfer, placement, and
   complete-path phase bindings through schema and semantic validation.
3. Emit the enforced profile and claim scope in the assessment so downstream reports cannot omit it.
4. Continue accepting v0.3 as legacy component evidence, but have the new reporter expose its claim
   scope as component-only rather than relying on publication prose.
5. Add negative tests proving that unknown or mismatched profile values, missing distributed
   bindings, and legacy v0.3 evidence cannot produce a disaggregated-scope PASS. Include a contract
   test so reporter behavior cannot silently diverge from the versioned JSON Schema.

These are v2 schema requirements, not claims about the current reporter. The schema should be
designed after the distributed evidence shape is validated; no profile field is added to v0.3 now.

## Initial v2 experiment sequence

| Arm | Purpose |
|---|---|
| D0 — existing single endpoint | Preserve the v1 component baseline |
| D1 — 1P1D direct, no persistent L2 | Attribute pure P→D handoff bytes and time |
| D2 — 1P1D producer-local L2 hit | Measure L2 read followed by P→D handoff |
| D3 — 1P1D decoder-local or shared L2 hit | Compare a decoder-side read with D2 |
| D4 — XpYd | Measure location routing, duplication, and the distributed envelope |

D2 and D3 are candidate placement arms, not universal product requirements. Select the mandatory
arm after the deployment's cache owner and save policy are known. Establish normal-path request
attribution before adding retry, failure, and reroute experiments.
