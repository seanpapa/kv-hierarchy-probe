# SPDX-License-Identifier: Apache-2.0
"""Command-line interface for capability checks and evidence reports."""

# Standard
import argparse
import json
import os
import sys
import textwrap
from collections.abc import Sequence
from pathlib import Path

# Local
from .configuration import ConfigurationError, fingerprint_configuration
from .doctor import build_manifest, find_gdscheck, render_manifest
from .geometry import GeometryError, derive_geometry
from .ingest import IngestError, ingest_files
from .model_source import (
    ModelSourceError,
    load_model_config_file,
    load_model_reference,
)
from .nvme_capture import CaptureError, capture_namespace
from .nvme_plan import TracePlanError, build_trace_plan, render_trace_plan
from .nvme_trace import TraceError, analyze_trace, render_trace
from .pool_sizing import (
    PoolSizingError,
    bracket_pool_slots,
    calculate_pool_plan,
    derive_required_live_chunks,
)
from .readings import compute_readings
from .report import EvidenceError, assess_evidence, render_markdown, render_text
from .serving_verification import VerificationError, render_verification, verify_serving
from .tier_check import (
    ARMS,
    DEFAULT_CHUNK_TOKENS,
    DEFAULT_CONTEXT_TOKENS,
    DEFAULT_FILLERS,
    NOT_READY,
    LocalHost,
    PoolSizes,
    TierCheckRequest,
    is_supported_platform,
    render_tier_check,
    run_tier_check,
)

_DEMO_EXPECTED = {
    "pass-all-attempts.json": "PASS",
    "attribution-unfiltered-counter.json": "UNPROVEN",
    "envelope-collapse-small-pool.json": "PARTIAL",
    "phantom-tier-framework-hit.json": "FAIL",
}

# (data origin, what is checked, what the verdict means) for each bundled case.
_DEMO_NOTES = {
    "pass-all-attempts.json": (
        "synthetic, hand-written sample",
        "Does evidence in which every request is correct and moved the expected "
        "bytes on the tier's own device pass all five gates, with Gate 3 counting "
        "every admission attempt, including fallbacks?",
        "PASS - every gate passed, so performance claims are allowed.",
    ),
    "attribution-unfiltered-counter.json": (
        "measured, 4096-token context run on a unified-memory aarch64 host",
        "Can the byte counter prove the tier served each request? Here the counter "
        "was host-wide, not filtered to the tier's device.",
        "UNPROVEN - bytes matched on every request, so nothing is violated, but the "
        "evidence cannot show the tier served them. Claims are withheld and Gates 2-5 "
        "are skipped because they rely on Gate 1.",
    ),
    "envelope-collapse-small-pool.json": (
        "measured, concurrency sweep with a small staging pool",
        "Up to what concurrency does the tier still serve every request (Gate 5 only)?",
        "PARTIAL - the Gate 5 measurement is valid: every batch met its tier-byte and "
        "correctness checks up to a lower concurrency, then batches stopped meeting "
        "them. Gates 1-4 are skipped because "
        "a concurrency run does not record what they need, so no overall claim.",
    ),
    "phantom-tier-framework-hit.json": (
        "synthetic, from a block-tier test fixture",
        "The framework reported cache hits. Did the tier's device actually read "
        "any bytes?",
        "FAIL - the framework said hit but the device read nothing, a measured "
        "violation. Claims are withheld and Gates 2-5 are skipped.",
    ),
}


_DEMO_INDENT = "       "
_DEMO_WIDTH = 100

_FIRST_VISIT_GATES = (
    ("1", "REALITY", "Did the intended tier actually serve each request?"),
    ("2", "BENEFIT", "At what context does retrieval beat recompute?"),
    ("3", "ECONOMICS", "How many realized reuses repay one admission?"),
    ("4", "NECESSITY", "When does upper-tier capacity make this tier useful?"),
    ("5", "LIMIT", "Up to what load does the tier keep serving every request?"),
)


def _hanging(prefix: str, text: str) -> str:
    """Wrap ``text`` after ``prefix`` with continuation lines under the text."""
    return textwrap.fill(
        text,
        width=_DEMO_WIDTH,
        initial_indent=prefix,
        subsequent_indent=" " * len(prefix),
        break_on_hyphens=False,
    )


def _gate_row(gate_id: str, label: str, text: str) -> str:
    """Format one gate row so every row's text starts in the same column."""
    return _hanging(f"  Gate {gate_id} · {label:<9}  ", text)


def _sentence(text: str) -> str:
    """End a fragment with a period unless it already ends a sentence."""
    text = text.strip()
    return text if text.endswith((".", "?", "!")) else text + "."


def _demo_field(label: str, text: str) -> str:
    """Wrap one labelled demo line with a hanging indent under its text."""
    prefix = f"{_DEMO_INDENT}{label}"
    wrapped = textwrap.fill(
        text,
        width=_DEMO_WIDTH,
        initial_indent=prefix,
        subsequent_indent=" " * len(prefix),
        break_on_hyphens=False,
    )
    # A no-break space keeps an item such as "4 (SKIP_UNSUPPORTED)" on one line.
    return wrapped.replace("\u00a0", " ")


def _geometry_summary(geometry: dict, output: Path | None) -> str:
    """Explain computed geometry and the next operator check."""
    runtime = geometry["runtime"]
    values = geometry["geometry"]
    selection = geometry["selection"]
    lines = ["kvprobe geometry — derived result"]
    if output is not None:
        lines.append(f"  Saved JSON: {output}")
    model = geometry["model"]
    if model.get("input_source") == "dimensions":
        lines.append(
            f"  Model inputs: layers={model['num_hidden_layers']}, "
            f"kv_heads={model['num_key_value_heads']}, head_dim={model['head_dim']} "
            "(entered as numbers, not read from a model file)"
        )
    else:
        source = model["config_source"]
        if source["kind"] == "huggingface":
            lines.append(
                "  Model config: "
                f"{source['model_id']}@{source['resolved_revision']} "
                "(Hugging Face config.json only; no weights)"
            )
        elif source["kind"] == "local_model_directory":
            lines.append(
                f"  Model config: local model directory {source['model_name']}"
            )
        lines.append(f"  Config SHA-256: {source['config_sha256']}")
    lines.append(
        f"  Runtime inputs: {runtime['dtype']} ({runtime['dtype_source']}), "
        f"chunk={runtime['chunk_tokens']} tokens, "
        f"TP={runtime['tensor_parallel_size']}, PP={runtime['pipeline_parallel_size']}"
    )
    for name, label in (
        ("full_model", "Full model"),
        ("per_tp_rank", "Per TP rank"),
        ("tp_group_aggregate", "TP-group aggregate"),
    ):
        value = values[name]
        lines.append(
            f"  {label}: {value['bytes_per_token']:,} bytes/token; "
            f"{value['bytes_per_chunk']:,} bytes/chunk "
            f"({value['bytes_per_chunk'] / 2**20:g} MiB)"
        )
    lines.extend(
        [
            (
                "  Counter selection (bytes/chunk): "
                f"per-device={selection['per_device_counter_bytes_per_chunk']:,}; "
                f"TP-group={selection['tp_group_counter_bytes_per_chunk']:,}"
            ),
            "  Derived from supplied config/settings; live serving settings and rank placement were not observed.",
            "  Next: verify model revision, dtype, chunk size and TP/PP against the serving process.",
            "  Select bytes_per_chunk matching the counter scope and record it in your experiment configuration.",
            "  This prepares expected bytes for Gate 1; it does not establish tier service or a gate PASS.",
            "  See docs/GEOMETRY.md and docs/CONFIGURATION.md.",
        ]
    )
    return "\n".join(lines)


