# SPDX-License-Identifier: Apache-2.0
"""Experimental reviewed serving companion; separate from trace/evidence verdicts."""

# Standard
import hashlib
import json
import re
from datetime import datetime
from itertools import zip_longest
from pathlib import Path

# Local
from .nvme_capture import assess_capture_loss, summarize_capture
from .nvme_trace import analyze_trace

REQUIRED_ROLES = {
    "capture_integrity": {"manifest", "trace", "phases", "stdout", "stderr"},
    "filter_clock_acceptance": {"acceptance"},
    "runtime_identity": {"runtime", "configuration"},
    "admission_complete": {"lifecycle", "framework_log"},
    "residency_isolation": {"residency", "runtime", "framework_log"},
    "retrieval_correctness_path": {"request", "framework_log", "counters"},
    "byte_policy": {"geometry", "byte_policy"},
    "audit_binding": {"manifest", "trace", "phases", "request", "configuration"},
}
CAPTURE_FILES = {
    "manifest.json": "manifest",
    "trace.csv": "trace",
    "phases.csv": "phases",
    "bpftrace.out": "stdout",
    "bpftrace.err": "stderr",
}
ROLES = set().union(*REQUIRED_ROLES.values())
MAX_ARTIFACT_BYTES = 256 * 1024 * 1024
MAX_TOTAL_BYTES = 512 * 1024 * 1024


class VerificationError(ValueError):
    """Raised for malformed or unsafe companion inputs; no serving verdict."""


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 1024:
        raise VerificationError(f"{name}: nonempty text <=1024 characters required")
    return value


def _natural(value: object, name: str) -> int:
    if type(value) is not int or value < 0:
        raise VerificationError(f"{name}: nonnegative integer required")
    return value


def _object(value: object, fields: set[str], name: str) -> dict:
    if not isinstance(value, dict) or set(value) != fields:
        raise VerificationError(f"{name}: expected fields {sorted(fields)}")
    return value


def _sha(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise VerificationError("SHA-256 must be 64 lowercase hex characters")
    return value


def _json_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise VerificationError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _relative(root: Path, value: object) -> Path:
    name = _text(value, "artifact path")
    if "\\" in name or any(part in ("", ".", "..") for part in name.split("/")):
        raise VerificationError("artifact path must be a canonical relative POSIX path")
    path = Path(name)
    if path.is_absolute():
        raise VerificationError("absolute artifact path is not allowed")
    current = root
    for part in path.parts:
        current = current / part
        if current.is_symlink():
            raise VerificationError("symlink artifact or parent is not allowed")
    if current.exists() and not current.is_file():
        raise VerificationError("artifact must be a regular file")
    return current


def _digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(65536), b""):
            result.update(block)
    return result.hexdigest()


def _review(path: Path) -> tuple[dict, str]:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 65536:
        raise VerificationError("review must be a regular non-symlink file <=64 KiB")
    raw = path.read_bytes()
    if len(raw) > 65536:
        raise VerificationError("review exceeds 64 KiB")
    data = json.loads(raw, object_pairs_hook=_json_object)
    _object(
        data,
        {
            "verification_contract_version",
            "ordering_policy",
            "scope",
            "reviewer",
            "artifacts",
            "input_sha256",
            "checks",
            "byte_policy",
        },
        "review",
    )
    if data["verification_contract_version"] != "0.1.0":
        raise VerificationError("unsupported verification_contract_version")
    reviewer = _object(data["reviewer"], {"id", "reviewed_at"}, "reviewer")
    _text(reviewer["id"], "reviewer.id")
    timestamp = datetime.fromisoformat(_text(reviewer["reviewed_at"], "reviewed_at"))
    if timestamp.utcoffset() is None:
        raise VerificationError("reviewed_at must have a timezone")
    return data, hashlib.sha256(raw).hexdigest()


def _delivery(root: Path, observation: dict | None) -> str:
    """Reconcile raw retained record delivery even under strict trace analysis."""
    if observation is None or any(
        not (root / name).is_file() for name in CAPTURE_FILES
    ):
        return "unresolved"
    manifest = json.loads((root / "manifest.json").read_text())
    raw = root / "bpftrace.out"
    summary = summarize_capture(raw)
    loss = assess_capture_loss(
        summary,
        root / "bpftrace.err",
        process_returncode=manifest.get("process_returncode"),
        stop_reason=manifest.get("stop_reason"),
    )
    if loss["loss_events"] != 0 or loss != manifest.get("loss_accounting"):
        return "unresolved"
    if (
        summary["malformed_records"]
        or summary["ready_ns"] != manifest["capture_start_ns"]
        or summary["end_ns"] != manifest["capture_end_ns"]
        or summary["controller_ids"] != [manifest["controller_id"]]
    ):
        return "unresolved"
    if _digest(raw) != manifest.get("raw_stdout_sha256"):
        return "violated"
    with raw.open() as raw_stream, (root / "trace.csv").open() as trace_stream:
        raw_records = (
            line.strip() for line in raw_stream if line.startswith(("S,", "C,"))
        )
        trace_records = (
            line.strip()
            for line in trace_stream
            if line.strip() and not line.startswith("#")
        )
        if any(a != b for a, b in zip_longest(raw_records, trace_records)):
            return "violated"
    return "satisfied"


