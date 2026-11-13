# SPDX-License-Identifier: Apache-2.0
"""Normalize five-gate evidence into a claim-safe report."""

# Standard
import math
from statistics import median
from typing import Any

# Local
from .configuration import ConfigurationError, validate_registry


class EvidenceError(ValueError):
    """Raised when normalized evidence is malformed or incomplete."""


def _object(parent: dict[str, Any], name: str) -> dict[str, Any]:
    """Return a required child object.

    Args:
        parent: Parent mapping.
        name: Required child field.

    Returns:
        Child mapping.

    Raises:
        EvidenceError: If the child is missing or is not an object.
    """
    value = parent.get(name)
    if not isinstance(value, dict):
        raise EvidenceError(f"{name} must be an object")
    return value


def _numbers(parent: dict[str, Any], name: str) -> list[float]:
    """Return a required non-empty list of finite numbers.

    Args:
        parent: Parent mapping.
        name: Required list field.

    Returns:
        Values converted to floats.

    Raises:
        EvidenceError: If the field is malformed.
    """
    value = parent.get(name)
    if not isinstance(value, list) or not value:
        raise EvidenceError(f"{name} must be a non-empty list")
    if not all(
        isinstance(item, (int, float))
        and not isinstance(item, bool)
        and math.isfinite(float(item))
        and float(item) >= 0
        for item in value
    ):
        raise EvidenceError(f"{name} must contain finite non-negative numbers")
    return [float(item) for item in value]


_GATE_NAMES = (
    "Serving Tier Verification",
    "Single-hit retrieval break-even",
    "Admission cost",
    "Capacity justification",
    "Operating envelope",
)
_SOURCES = {"target_tier", "upper_tier", "recompute"}
_GRANULARITIES = {"per_request", "per_batch", "per_run"}
_CONFIDENCE_LEVELS = {"direct", "corroborating", "heuristic"}
#: Confidence that can support a Gate 1 PASS when the counter is filtered to the
#: target device. A heuristic attribution never can.
_PASSING_CONFIDENCE = {"direct", "corroborating"}
#: Output contract of the assessment. 0.5.0: a gate passes when its measurement
#: is valid; what the measurement found (break-even, economics, necessity,
#: envelope) is a separate ``result`` that never changes the verdict. 0.5.1
#: added Gate 3/4 count context. 0.6.0 includes every classified reuse attempt
#: in Gate 3 repayment; Gate 4 keeps its overflow-count result. 0.6.1 states
#: the Gate 5 result at batch level (the counters cover a batch, not a request)
#: and names the per-request input size when the evidence records it. 0.6.2
#: no longer says the producer's records show whether a Gate 4 store or load
#: failed: they can separate no observed write from write activity without an
#: established retrieval, which does not place the entry on the target device.
ASSESSMENT_VERSION = "0.6.2"
_GATE_STATUSES = {"PASS", "FAIL", "UNPROVEN", "SKIP_UNSUPPORTED", "INVALID"}
_FATAL_SCOPES = {"measurement"}


def _non_empty_text(parent: dict[str, Any], name: str) -> str:
    """Return a required non-empty string."""
    value = parent.get(name)
    if not isinstance(value, str) or not value.strip():
        raise EvidenceError(f"{name} must be a non-empty string")
    return value


def _non_negative_number(parent: dict[str, Any], name: str) -> float:
    """Return a required finite non-negative number."""
    value = parent.get(name)
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(float(value))
        or float(value) < 0
    ):
        raise EvidenceError(f"{name} must be a finite non-negative number")
    return float(value)


def _finite_number(parent: dict[str, Any], name: str) -> float:
    """Return a required finite number, including a negative observation."""
    value = parent.get(name)
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(float(value))
    ):
        raise EvidenceError(f"{name} must be a finite number")
    return float(value)


def _gate_v0_2(
    gate_id: int, status: str, reason: str
) -> dict[str, int | str]:
    """Build one gate verdict.

    Args:
        gate_id: Gate number, 1 to 5.
        status: One of the gate statuses.
        reason: Why the measurement is or is not valid.

    Returns:
        The verdict. ``result`` starts empty; a valid measurement of gates 2 to
        5 later states what it found there.

    Raises:
        EvidenceError: If the status is unknown.
    """
    if status not in _GATE_STATUSES:
        raise EvidenceError(f"unknown gate status: {status}")
    return {
        "id": gate_id,
        "name": _GATE_NAMES[gate_id - 1],
        "status": status,
        "reason": reason,
        "result": "",
    }


def _availability(section: dict[str, Any], name: str) -> tuple[bool, str]:
    """Validate one section's support declaration."""
    availability = _object(section, "availability")
    status = availability.get("status")
    if status == "supported":
        return True, ""
    if status != "unsupported":
        raise EvidenceError(
            f"{name}.availability.status must be 'supported' or 'unsupported'"
        )
    reason_code = _non_empty_text(availability, "reason_code")
    detail = _non_empty_text(availability, "detail")
    return False, f"{reason_code}: {detail}"


def _artifact_ids(payload: dict[str, Any]) -> set[str]:
    """Validate provenance and return declared artifact identifiers."""
    provenance = _object(payload, "provenance")
    normalized_by = _object(provenance, "normalized_by")
    _non_empty_text(normalized_by, "adapter")
    _non_empty_text(normalized_by, "version")
    artifacts = provenance.get("artifacts")
    if not isinstance(artifacts, list):
        raise EvidenceError("provenance.artifacts must be a list")
    identifiers: set[str] = set()
    for index, artifact in enumerate(artifacts):
        if not isinstance(artifact, dict):
            raise EvidenceError(f"provenance artifact {index} must be an object")
        artifact_id = _non_empty_text(artifact, "id")
        _non_empty_text(artifact, "kind")
        digest = _non_empty_text(artifact, "sha256")
        invalid_character = any(
            character not in "0123456789abcdef" for character in digest
        )
        if len(digest) != 64 or invalid_character:
            raise EvidenceError(
                f"provenance artifact {artifact_id} sha256 must be 64 "
                "lowercase hex characters"
            )
        if artifact_id in identifiers:
            raise EvidenceError(f"duplicate provenance artifact id: {artifact_id}")
        identifiers.add(artifact_id)
    return identifiers


