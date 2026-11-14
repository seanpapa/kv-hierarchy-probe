# Changelog

The public history of this repository starts at its first release. Development before it took place
in a private repository; the first entry summarizes what that work produced rather than reproducing
its commit history. Data-contract versions are listed in [docs/VERSIONING.md](docs/VERSIONING.md).

## Unreleased

### Added

- **`tier-check`**, a read-only preflight for a local NVMe tier. Before a run it checks the host
  conditions that decide whether the run can establish Gate 1: the device the per-device counter
  reads and its scope, a dedicated disk on its own mount, NVMe without multipath or device stacking,
  cgroup v2 `io`, a filesystem cuFile accepts, native rather than compat mode, the `nvidia-fs`
  counters, the container prerequisites, the staging pool against the working set, and leftovers in
  a directory you name. It changes nothing on the host, and `READY` is not Gate 1 evidence. Its JSON
  output is a new data contract ([VERSIONING](docs/VERSIONING.md)). The minimal runbook runs it as
  step 2. It returns exit 0 only for `READY`; warnings, failures and unreadable required conditions
  return exit 1. Each JSON check records whether it is required for readiness. NVMe-oF is reported
  as outside the local PCIe baseline, and the compat check requires successful `gdscheck` output
  with both compat settings disabled. The runbook stops its preflight block on a nonzero exit.

### Changed

- The runbook's host preconditions say that a reboot turns the `nvidia-fs` counters off again, and
  give the command that turns them on.
- [FAILURE-CATALOG](docs/FAILURE-CATALOG.md) §§2–5 and §9 name the `tier-check` item that catches
  each failure before the run.

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
