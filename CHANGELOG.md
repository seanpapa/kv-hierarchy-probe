# Changelog

The public history of this repository starts at its first release. Development before it took place
in a private repository; the first entry summarizes what that work produced rather than reproducing
its commit history. Data-contract versions are listed in [docs/VERSIONING.md](docs/VERSIONING.md).

## Unreleased

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

- [FAILURE-CATALOG](docs/FAILURE-CATALOG.md#staging-buffer-full) §13 records the source-supported
  explanation and controlled CPU-cache-on/off comparison for staging-pool space held after a GDS
  load. The ROADMAP item that asked for the cause is closed. This documentation-only change does not
  alter the Gate 3 or Gate 4 pass conditions; reviewing whether those conditions remain sufficient
  is retained as the next ROADMAP item.

### Data contracts

- **Changed, compatible:** the evidence schema (an optional pair of Gate 4 counts), the ingest
  adapter and the assessment each move up one PATCH version, and the assessment one more for the
  correction under Fixed. The report still reads every earlier supported evidence version.
  [VERSIONING](docs/VERSIONING.md) lists the numbers.

### Known limits

- A Gate 3 reuse classified as `fallback` may be a recompute or a hit from a tier above. It is not
  split, and the break-even result does not depend on it
  ([METHOD, Gate 3](docs/METHOD.md#gate-3--admission-cost)).

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