def _artifact_refs(
    parent: dict[str, Any], artifact_ids: set[str], context: str
) -> list[str]:
    """Validate artifact references against provenance."""
    references = parent.get("artifact_refs")
    if (
        not isinstance(references, list)
        or not references
        or not all(isinstance(reference, str) and reference for reference in references)
    ):
        raise EvidenceError(f"{context}.artifact_refs must be a non-empty string list")
    unknown = sorted(set(references) - artifact_ids)
    if unknown:
        raise EvidenceError(
            f"{context} references unknown artifacts: {', '.join(unknown)}"
        )
    return references


def _fatal_observations(
    payload: dict[str, Any], artifact_ids: set[str]
) -> list[dict[str, Any]]:
    """Validate and return fatal observations."""
    validity = _object(payload, "validity")
    observations = validity.get("fatal_observations")
    if not isinstance(observations, list):
        raise EvidenceError("validity.fatal_observations must be a list")
    for index, observation in enumerate(observations):
        if not isinstance(observation, dict):
            raise EvidenceError(f"fatal observation {index} must be an object")
        _non_empty_text(observation, "code")
        scope = _non_empty_text(observation, "scope")
        if scope not in _FATAL_SCOPES:
            raise EvidenceError(f"fatal observation {index} has unknown scope: {scope}")
        _artifact_refs(observation, artifact_ids, f"fatal observation {index}")
    return observations


def _invalid_assessment(
    run_id: str,
    observations: list[dict[str, Any]],
    assessment_version: str = ASSESSMENT_VERSION,
) -> dict[str, Any]:
    """Return a claim-suppressed assessment for a fatally invalid run."""
    codes = ", ".join(str(observation["code"]) for observation in observations)
    reason = f"fatal measurement observation: {codes}"
    return {
        "assessment_version": assessment_version,
        "run_id": run_id,
        "overall": "INVALID",
        "performance_claims": "suppressed",
        "gates": [
            _gate_v0_2(index, "INVALID", reason) for index in range(1, 6)
        ],
        "validated_envelope": {"max_concurrency": None},
    }


def _warmup_note(section: dict[str, Any], artifact_ids: set[str]) -> str:
    """Return the Gate 1 reason suffix for warm-ups that moved no tier bytes.

    Warm-ups never change the verdict. When some moved no bytes, the suffix
    says so, because the measured rows alone would hide that the tier missed
    the first reuse.

    Args:
        section: The attribution section.
        artifact_ids: Artifact identifiers declared in provenance.

    Returns:
        An empty string, or a ``; ...`` suffix for the reason.

    Raises:
        EvidenceError: If ``warmup_observations`` is malformed.
    """
    if "warmup_observations" not in section:
        return ""
    warmups = _object(section, "warmup_observations")
    counts: dict[str, int] = {}
    for name in ("requests", "moved_no_bytes"):
        value = warmups.get(name)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise EvidenceError(
                f"attribution.warmup_observations.{name} must be a non-negative integer"
            )
        counts[name] = value
    if counts["moved_no_bytes"] > counts["requests"]:
        raise EvidenceError(
            "attribution.warmup_observations.moved_no_bytes exceeds requests"
        )
    _artifact_refs(warmups, artifact_ids, "attribution.warmup_observations")
    if counts["moved_no_bytes"] == 0:
        return ""
    return (
        f"; note: {counts['moved_no_bytes']}/{counts['requests']} warm-up requests "
        "moved no bytes from the tier (excluded from the verdict) — the tier did "
        "not serve every first reuse, as an admission policy that stores a block "
        "only on its second use would produce"
    )


def _capacity_classification_counts(
    section: dict[str, Any], overflow: bool, tier_reached: bool
) -> tuple[dict[str, int], dict[str, int]] | None:
    """Validate optional Gate 4 classification totals.

    Evidence written before the current ingest correction carries only the two
    Gate 4 booleans. New evidence also retains enough counts to prove that every
    retrieval was classified and that the booleans describe the rows beyond the
    declared upper-tier capacity.

    Args:
        section: Normalized capacity evidence.
        overflow: Recorded upper-tier overflow flag.
        tier_reached: Recorded target-tier service flag.

    Returns:
        Overall and beyond-capacity counts, or ``None`` for legacy evidence.

    Raises:
        EvidenceError: If counts are incomplete, inconsistent, or malformed.
    """
    names = ("classification_counts", "overflow_classification_counts")
    present = [name in section for name in names]
    if not any(present):
        return None
    if not all(present):
        raise EvidenceError(
            "capacity classification_counts and overflow_classification_counts "
            "must appear together"
        )

    fields = (
        "retrievals_expected",
        "upper_tier_served",
        "target_tier_served",
        "fallback",
        "miss",
    )
    normalized: list[dict[str, int]] = []
    for name in names:
        value = section.get(name)
        if not isinstance(value, dict) or set(value) != set(fields):
            detail = ", ".join(fields)
            raise EvidenceError(
                f"capacity.{name} must contain exactly {detail}"
            )
        counts: dict[str, int] = {}
        for field in fields:
            count = value.get(field)
            if (
                not isinstance(count, int)
                or isinstance(count, bool)
                or count < 0
            ):
                raise EvidenceError(
                    f"capacity.{name}.{field} must be a non-negative integer"
                )
            counts[field] = count
        classified = sum(counts[field] for field in fields[1:])
        if classified != counts["retrievals_expected"]:
            raise EvidenceError(
                f"capacity.{name} classified {classified} retrievals but expected "
                f"{counts['retrievals_expected']}"
            )
        normalized.append(counts)

    totals, beyond = normalized
    for field in fields:
        if beyond[field] > totals[field]:
            raise EvidenceError(
                f"capacity overflow count {field} exceeds the sweep total"
            )
    if overflow != (beyond["retrievals_expected"] > 0):
        raise EvidenceError(
            "capacity upper_tier_overflow_observed contradicts overflow counts"
        )
    if tier_reached != (beyond["target_tier_served"] > 0):
        raise EvidenceError(
            "capacity target_tier_served contradicts overflow target-tier counts"
        )
    return totals, beyond


