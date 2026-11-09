# SPDX-License-Identifier: Apache-2.0
"""Compare two matched storage evidence sets.

The script rejects structurally unmatched evidence before calculating TTFT.
``--provenance-verified`` is an explicit operator attestation that both arms used
the same host, GPU, SSD/filesystem, model, client revision, and test schedule.
The resulting drift-band verdict is descriptive; it is not a statistical test
of equivalence or significance.
"""

# Standard
import argparse
import json
import math
import os
from pathlib import Path
from statistics import median
from typing import Any, NoReturn


# MiB of native-GDS read per cacheable token, from the model's KV geometry:
# MIB_PER_CHUNK / CHUNK_TOKENS. Qwen2.5-32B = 64/256 = 0.25 (256 KiB/token);
# Llama-3.1-8B = 32/256 = 0.125 (128 KiB/token). Override with PROBE_MIB_PER_CHUNK
# so the matched read-volume gate expects the right per-token bytes.
MIB_PER_CACHEABLE_TOKEN = int(os.environ.get("PROBE_MIB_PER_CHUNK", "64")) / 256
MATCH_ARGUMENTS = ("samples", "warmup", "fillers", "filler_tokens")
REQUIRED_ROW_FIELDS = (
    "phase",
    "prompt_tokens",
    "cacheable_tokens",
    "ttft_s",
    "correct",
    "tier_ok",
)


def percentile(values: list[float], quantile: float) -> float:
    """Return a linearly interpolated percentile.

    Args:
        values: Non-empty list of numeric observations.
        quantile: Percentile expressed as a value from 0.0 through 1.0.

    Returns:
        The interpolated percentile value.

    Raises:
        ValueError: If ``values`` is empty or ``quantile`` is outside [0, 1].
    """
    if not values:
        raise ValueError("values must not be empty")
    if not 0.0 <= quantile <= 1.0:
        raise ValueError("quantile must be between 0 and 1")

    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def load_evidence(
    files: list[Path],
    context: int,
    expected_arm: str,
    expected_backend: str | None,
    provider: str,
) -> tuple[list[dict[str, Any]], tuple[Any, ...], set[str], set[Path]]:
    """Load and validate one side of a matched storage comparison.

    Args:
        files: Evidence JSON files for one comparison arm.
        context: Context length to select from each evidence file.
        expected_arm: Exact experiment-arm identity expected in every file.
        expected_backend: Reference backend identity, or ``None`` for legacy
            evidence that did not record one.
        provider: Counter class, either ``gds`` or ``posix``.

    Returns:
        Measured rows, schedule signature, container identities, and source paths.

    Raises:
        SystemExit: If evidence is malformed, incomplete, or internally unmatched.
    """
    if not files:
        _fail("each arm requires at least one evidence file")

    resolved_sources = {path.resolve() for path in files}
    if len(resolved_sources) != len(files):
        _fail("an evidence file is repeated within one arm")

    all_rows: list[dict[str, Any]] = []
    schedules: set[tuple[Any, ...]] = set()
    containers: set[str] = set()

    for path in files:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            _fail(f"cannot load {path}: {exc}")
        if not isinstance(payload, dict):
            _fail(f"{path}: top-level JSON must be an object")
        if payload.get("mode") != "context":
            _fail(f"{path}: mode must be 'context'")
        if payload.get("arm") != expected_arm:
            _fail(
                f"{path}: arm must be {expected_arm!r}, "
                f"got {payload.get('arm')!r}"
            )

        container = payload.get("container")
        if not isinstance(container, str) or not container.strip():
            _fail(f"{path}: a non-empty container identity is required")
        containers.add(container)

        arguments = payload.get("arguments")
        if not isinstance(arguments, dict):
            _fail(f"{path}: arguments must be an object")
        missing_arguments = [name for name in MATCH_ARGUMENTS if name not in arguments]
        if missing_arguments:
            _fail(f"{path}: missing match arguments: {', '.join(missing_arguments)}")
        if arguments.get("mode") != "context" or arguments.get("arm") != expected_arm:
            _fail(
                f"{path}: arguments mode/arm must be "
                f"context/{expected_arm}"
            )
        _validate_recorded_identity(
            path,
            payload,
            arguments,
            "attribution",
            provider,
            required=expected_backend is not None,
        )
        if expected_backend is not None:
            _validate_recorded_identity(
                path,
                payload,
                arguments,
                "backend",
                expected_backend,
                required=True,
            )
        contexts = arguments.get("contexts")
        if not isinstance(contexts, list) or context not in contexts:
            _fail(f"{path}: arguments contexts must include {context}")
        numeric_schedule = tuple(arguments[name] for name in MATCH_ARGUMENTS)
        if not all(
            isinstance(value, int) and value >= 0 for value in numeric_schedule
        ):
            _fail(f"{path}: match arguments must be non-negative integers")
        filler_key_policy = arguments.get("filler_key_policy", "unique")
        if filler_key_policy not in {"unique", "reuse"}:
            _fail(f"{path}: invalid filler_key_policy {filler_key_policy!r}")
        schedule = numeric_schedule + (filler_key_policy,)
        schedules.add(schedule)

        result = payload.get("result")
        if not isinstance(result, dict):
            _fail(f"{path}: result must be an object")
        context_result = result.get(str(context))
        if not isinstance(context_result, dict):
            _fail(f"{path}: result has no object for context {context}")
        rows = context_result.get("rows")
        if not isinstance(rows, list):
            _fail(f"{path}: context {context} rows must be a list")

        measured_rows: list[dict[str, Any]] = []
        warmup_rows = 0
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                _fail(f"{path}: row {index} must be an object")
            if row.get("phase") == "warmup":
                warmup_rows += 1
            if row.get("phase") == "measure":
                missing_fields = [
                    field for field in REQUIRED_ROW_FIELDS if field not in row
                ]
                if missing_fields:
                    _fail(
                        f"{path}: measured row {index} missing "
                        f"{', '.join(missing_fields)}"
                    )
                measured_rows.append(row)
        if not measured_rows:
            _fail(f"{path}: context {context} has no measured rows")
        expected_samples, expected_warmup = schedule[0], schedule[1]
        if len(measured_rows) != expected_samples or warmup_rows != expected_warmup:
            _fail(
                f"{path}: row phases do not match samples/warmup schedule "
                f"({len(measured_rows)}/{warmup_rows} vs "
                f"{expected_samples}/{expected_warmup})"
            )
        all_rows.extend(measured_rows)

    if len(schedules) != 1:
        _fail(f"evidence files within one arm use different schedules: {schedules!r}")
    return all_rows, schedules.pop(), containers, resolved_sources


