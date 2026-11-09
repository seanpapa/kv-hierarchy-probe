# SPDX-License-Identifier: Apache-2.0

"""Normalize a measurement run into the evidence contract.

This adapter bridges script-specific measurement output and the reporter's
versioned evidence contract without inventing missing attribution or identity.

The adapters read the four run shapes this repository produces — context sweep,
concurrency sweep, admission cost, capacity sweep — and emit `0.3.1`. They
derive only what a run actually recorded, and require the caller to state what a
run cannot know: the configuration, because no run records its own model
revision or eviction policy, and the expected reuse count, because that is a
property of the workload rather than of the measurement. Where the caller and
the run disagree about something both describe, they refuse.

Gates with no run supplied are emitted as `unsupported`, so an absent gate reads
as unmeasured instead of quietly passing.
"""

# Standard
import hashlib
import json
import math
from pathlib import Path
from statistics import mean, median
from typing import Any

# First Party
from .configuration import ConfigurationError, fingerprint_configuration, validate_configuration

ADAPTER_NAME = "g5-context-adapter"
ADAPTER_VERSION = "0.4.2"

#: Select by the configured arm, never by field presence or byte magnitude.
#: NVFS is a host-wide total and is never target-device filtered. cgroup_blk is
#: filtered only when the run records the block device its reader was limited to
#: (``cgroup_io_device`` and a known ``cgroup_io_device_scope``); runs from the
#: earlier reader summed every device the container touched and record neither.
_PROVIDERS: dict[str, tuple[str, float]] = {
    "nvidia_fs": ("nvfs_read_mib", 1024.0 * 1024.0),
    "cgroup_blk": ("cgroup_read_bytes", 1.0),
}


class IngestError(Exception):
    """Raised when a run cannot be normalized without inventing a fact."""