def _assess_attribution_v0_2(
    section: dict[str, Any], artifact_ids: set[str]
) -> dict[str, Any]:
    """Assess v0.2 request attribution evidence."""
    corroborating = section.get("corroborating_observations", [])
    if not isinstance(corroborating, list):
        raise EvidenceError("attribution.corroborating_observations must be a list")
    for index, observation in enumerate(corroborating):
        if not isinstance(observation, dict):
            raise EvidenceError(
                f"corroborating attribution observation {index} must be an object"
            )
        _non_empty_text(observation, "provider")
        if observation.get("granularity") not in {"per_batch", "per_run"}:
            raise EvidenceError(
                f"corroborating attribution observation {index} must be "
                "per_batch or per_run"
            )
        _non_negative_number(observation, "observed_bytes")
        _artifact_refs(
            observation,
            artifact_ids,
            f"corroborating attribution observation {index}",
        )
    supported, reason = _availability(section, "attribution")
    if not supported:
        return _gate_v0_2(1, "SKIP_UNSUPPORTED", reason)
    tolerance = section.get("tolerance_fraction", 0.10)
    if (
        not isinstance(tolerance, (int, float))
        or isinstance(tolerance, bool)
        or not 0 <= float(tolerance) <= 1
    ):
        raise EvidenceError("attribution.tolerance_fraction must be between 0 and 1")
    requests = section.get("requests")
    if not isinstance(requests, list) or not requests:
        raise EvidenceError("attribution.requests must be a non-empty list")

    target_count = 0
    proven_count = 0
    attributable_count = 0
    violated_count = 0
    direct_count = 0
    # Byte agreement is reported apart from attribution strength; these counts say
    # whether the tier moved the expected bytes or nothing at all (a phantom tier).
    byte_match_count = 0
    no_bytes_count = 0
    for index, request in enumerate(requests):
        if not isinstance(request, dict):
            raise EvidenceError(f"attribution request {index} must be an object")
        _non_empty_text(request, "id")
        source = request.get("source")
        if source not in _SOURCES:
            raise EvidenceError(f"attribution request {index} has an invalid source")
        if not isinstance(request.get("correct"), bool):
            raise EvidenceError(f"attribution request {index}.correct must be boolean")
        attribution = _object(request, "attribution")
        _non_empty_text(attribution, "provider")
        granularity = attribution.get("granularity")
        confidence = attribution.get("confidence")
        if granularity not in _GRANULARITIES:
            raise EvidenceError(
                f"attribution request {index} has an invalid granularity"
            )
        if confidence not in _CONFIDENCE_LEVELS:
            raise EvidenceError(f"attribution request {index} has invalid confidence")
        if not isinstance(attribution.get("target_device_filtered"), bool):
            raise EvidenceError(
                f"attribution request {index}.target_device_filtered must be boolean"
            )
        _artifact_refs(attribution, artifact_ids, f"attribution request {index}")
        expected = _non_negative_number(request, "expected_bytes")
        observed = _non_negative_number(request, "observed_bytes")
        filtered = attribution["target_device_filtered"] is True

        # A request is attributable when it was answered correctly and its own
        # interval was observed with a confidence that can support a verdict.
        attributable = (
            request["correct"] is True
            and granularity == "per_request"
            and confidence in _PASSING_CONFIDENCE
        )
        attributable_count += int(attributable)
        violated_count += int(request["correct"] is not True)
        if source != "target_tier":
            continue
        target_count += 1
        lower = expected * (1 - float(tolerance))
        upper = expected * (1 + float(tolerance))
        byte_match = expected > 0 and lower <= observed <= upper
        byte_match_count += int(byte_match)
        moved_nothing = expected > 0 and observed == 0
        no_bytes_count += int(moved_nothing)
        proven = attributable and filtered and byte_match
        proven_count += int(proven)
        direct_count += int(proven and confidence == "direct")
        # Evidence of a violation: nothing moved at all; or this request's own
        # interval shows fewer bytes than expected, which holds for any counter,
        # because an unfiltered counter only adds other devices' bytes; or a
        # counter filtered to the target device shows more than expected. More
        # bytes on an unfiltered counter prove nothing either way.
        per_request = granularity == "per_request"
        violated_count += int(
            moved_nothing
            or (per_request and observed < lower)
            or (per_request and filtered and observed > upper)
        )

    if target_count == 0 or violated_count > 0:
        status = "FAIL"
    elif attributable_count == len(requests) and proven_count == target_count:
        status = "PASS"
    else:
        status = "UNPROVEN"
    grade = ""
    if proven_count:
        grade = " (direct)" if direct_count == proven_count else " (corroborated)"
    reason = (
        f"{proven_count}/{target_count} target-tier requests have per-request, "
        f"target-device-filtered byte evidence{grade}; "
        f"{attributable_count}/{len(requests)} request intervals have qualifying "
        "per-request observations; "
        f"{byte_match_count}/{target_count} target-tier requests match expected "
        f"bytes; {no_bytes_count}/{target_count} moved no bytes from the tier"
    )
    if status == "UNPROVEN":
        reason += (
            "; evidence is insufficient, not a measured failure — record a "
            "counter filtered to the tier's device for each request"
        )
    reason += _warmup_note(section, artifact_ids)
    return _gate_v0_2(1, status, reason)


