# SPDX-License-Identifier: Apache-2.0
"""Summarize context runs into a reference summary that verify_against_baseline reads.

A regression check compares a candidate with a reviewed reference of the same
workload and configuration scope. Both sides are a ``baseline_version 0.1.0``
summary: ``result["<arm>/<context>"]["summary"]`` pooled over every run of that
arm, plus the ``workload`` and ``scope`` that ``verify_against_baseline.py``
requires to match exactly. This tool builds that summary from the context runs
``g5_controlled_kv_experiment.py`` (and ``nixl_matched.sh``) write.

What a run records, this tool takes from the runs: the prompt identity, the
filler and warm-up settings, the number of runs per arm, and whether the byte
counter was filtered to the tier's device. What a run does not record — model,
accelerator, tensor-parallel size, arm-order seed, chunk geometry — the operator
states in ``--scope``; a stated value that a run contradicts is refused rather
than overwritten.

Usage:
    python3 summarize_runs.py --scope SCOPE.json --output candidate.json \\
        --run recompute randomized-run1-recompute.json \\
        --run native-gds randomized-run1-gds.json ...
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

BASELINE_VERSION = "0.1.0"
#: Scope fields a run records; ``--scope`` may state them, but the runs decide.
RUN_SCOPE_FIELDS = (
    "warmup_requests_per_run",
    "measured_requests_per_run",
    "fillers",
    "filler_tokens",
    "filler_key_policy",
    "arm_order_runs",
    "concurrency",
    "guaranteed_hit",
    "counter_scope",
    "target_device_filtered",
)
#: Device scopes the reference client writes for a per-device cgroup reader.
FILTERED_SCOPES = {"whole_disk", "widened_to_parent_disk"}


class SummaryError(ValueError):
    """Raised when the runs cannot be summarized consistently."""


def percentile(values: list[float], fraction: float) -> float:
    """Return a percentile by the rule the measurement client uses.

    Args:
        values: Non-empty samples.
        fraction: Percentile as a fraction, e.g. 0.95.

    Returns:
        The sample at ``min(int(fraction * n), n - 1)`` of the sorted values.
    """
    ordered = sorted(values)
    return ordered[min(int(fraction * len(ordered)), len(ordered) - 1)]


def load_run(path: Path) -> dict[str, Any]:
    """Load one context run.

    Args:
        path: A context-mode run JSON.

    Returns:
        The decoded run.

    Raises:
        SummaryError: If the file is unreadable or not a context run.
    """
    try:
        run = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SummaryError(f"cannot read {path}: {error}") from error
    if not isinstance(run, dict) or run.get("mode") != "context" or "result" not in run:
        raise SummaryError(f"{path} is not a context run")
    return run


def _one(values: set[Any], what: str) -> Any:
    """Return the single value of a set that must not vary across runs.

    Args:
        values: Values collected from every run.
        what: Name used in the error.

    Returns:
        The one value.

    Raises:
        SummaryError: If the runs disagree.
    """
    if len(values) != 1:
        raise SummaryError(f"runs disagree on {what}: {sorted(map(str, values))}")
    return next(iter(values))


def summarize(
    runs: list[tuple[str, Path]], scope: dict[str, Any], kv_mib_per_chunk: int
) -> dict[str, Any]:
    """Build a reference summary from labelled context runs.

    Args:
        runs: ``(arm label, run path)`` pairs; every arm needs the same number of runs
            at every context.
        scope: Operator-stated scope, e.g. a reference's ``scope`` block.
        kv_mib_per_chunk: KV MiB per chunk, for ``kv_expected_mib``.

    Returns:
        A ``baseline_version 0.1.0`` document without ``source``.

    Raises:
        SummaryError: If runs disagree with each other or with ``scope``.
    """
    pooled: dict[str, list[dict[str, Any]]] = {}
    runs_per_cell: dict[str, int] = {}
    prompts: dict[str, dict[str, Any]] = {}
    versions: set[Any] = set()
    expected_outputs: set[Any] = set()
    arguments: list[dict[str, Any]] = []
    filtered: list[bool] = []
    for label, path in runs:
        run = load_run(path)
        args = run.get("arguments") or {}
        arguments.append(args)
        if run.get("attribution", args.get("attribution")) != "recompute":
            filtered.append(
                bool(run.get("cgroup_io_device"))
                and run.get("cgroup_io_device_scope") in FILTERED_SCOPES
            )
        for context, block in run["result"].items():
            measured = [row for row in block.get("rows", []) if row.get("phase") == "measure"]
            if not measured:
                raise SummaryError(f"{path}: no measured rows at context {context}")
            pooled.setdefault(f"{label}/{context}", []).extend(measured)
            runs_per_cell[f"{label}/{context}"] = runs_per_cell.get(f"{label}/{context}", 0) + 1
            prompt = block.get("prompt") or {}
            versions.add(prompt.get("version"))
            expected_outputs.add(prompt.get("expected_output"))
            identity = {
                "prompt_tokens": prompt.get("prompt_tokens"),
                "sha256": prompt.get("prompt_sha256"),
            }
            if prompts.setdefault(context, identity) != identity:
                raise SummaryError(f"context {context}: runs used different prompts")

    stated = {
        "warmup_requests_per_run": _one({a.get("warmup") for a in arguments}, "warm-up"),
        "measured_requests_per_run": _one({a.get("samples") for a in arguments}, "samples"),
        "fillers": _one({a.get("fillers") for a in arguments}, "fillers"),
        "filler_tokens": _one({a.get("filler_tokens") for a in arguments}, "filler tokens"),
        "filler_key_policy": _one(
            {a.get("filler_key_policy") for a in arguments}, "filler key policy"
        ),
        "arm_order_runs": _one(set(runs_per_cell.values()), "runs per arm and context"),
        "concurrency": 1,
        "guaranteed_hit": True,
        "target_device_filtered": bool(filtered) and all(filtered),
    }
    stated["counter_scope"] = (
        "target_device_filtered_corroboration"
        if stated["target_device_filtered"]
        else "controlled_interval_corroboration"
    )
    for field in RUN_SCOPE_FIELDS:
        if field in scope and scope[field] != stated[field]:
            raise SummaryError(
                f"--scope states {field}={scope[field]!r} but the runs show {stated[field]!r}"
            )
    merged_scope = dict(scope) | stated

    result: dict[str, Any] = {}
    for key, rows in sorted(pooled.items()):
        cacheable = _one({int(row["cacheable_tokens"]) for row in rows}, f"{key} cacheable tokens")
        ttft = [float(row["ttft_s"]) for row in rows]
        result[key] = {
            "summary": {
                "n": len(rows),
                "correct": sum(bool(row.get("correct")) for row in rows),
                "tier_ok": sum(bool(row.get("tier_ok")) for row in rows),
                "cacheable_tokens": cacheable,
                "kv_expected_mib": cacheable
                // int(merged_scope.get("chunk_tokens", 256))
                * kv_mib_per_chunk,
                "ttft_p50_s": round(percentile(ttft, 0.50), 5),
                "ttft_p95_s": round(percentile(ttft, 0.95), 5),
            }
        }

    workload = {
        "profile": _one({a.get("prompt_profile") for a in arguments}, "prompt profile"),
        "version": _one(versions, "prompt version"),
        "seed": _one({a.get("prompt_seed") for a in arguments}, "prompt seed"),
        "expected_output": _one(expected_outputs, "expected output"),
        "prompt_identity": {
            context: prompts[context] for context in sorted(prompts, key=int)
        },
    }
    return {
        "baseline_version": BASELINE_VERSION,
        "mode": "context",
        "workload": workload,
        "scope": merged_scope,
        "result": result,
    }


def parse_args(argv: list[str]) -> argparse.Namespace:
    """Parse command-line arguments.

    Args:
        argv: Arguments without the program name.

    Returns:
        Parsed arguments.
    """
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--run",
        nargs=2,
        action="append",
        metavar=("ARM", "RUN_JSON"),
        required=True,
        help="arm label as the reference names it (e.g. native-gds) and one run file; repeat",
    )
    parser.add_argument(
        "--scope",
        type=Path,
        required=True,
        help="JSON with the operator-stated scope, or a reference whose scope block applies",
    )
    parser.add_argument("--source", default="", help="free-text provenance for the summary")
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Write a reference summary from context runs.

    Args:
        argv: Arguments without the program name; ``sys.argv[1:]`` when omitted.

    Returns:
        0 on success, 1 when the runs cannot be summarized.
    """
    args = parse_args(sys.argv[1:] if argv is None else argv)
    stated = json.loads(args.scope.read_text(encoding="utf-8"))
    scope = stated.get("scope", stated) if isinstance(stated, dict) else {}
    try:
        summary = summarize(
            [(label, Path(path)) for label, path in args.run],
            scope,
            int(scope.get("kv_mib_per_chunk", 64)),
        )
    except SummaryError as error:
        print(f"!! {error}", file=sys.stderr)
        return 1
    summary["source"] = args.source
    args.output.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {args.output}: {len(summary['result'])} arm/context cells")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
