# Changelog

The public history of this repository starts at its first release. Development before it took place
in a private repository; the first entry summarizes what that work produced rather than reproducing
its commit history. Data-contract versions are listed in [docs/VERSIONING.md](docs/VERSIONING.md).

## Unreleased (0.3.0.dev0)

NFS/RDMA work is experimental. See the [adapter, policy and remaining acceptance](docs/NFS-RDMA-EXPERIMENTAL.md).
The reference path now produces G1-only assessments; generalized verified support is not declared.

### Changed

- Legacy evidence with NFS service-scope declarations is now refused instead of silently judged as
  block-device evidence. This is an incompatible input boundary, targeting the next minor release.
  Schema-valid local examples keep their results. General NFS serving-stack support is not declared.

### Fixed

- NFS reference ingest now carries request/quiet callback errors into an INVALID capture
  instead of allowing matching output/bytes to pass despite a failed measurement phase.
- Optional shutdown/cleanup diagnostic JSON no longer vetoes valid request evidence when its
  contents are unreadable. Its raw bytes/hashes remain retained; required measurement JSON,
  artifact containment and integrity checks stay strict.
- Legacy NFS replay now confines the resolved `artifacts/` directory to the original bundle root,
  rejecting outside directory symlinks while retaining valid internal aliases.
- Serving-candidate replay adds quiet-phase activity, READ RPC retry/timeout/error and request
  I/O-mode conflict diagnostics. Interphase activity now includes all retained I/O/RPC deltas.
  Raw counters remain intact; diagnostics do not produce a serving verdict or authorize claims.

### Measurement boundary correction

- Gate 1 request evidence is independent of serving-stack teardown health. Raw client replay now
  exposes a versioned `measurement_evidence` view and separate `stack_health`, accepts valid
  pre-teardown checkpoints without shutdown receipts, and permits a finished baseline response
  even if the stack later fails. Stack warnings never replace correctness/byte/scope/integrity
  checks or become a Gate 1 veto by themselves. Incomplete/corrupt measurement evidence remains
  unavailable/refused. Existing normalized report rules and CLI defaults are unchanged.

### Added

- Explicit `ingest --adapter nfs-reference` replays retained source/file/buffer/namespace and
  client/counter originals into the separate NFS evidence contract. The report distinguishes
  missing, contradicted and invalid measurements. G1-only PASS remains overall PARTIAL and
  suppresses performance claims. Default ingest and valid legacy local verdicts are unchanged.
  The NFS input needs a trusted reference producer. Qualified retained requests do not require a
  fresh positive run merely to connect an offline CLI; broader live workflow qualification remains
  separate from their verdicts.
- `fingerprint --evidence-version` selects an explicit configuration contract; the default is unchanged.
- Buffered NFS reference receipts require actual runtime/configuration mode agreement, so a
  client-cache-negative observation can be represented without pretending a direct read happened.

- Experimental pre-teardown workload checkpoints and a parent-owned POSIX process supervisor
  retain real exit/signal/timeout outcomes and original hashes after a child aborts. Replay separates
  workload consistency from cleanup/completion and refuses unsafe artifact seals. No process
  outcome alone grants or vetoes serving verification. Complete legacy client receipts remain readable;
  old aborted captures are not retroactively sealed or upgraded. The raw observation reader remains separate from normalized assessment.

- Experimental tiny reference-client replay cross-checks original input/output, an independent
  recompute baseline, model/package/source digests, process lifetime and four KV byte originals.
  NFS candidates also join callback timing/bytes and bracketing process/path receipts. These are
  consistency observations; source/runtime/isolation originals are required by the explicit reference adapter. Explicit draft 0.4.0 permits zero upper-tier reusable capacity; legacy 0.3.x does not.

- Experimental read-only process/path binding captures PID/start-time, cmdline, namespace and
  kernel-resolved FD mount/device metadata. Replay distinguishes a shared NFS counter filesystem,
  a mismatch, unavailable evidence and unstable capture. Namespace inequality and lexical path
  prefixes do not decide the connection. Actual payload access and serving verification remain
  unestablished; existing CLI defaults remain unchanged.

