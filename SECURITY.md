# Security policy

## Reporting a vulnerability

**Use GitHub's private vulnerability reporting** — the *Report a vulnerability* button under this
repository's **Security** tab. That channel is private between you and the maintainers.

**Do not open a public issue for a vulnerability**, and do not attach measurement material to one.
Reports frequently carry more than the reporter intends: raw serving logs, `docker inspect` output,
`provenance.txt`, container startup logs and evidence bundles can contain hostnames, usernames,
internal addresses, mount and model paths, container identifiers, image digests and tokens. The same
rule the evidence contract applies to published documents applies to reports — see
[Sanitized provenance](docs/EVIDENCE.md#sanitized-provenance).

A good report contains the smallest reproduction that still fails, the version fields involved (see
below), and **sanitized** output. If a sensitive value is load-bearing, say what kind of value it is
rather than pasting it.

### What to expect

This is an early-stage project maintained alongside other work, so no response-time guarantee is
offered. The intent is to acknowledge a report within a few working days, agree an assessment and a
disclosure timing with the reporter, and credit reporters who want it. If a report turns out to
describe intended behaviour, that answer will say so and point at the document that specifies it.

## Supported versions

The first public release is `0.1.0`; there are no earlier public versions to backport to. Fixes
land on `main` and ship in the next version.

Report against `main`. Please include the version fields relevant to the problem, because this
project versions its data contracts separately from the package: the package version, the evidence
`schema_version` of any document involved, and the `assessment_version` your report output carries.
[`docs/VERSIONING.md`](docs/VERSIONING.md) is the single registry of those fields, their current
values and the rules that govern them — this page deliberately does not copy the numbers.

Python 3.10 or newer is required. The portable core (`doctor`, `first-visit`, `geometry`,
`pool-plan`, `ingest`, `report`, `fingerprint`) declares no runtime dependencies; `jsonschema` is a
test-only extra.

## Scope

In scope, as security issues:

- the portable core reading untrusted input — a crafted evidence document, configuration, model
  config or measurement run that causes code execution, a path escape, or an unbounded read;
- a gate verdict that can be made to **pass or allow performance claims** on evidence that does not
  support it, or a reading that is published when the claim boundary should withhold it. Refusing
  valid evidence is a correctness bug, not a security issue, but it is still worth reporting;
- the measurement scripts doing more than they state — writing outside the paths they document,
  escalating beyond the privileges they ask for, or widening their blast radius;
- the published tree leaking an identifier the evidence contract forbids: a hostname, username,
  internal address, account or cluster name, credential, or private host path;
- CI or release provenance that would let a third party alter what is published.

Out of scope:

- **hardening someone else's serving stack.** The measurement scripts assume a **dedicated or
  otherwise trusted experiment host**: they start and stop containers, read kernel counters, evict
  page cache for the tier's files, and require the tier path to be empty before a run
  ([preconditions](scripts/README.md#preconditions-on-the-host)). They are not a way to run a
  measurement safely on a hostile multi-tenant machine, and this project does not claim otherwise.
  `IMAGE`, the model and vLLM/LMCache themselves are inputs you choose; nothing here judges whether
  a container image is malicious.
- **`kvprobe gate1 capture`** is experimental, root-only, and bounded by design. Its prerequisites
  and limits are documented; running it on a host you do not control is not a vulnerability in this
  project.
- vulnerabilities in vLLM, LMCache, NIXL, cuFile, `nvidia-fs`, Docker, or the kernel. Report those
  upstream. If this project's *use* of them is unsafe, that is in scope.
- a measurement being wrong because a hand-set input was wrong — for example a chunk-size constant
  that does not match the served model. That is a documented trap, not a vulnerability
  ([scripts/README.md](scripts/README.md)).

## A note on what this tool is for

This project exists to refuse unproven claims. A report showing that it can be made to **assert**
something it has not proven is the most valuable kind, and it will be treated as a security issue
even when no memory or privilege boundary is crossed.