def verify_serving(
    run_dir: Path, review_path: Path, *, ordering_policy: str = "strict"
) -> dict:
    """Reanalyze a capture and reconcile explicitly reviewed first-path evidence.

    Args:
        run_dir: Quiescent, operator-controlled artifact root. Files are read only.
        review_path: Companion JSON contract 0.1.0; declarations are human review,
            not independent proof of runtime, filter/clock or residency facts.
        ordering_policy: Strict by default; causal requires explicit caller opt-in
            and an identical review policy. Saved observations are never trusted.

    Returns:
        A separate versioned reviewed verdict and automatic checks. Missing evidence
        is incomplete, observed criterion violations are failed; all checks must
        be satisfied for verified. No evidence-v0.3 or confidence value is changed.

    Raises:
        VerificationError: For malformed/unsafe contracts or scope mismatch.
        TraceError: For malformed analyzer inputs or unsupported trace contracts.
        OSError: For unreadable files. Missing referenced artifacts are incomplete.
        ValueError: For invalid JSON, Unicode or date encoding.

    Notes:
        Hashes bind retained bytes, not their truth. This is not a request-ID
        hardware trace or a verifier for hostile, concurrently mutating directories.
        No files, devices, caches or serving processes are modified.
    """
    root = run_dir.resolve(strict=True)
    if not root.is_dir():
        raise VerificationError("run_dir must be a directory")
    review, review_hash = _review(review_path)
    if (
        ordering_policy not in ("strict", "causal")
        or review["ordering_policy"] != ordering_policy
    ):
        raise VerificationError("review policy must match explicit caller policy")
    scope = _object(
        review["scope"],
        {
            "namespace",
            "controller_id",
            "request_id",
            "phase",
            "backend",
            "endpoint_id",
            "cache_domain_id",
            "concurrency",
        },
        "scope",
    )
    for field in ("namespace", "request_id", "phase", "endpoint_id", "cache_domain_id"):
        _text(scope[field], f"scope.{field}")
    _natural(scope["controller_id"], "scope.controller_id")
    if (
        scope["backend"] != "native_cufile_gds"
        or type(scope["concurrency"]) is not int
        or scope["concurrency"] != 1
    ):
        raise VerificationError(
            "initial companion supports serial native_cufile_gds only"
        )
    if not isinstance(review["input_sha256"], dict):
        raise VerificationError("input_sha256 must be an object")
    for name, digest in review["input_sha256"].items():
        _relative(root, name)
        _sha(digest)
    entries = review["artifacts"]
    if not isinstance(entries, list) or len(entries) > 256:
        raise VerificationError("artifacts must be a list of at most 256 entries")
    artifacts = {}
    artifact_issues = {}
    total = 0
    for entry in entries:
        _object(entry, {"path", "sha256", "roles"}, "artifact")
        path = _relative(root, entry["path"])
        name = entry["path"]
        if name in artifacts:
            raise VerificationError("duplicate artifact path")
        _sha(entry["sha256"])
        roles = entry["roles"]
        if (
            not isinstance(roles, list)
            or not roles
            or any(not isinstance(role, str) for role in roles)
        ):
            raise VerificationError("artifact roles must be a nonempty string list")
        if len(set(roles)) != len(roles) or not set(roles) <= ROLES:
            raise VerificationError("unknown or duplicate artifact role")
        artifacts[name] = entry
        if not path.exists():
            artifact_issues[name] = "missing"
            continue
        size = path.stat().st_size
        total += size
        if size > MAX_ARTIFACT_BYTES or total > MAX_TOTAL_BYTES:
            raise VerificationError(
                "artifact size budget exceeded (256 MiB/file, 512 MiB total)"
            )
        if _digest(path) != entry["sha256"]:
            artifact_issues[name] = "hash_mismatch"
    checks = review["checks"]
    if not isinstance(checks, list) or len(checks) > len(REQUIRED_ROLES):
        raise VerificationError("checks must be a list of at most eight entries")
    reviewed = {}
    for check in checks:
        _object(check, {"id", "status", "reason", "artifacts"}, "check")
        check_id = _text(check["id"], "check.id")
        if check_id not in REQUIRED_ROLES or check_id in reviewed:
            raise VerificationError("unknown or duplicate check ID")
        if check["status"] not in ("satisfied", "unresolved", "violated"):
            raise VerificationError("unsupported check status")
        _text(check["reason"], "check.reason")
        refs = check["artifacts"]
        if not isinstance(refs, list) or any(not isinstance(ref, str) for ref in refs):
            raise VerificationError("check artifacts must be a string list")
        if len(refs) != len(set(refs)) or not set(refs) <= set(artifacts):
            raise VerificationError("duplicate or undeclared artifact reference")
        reviewed[check_id] = check
    # Deny symlink inputs even if the review omitted those capture references.
    for name in CAPTURE_FILES:
        path = _relative(root, name)
        if path.exists() and name not in artifacts:
            size = path.stat().st_size
            total += size
            if size > MAX_ARTIFACT_BYTES or total > MAX_TOTAL_BYTES:
                raise VerificationError("unreviewed capture input exceeds size budget")
    observation = None
    missing_inputs = [
        name
        for name in ("manifest.json", "phases.csv", "trace.csv")
        if not (root / name).exists()
    ]
    if not missing_inputs:
        observation = analyze_trace(root, ordering_policy=ordering_policy)
        if (
            scope["namespace"] != observation["scope"]["namespace"]
            or scope["controller_id"] != observation["scope"]["controller_id"]
        ):
            raise VerificationError("review namespace/controller scope mismatch")
        selected = [
            phase
            for phase in observation["phases"]
            if phase["request_id"] == scope["request_id"]
            and phase["phase"] == scope["phase"]
        ]
        if len(selected) != 1:
            raise VerificationError(
                "review must select exactly one observed request/phase"
            )
    else:
        selected = []
    automatic = {}
    automatic["record_delivery_integrity"] = _delivery(root, observation)
    automatic["capture_quality"] = (
        "satisfied"
        if observation and observation["quality"]["status"] == "usable"
        else "unresolved"
    )
    automatic["input_binding"] = (
        "satisfied"
        if observation and review["input_sha256"] == observation["input_sha256"]
        else "unresolved"
    )
    if observation and any(
        name in observation["input_sha256"]
        and digest != observation["input_sha256"][name]
        for name, digest in review["input_sha256"].items()
    ):
        automatic["input_binding"] = "violated"
    policy = review["byte_policy"]
    if policy is None:
        automatic["byte_policy"] = "unresolved"
    else:
        _object(
            policy,
            {
                "expected_read_bytes",
                "read_min_bytes",
                "read_max_bytes",
                "write_max_bytes",
                "declared_before_ns",
                "artifact",
            },
            "byte_policy",
        )
        for field in (
            "expected_read_bytes",
            "read_min_bytes",
            "read_max_bytes",
            "write_max_bytes",
            "declared_before_ns",
        ):
            _natural(policy[field], field)
        if (
            not 0
            < policy["read_min_bytes"]
            <= policy["expected_read_bytes"]
            <= policy["read_max_bytes"]
        ):
            raise VerificationError(
                "byte policy must bracket a positive expected payload"
            )
        _text(policy["artifact"], "byte_policy.artifact")
        if (
            policy["artifact"] not in artifacts
            or "byte_policy" not in artifacts[policy["artifact"]]["roles"]
        ):
            raise VerificationError("byte policy requires a declared policy artifact")
        automatic["byte_policy"] = "unresolved"
        if observation and selected and policy["artifact"] not in artifact_issues:
            phase = selected[0]
            automatic["byte_policy"] = "satisfied"
            declaration_path = _relative(root, policy["artifact"])
            if declaration_path.stat().st_size > 65536:
                raise VerificationError("byte policy declaration exceeds 64 KiB")
            declaration = json.loads(
                declaration_path.read_text(), object_pairs_hook=_json_object
            )
            if declaration != {k: v for k, v in policy.items() if k != "artifact"}:
                automatic["byte_policy"] = "violated"
            elif (
                policy["declared_before_ns"]
                >= min(p["start_ns"] for p in observation["phases"])
                or phase["expected_read_bytes"] != policy["expected_read_bytes"]
            ):
                automatic["byte_policy"] = "violated"
            elif automatic["capture_quality"] == "satisfied":
                if (
                    not policy["read_min_bytes"]
                    <= phase["successful_completed_read_bytes"]
                    <= policy["read_max_bytes"]
                    or phase["successful_completed_write_bytes"]
                    > policy["write_max_bytes"]
                ):
                    automatic["byte_policy"] = "violated"
            else:
                automatic["byte_policy"] = "unresolved"
    results = {}
    for check_id, required_roles in REQUIRED_ROLES.items():
        check = reviewed.get(check_id)
        reasons = []
        status = check["status"] if check else "unresolved"
        refs = check["artifacts"] if check else []
        if check is None:
            reasons.append("review_missing")
        roles = set().union(*(set(artifacts[ref]["roles"]) for ref in refs))
        if not required_roles <= roles:
            reasons.append("required_artifact_roles_missing")
        if any(artifact_issues.get(ref) == "missing" for ref in refs):
            reasons.append("artifact_missing")
        if any(artifact_issues.get(ref) == "hash_mismatch" for ref in refs):
            status = "violated"
            reasons.append("artifact_hash_mismatch")
        if reasons and status != "violated":
            status = "unresolved"
        if check_id == "capture_integrity":
            for name, role in CAPTURE_FILES.items():
                if name not in refs or role not in artifacts.get(name, {}).get(
                    "roles", []
                ):
                    reasons.append("capture_artifact_binding_missing")
                    if status != "violated":
                        status = "unresolved"
        if check_id == "byte_policy" and policy and policy["artifact"] not in refs:
            reasons.append("policy_review_reference_missing")
            if status != "violated":
                status = "unresolved"
        results[check_id] = {
            "status": status,
            "mode": "operator_review_with_artifact_binding",
            "reason": check["reason"] if check else None,
            "automatic_reasons": sorted(set(reasons)),
            "artifacts": refs,
        }
    # Rehash all present artifacts after analysis to catch ordinary concurrent edits.
    for name, entry in artifacts.items():
        if name not in artifact_issues:
            path = _relative(root, name)
            if not path.exists() or _digest(path) != entry["sha256"]:
                artifact_issues[name] = "changed_during_verification"
    automatic["artifact_integrity"] = (
        "violated"
        if any(issue != "missing" for issue in artifact_issues.values())
        else "unresolved"
        if artifact_issues
        else "satisfied"
    )
    dependencies = {
        "capture_integrity": ("capture_quality", "record_delivery_integrity"),
        "audit_binding": ("input_binding", "artifact_integrity"),
        "byte_policy": ("byte_policy",),
    }
    for check_id, names in dependencies.items():
        result = results[check_id]
        for name in names:
            status = automatic[name]
            if status != "satisfied":
                result["automatic_reasons"].append(f"{name}_{status}")
                if status == "violated" or result["status"] != "violated":
                    result["status"] = status
        result["mode"] = "automatic_and_operator_review"
    statuses = list(automatic.values()) + [
        check["status"] for check in results.values()
    ]
    verdict = (
        "failed"
        if "violated" in statuses
        else "incomplete"
        if "unresolved" in statuses
        else "verified"
    )
    return {
        "serving_verification_version": "0.1.0-experimental",
        "status": verdict,
        "scope": scope,
        "reviewer": review["reviewer"],
        "review_sha256": review_hash,
        "verifier_sha256": _digest(Path(__file__)),
        "analyzer_sha256": _digest(Path(__file__).with_name("nvme_trace.py")),
        "ordering_policy": ordering_policy,
        "automatic_checks": automatic,
        "checks": results,
        "artifact_issues": artifact_issues,
        "missing_capture_inputs": missing_inputs,
        "observation_quality": observation["quality"] if observation else None,
        "input_sha256": observation["input_sha256"] if observation else {},
        "limitations": [
            "Reviewed component-tier measurement only; "
            "not request-ID hardware attribution.",
            "Operator declarations and artifact roles are reviewed assertions, "
            "not independent facts.",
            "Byte-policy predeclaration time and geometry are reviewed assertions.",
            "Quiescent operator-controlled files required; "
            "adversarial mutation is out of scope.",
            "Existing trace observations, evidence v0.3 and confidence are unchanged.",
        ],
    }