def _positive_int(value: str) -> int:
    """Parse a command-line integer that must be at least one.

    Args:
        value: Argument text.

    Returns:
        The integer.

    Raises:
        argparse.ArgumentTypeError: If it is not an integer of at least one.
    """
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not an integer: {value}") from exc
    if number < 1:
        raise argparse.ArgumentTypeError(f"must be at least 1: {value}")
    return number


def _non_negative_int(value: str) -> int:
    """Parse a command-line integer that must be zero or more.

    Args:
        value: Argument text.

    Returns:
        The integer.

    Raises:
        argparse.ArgumentTypeError: If it is not an integer of at least zero.
    """
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not an integer: {value}") from exc
    if number < 0:
        raise argparse.ArgumentTypeError(f"must be zero or more: {value}")
    return number


def _write_output(content: str, output: Path | None) -> None:
    """Write command output to a file or standard output.

    Args:
        content: Text to emit.
        output: Destination path, or ``None`` for standard output.

    Returns:
        None.
    """
    if output is None:
        print(content)
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(content.rstrip() + "\n", encoding="utf-8")
    print(f"wrote {output}")


def _emit_result(
    summary: str, rendered: str, output: Path | None, as_json: bool
) -> None:
    """Show a derived result as a summary, as JSON, or both.

    The summary is the default because it answers the operator's question. JSON
    goes to standard output only when asked for, with the summary moved to
    standard error so a script reading standard output gets JSON alone.

    Args:
        summary: Human-readable explanation of the result.
        rendered: JSON text of the result.
        output: File to save the JSON to, or ``None``.
        as_json: Whether standard output carries the JSON.

    Returns:
        None.

    Raises:
        OSError: If the output file cannot be written.
    """
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered.rstrip() + "\n", encoding="utf-8")
    if as_json:
        print(rendered)
        print(summary, file=sys.stderr)
    else:
        print(summary)