def _assess_v0_2(payload: dict[str, Any]) -> dict[str, Any]:
    """Apply the capability-aware v0.2 evidence contract."""
    run_id = _non_empty_text(payload, "run_id")
    artifact_ids = _artifact_ids(payload)
    fatal = _fatal_observations(payload, artifact_ids)
    if fatal:
        return _invalid_assessment(run_id, fatal)

    evidence = _object(payload, "evidence")
    gate1 = _assess_attribution_v0_2(_object(evidence, "attribution"), artifact_ids)

    gate1_status = str(gate1["status"])
    break_even = _object(evidence, "break_even")
    break_even_supported, reason = _availability(break_even, "break_even")
    retrieval_p50: float | None = None
    recompute_p50: float | None = None
    if break_even_supported:
        retrieval_p50 = median(_numbers(break_even, "retrieval_ttft_s"))
        recompute_p50 = median(_numbers(break_even, "recompute_ttft_s"))
    if not break_even_supported:
        gate2 = _gate_v0_2(2, "SKIP_UNSUPPORTED", reason)
    elif gate1_status == "SKIP_UNSUPPORTED":
        gate2 = _gate_v0_2(
            2,
            "SKIP_UNSUPPORTED",
            "Gate 1 is unsupported here, so the retrieval timings cannot be "
            "tier-verified",
        )
    elif gate1_status != "PASS":
        gate2 = _gate_v0_2(
            2,
            "UNPROVEN",
            f"the retrieval timings are not tier-verified (Gate 1 {gate1_status}); "
            "the break-even cannot be established",
        )
    else:
        gate2 = _gate_v0_2(
            2,
            "PASS",
            "retrieval and recompute were measured on tier-verified requests",
        )

    admission = _object(evidence, "admission")
    admission_supported, reason = _availability(admission, "admission")
    store_cost: float | None = None
    expected_reuses: float | None = None
    reuses_to_repay: float | None = None
    attempt_counts: dict[str, int] | None = None
    realized_saving: float | None = None
    if not admission_supported:
        gate3 = _gate_v0_2(3, "SKIP_UNSUPPORTED", reason)
    elif gate2["status"] == "SKIP_UNSUPPORTED":
        gate3 = _gate_v0_2(
            3, "SKIP_UNSUPPORTED", "break-even evidence is unavailable"
        )
    else:
        store_cost = _non_negative_number(admission, "store_cost_s")
        expected_reuses = _non_negative_number(admission, "expected_reuses")
        all_attempts = admission.get("calculation_basis") == "all_attempts"
        if all_attempts:
            attempt_counts = _admission_attempt_counts(admission)
            realized_saving = _finite_number(
                admission, "realized_reuse_saving_s"
            )
        elif any(
            field in admission
            for field in (
                "calculation_basis",
                "attempt_counts",
                "realized_reuse_saving_s",
            )
        ):
            raise EvidenceError(
                "all-attempt admission evidence must use calculation_basis "
                "all_attempts and include its saving and counts"
            )
        if (
            gate2["status"] != "PASS"
            or retrieval_p50 is None
            or recompute_p50 is None
        ):
            gate3 = _gate_v0_2(
                3,
                "UNPROVEN",
                f"the saving per reuse is not established (Gate 2 {gate2['status']})",
            )
        elif attempt_counts is not None and attempt_counts["unattributed"] > 0:
            gate3 = _gate_v0_2(
                3,
                "UNPROVEN",
                f"{attempt_counts['unattributed']} of "
                f"{attempt_counts['attempted']} reuse attempts could not be "
                "classified; every attempt must remain attributable",
            )
        elif realized_saving is not None:
            reuses_to_repay = (
                store_cost / realized_saving if realized_saving > 0 else math.inf
            )
            gate3 = _gate_v0_2(
                3,
                "PASS",
                "every admission/reuse attempt was classified and retained; "
                "target-tier hits, fallbacks, and store-unattributed attempts all "
                "contribute to realized reuse saving; host-wide nvidia-fs activity "
                "shows presence, not target-device exclusivity",
            )
        else:
            avoided_per_reuse = recompute_p50 - retrieval_p50
            reuses_to_repay = (
                store_cost / avoided_per_reuse if avoided_per_reuse > 0 else math.inf
            )
            gate3 = _gate_v0_2(
                3,
                "PASS",
                "store cost was measured only for requests with observed host-wide "
                "nvidia-fs write activity (presence, not target-device exclusivity), "
                "and the saving per reuse comes from Gate 2",
            )

    capacity = _object(evidence, "capacity")
    capacity_supported, reason = _availability(capacity, "capacity")
    overflow = False
    tier_reached = False
    capacity_counts: tuple[dict[str, int], dict[str, int]] | None = None
    if capacity_supported:
        overflow = capacity.get("upper_tier_overflow_observed") is True
        tier_reached = capacity.get("target_tier_served") is True
        capacity_counts = _capacity_classification_counts(capacity, overflow, tier_reached)
        if overflow and not tier_reached:
            gate4 = _gate_v0_2(
                4,
                "FAIL",
                "the upper tier overflowed but no retrieval was served by the "
                "target tier",
            )
        else:
            gate4 = _gate_v0_2(
                4,
                "PASS",
                "every retrieval in the working-set sweep was classified",
            )
    else:
        gate4 = _gate_v0_2(4, "SKIP_UNSUPPORTED", reason)

    envelope = _object(evidence, "envelope")
    envelope_supported, reason = _availability(envelope, "envelope")
    validated_concurrency: int | None = None
    normalized_levels: list[dict[str, int]] = []
    request_tokens: int | None = None
    if envelope_supported:
        normalized_levels = _envelope_levels(envelope)
        request_tokens = _envelope_request_tokens(envelope)
        for level in normalized_levels:
            if level["requests"] > 0 and level["target_tier_served"] == level["requests"]:
                validated_concurrency = level["concurrency"]
            else:
                break
        empty = [level["concurrency"] for level in normalized_levels if level["requests"] == 0]
        if empty:
            gate5 = _gate_v0_2(
                5,
                "UNPROVEN",
                "no requests were measured at concurrency "
                + ", ".join(str(value) for value in empty),
            )
        elif validated_concurrency is None:
            gate5 = _gate_v0_2(
                5,
                "FAIL",
                "the target tier was not fully served even at the lowest "
                f"concurrency ({normalized_levels[0]['concurrency']})",
            )
        else:
            gate5 = _gate_v0_2(
                5,
                "PASS",
                "every declared concurrency level was measured with per-level "
                "tier evidence",
            )
    else:
        gate5 = _gate_v0_2(5, "SKIP_UNSUPPORTED", reason)

    gates = [gate1, gate2, gate3, gate4, gate5]
    statuses = {str(gate["status"]) for gate in gates}
    if "INVALID" in statuses:
        overall = "INVALID"
    elif "FAIL" in statuses:
        overall = "FAIL"
    elif "UNPROVEN" in statuses:
        overall = "UNPROVEN"
    elif "SKIP_UNSUPPORTED" in statuses:
        overall = "PARTIAL"
    else:
        overall = "PASS"

    # Results describe what a valid measurement found. They never change a
    # verdict, and their numbers are shown only when every gate passed.
    if gate2["status"] == "PASS" and retrieval_p50 is not None and recompute_p50 is not None:
        text, detail = _break_even_result(retrieval_p50, recompute_p50)
        gate2["result"] = f"{text} ({detail})" if overall == "PASS" else text
    if (
        gate3["status"] == "PASS"
        and expected_reuses is not None
        and reuses_to_repay is not None
    ):
        text, detail = _admission_result(
            expected_reuses,
            reuses_to_repay,
            "decision"
            if admission.get("calculation_basis") == "all_attempts"
            else "optimistic estimate",
        )
        gate3["result"] = (
            f"{text} ({detail})" if overall == "PASS" and detail else text
        ) + _admission_attempts_note(admission)
    if gate4["status"] == "PASS":
        gate4["result"] = _capacity_result(overflow, capacity_counts)
    if gate5["status"] == "PASS":
        gate5["result"] = _envelope_result(
            normalized_levels, validated_concurrency, request_tokens
        )

    assessment: dict[str, Any] = {
        "assessment_version": ASSESSMENT_VERSION,
        "run_id": run_id,
        "overall": overall,
        "performance_claims": "allowed" if overall == "PASS" else "suppressed",
        "gates": gates,
        "validated_envelope": {"max_concurrency": validated_concurrency},
    }
    if overall == "PASS":
        assessment["performance"] = {
            "retrieval_ttft_p50_s": retrieval_p50,
            "recompute_ttft_p50_s": recompute_p50,
            "reuses_to_repay": reuses_to_repay,
        }
    return assessment