- Experimental controlled-callback capture retains prior request/configuration/baseline bytes,
  before/after NFS snapshots and adjacent idle controls. Replay independently derives geometry,
  output comparisons and phase/gap deltas, while withholding serving verification pending client,
  baseline and isolation qualification. The explicit reference adapter performs a separate normalized assessment.

- Draft evidence 0.4.0 scope schema and explicit version-aware configuration fingerprint/binding
  APIs distinguish NFS service/export/client identity from a device filter. The explicit reference adapter and report branch use the new contract; legacy CLI defaults remain unchanged.

- Experimental raw NFS/RDMA capture and replay APIs retain per-snapshot namespace and clock-domain
  metadata, verify raw artifact hashes and recompute mount-scoped payload counters. Legacy file-I/O
  diagnostics retain their missing provenance. Replay is observation only: no serving-tier verdict,
  performance claim or hardware-verified NFS support is added by raw replay alone.

## 0.2.1 — 2026-11-26

A compatible release under the pre-1.0 rule: no data contract, verdict rule or report output
changes. A host with one GPU and no GDS can now produce Gate 5 on the POSIX arm, the runbook says
how to prepare a host and its image check no longer fails on a working image, the Gate 1 guide
becomes an execution walkthrough, and the package declares its licence as an SPDX expression.

### Added

- **Gate 1 execution walkthrough.** `docs/GATE1.md` starts with three synthetic verdicts, then
  connects reference-stack preparation, matched context collection, raw file names, cleanup,
  `ingest` and `report`. It explains why a valid Gate 1/2-only run remains `PARTIAL` with report
  exit 1, keeps optional NVMe diagnostics after the main path, and preserves incoming anchors.
  No measurement, verdict rule or data contract changes.
