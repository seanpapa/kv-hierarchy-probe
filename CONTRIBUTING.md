# Contributing

This project exists to refuse performance claims that the evidence does not support. Contributions
are judged by the same rule: a change is welcome when it makes a verdict more correct, a procedure
easier to follow, or a new tier or stack measurable — and it has to show its evidence.

## Before you open an issue

- **Never paste raw measurement material.** Serving logs, `docker inspect` output, `provenance.txt`,
  container startup logs and evidence bundles routinely carry hostnames, usernames, internal
  addresses, mount and model paths, image digests and tokens. Reduce the problem to the smallest
  reproduction and sanitize what remains ([Sanitized provenance](docs/EVIDENCE.md#sanitized-provenance)).
  If a sensitive value matters, say what kind of value it is instead of pasting it.
- **Security problems go through private reporting**, not a public issue — see [SECURITY.md](SECURITY.md).
  That includes any way to make a gate pass or allow claims on evidence that does not support it.
- **State the version fields involved:** the package version, the evidence `schema_version`, and the
  `assessment_version` from your report output ([VERSIONING](docs/VERSIONING.md)).

The issue forms ask for exactly this. A bug report is most useful when it names the command, the
expected and the observed verdict, and a sanitized input that reproduces the difference.

## What fits

| Contribution | What it needs |
|---|---|
| A wrong verdict, result or message | A failing test with the smallest evidence document that shows it, then the fix |
| A first-visit or documentation problem | The exact command or page, what you expected, what you saw |
| An evidence adapter for another stack or tier | See [New evidence sources](#new-evidence-sources) |
| A new failure pattern | The pattern without host-specific values, how to confirm it, and what it invalidates — the shape of [FAILURE-CATALOG](docs/FAILURE-CATALOG.md) |

Not in scope: benchmark comparisons between products presented as claims, tuning a configuration
until a gate passes, and hardening someone else's serving stack. A gate passes when its measurement
is valid; what the measurement found is a result, and a slow or unnecessary tier is a valid result.

## Pull requests

- Keep each pull request to one change, with a description of what it changes and why.
- Run the suite. The schema checks need the test extra:

  ```bash
  python3 -m pip install -e '.[test]'
  python3 -m unittest discover -s tests
  ```

  A change that leaves schema tests skipped is not ready.
- A change to a verdict, a data contract or a written field also updates
  [VERSIONING](docs/VERSIONING.md) and adds a test that shows the new behaviour.
- Never loosen a gate without evidence that the old rule refused something valid. Tightening is
  welcome; widening what passes needs a stronger argument than convenience.
- Examples are either synthetic and labelled as such, or sanitized with their provenance stated
  ([examples/README](examples/README.md)).
- Python follows the existing code: type hints on every function, a docstring that matches what the
  function does, no runtime dependency in the portable core (`kvprobe/`).
- A measurement script states every effect it has on the host
  ([What the launchers do to the host](scripts/README.md#what-the-launchers-do-to-the-host)); a new
  effect is documented in the same change.

## New evidence sources

Gate 1 accepts per-request bytes from outside the serving framework, filtered to the tier under
test. Today only a block-device counter meets that bar ([Scope](README.md#scope--what-is-verified-today)).
An adapter for another tier or stack must provide:

1. request-scoped evidence from outside the serving framework, and the grade it supports;
2. a matched recompute arm; and
3. a way to expose or disable transparent fallback.

Produce the documented [external-run shape](docs/EXTERNAL-RUNS.md), add a fixture and tests that
cover a pass, a fallback and a phantom hit, and mark the path "not hardware-verified" until a
hardware run is recorded. An adapter that cannot meet the bar is still useful if it reports
`UNPROVEN` honestly.

## AI-assisted contributions

AI tools may be used. The person who submits the change is responsible for every line of it: read
the diff, run the suite, and be able to explain each change. Do not paste confidential material
into an AI tool. Say in the pull request description when an AI tool wrote a substantial part of the
change. This project was itself developed with AI coding assistants under its author's direction
and review.

## Licence

Contributions are accepted under the project's licence, Apache-2.0 (see [LICENSE](LICENSE) and
[NOTICE](NOTICE)); its section 5 covers what you submit. No separate agreement is required.

## Support

This project is maintained alongside other work; replies are best effort. These are treated first:
a broken installation, a wrong verdict or claim boundary, exposure of sensitive data, and an
unusable first-visit path. Feature requests and broader hardware coverage are welcome, and are
planned rather than handled as emergencies.