def _envelope_levels(envelope: dict[str, Any]) -> list[dict[str, int]]:
    """Validate and sort the envelope levels.

    Args:
        envelope: The ``envelope`` evidence block.

    Returns:
        Levels with integer ``concurrency``, ``requests`` and
        ``target_tier_served``, in ascending concurrency.

    Raises:
        EvidenceError: If the levels are missing or malformed.
    """
    levels = envelope.get("levels")
    if not isinstance(levels, list) or not levels:
        raise EvidenceError("envelope.levels must be a non-empty list")
    normalized: list[dict[str, int]] = []
    for index, level in enumerate(levels):
        if not isinstance(level, dict):
            raise EvidenceError(f"envelope level {index} must be an object")
        values = (
            level.get("concurrency"),
            level.get("requests"),
            level.get("target_tier_served"),
        )
        if not all(
            isinstance(value, int) and not isinstance(value, bool) and value >= 0
            for value in values
        ):
            raise EvidenceError(
                f"envelope level {index} requires non-negative integer "
                "concurrency, requests, and target_tier_served"
            )
        normalized.append(
            {
                "concurrency": int(level["concurrency"]),
                "requests": int(level["requests"]),
                "target_tier_served": int(level["target_tier_served"]),
            }
        )
    return sorted(normalized, key=lambda level: level["concurrency"])


def _envelope_request_tokens(envelope: dict[str, Any]) -> int | None:
    """Return the per-request input size an envelope was measured at.

    Args:
        envelope: The ``envelope`` evidence block.

    Returns:
        ``request_cacheable_tokens`` when recorded, else None (evidence older
        than schema 0.3.1, or a run whose rows did not record it).

    Raises:
        EvidenceError: If the field is present but not a positive integer.
    """
    if "request_cacheable_tokens" not in envelope:
        return None
    value = envelope["request_cacheable_tokens"]
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise EvidenceError(
            "envelope.request_cacheable_tokens must be a positive integer"
        )
    return value


def _break_even_result(retrieval_p50: float, recompute_p50: float) -> tuple[str, str]:
    """Describe what a valid break-even measurement found.

    Args:
        retrieval_p50: Median guaranteed-hit retrieval TTFT, seconds.
        recompute_p50: Median recompute TTFT, seconds.

    Returns:
        ``(finding, numbers)``. The caller shows the numbers only when every
        gate passed.
    """
    numbers = f"retrieval {retrieval_p50:.4f} s vs recompute {recompute_p50:.4f} s"
    if retrieval_p50 < recompute_p50:
        return (
            "retrieval beats recompute on a single hit: offloading pays at this context",
            numbers,
        )
    return (
        "retrieval does not beat recompute: offloading efficiency is low at this context",
        numbers,
    )


def _admission_attempt_counts(admission: dict[str, Any]) -> dict[str, int]:
    """Validate and return Gate 3 all-attempt outcome counts."""
    counts = admission.get("attempt_counts")
    fields = (
        "attempted",
        "target_tier",
        "fallback",
        "store_unattributed",
        "unattributed",
    )
    if not isinstance(counts, dict) or not all(
        isinstance(counts.get(field), int)
        and not isinstance(counts.get(field), bool)
        and counts[field] >= (1 if field == "attempted" else 0)
        for field in fields
    ):
        raise EvidenceError(
            "admission attempt_counts must hold non-negative integers and at "
            "least one attempted reuse"
        )
    classified = sum(counts[field] for field in fields if field != "attempted")
    if classified != counts["attempted"]:
        raise EvidenceError("admission attempt_counts do not add up")
    return {field: counts[field] for field in fields}


