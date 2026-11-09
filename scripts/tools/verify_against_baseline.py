# SPDX-License-Identifier: Apache-2.0
"""Compare a probe run against a compatible reference, within tolerance.

This is the regression merge gate for changes to measurement behavior. The
reference and candidate must have the same workload identity and configuration
scope; this tool makes their metric comparison mechanical and reviewable. It
needs no GPU and no server -- it reads two JSON summaries and reports whether
their headline metrics are indistinguishable.

It understands both evidence shapes this repository produces:

* `g2_context_sweep.py` -> `sweep-summary.json` (a list of per-context rows).
* `g5_controlled_kv_experiment.py` -> an envelope `{... "result": {...}}` keyed by
  context or concurrency.
* A sanitized reference summary -> the same result/summary envelope keyed by
  arm and context.

Latency and throughput metrics are compared with a relative tolerance (default
5%, chosen to sit above the ~1-3% thermal/clock drift documented for the test
host). Integer gate counts -- correctness, tier attribution, zero-disk-read,
tier-purity verdicts -- must match exactly: a change there is a behavioral
change, not drift.

Usage:
    python3 verify_against_baseline.py --baseline REFERENCE.json --candidate NEW.json
    python3 verify_against_baseline.py -b REFERENCE.json -c NEW.json --tolerance 0.03

Exit status is 0 when every compared metric is within tolerance and every gate
count matches, and 1 otherwise.
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any

# Metrics compared with a relative tolerance (drift is expected here).
# Note: derived differences of near-equal latencies (e.g. saving_s = recompute - gds)
# are intentionally NOT listed. They amplify ordinary drift at small contexts; the two
# latencies that determine them are compared directly instead.
RELATIVE_METRICS = frozenset(
    {
        "recompute_ttft_p50",
        "gds_ttft_p50",
        "speedup",
        "gds_retrieve_gbs",
        "ttft_p50_s",
        "ttft_p95_s",
        "ttft_p99_s",
        "total_p50_s",
        "requests_per_s",
        "cacheable_tokens_per_s",
    }
)

# Gate counts and verdicts that must match exactly (a difference is behavioral).
EXACT_METRICS = frozenset(
    {
        "n",
        "correct",
        "tier_ok",
        "tier_ok_batches",
        "batches",
        "requests",
        "gds_zero_disk_read",
        "tier_purity_ok",
        "cacheable_tokens",
        "prompt_tokens",
        "kv_expected_mib",
    }
)

IDENTITY_FIELDS = ("workload", "scope")


def load(path: Path) -> Any:
    """Load and decode a JSON evidence file.

    Args:
        path: Path to a JSON file.

    Returns:
        The decoded JSON object.

    Raises:
        SystemExit: If the file cannot be read or parsed.
    """
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise SystemExit(f"cannot read {path}: {error}")


def flatten(evidence: Any) -> dict[str, dict[str, float]]:
    """Reduce an evidence object to {label: {metric: value}}.

    Args:
        evidence: A `sweep-summary.json` list or a `controlled_kv` envelope.

    Returns:
        A mapping from a stable label (context or concurrency) to the numeric
        metrics measured there. Only keys in RELATIVE_METRICS or EXACT_METRICS
        are kept; everything else (telemetry, timestamps, raw rows) is ignored.
    """
    comparable = RELATIVE_METRICS | EXACT_METRICS
    flat: dict[str, dict[str, float]] = {}

    def keep(source: dict[str, Any]) -> dict[str, float]:
        return {
            key: float(value)
            for key, value in source.items()
            if key in comparable and isinstance(value, (int, float, bool))
        }

    if isinstance(evidence, list):
        # g2_context_sweep.py summary: one row per context.
        for row in evidence:
            if isinstance(row, dict) and "cacheable_tokens" in row:
                label = f"ctx={row.get('cacheable_tokens')}"
                metrics = keep(row)
                if metrics:
                    flat[label] = metrics
        return flat

    if isinstance(evidence, dict) and "result" in evidence:
        # g5_controlled_kv_experiment.py envelope keyed by context or concurrency.
        prefix = "c" if evidence.get("mode") == "concurrency" else "ctx"
        for key, block in (evidence["result"] or {}).items():
            summary = block.get("summary", {}) if isinstance(block, dict) else {}
            metrics = keep(summary)
            if metrics:
                flat[f"{prefix}={key}"] = metrics
        return flat

    raise SystemExit("unrecognized evidence format (expected a sweep summary or a run envelope)")


def compare(
    baseline: dict[str, dict[str, float]],
    candidate: dict[str, dict[str, float]],
    tolerance: float,
) -> list[str]:
    """Compare candidate metrics against baseline and collect failures.

    Args:
        baseline: Flattened baseline metrics.
        candidate: Flattened candidate metrics.
        tolerance: Allowed relative difference for RELATIVE_METRICS.

    Returns:
        A list of human-readable failure lines; empty means the run matches.
    """
    failures: list[str] = []

    missing = sorted(set(baseline) - set(candidate))
    for label in missing:
        failures.append(f"[{label}] present in baseline but missing from candidate")

    for label in sorted(set(baseline) & set(candidate)):
        base_metrics = baseline[label]
        cand_metrics = candidate[label]
        # A point the baseline itself discarded (tier purity failed) carries no valid
        # measurement — only require the purity verdict to still match, skip its numbers.
        discarded = base_metrics.get("tier_purity_ok", 1.0) == 0.0
        for metric, base_value in sorted(base_metrics.items()):
            if discarded and metric != "tier_purity_ok":
                continue
            if metric not in cand_metrics:
                failures.append(f"[{label}] {metric}: missing from candidate")
                continue
            cand_value = cand_metrics[metric]
            if metric in EXACT_METRICS:
                if base_value != cand_value:
                    failures.append(
                        f"[{label}] {metric}: baseline {base_value:g} != candidate {cand_value:g} (must match exactly)"
                    )
                continue
            denominator = abs(base_value) if base_value else 1.0
            relative = abs(cand_value - base_value) / denominator
            if relative > tolerance:
                failures.append(
                    f"[{label}] {metric}: baseline {base_value:.4g} vs candidate {cand_value:.4g} "
                    f"= {relative * 100:.1f}% > {tolerance * 100:.1f}%"
                )
    return failures


def compare_identity(baseline: Any, candidate: Any) -> list[str]:
    """Require reference identity metadata to match exactly when present.

    Args:
        baseline: Decoded reference evidence.
        candidate: Decoded candidate evidence.

    Returns:
        Human-readable identity failures; empty means the recorded identity
        fields match or the legacy reference does not define them.
    """
    if not isinstance(baseline, dict):
        return []
    if not isinstance(candidate, dict):
        return ["candidate cannot carry reference identity metadata"]

    failures: list[str] = []
    for field in IDENTITY_FIELDS:
        if field not in baseline:
            continue
        if field not in candidate:
            failures.append(f"{field}: missing from candidate")
        elif candidate[field] != baseline[field]:
            failures.append(f"{field}: candidate does not match reference")
    return failures


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments.

    Returns:
        Parsed arguments.
    """
    parser = argparse.ArgumentParser(description="Compare a probe run against a recorded baseline.")
    parser.add_argument(
        "--baseline",
        "-b",
        type=Path,
        required=True,
        help="compatible reviewed reference JSON",
    )
    parser.add_argument(
        "--candidate",
        "-c",
        type=Path,
        required=True,
        help="candidate summary JSON with matching workload and configuration scope",
    )
    parser.add_argument(
        "--tolerance",
        "-t",
        type=float,
        default=0.05,
        help="allowed relative difference for latency/throughput metrics (default 0.05)",
    )
    return parser.parse_args()


def main() -> int:
    """Run the comparison and report the verdict.

    Returns:
        Process exit status: 0 if the run matches the baseline, 1 otherwise.
    """
    args = parse_args()
    baseline_evidence = load(args.baseline)
    candidate_evidence = load(args.candidate)
    baseline = flatten(baseline_evidence)
    candidate = flatten(candidate_evidence)
    if not baseline:
        raise SystemExit("baseline contained no comparable metrics")

    failures = compare_identity(baseline_evidence, candidate_evidence)
    failures.extend(compare(baseline, candidate, args.tolerance))

    labels = sorted(set(baseline) & set(candidate))
    print(f"compared {len(labels)} label(s) at tolerance {args.tolerance * 100:.1f}%")
    if failures:
        print(f"\nFAIL - {len(failures)} difference(s):")
        for line in failures:
            print(f"  {line}")
        print("\nThe run is NOT indistinguishable from the baseline. Do not merge to main.")
        return 1
    print("PASS - every compared metric is within tolerance and every gate count matches.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