def validate_match(
    side_a: tuple[list[dict[str, Any]], tuple[Any, ...], set[str], set[Path]],
    side_b: tuple[list[dict[str, Any]], tuple[Any, ...], set[str], set[Path]],
    provider: str,
) -> None:
    """Enforce the hard validity gates for a matched comparison.

    Args:
        side_a: Loaded evidence metadata for the first arm.
        side_b: Loaded evidence metadata for the second arm.
        provider: Counter class shared by both arms.

    Raises:
        SystemExit: If the arms are not demonstrably comparable.
    """
    rows_a, schedule_a, containers_a, sources_a = side_a
    rows_b, schedule_b, containers_b, sources_b = side_b

    if sources_a & sources_b:
        _fail("the same evidence file cannot be used for both arms")
    if containers_a & containers_b:
        _fail("the two arms must use distinct container identities")
    if schedule_a != schedule_b:
        _fail(f"schedule differs: A={schedule_a!r}, B={schedule_b!r}")
    if len(rows_a) != len(rows_b):
        _fail(f"measured row count differs: A={len(rows_a)}, B={len(rows_b)}")

    prompt_tokens_a = _uniform_integer(rows_a, "prompt_tokens", "A")
    prompt_tokens_b = _uniform_integer(rows_b, "prompt_tokens", "B")
    if prompt_tokens_a != prompt_tokens_b:
        _fail(f"prompt_tokens differs: A={prompt_tokens_a}, B={prompt_tokens_b}")

    cacheable_tokens_a = _uniform_integer(rows_a, "cacheable_tokens", "A")
    cacheable_tokens_b = _uniform_integer(rows_b, "cacheable_tokens", "B")
    if cacheable_tokens_a != cacheable_tokens_b:
        _fail(
            "cacheable_tokens differs: "
            f"A={cacheable_tokens_a}, B={cacheable_tokens_b}"
        )

    expected_read_mib = cacheable_tokens_a * MIB_PER_CACHEABLE_TOKEN
    counter_field = "nvfs_read_mib" if provider == "gds" else "cgroup_read_bytes"
    expected_counter = (
        expected_read_mib
        if provider == "gds"
        else expected_read_mib * 1024 * 1024
    )
    absolute_tolerance = 1.0 if provider == "gds" else 1024 * 1024
    for label, rows in (("A", rows_a), ("B", rows_b)):
        for index, row in enumerate(rows):
            if row["correct"] is not True:
                _fail(f"{label} measured row {index}: correct is not true")
            if row["tier_ok"] is not True:
                _fail(f"{label} measured row {index}: tier_ok is not true")
            counter = row.get(counter_field)
            if not isinstance(counter, (int, float)) or isinstance(counter, bool):
                _fail(f"{label} measured row {index}: {counter_field} is not numeric")
            if not math.isclose(
                float(counter),
                expected_counter,
                rel_tol=0.05,
                abs_tol=absolute_tolerance,
            ):
                _fail(
                    f"{label} measured row {index}: {counter_field}={counter} "
                    f"does not match expected {expected_counter:.1f}"
                )