def _measure_rows(result: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Return the measured rows of a context run, warm-ups excluded.

    Args:
        result: The run envelope's ``result`` object, keyed by context length.

    Returns:
        Pairs of context key and row, in run order.

    Raises:
        IngestError: If a block carries no rows or a row has no phase.
    """
    rows: list[tuple[str, dict[str, Any]]] = []
    for context, block in result.items():
        block_rows = block.get("rows")
        if not isinstance(block_rows, list) or not block_rows:
            raise IngestError(f"context {context} has no rows")
        for row in block_rows:
            phase = row.get("phase")
            if phase is None:
                raise IngestError(f"context {context} row has no phase")
            if phase == "measure":
                rows.append((context, row))
    if not rows:
        raise IngestError("run contains no measured rows")
    return rows


#: Device scopes the run's cgroup reader can record. ``whole_disk``: the tier's
#: own disk. ``widened_to_parent_disk``: the tier is on a partition and io.stat
#: accounts whole disks, so the counter covers that disk's other partitions too.
_FILTERED_SCOPES = {"whole_disk", "widened_to_parent_disk"}


def _device_filtered(run: dict[str, Any], provider: str) -> bool:
    """Return whether a run's counter for ``provider`` was limited to one device.

    Args:
        run: Parsed context run, including its envelope fields.
        provider: Attribution provider declared for the measurement arm.

    Returns:
        True only for ``cgroup_blk`` when the run names the device and a known
        scope; False for every other provider or an unrecorded scope.
    """
    if provider != "cgroup_blk":
        return False
    device = run.get("cgroup_io_device")
    return (
        isinstance(device, str)
        and bool(device)
        and run.get("cgroup_io_device_scope") in _FILTERED_SCOPES
    )


def _observed_bytes(
    row: dict[str, Any], provider: str, filtered: bool = False
) -> tuple[int, str, str, bool]:
    """Return the bytes a row observed and the attribution they support.

    Args:
        row: One measured request row.
        provider: Attribution provider declared for the measurement arm.
        filtered: Whether the run's counter was limited to the tier's device.

    Returns:
        Observed bytes, provider name, confidence (always ``corroborating``: a
        counter delta is not request-ID hardware tracing), and ``filtered``.

    Raises:
        IngestError: If the provider is unsupported or its counter is missing,
            negative, non-finite, or malformed. Other counters are not fallback.
    """
    if provider not in _PROVIDERS:
        raise IngestError(f"unsupported attribution provider: {provider}")
    field, scale = _PROVIDERS[provider]
    value = row.get(field)
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(value)
        or value < 0
    ):
        raise IngestError(
            f"configured provider {provider} needs a finite non-negative {field}; "
            "missing counters and counter resets cannot be replaced by another provider"
        )
    return int(value * scale), provider, "corroborating", filtered


def _source(row: dict[str, Any], observed: int) -> str:
    """Classify which tier served a request.

    Args:
        row: One measured request row.
        observed: Bytes the counter attributed to the request.

    Returns:
        One of ``target_tier``, ``upper_tier`` or ``recompute``.

    Raises:
        IngestError: If the row moved no bytes and the run cannot distinguish a
            resident-tier hit from a recompute.
    """
    if row.get("tier_ok"):
        return "target_tier"
    if observed > 0:
        return "target_tier"
    raise IngestError(
        "request moved no bytes and failed the tier gate: the tier did not serve "
        "it (a phantom tier if the framework reported a hit), and a run of this "
        "shape cannot separate an upper-tier hit from a recompute, so the adapter "
        "will not guess. Re-ingest with a run that records the serving tier."
    )


def classify(run: dict[str, Any]) -> str:
    """Return which run shape a parsed measurement output is.

    Args:
        run: Parsed measurement output.

    Returns:
        One of ``context``, ``concurrency``, ``admission`` or ``capacity``.

    Raises:
        IngestError: If the shape is not one this adapter knows.
    """
    mode = run.get("mode")
    if mode in {"context", "concurrency"}:
        return str(mode)
    gate = run.get("gate")
    if gate == "g3_admission_cost":
        return "admission"
    if gate == "capacity_justification":
        return "capacity"
    raise IngestError(
        f"unrecognized run shape (mode={mode!r}, gate={gate!r}); "
        "expected a context, concurrency, admission or capacity run"
    )


def _context_medians(run: dict[str, Any]) -> dict[str, float]:
    """Return the measured TTFT median per context of a context run.

    Args:
        run: Parsed context run.

    Returns:
        Mapping from context key to median time-to-first-token.

    Raises:
        IngestError: If a block reports no median.
    """
    medians: dict[str, float] = {}
    for context, block in (run.get("result") or {}).items():
        value = (block.get("summary") or {}).get("ttft_p50_s")
        if not isinstance(value, (int, float)):
            raise IngestError(f"context {context} has no ttft_p50_s to compare")
        medians[context] = float(value)
    return medians


def build_break_even(tier: dict[str, Any], recompute: dict[str, Any]) -> dict[str, Any]:
    """Build the gate 2 block from a tier run and a recompute run.

    Args:
        tier: Context run measured against the tier under test.
        recompute: Context run measured with the cache disabled.

    Returns:
        A ``break_even`` evidence block.

    Raises:
        IngestError: If the two runs do not cover the same contexts.
    """
    tier_medians = _context_medians(tier)
    recompute_medians = _context_medians(recompute)
    if set(tier_medians) != set(recompute_medians):
        raise IngestError(
            "break-even needs the same contexts in both arms: "
            f"tier has {sorted(tier_medians)}, recompute has {sorted(recompute_medians)}"
        )
    contexts = sorted(tier_medians, key=int)
    return {
        "availability": {"status": "supported"},
        "retrieval_ttft_s": [tier_medians[c] for c in contexts],
        "recompute_ttft_s": [recompute_medians[c] for c in contexts],
    }


def build_envelope(run: dict[str, Any]) -> dict[str, Any]:
    """Build the gate 5 block from a concurrency run.

    The tier counters are read around a whole batch, so a batch is the unit of
    evidence. A batch credits its requests as target-tier served only when it
    met its tier byte check (``tier_ok``) *and* every response in it was
    correct; a row without a recorded ``correct: true`` counts as incorrect.
    A batch that fails either check cannot be split into served and unserved
    requests, so none of it is counted as served. That is deliberately
    conservative: the envelope should end at the last level proven clean.

    The envelope holds only for the input size it was measured at, so the block
    records ``request_cacheable_tokens`` when every measured row records the
    same ``prompt.cacheable_tokens``. A run whose rows do not all record it
    (older producers) yields no size; a run whose rows record different sizes
    is refused, because one envelope cannot describe them.

    Args:
        run: Parsed concurrency run.

    Returns:
        An ``envelope`` evidence block.

    Raises:
        IngestError: If a level reports no batches, or the measured rows
            record more than one per-request input size.
    """
    levels: list[dict[str, int]] = []
    sizes: list[int | None] = []
    for level, block in sorted(
        (run.get("result") or {}).items(), key=lambda item: int(item[0])
    ):
        batches = [b for b in (block.get("batches") or []) if b.get("phase") == "measure"]
        if not batches:
            raise IngestError(f"concurrency {level} has no measured batches")
        concurrency = int(level)
        requests = sum(len(b.get("rows") or []) for b in batches)
        sizes.extend(
            _row_cacheable_tokens(row) for b in batches for row in (b.get("rows") or [])
        )
        served = sum(
            len(b.get("rows") or [])
            for b in batches
            if b.get("tier_ok") is True
            and all(row.get("correct") is True for row in (b.get("rows") or []))
        )
        levels.append(
            {
                "concurrency": concurrency,
                "requests": requests,
                "target_tier_served": served,
            }
        )
    if not levels:
        raise IngestError("concurrency run has no levels")
    block: dict[str, Any] = {"availability": {"status": "supported"}, "levels": levels}
    recorded = {size for size in sizes if size is not None}
    if len(recorded) > 1:
        raise IngestError(
            "concurrency run mixes per-request input sizes "
            f"(prompt.cacheable_tokens {sorted(recorded)}); one envelope "
            "cannot describe them"
        )
    if recorded and None not in sizes:
        block["request_cacheable_tokens"] = recorded.pop()
    return block


def _row_cacheable_tokens(row: dict[str, Any]) -> int | None:
    """Return the cacheable tokens a concurrency row records, if any.

    Args:
        row: One measured request row.

    Returns:
        ``prompt.cacheable_tokens`` when it is a positive integer, else None.
    """
    prompt = row.get("prompt")
    if not isinstance(prompt, dict):
        return None
    value = prompt.get("cacheable_tokens")
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return None


def build_admission(run: dict[str, Any], expected_reuses: float) -> dict[str, Any]:
    """Build the gate 3 block from an admission-cost run.

    Args:
        run: Parsed admission run.
        expected_reuses: How many times the workload is expected to reuse an
            entry. The measurement cannot supply this; the caller must.

    Returns:
        An ``admission`` evidence block.

    Raises:
        IngestError: If no row carries a measured admission cost, or an
            all-attempt row is malformed or holds fewer or more attempts than
            the run's ``arguments.samples`` requested.
    """
    rows = run.get("rows") or []
    attempt_rows = [row for row in rows if isinstance(row.get("attempts"), list)]
    if attempt_rows:
        if len(attempt_rows) != len(rows):
            raise IngestError(
                "admission run mixes all-attempt and legacy row contracts"
            )
        costs: list[float] = []
        savings: list[float] = []
        counts = {
            "attempted": 0,
            "target_tier": 0,
            "fallback": 0,
            "store_unattributed": 0,
            "unattributed": 0,
        }
        outcomes = set(counts) - {"attempted"}
        # Every requested attempt must be present: a row that holds fewer
        # attempts than the run asked for has lost outcomes the repayment needs.
        requested = (run.get("arguments") or {}).get("samples")
        for row_index, row in enumerate(attempt_rows):
            attempts = row["attempts"]
            if not attempts:
                raise IngestError(f"admission row {row_index} has no attempts")
            if (
                isinstance(requested, int)
                and not isinstance(requested, bool)
                and len(attempts) != requested
            ):
                raise IngestError(
                    f"admission row {row_index} holds {len(attempts)} attempts "
                    f"but the run requested {requested}"
                )
            for attempt_index, attempt in enumerate(attempts):
                if not isinstance(attempt, dict):
                    raise IngestError(
                        "admission attempt "
                        f"{row_index}:{attempt_index} must be an object"
                    )
                cost = attempt.get("admission_cost_s")
                saving = attempt.get("realized_reuse_saving_s")
                outcome = attempt.get("outcome")
                if (
                    not isinstance(cost, (int, float))
                    or isinstance(cost, bool)
                    or not math.isfinite(float(cost))
                    or float(cost) < 0
                ):
                    raise IngestError(
                        f"admission attempt {row_index}:{attempt_index} has an "
                        "invalid admission cost"
                    )
                if (
                    not isinstance(saving, (int, float))
                    or isinstance(saving, bool)
                    or not math.isfinite(float(saving))
                ):
                    raise IngestError(
                        f"admission attempt {row_index}:{attempt_index} has an "
                        "invalid realized reuse saving"
                    )
                if outcome not in outcomes:
                    raise IngestError(
                        f"admission attempt {row_index}:{attempt_index} has an "
                        "invalid outcome"
                    )
                costs.append(float(cost))
                savings.append(float(saving))
                counts["attempted"] += 1
                counts[str(outcome)] += 1
        return {
            "availability": {"status": "supported"},
            "store_cost_s": mean(costs),
            "expected_reuses": float(expected_reuses),
            "realized_reuse_saving_s": mean(savings),
            "attempt_counts": counts,
            "calculation_basis": "all_attempts",
        }

    costs = [
        float(row["g3_admission_cost_s"])
        for row in rows
        if isinstance(row.get("g3_admission_cost_s"), (int, float))
    ]
    if not costs:
        raise IngestError(
            "admission run recorded no admission cost: every row was dropped, "
            "so there is nothing to repay"
        )
    block: dict[str, Any] = {
        "availability": {"status": "supported"},
        "store_cost_s": median(costs),
        "expected_reuses": float(expected_reuses),
    }
    # How many stores the cost rests on. A store that showed no device write is
    # dropped by the producer, and the report says how many were.
    if rows and all(
        isinstance(row.get(field), int) and not isinstance(row.get(field), bool)
        for row in rows
        for field in ("stored", "dropped_store")
    ):
        valid = sum(int(row["stored"]) for row in rows)
        dropped = sum(int(row["dropped_store"]) for row in rows)
        block["sample_counts"] = {
            "requested": valid + dropped,
            "valid": valid,
            "dropped": dropped,
        }
    return block


def build_capacity(
    run: dict[str, Any], upper_tier_capacity_bytes: int
) -> dict[str, Any]:
    """Build the gate 4 block from a capacity sweep.

    Args:
        run: Parsed capacity run.
        upper_tier_capacity_bytes: Declared usable capacity of the tier above
            the target tier.

    Returns:
        A ``capacity`` evidence block.

    Raises:
        IngestError: If the sweep recorded no rows, a row does not match the
            current Gate 4 producer contract, or its classification counts do
            not cover the declared working set exactly.
    """
    rows = run.get("rows") or []
    if not rows:
        raise IngestError("capacity run has no rows")
    if (
        not isinstance(upper_tier_capacity_bytes, int)
        or isinstance(upper_tier_capacity_bytes, bool)
        or upper_tier_capacity_bytes <= 0
    ):
        raise IngestError("upper-tier capacity must be a positive integer")

    count_fields = {
        "dram_hits": "upper_tier_served",
        "nvme_hits": "target_tier_served",
        "fallback": "fallback",
        "miss": "miss",
    }
    totals = {
        "retrievals_expected": 0,
        "upper_tier_served": 0,
        "target_tier_served": 0,
        "fallback": 0,
        "miss": 0,
    }
    overflow_totals = dict.fromkeys(totals, 0)
    previous_working_set = 0
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise IngestError(f"capacity row {index} must be an object")
        working_set = row.get("working_set")
        resident_mib = row.get("resident_mib")
        if (
            not isinstance(working_set, int)
            or isinstance(working_set, bool)
            or working_set <= previous_working_set
        ):
            raise IngestError(
                "capacity working sets must be positive, unique, and ascending"
            )
        if (
            not isinstance(resident_mib, (int, float))
            or isinstance(resident_mib, bool)
            or resident_mib <= 0
        ):
            raise IngestError(f"capacity row {index} has invalid resident_mib")
        counts: dict[str, int] = {}
        for source, normalized in count_fields.items():
            value = row.get(source)
            if (
                not isinstance(value, int)
                or isinstance(value, bool)
                or value < 0
            ):
                raise IngestError(
                    f"capacity row {index}.{source} must be a non-negative integer"
                )
            counts[normalized] = value
        classified = sum(counts.values())
        if classified != working_set:
            raise IngestError(
                f"capacity row {index} classified {classified} retrievals for "
                f"working_set={working_set}"
            )

        row_totals = {"retrievals_expected": working_set, **counts}
        for name, value in row_totals.items():
            totals[name] += value
        resident_bytes = int(float(resident_mib) * 1024 * 1024)
        if resident_bytes > upper_tier_capacity_bytes:
            for name, value in row_totals.items():
                overflow_totals[name] += value
        previous_working_set = working_set

    overflow = overflow_totals["retrievals_expected"] > 0
    served = overflow_totals["target_tier_served"] > 0
    return {
        "availability": {"status": "supported"},
        "upper_tier_overflow_observed": overflow,
        "target_tier_served": served,
        "classification_counts": totals,
        "overflow_classification_counts": overflow_totals,
    }


def _not_measured(reason_code: str, detail: str) -> dict[str, Any]:
    """Return a gate block for something the supplied runs did not measure.

    Args:
        reason_code: Machine-readable reason, usually ``<gate>_not_measured``.
        detail: What was missing, in one sentence.

    Returns:
        A gate block whose availability is ``unsupported``.
    """
    return {
        "availability": {
            "status": "unsupported",
            "reason_code": reason_code,
            "detail": detail,
        }
    }


def build_document(
    runs: list[tuple[str, dict[str, Any], str]],
    configuration: dict[str, Any],
    *,
    run_id: str,
    expected_reuses: float | None = None,
    configuration_id: str = "config-a",
) -> dict[str, Any]:
    """Normalize one or more measurement runs into a single evidence document.

    Args:
        runs: Triples of artifact id, parsed run, and the run file's digest.
        configuration: Configuration parameters describing every run given.
        run_id: Identifier for the emitted document.
        expected_reuses: Reuses the workload expects per admitted entry, needed
            only when an admission run is supplied.
        configuration_id: Identifier the measurements are bound to.

    Returns:
        An evidence document at ``schema_version`` 0.3.1.

    Raises:
        IngestError: If a run shape is unknown, two runs of the same kind
            conflict, a run contradicts the configuration, or an admission run
            arrives without the workload's expected reuse count.
        ConfigurationError: If the configuration itself is malformed.
    """
    if not runs:
        raise IngestError("no runs supplied")
    validate_configuration(configuration)

    by_kind: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    artifacts: list[dict[str, str]] = []
    for artifact_id, run, digest in runs:
        kind = classify(run)
        _check_configuration_matches(run, configuration, kind)
        by_kind.setdefault(kind, []).append((artifact_id, run))
        artifacts.append(
            {"id": artifact_id, "kind": "measurement_output", "sha256": digest}
        )

    context_runs = by_kind.get("context", [])
    tier_runs: list[tuple[str, dict[str, Any]]] = []
    recompute_runs: list[tuple[str, dict[str, Any]]] = []
    for artifact_id, run in context_runs:
        arm = _configured_arm(run, configuration, required=True)
        assert arm is not None
        destination = recompute_runs if arm["name"] == "recompute" else tier_runs
        destination.append((artifact_id, run))
    for kind, entries in by_kind.items():
        if kind != "context" and len(entries) > 1:
            raise IngestError(f"more than one {kind} run supplied; ingest them separately")
    if len(tier_runs) > 1 or len(recompute_runs) > 1:
        raise IngestError(
            "more than one context run per arm supplied; ingest them separately"
        )

    evidence: dict[str, Any] = {}
    requests: list[dict[str, Any]] = []
    if tier_runs:
        artifact_id, run = tier_runs[0]
        arm = _configured_arm(run, configuration, required=True)
        assert arm is not None
        requests = _requests(
            run, artifact_id, configuration["kv_geometry"], arm["attribution_provider"]
        )
        evidence["attribution"] = {
            "availability": {"status": "supported"},
            "requests": requests,
        }
        warmups = _warmup_observations(run, artifact_id, arm["attribution_provider"])
        if warmups is not None:
            evidence["attribution"]["warmup_observations"] = warmups
    else:
        evidence["attribution"] = _not_measured(
            "per_request_attribution_not_recorded",
            "No context run was supplied; only a context run records bytes per request.",
        )

    if tier_runs and recompute_runs:
        evidence["break_even"] = build_break_even(tier_runs[0][1], recompute_runs[0][1])
    else:
        evidence["break_even"] = _not_measured(
            "break_even_not_measured",
            "Break-even needs a tier context run and a recompute context run.",
        )

    if "admission" in by_kind:
        if expected_reuses is None:
            raise IngestError(
                "an admission run needs --expected-reuses: how often the workload "
                "reuses an entry is a property of the workload, not of the run"
            )
        evidence["admission"] = build_admission(by_kind["admission"][0][1], expected_reuses)
    else:
        evidence["admission"] = _not_measured(
            "admission_not_measured", "No admission-cost run was supplied."
        )

    if "capacity" in by_kind:
        evidence["capacity"] = build_capacity(
            by_kind["capacity"][0][1],
            int(configuration["cache"]["upper_tier_capacity_bytes"]),
        )
    else:
        evidence["capacity"] = _not_measured(
            "capacity_not_measured", "No capacity sweep was supplied."
        )

    if "concurrency" in by_kind:
        evidence["envelope"] = build_envelope(by_kind["concurrency"][0][1])
        level_count = len(evidence["envelope"]["levels"])
    else:
        evidence["envelope"] = _not_measured(
            "concurrency_not_measured", "No concurrency sweep was supplied."
        )
        level_count = 0

    def bound(gate: str) -> str | None:
        """Return the configuration a gate is bound to, or None if unmeasured."""
        return (
            configuration_id
            if evidence[gate]["availability"]["status"] == "supported"
            else None
        )

    return {
        "schema_version": "0.3.1",
        "run_id": run_id,
        "description": (
            "normalized by "
            + ADAPTER_NAME
            + " from "
            + ", ".join(sorted(by_kind))
            + " run(s)"
        ),
        "provenance": {
            "normalized_by": {"adapter": ADAPTER_NAME, "version": ADAPTER_VERSION},
            "artifacts": artifacts,
        },
        "validity": {"fatal_observations": []},
        "configurations": [
            {
                "id": configuration_id,
                "fingerprint": fingerprint_configuration(configuration),
                "parameters": configuration,
            }
        ],
        "bindings": {
            "attribution_requests": [
                {"request_id": request["id"], "configuration_id": configuration_id}
                for request in requests
            ],
            "break_even": bound("break_even"),
            "admission": bound("admission"),
            "capacity": bound("capacity"),
            "envelope_levels": [
                {"level_index": index, "configuration_id": configuration_id}
                for index in range(level_count)
            ],
        },
        "evidence": evidence,
    }


def _configured_arm(
    run: dict[str, Any], configuration: dict[str, Any], *, required: bool
) -> dict[str, Any] | None:
    """Resolve a recorded arm to its declared configuration.

    Args:
        run: Parsed measurement output.
        configuration: Validated campaign configuration.
        required: Whether this run shape must record an arm name.

    Returns:
        Matching arm parameters, or None for a legacy run without an arm.

    Raises:
        IngestError: If the arm is missing, contradictory, or undeclared.
    """
    arguments = run.get("arguments") or {}
    if not isinstance(arguments, dict):
        raise IngestError("run arguments must be an object")
    name = run.get("arm", arguments.get("arm"))
    if "arm" in arguments and arguments["arm"] != name:
        raise IngestError("run arm contradicts arguments.arm")
    if name is None and not required:
        return None
    if not isinstance(name, str) or not name:
        raise IngestError("context/concurrency run must record its arm")
    for arm in configuration["arms"]:
        if arm["name"] == name:
            return arm
    raise IngestError(f"run arm {name!r} is not declared in configuration.arms")


def _check_configuration_matches(
    run: dict[str, Any], configuration: dict[str, Any], kind: str
) -> None:
    """Fail when the configuration contradicts a run it claims to describe.

    Args:
        run: Parsed measurement output.
        configuration: Configuration parameters supplied by the caller.
        kind: The run shape, as returned by :func:`classify`.

    Raises:
        IngestError: If a field both describe disagrees.
    """
    arm = _configured_arm(
        run, configuration, required=kind in {"context", "concurrency"}
    )
    arguments = run.get("arguments") or {}
    measurement = configuration["measurement"]
    fields = ("contexts", "samples", "warmup")
    if kind == "concurrency":
        fields = ("concurrency", "warmup")
    for field in fields:
        stated, actual = measurement.get(field), arguments.get(field)
        if actual is not None and stated != actual:
            raise IngestError(
                f"configuration {field}={stated!r} contradicts the "
                f"{kind} run's {field}={actual!r}"
            )
    if kind == "concurrency" and "batches" in arguments:
        if arguments["batches"] != measurement["samples"]:
            raise IngestError("configuration samples contradicts concurrency batches")

    comparisons = {
        "model_root": configuration["model"]["root"],
        "model_revision": configuration["model"]["revision"],
        "served_name": configuration["model"]["served_name"],
        "image_digest": configuration["runtime"]["image_digest"],
        # Storage counters do not identify which accelerator issued the I/O,
        # even when cgroup accounting is filtered to one block device. When
        # the run names an accelerator, the configuration must agree.
        "device_id": configuration["runtime"]["device_id"],
        "chunk_tokens": configuration["kv_geometry"]["chunk_tokens"],
        "bytes_per_chunk": configuration["kv_geometry"]["bytes_per_chunk"],
        "dtype": configuration["kv_geometry"]["dtype"],
        "upper_tier_capacity_bytes": configuration["cache"]["upper_tier_capacity_bytes"],
    }
    # Optional in the configuration: compared only when the caller stated it.
    if "gpu_index" in configuration["runtime"]:
        comparisons["gpu_index"] = configuration["runtime"]["gpu_index"]
    if arm is not None:
        for field in (
            "backend", "tier_path", "attribution_provider", "staging_capacity_bytes",
            "eviction_policy", "eviction_parameters",
        ):
            comparisons[field] = arm[field]
    for field, stated in comparisons.items():
        for location, recorded in (("run", run), ("arguments", arguments)):
            if field in recorded and recorded[field] != stated:
                raise IngestError(
                    f"configuration {field}={stated!r} contradicts "
                    f"{location}.{field}={recorded[field]!r}"
                )


def _expected_bytes(row: dict[str, Any], geometry: dict[str, Any]) -> int:
    """Return the bytes a request should have moved, from geometry not observation.

    Deriving this from what was observed would make every request match itself
    and gate 1 could never catch a short read. It comes from the cacheable
    tokens the run recorded and the chunk geometry the configuration states.

    Args:
        row: One measured request row.
        geometry: The configuration's ``kv_geometry``.

    Returns:
        Expected bytes for the request.

    Raises:
        IngestError: If the row did not record its cacheable tokens.
    """
    cacheable = row.get("cacheable_tokens")
    if not isinstance(cacheable, int) or cacheable <= 0:
        raise IngestError(
            "row has no cacheable_tokens, so the bytes it should have moved "
            "cannot be derived; a run of this shape cannot support gate 1"
        )
    chunks = cacheable // int(geometry["chunk_tokens"])
    return chunks * int(geometry["bytes_per_chunk"])


def _requests(
    run: dict[str, Any], artifact_id: str, geometry: dict[str, Any], provider: str
) -> list[dict[str, Any]]:
    """Return the per-request attribution records of a context run.

    Args:
        run: Parsed context run.
        artifact_id: Artifact the rows came from.
        geometry: The configuration's ``kv_geometry``, used for expected bytes.
        provider: Counter provider declared by this arm's configuration.

    Returns:
        Request records for the attribution block.
    """
    records: list[dict[str, Any]] = []
    device_filtered = _device_filtered(run, provider)
    for context, row in _measure_rows(run.get("result") or {}):
        observed, selected_provider, confidence, filtered = _observed_bytes(
            row, provider, device_filtered
        )
        records.append(
            {
                "id": f"ctx{context}-{row.get('index', len(records))}",
                "correct": bool(row.get("correct")),
                "source": _source(row, observed),
                "expected_bytes": _expected_bytes(row, geometry),
                "observed_bytes": observed,
                "attribution": {
                    "provider": selected_provider,
                    "granularity": "per_request",
                    "confidence": confidence,
                    "target_device_filtered": filtered,
                    "artifact_refs": [artifact_id],
                },
            }
        )
    return records


def _warmup_observations(
    run: dict[str, Any], artifact_id: str, provider: str
) -> dict[str, Any] | None:
    """Count warm-up requests of a context run that moved no bytes from the tier.

    Warm-ups stay out of the verdict. Counting them keeps a tier that did not
    serve its first reuse visible instead of discarding that row silently.
    A warm-up whose selected counter is missing or malformed is not counted.

    Args:
        run: Parsed context run.
        artifact_id: Artifact the rows came from.
        provider: Counter provider declared by this arm's configuration.

    Returns:
        The ``warmup_observations`` block, or None when the run has no warm-ups.
    """
    requests = 0
    moved_no_bytes = 0
    for block in (run.get("result") or {}).values():
        for row in block.get("rows") or []:
            if row.get("phase") != "warmup":
                continue
            try:
                observed = _observed_bytes(row, provider)[0]
            except IngestError:
                continue
            requests += 1
            moved_no_bytes += int(observed == 0)
    if requests == 0:
        return None
    return {
        "requests": requests,
        "moved_no_bytes": moved_no_bytes,
        "artifact_refs": [artifact_id],
    }


def ingest_files(
    run_paths: list[Path],
    configuration_path: Path,
    run_id: str,
    expected_reuses: float | None = None,
) -> dict[str, Any]:
    """Read runs and a configuration from disk and return evidence.

    Args:
        run_paths: Paths to measurement outputs.
        configuration_path: Path to the configuration parameters.
        run_id: Identifier for the emitted document.
        expected_reuses: Reuses expected per admitted entry, for gate 3.

    Returns:
        An evidence document at ``schema_version`` 0.3.1.

    Raises:
        IngestError: If an input is not valid JSON or cannot be normalized.
        ConfigurationError: If the configuration is malformed.
    """
    runs: list[tuple[str, dict[str, Any], str]] = []
    try:
        configuration = json.loads(configuration_path.read_text(encoding="utf-8"))
        for path in run_paths:
            raw = path.read_bytes()
            runs.append((path.stem, json.loads(raw), hashlib.sha256(raw).hexdigest()))
    except json.JSONDecodeError as error:
        raise IngestError(f"input is not valid JSON: {error}") from error
    return build_document(
        runs, configuration, run_id=run_id, expected_reuses=expected_reuses
    )