def render_verification(result: dict) -> str:
    """Render the separate reviewed result and unresolved checks in English.

    Args:
        result: Output of verify_serving, not an evidence-v0.3 report.

    Returns:
        A summary disclosing review scope, unresolved/violated checks and limits.
        Reviewed strings are escaped to keep terminal control text visible.
    """
    scope = result["scope"]
    lines = [
        "Gate 1 — reviewed serving companion (experimental)",
        f"Reviewed status: {result['status']}",
        f"Namespace: {scope['namespace']}; request={scope['request_id']!r}; "
        f"phase={scope['phase']!r}",
        f"Ordering policy: {result['ordering_policy']}",
    ]
    for name, status in result["automatic_checks"].items():
        lines.append(f"  Automatic {name}: {status}")
    for name, check in result["checks"].items():
        lines.append(f"  {name}: {check['status']} ({check['mode']})")
        if check["status"] != "satisfied":
            lines.append(f"    Review reason: {check['reason']!r}")
            for reason in check["automatic_reasons"]:
                lines.append(f"    Review: {reason}")
    lines.extend(
        [
            "Operator-reviewed, scoped component-tier result; "
            "not request-ID hardware attribution.",
            "Synthetic verified fixtures are tool tests, not measured serving PASSes.",
            "Original trace observations, evidence v0.3 and confidence are unchanged.",
        ]
    )
    return "\n".join(lines)
