# Changelog

The public history of this repository starts at its first release. Development before it took place
in a private repository; the first entry summarizes what that work produced rather than reproducing
its commit history. Data-contract versions are listed in [docs/VERSIONING.md](docs/VERSIONING.md).

## 0.1.0 — unreleased (first public baseline)

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

### Known limits

- Only a **local NVMe** tier has passed Gate 1 on hardware, on a single-path device. A DRAM tier is
  inferred, not verified; remote tiers need their own evidence adapters.
- Hardware results come from one host and one stack; there is no independent reproduction yet.
- The NIXL mode matrix runner is not hardware-verified.
- The first public package baseline is `0.1.0`; data contracts may change before 1.0 under the rules in
  [VERSIONING](docs/VERSIONING.md).