def summarize_side(
    rows: list[dict[str, Any]], provider: str
) -> dict[str, float | int]:
    """Summarize TTFT and selected read telemetry for validated rows.

    Args:
        rows: Measured rows that passed ``validate_match``.
        provider: Counter class shared by both arms.

    Returns:
        Count, TTFT percentiles, median read volume, and median retrieval rate.
    """
    ttft = [_positive_number(row["ttft_s"], "ttft_s") for row in rows]
    if provider == "gds":
        reads = [_number(row["nvfs_read_mib"], "nvfs_read_mib") for row in rows]
    else:
        reads = [
            _number(row["cgroup_read_bytes"], "cgroup_read_bytes") / 1024**2
            for row in rows
        ]
    retrieval = [
        _number(row["retrieve_gib_s"], "retrieve_gib_s")
        for row in rows
        if row.get("retrieve_gib_s") is not None
    ]
    return {
        "n": len(rows),
        "ttft_p50_s": median(ttft),
        "ttft_p95_s": percentile(ttft, 0.95),
        "observed_read_mib_p50": median(reads),
        "retrieve_gib_s_p50": median(retrieval) if retrieval else math.nan,
    }


def main() -> None:
    """Run the command-line comparator."""
    args = _parse_args()
    side_a = load_evidence(
        args.a, args.context, args.arm_a, args.backend_a, args.provider
    )
    side_b = load_evidence(
        args.b, args.context, args.arm_b, args.backend_b, args.provider
    )
    validate_match(side_a, side_b, args.provider)

    summary_a = summarize_side(side_a[0], args.provider)
    summary_b = summarize_side(side_b[0], args.provider)
    p50_a = float(summary_a["ttft_p50_s"])
    p50_b = float(summary_b["ttft_p50_s"])
    relative_change = (p50_b - p50_a) / p50_a
    drift = abs(relative_change)

    print(f"context={args.context}")
    print("MATCH GATE: PASS")
    print(
        f"  both envelopes: mode=context, provider={args.provider}; "
        f"arms={args.arm_a},{args.arm_b}"
    )
    print("  schedules, row counts, prompt/cacheable tokens: identical")
    print("  every row: correct=true, tier_ok=true, selected read bytes plausible")
    print("  evidence sources and container identities: distinct")
    print("  external provenance attestation: verified by operator")
    print()
    print(
        "arm\tn\tttft_p50_s\tttft_p95_s\t"
        "observed_read_mib_p50\tretrieve_gib_s_p50"
    )
    print(_format_summary(args.label_a, summary_a))
    print(_format_summary(args.label_b, summary_b))
    print()
    relation = "inside" if drift <= args.tolerance else "outside"
    direction = "lower" if relative_change < 0 else "higher"
    print(
        f"DESCRIPTIVE VERDICT: {args.label_b} p50 TTFT is "
        f"{abs(relative_change):.1%} {direction} than {args.label_a}, {relation} "
        f"the configured {args.tolerance:.1%} band."
    )
    print("This is not a statistical equivalence or significance test.")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", type=int, required=True)
    parser.add_argument(
        "--provider",
        choices=["gds", "posix"],
        default="gds",
        help="Tier-attribution counter class shared by both arms (default: gds).",
    )
    parser.add_argument("--a-arm", dest="arm_a", default="gds")
    parser.add_argument("--b-arm", dest="arm_b", default="gds")
    parser.add_argument("--a-backend", dest="backend_a")
    parser.add_argument("--b-backend", dest="backend_b")
    parser.add_argument("--label-a", "--a-label", dest="label_a", default="native-GDS")
    parser.add_argument("--label-b", "--b-label", dest="label_b", default="NIXL-GDS_MT")
    parser.add_argument(
        "--a", "--a-files", dest="a", type=Path, nargs="+", required=True
    )
    parser.add_argument(
        "--b", "--b-files", dest="b", type=Path, nargs="+", required=True
    )
    parser.add_argument(
        "--tolerance",
        type=float,
        default=0.05,
        help="Descriptive relative p50 drift band (default: 0.05).",
    )
    parser.add_argument(
        "--provenance-verified",
        action="store_true",
        required=True,
        help=(
            "Attest that both arms used the same host, GPU, SSD/filesystem, "
            "model, client revision, and controlled schedule."
        ),
    )
    args = parser.parse_args()
    if args.context <= 0:
        parser.error("--context must be positive")
    if not 0.0 <= args.tolerance <= 1.0:
        parser.error("--tolerance must be between 0 and 1")
    return args