def _admission_attempts_note(admission: dict[str, Any]) -> str:
    """Return the Gate 3 result suffix that states what the decision rests on.

    Args:
        admission: The ``admission`` evidence block.

    Returns:
        For all-attempt evidence, the outcome counts of every attempt. For legacy
        evidence, a statement that only successful samples were used, followed by
        the sample counts when the block carries them.

    Raises:
        EvidenceError: If the counts are malformed or do not add up.
    """
    if admission.get("calculation_basis") == "all_attempts":
        counts = _admission_attempt_counts(admission)
        return (
            f"; all {counts['attempted']} attempts included: "
            f"{counts['target_tier']} target-tier, {counts['fallback']} fallback, "
            f"{counts['store_unattributed']} store-unattributed"
        )
    return (
        "; legacy successful-sample basis: attempts without an observed write or "
        "read were left out, so this is not the realized decision"
        + _admission_samples_note(admission)
    )


def _admission_samples_note(admission: dict[str, Any]) -> str:
    """Return the Gate 3 result suffix that states how many stores the cost rests on.

    Sample counts are integrity facts, not performance numbers, so they are shown
    whatever the overall verdict.

    Args:
        admission: The ``admission`` evidence block.

    Returns:
        ``""`` for evidence without ``sample_counts``, otherwise a clause such as
        ``"; store cost from 3 of 10 samples, 7 dropped ..."``.

    Raises:
        EvidenceError: If the counts are malformed or do not add up.
    """
    counts = admission.get("sample_counts")
    if counts is None:
        return ""
    if not isinstance(counts, dict) or not all(
        isinstance(counts.get(field), int)
        and not isinstance(counts.get(field), bool)
        and counts[field] >= 0
        for field in ("requested", "valid", "dropped")
    ):
        raise EvidenceError("admission sample_counts must hold non-negative integers")
    if counts["valid"] + counts["dropped"] != counts["requested"]:
        raise EvidenceError("admission sample_counts do not add up")
    if counts["dropped"] == 0:
        return f"; store cost from all {counts['valid']} samples"
    note = (
        f"; store cost from {counts['valid']} of {counts['requested']} samples, "
        f"{counts['dropped']} dropped for want of an observed device write"
    )
    if counts["dropped"] > counts["valid"]:
        note += " — most stores showed no write, so read the cost as low-confidence"
    return note


def _capacity_result(
    overflow: bool, counts: tuple[dict[str, int], dict[str, int]] | None
) -> str:
    """Describe what a valid capacity sweep found, with the counts behind it.

    Args:
        overflow: Whether the upper tier overflowed in the sweep.
        counts: Overall and beyond-capacity classification counts, or ``None``
            for evidence written before they were recorded.

    Returns:
        The Gate 4 result sentence.
    """
    if not overflow:
        return (
            "the upper tier never overflowed in the swept range: the tier's "
            "necessity is not shown for this workload"
        )
    if counts is None:
        return (
            "the upper tier overflowed and the target tier served the overflow: "
            "the tier is needed beyond the upper tier's capacity"
        )
    beyond = counts[1]
    lost = beyond["fallback"] + beyond["miss"]
    served = beyond["target_tier_served"]
    total = beyond["retrievals_expected"]
    if lost == 0:
        return (
            f"the upper tier overflowed and the target tier served the overflow "
            f"({served} of {total} retrievals beyond its capacity; the rest stayed in "
            "the upper tier): the tier is needed beyond the upper tier's capacity"
        )
    return (
        f"the upper tier overflowed, but beyond its capacity the target tier served "
        f"only {served} of {total} retrievals and {lost} fell back to recompute or "
        "missed: the tier is needed there but did not serve what overflowed — "
        "before relying on it, read the producer's per-request store and "
        "retrieval records where it keeps them: they can separate a store with "
        "no observed write from one with write activity observed but retrieval "
        "not established, and observed write activity alone does not show that "
        "an entry reached the target device"
    )


def _admission_result(
    expected_reuses: float, reuses_to_repay: float, label: str
) -> tuple[str, str]:
    """Describe what a valid admission-cost measurement found.

    Args:
        expected_reuses: Reuses the workload expects per admitted entry.
        reuses_to_repay: Reuses needed to repay one admission; infinite when
            reuse saves nothing.
        label: What the finding is — ``"decision"`` for all-attempt evidence,
            ``"optimistic estimate"`` for legacy successful-sample evidence.

    Returns:
        ``(finding, numbers)``; ``numbers`` is empty when there is no finite
        repayment to state. The caller shows the numbers only when every gate
        passed.
    """
    if not math.isfinite(reuses_to_repay):
        return (
            f"{label}: NOT_ECONOMICAL — reuse saves nothing, so admission is "
            "never repaid",
            "",
        )
    numbers = f"expected reuses {expected_reuses:.2f}; reuses to repay {reuses_to_repay:.2f}"
    if expected_reuses >= reuses_to_repay:
        return (
            f"{label}: ECONOMICAL — admission is repaid within the expected reuses",
            numbers,
        )
    return (
        f"{label}: NOT_ECONOMICAL — admission needs more reuses than the "
        "workload expects",
        numbers,
    )


def _envelope_result(
    levels: list[dict[str, int]],
    validated_concurrency: int | None,
    request_tokens: int | None,
) -> str:
    """Describe the operating envelope a valid concurrency sweep found.

    The tier counters are read around a whole batch, so the sentence speaks of
    batches: a request is credited as target-tier served only when its batch
    met the tier byte check and every response in it was correct. Requests in
    a batch that did not are *not credited*; the evidence cannot say where
    each of them was served from, so the sentence does not claim they left
    the tier.

    Args:
        levels: Validated levels in ascending concurrency.
        validated_concurrency: The highest level at and below which every
            request was credited as target-tier served.
        request_tokens: Cacheable tokens per request, when the evidence
            records it. The envelope holds only for that input size.

    Returns:
        The Gate 5 result sentence.
    """
    size = f" at {request_tokens:,} tokens/request" if request_tokens else ""
    beyond = [level for level in levels if level["concurrency"] > (validated_concurrency or 0)]
    if not beyond:
        return (
            "every measured batch met its tier-byte and correctness checks "
            f"through concurrency {validated_concurrency}{size}; the envelope's "
            "upper bound was not reached"
        )
    total = sum(level["requests"] for level in beyond)
    uncredited = sum(level["requests"] - level["target_tier_served"] for level in beyond)
    return (
        "every measured batch met its tier-byte and correctness checks through "
        f"concurrency {validated_concurrency}{size}; above it, {uncredited} of "
        f"{total} requests were in batches that did not, so they are not "
        "credited to the target tier: validated envelope is concurrency "
        f"{validated_concurrency}"
    )