- **Gate 5 on a host without GDS.** The controlled launcher's `concurrency-posix` mode runs the
  concurrency sweep on the POSIX arm alone and writes `concurrency-posix.json`, which `ingest` judges
  as Gate 5 against a configuration that declares a `posix` arm. Before, the only sweeps were
  `concurrency` (all three arms) and `concurrency-gds`, so a host without `nvidia-fs` could not
  produce Gate 5 ([runbook step 5](scripts/README.md#minimal-runbook--all-five-gates-one-configuration)).
- **`preflight [ARM]`.** `preflight posix` and `preflight recompute` skip `gdscheck` and say so;
  `preflight` with no arm, or `preflight gds`, still requires it and now names a missing `gdscheck`
  instead of stopping on the shell's error.
- **Runbook: preparing a host.** What to install for each arm and how to check it, which gates a
  host with one GPU and no GDS can produce, disk layout, a GPU shared with a resident service,
  model directory forms, and sizing the fillers against the 3 GiB CPU tier for models with smaller
  KV chunks ([scripts/README.md](scripts/README.md#preparing-a-host)). The single-GPU POSIX path
  names the skipped steps, its arm configuration, cleanup and three-file ingest, and the expected
  `PARTIAL` verdict when Gates 3 and 4 were not measured. Getting an image says how to read the
  GPU's compute capability for `TORCH_CUDA_ARCH_LIST`, which can cover several capabilities and
  PTX, and that Docker without buildx builds with the legacy builder.

### Fixed

- `preflight` looked for the model directory by the basename of `MODEL`, so a nested model path
  under `/models` (a Hugging Face cache snapshot, for example) failed without a message. It now
  resolves `MODEL` relative to `/models`, refuses a `MODEL` outside `/models`, and names a missing
  directory. Parent-directory (`..`) components are refused even when the resulting host path
  exists; dots within a directory name remain allowed.
- The controlled launcher started the recompute arm only under bash 4.4 or later: an empty option
  array is unset under `set -u` in older bash.
- The runbook's image check imported `lmcache.c_ops` alone, which fails with `libc10.so` on a
  working image because `c_ops` needs torch loaded first; it now imports `torch, lmcache.c_ops`.
  Serving was not affected.
- The script table listed the controlled launcher as producing Gates 1 and 5; its context runs
  (runbook step 4) also produce Gate 2.
- TIER-CHECK T6 said `io` is enabled for child cgroups when the first container starts; on the
  host checked it was enabled when the Docker service started.

### Changed

- **Packaging metadata.** `pyproject.toml` declares the licence as the SPDX expression
  `Apache-2.0` with `license-files = ["LICENSE", "NOTICE"]`, and the build requires setuptools 77
  or newer. Setuptools warns that the deprecated table form must be migrated by 2027-02-18
  to retain build support. Built packages carry the same
  LICENSE and NOTICE files and now state `License-Expression: Apache-2.0`. An install with
  `--no-build-isolation` needs setuptools 77 or newer in that environment.
- **Unchanged:** every contract; [VERSIONING](docs/VERSIONING.md#history) lists the versions
  this release writes and reads.

### Known limits

- The POSIX-only Gate 5 sweep and the host preparation guide were exercised on one host with one
  GPU and no GDS. The current single-GPU POSIX runbook path does not measure Gates 3 and 4;
  with valid evidence for Gates 1, 2 and 5, its overall verdict is `PARTIAL`.
  The single-file Dockerfile built there has not been re-measured on the GDS configuration.
- The all-five native GDS result remains limited to one host and one configuration. The POSIX-only
  path adds checks on a second host, but independent reproduction remains unestablished.
  The other [known limits of `0.2.0`](#020--2026-11-19) still apply.

## 0.2.0 — 2026-11-19

An incompatible release under the pre-1.0 rule: two Gate 3 and Gate 4 verdicts changed for documents
the report already accepted, `geometry` refuses configs it used to answer, and `tier-check` gains a
thirteenth check that can turn a host from ready to not ready. The 2026-10-08 all-five run keeps its
verdict and counts under the new assessment. Reports also gain Gate 1 next steps and Gate 4 fallback
kinds.

### Added

- **Gate 1 next steps.** When Gate 1 is `FAIL` or `UNPROVEN`, the text and Markdown reports, and the
  bundled reports `first-visit` writes, end with a block that names each finding with its count,
  why it keeps Gate 1 from passing, what to check or collect next, what it does not show, and where
  to read more ([GATE1](docs/GATE1.md#next-steps-in-the-report)). Violations come first, then
  missing evidence, and every finding that applies is listed. Verdicts, claim state, exit codes, the
  JSON report and `assessment_version` are unchanged.
- **Gate 4 fallback kinds.** When the capacity producer kept per-request `stores` and `retrievals`
  records, ingest splits each `fallback` by whether a store write of the same entry was observed
  earlier in the sweep, and the Gate 4 result (or `FAIL` reason) names both counts and where to look
  first: the load path and the staging pool, the store path, or both
  ([EVIDENCE](docs/EVIDENCE.md#producing-evidence-from-a-run)). The write counter is host-wide, and
  the result says an observed write does not place the entry on the target device. Evidence without
  the records keeps the count-only result. Verdicts and claim state are unchanged.

### Fixed

- The report accepted a document whose declared evidence version does not define a field it
  carries: Gate 5's per-request input size in a document declaring the version before it was
  added. Its JSON Schema refuses that document, so tools disagreed about the same file. The report
  now refuses it too, naming the version that defines the field; a test holds the report and the
  schemas to the same answer for every version-specific field. Documents that match their declared
  version are judged as before.

### Changed

- **`tier-check` checks Docker access and the NVIDIA runtime (T13).** The reference launchers
  start every arm with `docker run --gpus`, but no check covered Docker itself: on a host without it,
  the POSIX arm could read `READY WITH WARNINGS` when T2 had already found a shared disk. T13 runs
  `docker info` as this user and fails when it does not succeed; it warns when no `nvidia` runtime is
  registered. It applies to both arms. It does not start a container. The daemon must also be
  confirmed on this host (a `unix://` endpoint and a matching host name); otherwise, for example
  through `DOCKER_HOST` or a remote context, T13 gives a required `SKIP`, and T2 no longer compares
  that daemon's root directory with this host's disks. T6 now also says whether the `io` controller is already
  enabled for child cgroups; on a host where no container has run yet it usually is not, and the
  container runtime enables it when it starts one. The usage error for only one of
  `--cufile-buf-mib` and `--mib-per-chunk` now says they must be given together
  ([TIER-CHECK](docs/TIER-CHECK.md#4-the-checks)).
- **`geometry` refuses unsupported declared cache layouts.** The standard formula counts every
  layer as full attention and has no term for recurrent state. Explicit `layer_types` must now
  contain one `full_attention` name per declared layer. Mixed, uniform non-full-attention,
  unknown and malformed declarations are refused, as are known hybrid markers at the top level
  or in `text_config` ([fail-closed boundaries](docs/GEOMETRY.md#fail-closed-boundaries)). Absent
  or null layer types keep the previous standard-formula assumption, not a verified layout. A
  wrapper config whose layer fields sit only under `text_config` is refused with a message naming
  it, instead of "must be a positive integer". Standard-attention configs give the same result as
  before. A config that used to get a number and is now refused makes `geometry` exit 2.
- **Gate 3 and Gate 4 no longer pass on incomplete evidence.** A Gate 3 block that keeps successful
  samples only — a store cost without the per-attempt outcomes — is now `UNPROVEN`. It used to pass
  with its result labelled an optimistic estimate. A Gate 4 sweep that exceeded the upper tier's
  capacity and recorded target-tier service without its classification counts is now `UNPROVEN`.
  It used to pass and say the tier served the overflow, although two booleans show only that at
  least one retrieval was served. If the upper tier overflowed and the target tier served nothing,
  Gate 4 remains `FAIL`, with or without counts.
  Both newly unproven shapes withhold performance claims and make `report` exit 1
  ([METHOD, Gate 3](docs/METHOD.md#gate-3--admission-cost) and
  [Gate 4](docs/METHOD.md#gate-4--capacity-justification)). Documents that keep every attempt and
  the counts are judged as before; the verdict and results of the 2026-10-08 run in
  [docs/README](docs/README.md) are unchanged. This closes the ROADMAP review of the two pass
  conditions against the staging-pool mechanism in
  [FAILURE-CATALOG §13](docs/FAILURE-CATALOG.md#staging-buffer-full).
- **Bundled examples follow the change.** `pass-v0.3.json` is renamed
  `admission-successful-samples-only.json` and is `UNPROVEN`. `pass-capabilities.json` gains an
  all-attempt Gate 3 block and Gate 4 classification counts, and `partial-unsupported.json` gains
  the counts; both keep their verdicts ([examples](examples/README.md)).
- [FAILURE-CATALOG](docs/FAILURE-CATALOG.md#staging-buffer-full) §13 records the source-supported
  explanation and controlled CPU-cache-on/off comparison for staging-pool space held after a GDS
  load. That documentation-only change closed the cause-finding item and left the Gate 3 and Gate 4
  pass-condition review as follow-up work. The review is completed by the assessment change above.
  A further ROADMAP item then covered explaining the staging-pool mechanism in the Gate 4 result and
  in METHOD for Gates 3 and 4, without another verdict change; the Gate 4 wording change below
  completes it.
- **Gate 4 names the staging pool where it can explain the losses.** When the Gate 4 result or a
  `FAIL` reason describes fallbacks beyond the upper tier's capacity, it now adds that a staging pool
  still held by the serving stack can cause them, so the counts alone do not show a device fault
  ([FAILURE-CATALOG §13](docs/FAILURE-CATALOG.md#staging-buffer-full)). The note is left out when
  the fallback split shows that none followed an observed store write, and when the only losses are
  misses. METHOD now says that the same cause can show up in Gate 3 as `store_unattributed`
  attempts, which can raise the reuses needed; their count shows how often, not how much of the
  cost. Verdicts, claim state and exit codes are unchanged; the
  2026-10-08 run keeps its verdict and counts, and its Gate 4 result gains the note. This closes
  the ROADMAP item that followed the pass-condition review.

### Data contracts

- **Changed, compatible:** the evidence schema (an optional pair of Gate 4 counts), the ingest
  adapter and the assessment each move up one PATCH version, and the assessment one more for the
  correction under Fixed. The report still reads every earlier supported evidence version.
  [VERSIONING](docs/VERSIONING.md) lists the numbers.
- **Changed, incompatible:** the assessment moves up one MINOR version, because two verdicts changed
  for documents the report already accepted. [VERSIONING](docs/VERSIONING.md#rules) now states that
  rule. The evidence schema and the ingest adapter are unchanged.
- **Changed, compatible:** the assessment then moves up one PATCH version for the Gate 4 wording
  above, a correction under that rule.
- **Changed, incompatible:** `geometry` moves up one MINOR version, because configs it used to
  answer with a number now exit 2. Its output format is unchanged.
- **Changed, incompatible:** the `tier-check` result moves up one MINOR version: it holds thirteen
  checks. A host where `docker info` fails for this user now reads `NOT READY` (exit 1) where it
  could read `READY WITH WARNINGS`, and one whose Docker lists no `nvidia` runtime reads
  `READY WITH WARNINGS` where it could read `READY`. A daemon not confirmed on this host now gives
  `NOT READY`. A new schema file describes it; the earlier one stays for results written before.
- **Unchanged:** every other contract; [VERSIONING](docs/VERSIONING.md#history) lists the versions
  this release writes and reads.

### Known limits

- A Gate 3 reuse classified as `fallback` may be a recompute or a hit from a tier above. It is not
  split, and the break-even result does not depend on it
  ([METHOD, Gate 3](docs/METHOD.md#gate-3--admission-cost)).
- `geometry` detects unsupported layouts only from an explicit `layer_types` list and the listed
  hybrid markers. A config without either, or layer counts entered with `--layers`, still gets the
  standard formula ([fail-closed boundaries](docs/GEOMETRY.md#fail-closed-boundaries)).
- `tier-check` T13 does not start a container: it shows Docker access and a registered `nvidia`
  runtime, not that the image or the GPU work. "On this host" is a host-name match, and a reachable
  tcp or ssh daemon is covered by tests only ([TIER-CHECK](docs/TIER-CHECK.md#7-known-limits)).
- The [known limits of `0.1.1`](#011--2026-11-15) still apply.

## 0.1.1 — 2026-11-15

A compatible addition: one new command, `tier-check`, and its data contract. No existing command,
data contract or verdict changed.

### Added

- **`tier-check`**, a read-only preflight for a local NVMe tier. Before a run it checks the host
  conditions that decide whether the run can establish Gate 1: the device the per-device counter
  reads and its scope, a dedicated disk on its own mount, NVMe without multipath or device
  stacking, cgroup v2 `io`, a filesystem cuFile accepts, native rather than compat mode, the
  `nvidia-fs` counters, the container prerequisites, the staging pool against the working set, and
  leftovers in a directory you name. It changes nothing on the host, and `READY` is not Gate 1
  evidence. Its JSON output is a new data contract ([VERSIONING](docs/VERSIONING.md)). The minimal
  runbook runs it as step 2. `READY` and `READY WITH WARNINGS` return exit 0; failures and
  unreadable required conditions are `NOT READY` and return exit 1. Each JSON check records
  whether it is required for readiness. NVMe-oF is reported as outside the local PCIe baseline,
  and the compat check requires successful `gdscheck` output with both compat settings disabled.
  Multipath is judged from the tier's own device: a native multipath head warns and has the
  transport of each of its paths checked (of each controller in its subsystem on kernels that do
  not link the paths), while a namespace without a head passes even though `nvme_core`
  `multipath` defaults to `Y`. The runbook stops its preflight block on a nonzero
  exit. An unreadable required transport blocks readiness even if another path is known to
  be remote; the result retains both the remote finding and the missing information. The staging-pool
  check sizes the working set as the target entry plus each filler at its own size
  (`--filler-tokens`, default 4096 as in the runbook), and warns when the target or a filler entry is
  larger than the pool, a store path no recorded run measured.
- **[TIER-CHECK](docs/TIER-CHECK.md)**, the reference for `tier-check`: what each check (T1–T12)
  reads and decides, the statuses, the overall result, the exit codes and how native NVMe
  multipath is judged. A test keeps its table in step with the titles the command prints and
  checks that its links resolve. README, CHECKLIST, the runbook and the docs index link to it.

### Changed

- The runbook's host preconditions say that a reboot turns the `nvidia-fs` counters off again, and
  give the command that turns them on.
- [FAILURE-CATALOG](docs/FAILURE-CATALOG.md) §§2–5 and §9 name the `tier-check` item that catches
  each failure before the run.
- README section 3 says only what `tier-check` does, when it stops the runbook and that `READY` is
  not serving evidence; the exit codes are in [TIER-CHECK](docs/TIER-CHECK.md#5-output-and-exit-codes).
- The `0.1.0` known limits below were reworded after release to separate the all-five run from the
  Gate 1 and Gate 2 runs on three paths. The scope they describe did not change.

### Data contracts

- **New:** `tier_check_version` `0.1.0`, written by `tier-check` and described by
  `schemas/tier-check-v0.1.0.schema.json`.
- **Unchanged:** every contract that `0.1.0` wrote or read. [VERSIONING](docs/VERSIONING.md) lists
  them.

### Known limits

- `tier-check` has run on hardware on one host, against a single-path local NVMe tier, where it
  returned `READY`. Native multipath heads, NVMe-oF and device stacking are covered by tests only.
- `READY` is not Gate 1 evidence. The staging-pool check (T11) is arithmetic on the sizes you pass;
  only the run's per-request read bytes show that the device served a request.
- The known limits of `0.1.0` still apply.

## 0.1.0 — 2026-11-13 (first public release)

### What it is

A measurement procedure for deciding whether a KV-cache tier belongs in an LLM serving hierarchy:
prove that the tier served each request, then judge whether it pays against recompute. Five ordered
gates — serving-tier verification, single-hit break-even, admission cost, capacity justification
and operating envelope — and a reporter that withholds every performance claim unless the evidence
supports it.

### Included

- **Portable CLI** (`python3 -m kvprobe`, no runtime dependencies): `first-visit`, `doctor`,
  `geometry` (model-first, from a local config or one public `config.json`), `pool-plan`, `ingest`,
  `report`, `fingerprint`, and the experimental `gate1` NVMe trace commands.
- **Evidence contract**: JSON schemas, configuration fingerprints, and an assessment that separates a
  gate's verdict (is the measurement valid?) from its result (what did it find?). Gate 3 counts every
  admission attempt, including fallbacks and stores with no observed write. Gate 5 credits a
  concurrency batch to the tier only when it meets its byte check with every response correct, and
  its result names the per-request input size the envelope was measured at.
- **Reference implementation** for vLLM + LMCache v0.4.1 with buffered POSIX, native GDS and NIXL
  static paths, and a minimal runbook that takes a prepared host through all five gates.
- **Public contracts:** report accepts configuration-bound evidence `0.3.0` and `0.3.1`; offline
  NVMe analysis requires CPU-bearing trace input `0.2.0`. Regression summaries use `baseline_version: "0.1.0"`.
- **Examples**: synthetic cases for PASS, UNPROVEN, PARTIAL, FAIL and INVALID verdicts, and sanitized
  measured examples labelled with their provenance. A generated first-visit artifact index orders
  the four bundled reports and separates them from optional host-capability detail.
- **Method documents**: the five gates, a measurement checklist, a failure catalog of plausible but
  false results, and portability notes.
- **Documentation added after the repository opened (2026-11-10):** a [roadmap](docs/ROADMAP.md)
  of unverified scope and known limitations,
  [unified-memory lessons](docs/reference/UNIFIED-MEMORY-LESSONS.md), and the 2026-10-08 all-five
  run in the [docs index](docs/README.md) status block. The Gate 4 result no longer says the
  producer's records show which transfer failed.

### Known limits

- All five gates have been measured together for one native cuFile GDS configuration on a local,
  single-path NVMe tier. These results are configuration-specific. Separate runs passed Gates 1 and 2
  on three local NVMe paths: buffered POSIX, native GDS and NIXL static `GDS_MT`. CPU DRAM serving
  remains inferred, and remote tiers require their own evidence sources and validation.
- Hardware results come from one host and one stack; there is no independent reproduction yet.
- The NIXL mode matrix runner is not hardware-verified.
- `0.1.0` is the first public release; data contracts may change before 1.0 under the rules in
  [VERSIONING](docs/VERSIONING.md).
- What is planned or not yet verified is listed in the [roadmap](docs/ROADMAP.md).