def _uniform_integer(rows: list[dict[str, Any]], field: str, label: str) -> int:
    values: set[int] = set()
    for row in rows:
        value = row[field]
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            _fail(f"{label}: {field} must be a non-negative integer")
        values.add(value)
    if len(values) != 1:
        _fail(f"{label}: {field} varies across measured rows: {values!r}")
    return values.pop()


def _number(value: Any, field: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        _fail(f"{field} must be numeric")
    numeric = float(value)
    if not math.isfinite(numeric) or numeric < 0:
        _fail(f"{field} must be a finite non-negative number")
    return numeric


def _positive_number(value: Any, field: str) -> float:
    numeric = _number(value, field)
    if numeric == 0:
        _fail(f"{field} must be greater than zero")
    return numeric


def _format_summary(label: str, summary: dict[str, float | int]) -> str:
    return "\t".join(
        (
            label,
            str(summary["n"]),
            f"{summary['ttft_p50_s']:.4f}",
            f"{summary['ttft_p95_s']:.4f}",
            f"{summary['observed_read_mib_p50']:.1f}",
            f"{summary['retrieve_gib_s_p50']:.4f}",
        )
    )


def _fail(message: str) -> NoReturn:
    raise SystemExit(f"MATCH GATE FAIL: {message}")


def _validate_recorded_identity(
    path: Path,
    payload: dict[str, Any],
    arguments: dict[str, Any],
    field: str,
    expected: str,
    *,
    required: bool,
) -> None:
    """Validate a top-level/argument identity without guessing a missing value.

    Args:
        path: Evidence path used in error messages.
        payload: Raw evidence envelope.
        arguments: Recorded command arguments.
        field: Identity field to validate.
        expected: Exact expected value.
        required: Whether at least one location must record the field.

    Raises:
        SystemExit: If the identity is missing, contradictory, or unexpected.
    """
    recorded = [
        value
        for value in (payload.get(field), arguments.get(field))
        if value is not None
    ]
    if required and not recorded:
        _fail(f"{path}: {field} must be recorded as {expected!r}")
    if any(not isinstance(value, str) or not value for value in recorded):
        _fail(f"{path}: {field} must be a non-empty string")
    if recorded and any(value != recorded[0] for value in recorded[1:]):
        _fail(f"{path}: envelope and arguments disagree on {field}")
    if recorded and recorded[0] != expected:
        _fail(
            f"{path}: {field} must be {expected!r}, got {recorded[0]!r}"
        )


if __name__ == "__main__":
    main()