def _bound_configuration_id(
    value: Any,
    registry: dict[str, str],
    context: str,
) -> str:
    """Validate and return one configuration binding."""
    if not isinstance(value, str) or not value.strip():
        raise EvidenceError(f"{context} must name a configuration ID")
    if value not in registry:
        raise EvidenceError(f"{context} names unknown configuration: {value}")
    return value


def _section_binding(
    section: dict[str, Any],
    value: Any,
    registry: dict[str, str],
    context: str,
) -> str | None:
    """Validate a section binding, including unsupported sections."""
    supported, _ = _availability(section, context)
    if supported:
        return _bound_configuration_id(value, registry, context)
    if value is not None:
        raise EvidenceError(f"unsupported {context} binding must be null")
    return None


def _validate_bindings_v0_3(
    evidence: dict[str, Any],
    bindings: Any,
    registry: dict[str, str],
) -> set[str]:
    """Validate complete measurement-to-configuration bindings."""
    if not isinstance(bindings, dict):
        raise EvidenceError("bindings must be an object")
    expected_binding_keys = {
        "attribution_requests",
        "break_even",
        "admission",
        "capacity",
        "envelope_levels",
    }
    if set(bindings) != expected_binding_keys:
        raise EvidenceError(
            "bindings must contain exactly attribution_requests, break_even, "
            "admission, capacity, and envelope_levels"
        )
    used: set[str] = set()

    attribution = _object(evidence, "attribution")
    attribution_supported, _ = _availability(attribution, "attribution")
    request_bindings = bindings.get("attribution_requests")
    if not isinstance(request_bindings, list):
        raise EvidenceError("bindings.attribution_requests must be a list")
    if attribution_supported:
        requests = attribution.get("requests")
        if not isinstance(requests, list) or not requests:
            raise EvidenceError("attribution.requests must be a non-empty list")
        request_ids: list[str] = []
        for index, request in enumerate(requests):
            if not isinstance(request, dict):
                raise EvidenceError(f"attribution request {index} must be an object")
            request_ids.append(_non_empty_text(request, "id"))
        if len(set(request_ids)) != len(request_ids):
            raise EvidenceError("attribution request IDs must be unique")
        bound_requests: dict[str, str] = {}
        for index, binding in enumerate(request_bindings):
            if not isinstance(binding, dict):
                raise EvidenceError(
                    f"attribution request binding {index} must be an object"
                )
            if set(binding) != {"request_id", "configuration_id"}:
                raise EvidenceError(
                    f"attribution request binding {index} has unexpected keys"
                )
            request_id = _non_empty_text(binding, "request_id")
            if request_id in bound_requests:
                raise EvidenceError(
                    f"duplicate attribution request binding: {request_id}"
                )
            bound_requests[request_id] = _bound_configuration_id(
                binding.get("configuration_id"),
                registry,
                f"attribution request {request_id} binding",
            )
        if set(bound_requests) != set(request_ids):
            raise EvidenceError(
                "attribution request bindings must exactly cover request IDs"
            )
        used.update(bound_requests.values())
    elif request_bindings:
        raise EvidenceError(
            "unsupported attribution must have no request bindings"
        )

    for name in ("break_even", "admission", "capacity"):
        configuration_id = _section_binding(
            _object(evidence, name), bindings.get(name), registry, name
        )
        if configuration_id is not None:
            used.add(configuration_id)

    envelope = _object(evidence, "envelope")
    envelope_supported, _ = _availability(envelope, "envelope")
    level_bindings = bindings.get("envelope_levels")
    if not isinstance(level_bindings, list):
        raise EvidenceError("bindings.envelope_levels must be a list")
    if envelope_supported:
        levels = envelope.get("levels")
        if not isinstance(levels, list) or not levels:
            raise EvidenceError("envelope.levels must be a non-empty list")
        by_index: dict[int, str] = {}
        for position, binding in enumerate(level_bindings):
            if not isinstance(binding, dict):
                raise EvidenceError(
                    f"envelope level binding {position} must be an object"
                )
            if set(binding) != {"level_index", "configuration_id"}:
                raise EvidenceError(
                    f"envelope level binding {position} has unexpected keys"
                )
            index = binding.get("level_index")
            if (
                not isinstance(index, int)
                or isinstance(index, bool)
                or index < 0
                or index in by_index
            ):
                raise EvidenceError(
                    f"envelope level binding {position} has an invalid index"
                )
            by_index[index] = _bound_configuration_id(
                binding.get("configuration_id"),
                registry,
                f"envelope level {index} binding",
            )
        if set(by_index) != set(range(len(levels))):
            raise EvidenceError(
                "envelope level bindings must exactly cover level indexes"
            )
        used.update(by_index.values())
    elif level_bindings:
        raise EvidenceError("unsupported envelope must have no level bindings")
    return used


