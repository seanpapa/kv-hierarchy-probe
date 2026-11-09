# SPDX-License-Identifier: Apache-2.0
"""Optional efficiency readings; gate verdicts remain the claim authority."""

# Standard
from statistics import median
from typing import Any

# Local
from .report import assess_evidence


#: Readings that describe whether the measurement holds together at all. They
#: stay visible when a gate fails, because a failed run is still evidence and
#: these are the observations that explain the failure.
_INTEGRITY = ("read_amplification", "tier_served_ratio")

#: Readings that quantify how well the tier performed. A failed gate means no
#: performance number may be read off this run — the repository's core rule —
#: so these are withheld rather than printed beside a caveat. A label one line
#: above a number does not survive a screenshot.
_PERFORMANCE = (
    "effective_read_bandwidth",
    "saving_per_hit",
    "speedup",
    "repay_reuses",
    "margin",
)


def _metric(value: Any, unit: str, n: int, grade: str) -> dict[str, Any]:
    return {
        "value": value,
        "unit": unit,
        "n": n,
        "status": "not_measured" if value is None else "ok",
        "grade": grade,
    }


def _withheld(metric: dict[str, Any], verdict: str) -> dict[str, Any]:
    """Return a measured performance metric with its value withheld.

    Args:
        metric: A metric whose status is ``ok``.
        verdict: The overall gate verdict that withholds it.

    Returns:
        The metric without its value, marked ``withheld`` and carrying the
        sample count. ``withheld`` is kept distinct from ``not_measured``: the
        quantity was measured, and saying otherwise would misreport the run.
    """
    return {
        **metric,
        "value": None,
        "status": "withheld",
        "withheld_reason": f"gates did not pass (overall {verdict})",
    }


def compute_readings(payload: dict[str, Any]) -> dict[str, Any]:
    """Calculate efficiency from supported, consistently bound evidence.

    Args:
        payload: Normalized evidence version 0.2.0, 0.3.0 or 0.3.1.

    Returns:
        Efficiency metrics with unrounded values, units, sample counts and
        attribution grade, plus the gate verdict that governs them. Invalid
        runs return unmeasured metrics. No input is modified. Bandwidth
        requires one common positive byte size and full, correct target-tier
        service; TTFT samples are not paired to individual byte observations
        by the evidence schema.

        When the gates did not pass, performance metrics are returned with
        their values withheld. Integrity metrics stay, because they describe
        the measurement rather than the tier's performance.

    Raises:
        EvidenceError: If the evidence contract is malformed or unsupported.
    """
    assessment = assess_evidence(payload)
    claims_allowed = assessment["performance_claims"] == "allowed"
    grade = "UNVERIFIED"
    units = {
        "read_amplification": "×",
        "effective_read_bandwidth": "GiB/s",
        "tier_served_ratio": "ratio",
        "saving_per_hit": "s",
        "speedup": "×",
        "repay_reuses": "reuses",
        "margin": "×",
    }
    metrics = {key: _metric(None, unit, 0, grade) for key, unit in units.items()}
    result = {
        "efficiency": metrics,
        "grade": grade,
        "off_tier": False,
        "verdict": assessment["overall"],
        "claims_allowed": claims_allowed,
    }
    if assessment["overall"] == "INVALID":
        return result
    evidence = payload["evidence"]

    def supported(name: str) -> bool:
        return evidence[name]["availability"]["status"] == "supported"

    requests = evidence["attribution"]["requests"] if supported("attribution") else []
    grades = []
    for request in requests:
        attribution = request["attribution"]
        if (
            attribution["granularity"] == "per_request"
            and attribution["confidence"] == "direct"
            and attribution["target_device_filtered"]
        ):
            grades.append("ATTRIBUTED")
        elif request["observed_bytes"] > 0:
            grades.append("CORROBORATED")
        else:
            grades.append("UNVERIFIED")
    order = ["UNVERIFIED", "CORROBORATED", "ATTRIBUTED"]
    grade = min(grades, key=order.index) if grades else "UNVERIFIED"
    result["grade"] = grade
    result["off_tier"] = any(
        r["source"] != "target_tier"
        or (r["expected_bytes"] > 0 and r["observed_bytes"] < r["expected_bytes"])
        for r in requests
    )
    for metric in metrics.values():
        metric["grade"] = grade

    ratios = [
        r["observed_bytes"] / r["expected_bytes"]
        for r in requests
        if r["expected_bytes"] > 0
    ]
    if ratios:
        metrics["read_amplification"] = _metric(
            {"min": min(ratios), "median": median(ratios), "max": max(ratios)},
            "×",
            len(ratios),
            grade,
        )
    if supported("break_even"):
        section = evidence["break_even"]
        retrieval = median(section["retrieval_ttft_s"])
        recompute = median(section["recompute_ttft_s"])
        n = len(section["retrieval_ttft_s"])
        saving = recompute - retrieval
        metrics["saving_per_hit"] = _metric(saving, "s", n, grade)
        metrics["speedup"] = _metric(
            recompute / retrieval if retrieval > 0 else None, "×", n, grade
        )
        sizes = {r["expected_bytes"] for r in requests}
        if (
            len(sizes) == 1
            and min(sizes) > 0
            and retrieval > 0
            and not result["off_tier"]
            and all(r["correct"] for r in requests)
        ):
            metrics["effective_read_bandwidth"] = _metric(
                next(iter(sizes)) / retrieval / (2**30), "GiB/s", n, grade
            )
        if supported("admission"):
            admission = evidence["admission"]
            repayment_saving = admission.get("realized_reuse_saving_s", saving)
            repay = (
                admission["store_cost_s"] / repayment_saving
                if repayment_saving > 0
                else "never"
            )
            repayment_n = admission.get("attempt_counts", {}).get("attempted", n)
            margin = (
                admission["expected_reuses"] / repay
                if isinstance(repay, (int, float)) and repay > 0
                else None
            )
            metrics["repay_reuses"] = _metric(
                repay, "reuses", repayment_n, grade
            )
            metrics["repay_reuses"]["expected_reuses"] = admission["expected_reuses"]
            metrics["margin"] = _metric(margin, "×", n, grade)
    if supported("envelope"):
        levels = [
            {
                **level,
                "ratio": level["target_tier_served"] / level["requests"]
                if level["requests"] > 0
                else None,
            }
            for level in evidence["envelope"]["levels"]
        ]
        metrics["tier_served_ratio"] = _metric(levels, "ratio", len(levels), grade)
    # The claim boundary applies to the readings too, and in the document the
    # report emits — not only to its rendering. A suppressed run must not carry
    # a speedup in its JSON for a downstream reader to pick up.
    if not claims_allowed:
        for name in _PERFORMANCE:
            if metrics[name]["status"] == "ok":
                metrics[name] = _withheld(metrics[name], assessment["overall"])
    return result