def _gate1_capture(args: argparse.Namespace) -> int:
    """Capture experimental raw observations; exit one is not a serving failure."""
    try:
        result = capture_namespace(args.disk, args.run_dir, args.duration_seconds)
    except (OSError, CaptureError) as exc:
        print(f"capture error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    print("Experimental capture retained; scoped accounting is not Gate 1 PASS.")
    return 0 if (
        result["loss_events"] == 0 and result["capture_exit_code"] == 0
        and result["capture_ready"] and result["controller_id"] is not None
        and not result["loss_accounting"]["reasons"]
    ) else 1


def _gate1_plan(args: argparse.Namespace) -> int:
    """Run read-only preflight without starting or approving capture.

    Args:
        args: Literal namespace and optional JSON destination.

    Returns:
        Zero for eligible prerequisites, one for unavailable/unsupported host
        prerequisites, or two for invalid arguments/output errors. Never a PASS.
    """
    try:
        if args.output is not None and any(
            args.output.resolve().is_relative_to(root)
            for root in (Path("/sys"), Path("/proc"), Path("/dev"))
        ):
            raise TracePlanError("output must not target /sys, /proc or /dev")
        plan = build_trace_plan(args.disk)
        _emit_result(
            render_trace_plan(plan), json.dumps(plan, indent=2, sort_keys=True),
            args.output, args.json,
        )
    except (OSError, TracePlanError) as exc:
        print(f"trace plan error: {exc}", file=sys.stderr)
        return 2
    return 0 if plan["preflight_status"] == "eligible" else 1


def _gate1_analyze(args: argparse.Namespace) -> int:
    """Analyze experimental namespace observations without changing evidence.

    Args:
        args: Run directory and output options parsed by the CLI.

    Returns:
        Zero for usable observations, one for incomplete observations, or two
        for unreadable/unsupported inputs. Zero is not a Gate 1 PASS.
    """
    if args.output is not None:
        if args.ordering_policy == "causal" and args.output.exists():
            print("trace error: causal output must be a new file", file=sys.stderr)
            return 2
        inputs = {
            args.run_dir / name
            for name in (
                "manifest.json",
                "phases.csv",
                "trace.csv",
                "bpftrace.err",
                "bpftrace.out",
            )
        }
        if args.output.resolve() in {path.resolve() for path in inputs} or (
            args.output.exists()
            and any(path.exists() and args.output.samefile(path) for path in inputs)
        ):
            print("trace error: output must not overwrite a raw input", file=sys.stderr)
            return 2
    try:
        observation = analyze_trace(args.run_dir, ordering_policy=args.ordering_policy)
        rendered = json.dumps(observation, indent=2, sort_keys=True)
        causal_output = args.ordering_policy == "causal" and args.output is not None
        if causal_output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            with args.output.open("x", encoding="utf-8") as handle:
                handle.write(rendered + "\n")
        _emit_result(
            render_trace(observation),
            rendered,
            None if causal_output else args.output,
            args.json,
        )
    except (OSError, ValueError, TraceError) as exc:
        print(f"trace error: {exc}", file=sys.stderr)
        return 2
    return 0 if observation["quality"]["status"] == "usable" else 1


def _gate1_verify(args: argparse.Namespace) -> int:
    """Produce a separate reviewed serving companion, never rewriting evidence.

    Args:
        args: Run directory, review JSON, explicit policy and output options.

    Returns:
        Zero for reviewed verified; one for incomplete or failed; two for contract,
        I/O or output errors. Existing output files are always refused.
    """
    try:
        if args.output is not None:
            if args.output.exists() or args.output.is_symlink():
                raise VerificationError("output must be a new file")
            if any(
                args.output.resolve().is_relative_to(root)
                for root in (Path("/sys"), Path("/proc"), Path("/dev"))
            ):
                raise VerificationError("output must not target /sys, /proc or /dev")
            if args.output.resolve() == args.review.resolve():
                raise VerificationError("output must not overwrite the review")
        result = verify_serving(
            args.run_dir, args.review, ordering_policy=args.ordering_policy
        )
        rendered = json.dumps(result, indent=2, sort_keys=True)
        if args.output is not None:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            with args.output.open("x", encoding="utf-8") as handle:
                handle.write(rendered + "\n")
        _emit_result(render_verification(result), rendered, None, args.json)
    except (OSError, ValueError) as exc:
        print(f"serving verification error: {exc}", file=sys.stderr)
        return 2
    return 0 if result["status"] == "verified" else 1


def _doctor(args: argparse.Namespace) -> int:
    """Run the host capability probe.

    Args:
        args: Parsed command arguments.

    Returns:
        Process exit status.
    """
    manifest = build_manifest()
    print(render_manifest(manifest))
    if args.json_output is not None:
        _write_output(json.dumps(manifest, indent=2, sort_keys=True), args.json_output)
    return 0


def _tier_check(args: argparse.Namespace) -> int:
    """Check that a local NVMe tier can support a Gate 1 run.

    Args:
        args: Parsed command arguments.

    Returns:
        Zero when no check fails, one when at least one fails, two for a usage
        error, a host that is not Linux, or a result that cannot be saved.
    """
    if not is_supported_platform():
        print(
            "tier-check reads Linux /sys and /proc; nothing was checked on this host.",
            file=sys.stderr,
        )
        return 2
    gdscheck = args.gdscheck or os.environ.get("GDSCHECK", "") or find_gdscheck() or ""
    if (args.cufile_buf_mib is None) != (args.mib_per_chunk is None):
        print(
            "tier-check: --cufile-buf-mib and --mib-per-chunk are given together",
            file=sys.stderr,
        )
        return 2
    pool = None
    if args.cufile_buf_mib is not None:
        pool = PoolSizes(
            cufile_buf_mib=args.cufile_buf_mib,
            mib_per_chunk=args.mib_per_chunk,
            context_tokens=args.context_tokens,
            fillers=args.fillers,
            chunk_tokens=args.chunk_tokens,
        )
    request = TierCheckRequest(
        tier_mount=args.tier_mount,
        arm=args.arm,
        tier_dir=args.tier_dir or "",
        model_dir=args.model_dir or "",
        gdscheck=gdscheck,
        pool=pool,
    )
    document = run_tier_check(request, LocalHost())
    try:
        _emit_result(
            render_tier_check(document),
            json.dumps(document, indent=2, sort_keys=True),
            args.output,
            args.json,
        )
    except OSError as exc:
        print(f"tier-check output error: {exc}", file=sys.stderr)
        return 2
    return 1 if document["overall"] == NOT_READY else 0


def _report(args: argparse.Namespace) -> int:
    """Assess one evidence document and render its verdict.

    Args:
        args: Parsed command arguments.

    Returns:
        Zero for a passing report, one for a valid failing report, or two for
        malformed evidence.
    """
    try:
        payload = json.loads(args.evidence.read_text(encoding="utf-8"))
        assessment = assess_evidence(payload)
    except (OSError, json.JSONDecodeError, EvidenceError) as exc:
        print(f"evidence error: {exc}", file=sys.stderr)
        print("exit 2: the evidence could not be read or assessed.", file=sys.stderr)
        return 2

    readings = compute_readings(payload) if args.readings else None
    if args.format == "json":
        if readings is not None:
            assessment = {**assessment, "readings": readings}
        rendered = json.dumps(assessment, indent=2, sort_keys=True)
    elif args.format == "markdown":
        rendered = render_markdown(assessment, readings)
    else:
        rendered = render_text(assessment, readings)
    _write_output(rendered, args.output)
    overall = str(assessment["overall"])
    if overall.lower() == "pass":
        return 0
    # A non-zero exit here is a verdict, not a crash; say so where scripts and
    # first-time users both look, without touching the rendered report.
    print(
        f"exit 1: overall {overall} — the report is valid, but not every gate passed, so "
        "performance claims are withheld. Each gate's reason is in the report. "
        "This is a verdict, not an installation or runtime error.",
        file=sys.stderr,
    )
    return 1


def _fingerprint(args: argparse.Namespace) -> int:
    """Print the canonical fingerprint of a configuration parameter file.

    Args:
        args: Parsed command arguments.

    Returns:
        Zero for a valid configuration or two for malformed input.
    """
    try:
        parameters = json.loads(args.configuration.read_text(encoding="utf-8"))
        fingerprint = fingerprint_configuration(parameters)
    except (OSError, json.JSONDecodeError, ConfigurationError) as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    print(fingerprint)
    return 0


_GEOMETRY_SCOPES = ("full_model", "per_tp_rank", "tp_group_aggregate")


def _read_geometry_document(path: Path) -> dict:
    """Load and validate a ``kvprobe geometry`` JSON document.

    Args:
        path: Geometry document written by ``kvprobe geometry --output``.

    Returns:
        The parsed document.

    Raises:
        PoolSizingError: If the file is unreadable or not a geometry document.
    """
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        for name in _GEOMETRY_SCOPES:
            document["geometry"][name]["bytes_per_chunk"]
        document["runtime"]["chunk_tokens"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise PoolSizingError(
            f"{path} is not a kvprobe geometry document ({exc.__class__.__name__})"
        ) from exc
    return document


def _bytes_from_geometry(document: dict, scope: str | None) -> tuple[int, str]:
    """Select bytes per chunk from a geometry document.

    Args:
        document: Result of :func:`_read_geometry_document`.
        scope: Geometry scope to use, or None when every scope agrees.

    Returns:
        The selected bytes per chunk and a source label for the output.

    Raises:
        PoolSizingError: If the scope is ambiguous.
    """
    values = {
        name: document["geometry"][name]["bytes_per_chunk"]
        for name in _GEOMETRY_SCOPES
    }
    if scope is None:
        if len(set(values.values())) != 1:
            listed = ", ".join(f"{k}={v:,}" for k, v in values.items())
            raise PoolSizingError(
                "geometry scopes differ; choose one with --geometry-scope "
                f"({listed})"
            )
        return values["per_tp_rank"], "geometry:all_scopes_equal"
    return values[scope], f"geometry:{scope}"


def _format_bytes(value: int) -> str:
    """Format a byte count in the largest exact-enough binary unit."""
    for unit, size in (("GiB", 2**30), ("MiB", 2**20), ("KiB", 2**10)):
        if value >= size:
            return f"{value / size:g} {unit}"
    return f"{value} B"


def _with_tokens(plan: dict, chunk_tokens: int | None) -> dict:
    """Add token capacities when the tokens per chunk are known.

    Args:
        plan: Result of ``calculate_pool_plan``.
        chunk_tokens: Tokens held by one chunk, or None when unknown.

    Returns:
        The plan, plus ``logical_capacity_tokens`` and ``required_live_tokens``
        (slots and live chunks times ``chunk_tokens``) when it is known.
    """
    if chunk_tokens is None:
        return plan
    return {
        **plan,
        "logical_capacity_tokens": plan["pool_slots"] * chunk_tokens,
        "required_live_tokens": plan["required_live_chunks"] * chunk_tokens,
    }


def _tokens_suffix(plan: dict, key: str) -> str:
    """Return ``" (N tokens)"`` for a plan field, or nothing when unknown."""
    return f" ({plan[key]:,} tokens)" if key in plan else ""


_RELATION = {
    "below_live_set": "below the live set",
    "at_live_set": "at the live set",
    "above_live_set": "above the live set",
}


def _pool_plan_summary(
    plans: list[dict], source: str, derivation: dict | None, output: Path | None
) -> str:
    """Explain one or several capacity calculations in operator units."""
    first = plans[0]
    lines = ["kvprobe pool-plan — derived result"]
    if output is not None:
        lines.append(f"  Saved JSON: {output}")
    if derivation is not None:
        lines.append(
            f"  Live set: {derivation['concurrent_contexts']:,} contexts × "
            f"{derivation['chunks_per_context']:,} chunks "
            f"({derivation['context_tokens']:,} tokens / "
            f"{derivation['chunk_tokens']:,} per chunk"
            + (
                ""
                if derivation["partial_chunk"] == "not_applicable"
                else f", trailing partial chunk: {derivation['partial_chunk']}"
            )
            + f") = {derivation['required_live_chunks']:,} chunks"
        )
        lines.append(f"    Assumption: {derivation['assumption']}")
    lines.append(
        f"  Intended live set: {first['required_live_chunks']:,} chunks × "
        f"{_format_bytes(first['bytes_per_chunk'])} = "
        f"{_format_bytes(first['required_live_bytes'])}"
        f"{_tokens_suffix(first, 'required_live_tokens')}"
    )
    for plan in plans:
        lines.append(
            f"  Pool {plan['pool_slots']:,} slots = "
            f"{_format_bytes(plan['logical_capacity_bytes'])}"
            f"{_tokens_suffix(plan, 'logical_capacity_tokens')}: "
            f"headroom {plan['slot_headroom']:,} "
            f"({_RELATION[plan['classification']]}), "
            f"forced evictions >= {plan['forced_evictions_lower_bound']:,}"
        )
    if len(plans) > 1:
        lines.append(
            "  Sweep values bracket the live set; none is a recommendation."
        )
        if all(p["classification"] != "below_live_set" for p in plans):
            lines.append("  No value below the live set exists for this live set.")
    lines.append(f"  Bytes per chunk from: {source}")
    lines.append("  Capacity arithmetic only; not a performance prediction.")
    return "\n".join(lines)


def _resolve_pool_plan_inputs(args: argparse.Namespace) -> dict:
    """Combine the pool-plan inputs into one validated set.

    Args:
        args: Parsed command arguments.

    Returns:
        ``bytes_per_chunk``, its source, ``required_live_chunks`` and an
        optional derivation record.

    Raises:
        PoolSizingError: If inputs conflict, are missing, or are ambiguous.
    """
    document = None
    if args.geometry is not None:
        document = _read_geometry_document(args.geometry)
        bytes_per_chunk, source = _bytes_from_geometry(
            document, args.geometry_scope
        )
    elif args.geometry_scope is not None:
        raise PoolSizingError("--geometry-scope requires --geometry")
    else:
        bytes_per_chunk, source = args.bytes_per_chunk, "explicit"

    chunk_tokens = args.chunk_tokens
    if document is not None:
        recorded = document["runtime"]["chunk_tokens"]
        if chunk_tokens is not None and chunk_tokens != recorded:
            raise PoolSizingError(
                f"--chunk-tokens {chunk_tokens} differs from the geometry "
                f"document's chunk_tokens {recorded}"
            )
        chunk_tokens = recorded
    if chunk_tokens is not None and chunk_tokens <= 0:
        raise PoolSizingError("chunk_tokens must be a positive integer")

    derivation = None
    if args.concurrent_contexts is not None:
        if args.context_tokens is None:
            raise PoolSizingError("--concurrent-contexts requires --context-tokens")
        if chunk_tokens is None:
            raise PoolSizingError(
                "chunk tokens are unknown; pass --chunk-tokens or --geometry"
            )
        derivation = derive_required_live_chunks(
            args.concurrent_contexts,
            args.context_tokens,
            chunk_tokens,
            args.partial_chunk,
        )
        required = derivation["required_live_chunks"]
    else:
        stray = [
            flag
            for flag, value in (
                ("--context-tokens", args.context_tokens),
                ("--partial-chunk", args.partial_chunk),
            )
            if value is not None
        ]
        if stray:
            raise PoolSizingError(
                f"{', '.join(stray)} apply only with --concurrent-contexts"
            )
        required = args.required_live_chunks
    return {
        "bytes_per_chunk": bytes_per_chunk,
        "source": source,
        "required_live_chunks": required,
        "derivation": derivation,
        "chunk_tokens": chunk_tokens,
    }


def _pool_plan(args: argparse.Namespace) -> int:
    """Calculate the capacity envelope of one static NIXL pool, or a bracket.

    Args:
        args: Parsed command arguments.

    Returns:
        Zero for valid inputs or two when an input is invalid.
    """
    try:
        inputs = _resolve_pool_plan_inputs(args)
        slots = (
            bracket_pool_slots(inputs["required_live_chunks"])
            if args.sweep
            else [args.pool_slots]
        )
        plans = [
            _with_tokens(
                calculate_pool_plan(
                    pool_slots=count,
                    bytes_per_chunk=inputs["bytes_per_chunk"],
                    required_live_chunks=inputs["required_live_chunks"],
                ),
                inputs["chunk_tokens"],
            )
            for count in slots
        ]
    except PoolSizingError as exc:
        print(f"pool sizing error: {exc}", file=sys.stderr)
        return 2
    extra = {
        "bytes_per_chunk_source": inputs["source"],
        **(
            {"chunk_tokens": inputs["chunk_tokens"]}
            if inputs["chunk_tokens"] is not None
            else {}
        ),
        "required_live_chunks_source": (
            "derived" if inputs["derivation"] else "explicit"
        ),
    }
    if inputs["derivation"]:
        extra["derivation"] = inputs["derivation"]
    if args.sweep:
        result = {
            **extra,
            "bytes_per_chunk": inputs["bytes_per_chunk"],
            "required_live_chunks": inputs["required_live_chunks"],
            "sweep": plans,
            "sweep_note": (
                "bracket around the live set to sweep; not a recommendation"
            ),
        }
    else:
        result = {**plans[0], **extra}
    try:
        _emit_result(
            _pool_plan_summary(
                plans, inputs["source"], inputs["derivation"], args.output
            ),
            json.dumps(result, indent=2, sort_keys=True),
            args.output,
            args.json,
        )
    except OSError as exc:
        print(f"pool sizing error: cannot save {args.output}: {exc}", file=sys.stderr)
        return 2
    return 0


def _geometry(args: argparse.Namespace) -> int:
    """Derive model and runtime KV-cache byte geometry.

    Args:
        args: Parsed command arguments.

    Returns:
        Zero for a derived geometry or two when an input is unsupported.
    """
    try:
        if args.revision is not None and args.model is None:
            raise GeometryError("--revision requires --model")
        if args.model is not None:
            if any(
                value is not None for value in (args.kv_heads, args.head_dim)
            ):
                raise GeometryError(
                    "--kv-heads and --head-dim go with --layers, not --model"
                )
            model_config, source = load_model_reference(
                args.model, revision=args.revision
            )
        elif args.model_config is not None:
            if any(
                value is not None for value in (args.kv_heads, args.head_dim)
            ):
                raise GeometryError(
                    "--kv-heads and --head-dim go with --layers, not --model-config"
                )
            model_config, source = load_model_config_file(args.model_config)
        else:
            model_config = _dimensions_config(args)
            source = {"input_source": "dimensions"}
        geometry = derive_geometry(
            model_config,
            chunk_tokens=args.chunk_tokens,
            dtype=args.dtype,
            tensor_parallel_size=args.tensor_parallel_size,
            pipeline_parallel_size=args.pipeline_parallel_size,
        )
    except (OSError, json.JSONDecodeError, GeometryError, ModelSourceError) as exc:
        print(f"geometry error: {exc}", file=sys.stderr)
        return 2
    geometry["model"].update(source)
    try:
        _emit_result(
            _geometry_summary(geometry, args.output),
            json.dumps(geometry, indent=2, sort_keys=True),
            args.output,
            args.json,
        )
    except OSError as exc:
        print(f"geometry error: cannot save {args.output}: {exc}", file=sys.stderr)
        return 2
    return 0


def _dimensions_config(args: argparse.Namespace) -> dict:
    """Build model fields from numbers entered on the command line.

    Args:
        args: Parsed ``geometry`` arguments carrying ``--layers``.

    Returns:
        The ``config.json`` fields the standard KV formula reads.

    Raises:
        GeometryError: If a dimension is missing, or the dtype is not given.
    """
    missing = [
        flag
        for flag, value in (("--kv-heads", args.kv_heads), ("--head-dim", args.head_dim))
        if value is None
    ]
    if missing:
        raise GeometryError(f"--layers also needs {' and '.join(missing)}")
    if args.dtype is None:
        raise GeometryError(
            "--layers needs --dtype: without a model file there is no dtype to read"
        )
    return {
        "num_hidden_layers": args.layers,
        "num_key_value_heads": args.kv_heads,
        "head_dim": args.head_dim,
    }


def _ingest(args: argparse.Namespace) -> int:
    """Normalize a measurement run into an evidence document.

    Args:
        args: Parsed command arguments.

    Returns:
        Zero on success, or two when the run and the configuration cannot be
        reconciled without inventing a fact.
    """
    try:
        document = ingest_files(
            args.run, args.configuration, args.run_id, args.expected_reuses
        )
    except (OSError, IngestError, ConfigurationError) as exc:
        print(f"ingest error: {exc}", file=sys.stderr)
        return 2
    rendered = json.dumps(document, indent=2) + "\n"
    if args.output is None:
        print(rendered, end="")
    else:
        args.output.write_text(rendered, encoding="utf-8")
    return 0


def _demo(args: argparse.Namespace) -> int:
    """Run the reporter against bundled CPU-only examples.

    Args:
        args: Parsed command arguments.

    Returns:
        Zero when every bundled example matches its expected verdict.
    """
    return _run_demo(args.output_dir)


def _assess_demo_examples(
    output_dir: Path | None,
) -> list[tuple[str, str, dict]]:
    """Assess bundled examples and optionally preserve their reports.

    Args:
        output_dir: Directory for Markdown reports, or ``None`` to avoid files.

    Returns:
        Filename, expected verdict and assessment for every bundled example.
    """
    examples = Path(__file__).parents[1] / "examples" / "evidence"
    assessed = []
    for filename, expected_verdict in _DEMO_EXPECTED.items():
        payload = json.loads((examples / filename).read_text(encoding="utf-8"))
        assessment = assess_evidence(payload)
        assessed.append((filename, expected_verdict, assessment))

        if output_dir is not None:
            output_dir.mkdir(parents=True, exist_ok=True)
            destination = output_dir / filename.replace(".json", ".report.md")
            destination.write_text(
                _demo_report_markdown(filename, assessment), encoding="utf-8"
            )
    return assessed


def _demo_report_markdown(filename: str, assessment: dict) -> str:
    """Render one bundled example with standalone provenance.

    Args:
        filename: Bundled evidence filename used to identify its origin.
        assessment: Result returned by :func:`assess_evidence`.

    Returns:
        Markdown that cannot be mistaken for a measurement of the current host.
    """
    origin, _, _ = _DEMO_NOTES[filename]
    rendered = render_markdown(assessment).rstrip()
    title, _, body = rendered.partition("\n")
    banner = "\n".join(
        (
            "> **Bundled example — not a measurement of the current host.**",
            ">",
            f"> **Data origin:** {origin}.",
            "> This is an independent case, not a phase of another bundled report.",
        )
    )
    return f"{title}\n\n{banner}\n\n{body.lstrip()}\n"


def _demo_failed(assessed: list[tuple[str, str, dict]]) -> bool:
    """Return whether a bundled assessment differs from its expected verdict."""
    return any(
        str(assessment["overall"]) != expected
        for _, expected, assessment in assessed
    )


def _print_demo_details(assessed: list[tuple[str, str, dict]]) -> None:
    """Print the detailed reporter self-check for bundled examples."""
    print(
        textwrap.fill(
            "Each case judges one bundled evidence file. The marker PASS means the "
            "tool's verdict matched the expected one (the tool behaves as "
            "designed); it does not mean the data passed.",
            width=_DEMO_WIDTH,
        )
    )
    for filename, expected_verdict, assessment in assessed:
        actual = str(assessment["overall"])
        marker = "PASS" if actual == expected_verdict else "MISMATCH"
        print(
            f"\n[{marker}] {filename}: expected={expected_verdict} actual={actual} "
            f"claims={assessment['performance_claims']}"
        )
        unmet = [
            f"{gate['id']} ({gate['status']})"
            for gate in assessment["gates"]
            if gate["status"] != "PASS"
        ]
        note = _DEMO_NOTES.get(filename)
        if note is not None:
            origin, check, meaning = note
            print(_demo_field("data:    ", origin))
            print(_demo_field("check:   ", check))
            print(_demo_field("result:  ", meaning))
        if unmet:
            print(
                _demo_field(
                    "gates not passed: ",
                    ", ".join(item.replace(" ", "\u00a0") for item in unmet),
                )
            )


def _run_demo(output_dir: Path | None) -> int:
    """Assess the bundled examples and optionally write their reports.

    Args:
        output_dir: Directory for Markdown reports, or ``None`` to avoid files.

    Returns:
        Zero when every bundled example matches its expected verdict.
    """
    assessed = _assess_demo_examples(output_dir)
    _print_demo_details(assessed)

    if output_dir is not None:
        print(f"reports: {output_dir}")
    return 1 if _demo_failed(assessed) else 0


def _run_first_visit_demo(output_dir: Path | None, verbose: bool) -> int:
    """Show the decisions unlocked by the gates using bundled evidence.

    Args:
        output_dir: Directory for Markdown reports, or ``None`` to avoid files.
        verbose: Whether to print every bundled reporter self-check.

    Returns:
        Zero when every bundled example matches its expected verdict.
    """
    assessed = _assess_demo_examples(output_dir)
    by_name = {filename: assessment for filename, _, assessment in assessed}
    sample = by_name["pass-all-attempts.json"]
    gates = {int(gate["id"]): gate for gate in sample["gates"]}

    print("  SYNTHETIC EXAMPLE — these values do not describe this host\n")
    print(
        _gate_row(
            "1",
            "REALITY",
            "PASS - target-device bytes matched the expected KV payload.",
        )
    )
    print(_gate_row("2", "BENEFIT", _sentence(str(gates[2]["result"]))))
    print(_gate_row("3", "ECONOMICS", _sentence(str(gates[3]["result"]))))
    print(_gate_row("4", "NECESSITY", _sentence(str(gates[4]["result"]))))
    print(_gate_row("5", "LIMIT", _sentence(str(gates[5]["result"]))))

    matched = len(assessed) - sum(
        str(assessment["overall"]) != expected
        for _, expected, assessment in assessed
    )
    print(
        f"\n  Reporter self-check: {matched}/{len(assessed)} "
        "expected verdicts matched."
    )
    print("  WHY GATE 1 MATTERS")
    print(
        "    UNPROVEN - matching host-wide bytes cannot identify the tier's device."
    )
    print("    FAIL     - a framework hit with zero device bytes is a phantom tier.")

    if verbose:
        print("\n  DETAILED BUNDLED CASES")
        _print_demo_details(assessed)
    else:
        print("  Rerun with --verbose to inspect all four bundled cases.")
    return 1 if _demo_failed(assessed) else 0


def _first_visit_artifact_index(manifest: dict) -> str:
    """Return the reading guide stored with first-visit artifacts.

    Args:
        manifest: Host capability manifest written beside the guide.

    Returns:
        Markdown that orders the bundled reports, explains ``doctor.json``,
        and points the visitor to the Gate 1 checkpoint.
    """
    adapter = manifest["current_adapter"]
    route_status = str(adapter["route_status"]).upper()
    serving_verification = str(adapter["serving_verification"]).upper()
    return f"""# First-visit artifacts

These files contain four independent bundled examples, not four phases or
outputs of one run. They demonstrate how the reporter distinguishes a supported
result from ambiguous or failed evidence. They do **not** measure this host.

Read the reports in this order:

1. [`pass-all-attempts.report.md`](pass-all-attempts.report.md) — a synthetic
   hand-written end-to-end `PASS`; all five gates support the claim.
2. [`attribution-unfiltered-counter.report.md`](attribution-unfiltered-counter.report.md)
   — a measured unified-memory Arm64 example where matching host-wide bytes
   remain `UNPROVEN` because they do not identify the tier's device.
3. [`phantom-tier-framework-hit.report.md`](phantom-tier-framework-hit.report.md)
   — a synthetic block-tier fixture where a framework hit with zero device
   bytes is a measured `FAIL`.
4. [`envelope-collapse-small-pool.report.md`](envelope-collapse-small-pool.report.md)
   — a measured small-pool concurrency sweep where a valid Gate 5 observation
   identifies batches without complete tier-byte and correctness evidence.
   The overall result is `PARTIAL` because the other gates were not part of that run shape.

## Current host route

`doctor.json` is optional host-capability detail. It routes the next step but
does not establish a hardware measurement or a Gate 1 PASS.

- **Reference adapter:** `{route_status}`
- **Measurement-ready scope:** host prerequisites only
- **Counter attribution scope:** counter-source presence only
- **Serving verification:** `{serving_verification}`

Missing `lmcache`, `nixl`, or `kvbm` packages in `doctor.json` refer only to the
host Python environment; the reference workflow may provide them in a container.

Next, return to the repository and open `docs/GATE1.md`. Before a hardware run,
confirm that you can run matched recompute, align an external tier-byte counter
to controlled intervals, and detect or disable silent fallback. Use
`docs/CHECKLIST.md` during every Gate 2–5 experiment.
"""


def _invocation() -> str:
    """Return the command prefix the user ran, for commands we suggest next.

    A clean clone runs ``python3 -m kvprobe`` before anything is installed, so a
    suggested ``kvprobe ...`` would fail there with "command not found".

    Returns:
        ``"python3 -m kvprobe"`` when started with ``-m``, otherwise ``"kvprobe"``.
    """
    return "python3 -m kvprobe" if sys.argv[0].endswith("__main__.py") else "kvprobe"


def _first_visit(args: argparse.Namespace) -> int:
    """Run the dependency-free first-visit journey.

    Args:
        args: Parsed command arguments.

    Returns:
        Zero when the bundled tool demonstration is internally consistent.
        Missing host capabilities do not make the command fail because they
        are a routing result, not a tool error.
    """
    manifest = build_manifest()
    adapter = manifest["current_adapter"]
    command = _invocation()

    print("kvprobe first-visit")
    print("\nSTEP 1/4 — See the five decisions this probe can unlock")
    for gate_id, label, question in _FIRST_VISIT_GATES:
        print(_gate_row(gate_id, label, question))
    print(
        "  Gate 1 evidence is required before Gate 2-5 performance results "
        "can be trusted."
    )

    print("\nSTEP 2/4 — Explore one end-to-end result and two traps")
    demo_status = _run_first_visit_demo(args.output_dir, args.verbose)

    print("\nSTEP 3/4 — Classify this host")
    if args.verbose:
        print(render_manifest(manifest))
    if args.output_dir is not None:
        args.output_dir.mkdir(parents=True, exist_ok=True)
        _write_output(
            json.dumps(manifest, indent=2, sort_keys=True),
            args.output_dir / "doctor.json",
        )
        _write_output(
            _first_visit_artifact_index(manifest),
            args.output_dir / "README.md",
        )
    if adapter["measurement_ready"]:
        adapter_status = "CANDIDATE"
        adapter_detail = (
            "host prerequisites for the bundled adapter (GPU, docker, "
            "nvidia-fs) were detected; the vLLM + LMCache image is not "
            "checked here, and the target filesystem and request-aligned "
            "counter still require validation"
        )
    else:
        adapter_status = "ADAPTER_REQUIRED"
        adapter_detail = (
            "host prerequisites for the bundled adapter (GPU, docker, "
            "nvidia-fs) are not all present; the method may still apply with "
            "another evidence source"
        )
    if not args.verbose:
        print(f"  GPU: {manifest['gpu']['status']}")
        print(f"  docker: {manifest['capabilities']['docker']['status']}")
        print(f"  nvidia_fs: {manifest['capabilities']['nvidia_fs']['status']}")
    print(f"  reporter self-check: {'PASS' if demo_status == 0 else 'FAIL'}")
    print(f"  reference adapter: {adapter_status}")
    print(_hanging("  detail: ", adapter_detail + "."))
    print("  hardware measurement: NOT_RUN")
    print("  Gate 1 serving-tier verification: NOT_ESTABLISHED")
    if not args.verbose:
        print(f"  Full capability inventory: {command} doctor or rerun with --verbose.")

    print("\nSTEP 4/4 — Choose what to unlock next")
    print("  AVAILABLE NOW — capacity arithmetic, without a GPU or model weights")
    print("    Any stack — expected bytes per chunk (Gate 1 preparation):")
    print("      Example model source: replace ORG/MODEL_NAME with your own")
    print("      public model ID or use a local model directory.")
    print(
        f"      {command} geometry --model ORG/MODEL_NAME "
        "--chunk-tokens 256 --output geometry.json"
    )
    print("      Only config.json is fetched; model weights are not downloaded.")
    print(
        "    Optional — fixed-slot pool planning; documented reference: "
        "LMCache NIXL static (Gate 4/5 preparation):"
    )
    print(
        f"      {command} pool-plan --sweep --geometry geometry.json "
        "--concurrent-contexts N --context-tokens T"
    )
    print("\n  MEASUREMENT ROUTE")
    print(f"    reference adapter: {adapter_status}")
    print("    Gate 1: NOT_ESTABLISHED — prove the serving source before timing it.")
    print("    Gates 2-5 (listed in STEP 1) become meaningful once Gate 1 holds.")
    print("\n  NEXT CHECKPOINT — answer all three before a hardware run")
    print("    [ ] Can you run a matched recompute arm with the cache disabled?")
    print(
        "    [ ] Can you align an external tier byte counter to each "
        "controlled request interval?"
    )
    print("    [ ] Can you detect or disable silent fallback?")
    for note in (
        "If yes to all three, continue with docs/GATE1.md and use "
        "docs/CHECKLIST.md during every experiment.",
        f"Before freezing configuration: rerun {command} geometry with the "
        "selected model and the live dtype/chunk/TP settings; it gives the "
        "expected bytes a measurement is judged against.",
        "If the current adapter is CANDIDATE: inspect scripts/README.md; "
        "capability discovery alone is not a hardware PASS.",
        "If any answer is no: implement an adapter/evidence source or "
        "stop; do not report a tier-performance claim.",
    ):
        print(_hanging("  ", note))
    if args.output_dir is not None:
        print(f"\nfirst-visit artifacts: {args.output_dir}")
        print(f"start with: {args.output_dir / 'README.md'}")
    return demo_status


def build_parser() -> argparse.ArgumentParser:
    """Build the public command-line parser.

    Returns:
        Configured argument parser.
    """
    parser = argparse.ArgumentParser(
        prog="kvprobe",
        description="Prove a KV-cache tier before reporting its performance.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    gate1_parser = subparsers.add_parser(
        "gate1", help="experimental NVMe observations and reviewed companion"
    )
    gate1_commands = gate1_parser.add_subparsers(dest="gate1_command", required=True)
    plan_parser = gate1_commands.add_parser(
        "plan", help="inspect local namespace/tracepoint prerequisites without sudo"
    )
    plan_parser.add_argument("--disk", required=True, help="nvmeNnM or /dev/nvmeNnM")
    plan_parser.add_argument("--output", type=Path, help="save preflight JSON")
    plan_parser.add_argument(
        "--json", action="store_true", help="print JSON; summary goes to stderr"
    )
    plan_parser.set_defaults(handler=_gate1_plan)
    capture_parser = gate1_commands.add_parser(
        "capture", help="experimental root-only bounded capture; no serving verdict"
    )
    capture_parser.add_argument("--disk", required=True)
    capture_parser.add_argument("--run-dir", type=Path, required=True)
    capture_parser.add_argument("--duration-seconds", type=int, default=30)
    capture_parser.set_defaults(handler=_gate1_capture)
    trace_parser = gate1_commands.add_parser(
        "analyze", help="analyze normalized offline NVMe trace inputs"
    )
    trace_parser.add_argument("run_dir", type=Path)
    trace_parser.add_argument(
        "--ordering-policy",
        choices=("strict", "causal"),
        default="strict",
        help="strict default; causal is experimental opt-in, not a serving PASS",
    )
    trace_parser.add_argument("--output", type=Path, help="save observation JSON")
    trace_parser.add_argument(
        "--json", action="store_true", help="print JSON; summary goes to stderr"
    )
    trace_parser.set_defaults(handler=_gate1_analyze)

    verify_parser = gate1_commands.add_parser(
        "verify", help="experimental reviewed companion; not hardware attribution"
    )
    verify_parser.add_argument("run_dir", type=Path)
    verify_parser.add_argument(
        "--review", type=Path, required=True,
        help="explicit operator review JSON contract 0.1.0",
    )
    verify_parser.add_argument(
        "--ordering-policy", choices=("strict", "causal"), default="strict",
        help="must match review; strict default",
    )
    verify_parser.add_argument(
        "--output", type=Path, help="save a new companion JSON; never overwrite"
    )
    verify_parser.add_argument(
        "--json", action="store_true", help="print JSON; summary goes to stderr"
    )
    verify_parser.set_defaults(handler=_gate1_verify)

    doctor_parser = subparsers.add_parser(
        "doctor", help="emit a conservative host capability manifest"
    )
    doctor_parser.add_argument(
        "--json-output", type=Path, help="also write the manifest as JSON"
    )
    doctor_parser.set_defaults(handler=_doctor)

    tier_parser = subparsers.add_parser(
        "tier-check",
        help="check, read-only, that a local NVMe tier can support a Gate 1 run",
    )
    tier_parser.add_argument(
        "--tier-mount",
        required=True,
        help="host path of the tier filesystem (GDS_MOUNT in the runbook)",
    )
    tier_parser.add_argument(
        "--arm",
        choices=ARMS,
        default="gds",
        help="arm to check for; NIXL static GDS_MT is checked as gds (default: gds)",
    )
    tier_parser.add_argument(
        "--tier-dir",
        help="directory your launcher reuses between runs; checked to be empty",
    )
    tier_parser.add_argument(
        "--model-dir", help="host model directory, compared with the tier disk"
    )
    tier_parser.add_argument(
        "--gdscheck", help="gdscheck executable (default: $GDSCHECK, then a search)"
    )
    tier_parser.add_argument(
        "--cufile-buf-mib",
        type=_positive_int,
        help="cuFile staging pool in MiB (PROBE_CUFILE_BUF_MIB); needs --mib-per-chunk",
    )
    tier_parser.add_argument(
        "--mib-per-chunk",
        type=_positive_int,
        help="KV MiB per chunk of the served model (PROBE_MIB_PER_CHUNK)",
    )
    tier_parser.add_argument(
        "--context-tokens",
        type=_positive_int,
        default=DEFAULT_CONTEXT_TOKENS,
        help=f"measured context tokens (REP_CONTEXT; default {DEFAULT_CONTEXT_TOKENS})",
    )
    tier_parser.add_argument(
        "--fillers",
        type=_non_negative_int,
        default=DEFAULT_FILLERS,
        help=f"distinct filler contexts between measurements (FILLERS; default {DEFAULT_FILLERS})",
    )
    tier_parser.add_argument(
        "--chunk-tokens",
        type=_positive_int,
        default=DEFAULT_CHUNK_TOKENS,
        help=f"tokens per stored chunk (default {DEFAULT_CHUNK_TOKENS})",
    )
    tier_parser.add_argument(
        "--output", type=Path, help="also save the result as JSON to this file"
    )
    tier_parser.add_argument(
        "--json",
        action="store_true",
        help="print JSON on standard output instead of the summary (summary goes to stderr)",
    )
    tier_parser.set_defaults(handler=_tier_check)

    demo_parser = subparsers.add_parser(
        "demo", help="judge bundled evidence without a GPU"
    )
    demo_parser.add_argument(
        "--output-dir", type=Path, help="write Markdown reports to this directory"
    )
    demo_parser.set_defaults(handler=_demo)

    first_visit_parser = subparsers.add_parser(
        "first-visit",
        help="show the five decisions, classify the host, and route the next step",
    )
    first_visit_parser.add_argument(
        "--output-dir",
        type=Path,
        help="write the host manifest and bundled Markdown reports",
    )
    first_visit_parser.add_argument(
        "--verbose",
        action="store_true",
        help="show the full host inventory and all bundled reporter cases",
    )
    first_visit_parser.set_defaults(handler=_first_visit)

    report_parser = subparsers.add_parser(
        "report", help="judge a normalized evidence JSON document"
    )
    report_parser.add_argument("evidence", type=Path)
    report_parser.add_argument(
        "--format", choices=("text", "json", "markdown"), default="text"
    )
    report_parser.add_argument("--output", type=Path)
    report_parser.add_argument(
        "--readings",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "show efficiency readings with evidence grades (default); "
            "--no-readings restores the claim-only report"
        ),
    )
    report_parser.set_defaults(handler=_report)

    ingest_parser = subparsers.add_parser(
        "ingest", help="normalize a measurement run into evidence JSON"
    )
    ingest_parser.add_argument("run", type=Path, nargs="+")
    ingest_parser.add_argument("--configuration", type=Path, required=True)
    ingest_parser.add_argument("--run-id", required=True)
    ingest_parser.add_argument(
        "--expected-reuses",
        type=float,
        help="reuses the workload expects per admitted entry (gate 3 only)",
    )
    ingest_parser.add_argument("--output", type=Path)
    ingest_parser.set_defaults(handler=_ingest)

    fingerprint_parser = subparsers.add_parser(
        "fingerprint", help="fingerprint canonical experiment configuration JSON"
    )
    fingerprint_parser.add_argument("configuration", type=Path)
    fingerprint_parser.set_defaults(handler=_fingerprint)

    pool_parser = subparsers.add_parser(
        "pool-plan",
        help="calculate static-pool capacity without claiming an optimum",
    )
    slots_source = pool_parser.add_mutually_exclusive_group(required=True)
    slots_source.add_argument("--pool-slots", type=int)
    slots_source.add_argument(
        "--sweep",
        action="store_true",
        help=(
            "plan pool sizes that bracket the live set (ceil(live/2), live, "
            "2*live, 4*live) instead of one --pool-slots value"
        ),
    )
    chunk_source = pool_parser.add_mutually_exclusive_group(required=True)
    chunk_source.add_argument("--bytes-per-chunk", type=int)
    chunk_source.add_argument(
        "--geometry",
        type=Path,
        help="JSON written by `kvprobe geometry --output`; replaces --bytes-per-chunk",
    )
    pool_parser.add_argument(
        "--geometry-scope",
        choices=_GEOMETRY_SCOPES,
        help=(
            "geometry scope to use; required only when the scopes differ "
            "(TP > 1), so per-device and TP-group bytes are never mixed"
        ),
    )
    live_source = pool_parser.add_mutually_exclusive_group(required=True)
    live_source.add_argument("--required-live-chunks", type=int)
    live_source.add_argument(
        "--concurrent-contexts",
        type=int,
        help=(
            "derive the live set from contexts that must stay retrievable at "
            "once; needs --context-tokens. Assumes every context stores its own "
            "distinct chunks; if contexts share a prefix, compute the distinct "
            "chunks yourself and use --required-live-chunks"
        ),
    )
    pool_parser.add_argument("--context-tokens", type=int)
    pool_parser.add_argument(
        "--chunk-tokens",
        type=int,
        help=(
            "tokens per chunk; read from --geometry when omitted. Also adds "
            "token capacities to the output"
        ),
    )
    pool_parser.add_argument(
        "--partial-chunk",
        choices=("drop", "keep"),
        help=(
            "whether a trailing partial chunk is stored; required when "
            "--context-tokens is not a multiple of the chunk size"
        ),
    )
    pool_parser.add_argument(
        "--output", type=Path, help="also save the result as JSON to this file"
    )
    pool_parser.add_argument(
        "--json",
        action="store_true",
        help="print JSON on standard output instead of the summary (summary goes to stderr)",
    )
    pool_parser.set_defaults(handler=_pool_plan)

    geometry_parser = subparsers.add_parser(
        "geometry",
        help="derive standard-attention KV bytes from model and runtime settings",
    )
    model_source = geometry_parser.add_mutually_exclusive_group(required=True)
    model_source.add_argument(
        "--model",
        help=(
            "existing local model directory/config.json or public Hugging Face "
            "model ID; remote resolution fetches config.json only"
        ),
    )
    model_source.add_argument(
        "--model-config",
        type=Path,
        help="local model config.json readable from this shell; no automatic download",
    )
    model_source.add_argument(
        "--layers",
        type=int,
        help=(
            "number of layers, for a model without a config file at hand; "
            "needs --kv-heads, --head-dim and --dtype"
        ),
    )
    geometry_parser.add_argument(
        "--kv-heads", type=int, help="global KV-head count (with --layers)"
    )
    geometry_parser.add_argument(
        "--head-dim", type=int, help="dimension of one attention head (with --layers)"
    )
    geometry_parser.add_argument(
        "--revision",
        help=(
            "Hugging Face branch, tag, or full commit SHA used with --model; "
            "defaults to main and records the resolved commit"
        ),
    )
    geometry_parser.add_argument("--chunk-tokens", type=int, required=True)
    geometry_parser.add_argument(
        "--dtype",
        help="runtime KV dtype; defaults to model_config dtype/torch_dtype",
    )
    geometry_parser.add_argument("--tensor-parallel-size", type=int, default=1)
    geometry_parser.add_argument("--pipeline-parallel-size", type=int, default=1)
    geometry_parser.add_argument(
        "--output", type=Path, help="also save the result as JSON to this file"
    )
    geometry_parser.add_argument(
        "--json",
        action="store_true",
        help="print JSON on standard output instead of the summary (summary goes to stderr)",
    )
    geometry_parser.set_defaults(handler=_geometry)
    return parser


def main(arguments: Sequence[str] | None = None) -> int:
    """Run the public command-line interface.

    Args:
        arguments: Optional arguments without the executable name.

    Returns:
        Process exit status.
    """
    parser = build_parser()
    args = parser.parse_args(arguments)
    return int(args.handler(args))