def _assess_v0_3(payload: dict[str, Any]) -> dict[str, Any]:
    """Apply v0.2 gates after enforcing a single experiment configuration."""
    run_id = _non_empty_text(payload, "run_id")
    artifact_ids = _artifact_ids(payload)
    try:
        registry = validate_registry(payload.get("configurations"))
    except ConfigurationError as exc:
        raise EvidenceError(str(exc)) from exc
    fatal = _fatal_observations(payload, artifact_ids)
    if fatal:
        bindings = payload.get("bindings")
        if bindings != {
            "attribution_requests": [],
            "break_even": None,
            "admission": None,
            "capacity": None,
            "envelope_levels": [],
        }:
            raise EvidenceError(
                "fatal v0.3 evidence requires empty measurement bindings"
            )
        assessment = _invalid_assessment(run_id, fatal)
        assessment["configurations"] = registry
        return assessment

    evidence = _object(payload, "evidence")
    used = _validate_bindings_v0_3(
        evidence, payload.get("bindings"), registry
    )
    if len(used) != 1:
        configurations = ", ".join(sorted(used)) or "none"
        assessment = _invalid_assessment(
            run_id,
            [
                {
                    "code": "configuration_mismatch",
                    "scope": "measurement",
                }
            ],
        )
        assessment["configurations"] = registry
        assessment["configuration_mismatch"] = {
            "bound_configuration_ids": sorted(used),
            "reason": (
                "measured evidence must bind exactly one configuration; "
                f"found {configurations}"
            ),
        }
        return assessment

    assessment = _assess_v0_2(payload)
    configuration_id = next(iter(used))
    assessment["configuration"] = {
        "id": configuration_id,
        "fingerprint": registry[configuration_id],
    }
    return assessment


def assess_evidence(payload: Any) -> dict[str, Any]:
    """Apply the versioned five-gate contract to normalized evidence.

    Args:
        payload: Parsed normalized evidence JSON.

    Returns:
        Assessment with gate verdicts and claim-suppression state.

    Raises:
        EvidenceError: If the schema version is unsupported or evidence is malformed.
    """
    if not isinstance(payload, dict):
        raise EvidenceError("top-level evidence must be an object")
    schema_version = payload.get("schema_version")
    if schema_version in {"0.1.0", "0.2.0"}:
        raise EvidenceError(
            f"unsupported evidence schema_version {schema_version}; re-normalize the "
            "measurement run with 'kvprobe ingest' to produce 0.3.1 "
            "(see docs/EVIDENCE.md)"
        )
    if schema_version in {"0.3.0", "0.3.1"}:
        return _assess_v0_3(payload)
    raise EvidenceError(
        "schema_version must be '0.3.0' or '0.3.1'; "
        "normalize runs with 'kvprobe ingest'"
    )


def render_text(
    assessment: dict[str, Any], readings: dict[str, Any] | None = None
) -> str:
    """Render an assessment for a terminal.

    Args:
        assessment: Result returned by :func:`assess_evidence`.
        readings: Optional compute_readings result; omitted by default.

    Returns:
        Plain-text report.
    """
    lines = [
        f"Run: {assessment['run_id']}",
        f"Overall: {str(assessment['overall']).upper()}",
        f"Performance claims: {str(assessment['performance_claims']).upper()}",
    ]
    configuration = assessment.get("configuration")
    if isinstance(configuration, dict):
        lines.append(
            f"Configuration: {configuration['id']} ({configuration['fingerprint']})"
        )
    mismatch = assessment.get("configuration_mismatch")
    if isinstance(mismatch, dict):
        lines.append(
            "Configuration mismatch: "
            + ", ".join(str(item) for item in mismatch["bound_configuration_ids"])
        )
    for gate in assessment["gates"]:
        lines.append(
            f"Gate {gate['id']} {gate['name']}: "
            f"{str(gate['status']).upper()} — {gate['reason']}"
        )
        if gate.get("result"):
            lines.append(f"  Result: {gate['result']}")
    concurrency = assessment["validated_envelope"]["max_concurrency"]
    envelope = (
        f"concurrency <= {concurrency}"
        if concurrency is not None
        else "not established"
    )
    lines.append(f"Validated envelope: {envelope}")
    if assessment["performance_claims"] == "suppressed" and readings is None:
        lines.append("Performance numbers omitted because not all gates passed.")
    if readings is not None:
        # Local
        from .readings import render_readings

        lines.extend(["", render_readings(readings, assessment["overall"] == "PASS")])
    return "\n".join(lines)


def render_markdown(
    assessment: dict[str, Any], readings: dict[str, Any] | None = None
) -> str:
    """Render an assessment as portable Markdown.

    Args:
        assessment: Result returned by :func:`assess_evidence`.
        readings: Optional compute_readings result; omitted by default.

    Returns:
        Markdown report.
    """
    lines = [
        f"# kvprobe report — `{assessment['run_id']}`",
        "",
        f"- **Overall:** {str(assessment['overall']).upper()}",
        f"- **Performance claims:** {str(assessment['performance_claims']).upper()}",
        "- **Validated envelope:** "
        + (
            f"concurrency ≤ {assessment['validated_envelope']['max_concurrency']}"
            if assessment["validated_envelope"]["max_concurrency"] is not None
            else "not established"
        ),
    ]
    configuration = assessment.get("configuration")
    if isinstance(configuration, dict):
        lines.append(
            f"- **Configuration:** `{configuration['id']}` "
            f"(`{configuration['fingerprint']}`)"
        )
    mismatch = assessment.get("configuration_mismatch")
    if isinstance(mismatch, dict):
        identifiers = ", ".join(
            f"`{item}`" for item in mismatch["bound_configuration_ids"]
        )
        lines.append(f"- **Configuration mismatch:** {identifiers}")
    lines.extend(["", "| Gate | Status | Reason | Result |", "|---|---|---|---|"])
    for gate in assessment["gates"]:
        lines.append(
            f"| {gate['id']} — {gate['name']} | "
            f"{str(gate['status']).upper()} | {gate['reason']} | "
            f"{gate.get('result') or '—'} |"
        )
    if assessment["performance_claims"] == "suppressed" and readings is None:
        lines.extend(
            [
                "",
                "> Performance numbers are omitted because not all gates passed.",
            ]
        )
    if readings is not None:
        # Local
        from .readings import render_readings

        lines.extend(
            [
                "",
                "```text",
                render_readings(readings, assessment["overall"] == "PASS"),
                "```",
            ]
        )
    return "\n".join(lines)