def render_readings(readings: dict[str, Any], claims_allowed: bool) -> str:
    """Render optional readings for text or a Markdown fenced block.

    Args:
        readings: Result of compute_readings.
        claims_allowed: Whether all five gates passed.

    Returns:
        Text with sample counts, evidence grade and bandwidth caveat. When the
        gates did not pass, the heading leads with the verdict and the
        performance readings read as withheld rather than as unmeasured.
    """
    if claims_allowed:
        lines = [f"READINGS (efficiency) — grade: {readings['grade']}"]
    else:
        lines = [
            f"READINGS (integrity only) — {readings['verdict']}: "
            f"performance readings withheld · evidence grade: {readings['grade']}"
        ]
        lines.append(
            "  gates did not pass, so no performance number may be read off this run"
        )
    if readings["off_tier"]:
        lines.append("  OFF-TIER: evidence does not establish full target-tier service")
    if readings["grade"] == "CORROBORATED":
        shown = [
            name
            for name in ("read_amplification", "effective_read_bandwidth")
            if readings["efficiency"][name]["status"] == "ok"
        ]
        if shown:
            affected = "/".join(name.split("_")[-1] for name in shown)
            lines.append(
                f"  Byte counters may include other traffic; {affected} may be inflated."
            )
    for name, metric in readings["efficiency"].items():
        label = name.replace("_", " ")
        value = metric["value"]
        if metric["status"] == "withheld":
            display = "withheld — gates did not pass"
        elif metric["status"] == "not_measured":
            display = "not measured by this run"
        elif name == "read_amplification":
            display = f"{value['median']:.2f}× [{value['min']:.2f}–{value['max']:.2f}]"
        elif name == "tier_served_ratio":
            display = " · ".join(
                f"c={v['concurrency']} {v['target_tier_served']}/{v['requests']}"
                if v["ratio"] is not None
                else f"c={v['concurrency']} not measured by this run"
                for v in value
            )
        elif isinstance(value, str):
            display = value
        else:
            display = f"{value:.2f} {metric['unit']}"
        if name == "repay_reuses" and metric["status"] == "ok":
            display += f" · expected {metric['expected_reuses']:.2f}"
        if name == "effective_read_bandwidth" and metric["status"] == "ok":
            display += " (TTFT-bounded lower bound; not measured device bandwidth)"
        lines.append(f"  {label}: {display}  n={metric['n']}")
    return "\n".join(lines)
