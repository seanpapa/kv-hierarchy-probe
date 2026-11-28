# SPDX-License-Identifier: Apache-2.0
"""Draft evidence 0.4.0 scope/configuration binding checks; no serving assessment."""

# Standard
from typing import Any

# Local
from .configuration import ConfigurationError, validate_registry
from .tier_scope import ScopeError, validate_target_scope


def validate_scope_bindings(payload: Any) -> dict[str, str]:
    """Check draft service scopes against versioned configuration fingerprints.

    This API validates scope declarations and their bindings only. It does not
    validate the complete evidence envelope, read raw artifacts, assess a gate,
    or authorize performance claims. Full report dispatch remains a later step.

    Args:
        payload: Parsed draft 0.4.0 evidence document.

    Returns:
        Configuration ID-to-fingerprint mapping after checking scoped requests.

    Raises:
        ScopeError: Unsupported version, missing/duplicate request binding,
            unknown configuration, malformed scope or mismatch with its arm.
    """
    if not isinstance(payload, dict) or payload.get("schema_version") != "0.4.0":
        raise ScopeError("scope binding validation requires schema_version 0.4.0")
    try:
        registry = validate_registry(
            payload.get("configurations"), evidence_version="0.4.0"
        )
    except ConfigurationError as exc:
        raise ScopeError(str(exc)) from exc
    evidence = payload.get("evidence")
    section = evidence.get("attribution") if isinstance(evidence, dict) else None
    bindings = payload.get("bindings")
    refs = bindings.get("attribution_requests") if isinstance(bindings, dict) else None
    if not isinstance(section, dict) or not isinstance(refs, list):
        raise ScopeError("attribution section and request bindings are required")
    availability = section.get("availability")
    if not isinstance(availability, dict):
        raise ScopeError("attribution availability is required")
    if availability.get("status") == "unsupported":
        if refs or "requests" in section:
            raise ScopeError("unsupported attribution cannot carry request scopes")
        return registry
    if availability.get("status") != "supported":
        raise ScopeError("unsupported attribution availability status")
    requests = section.get("requests")
    if not isinstance(requests, list) or not requests:
        raise ScopeError("supported attribution requires nonempty requests")
    bound = {}
    for ref in refs:
        if not isinstance(ref, dict) or set(ref) != {"request_id", "configuration_id"}:
            raise ScopeError("malformed request binding")
        name, config_id = ref["request_id"], ref["configuration_id"]
        if not isinstance(name, str) or not name or name in bound:
            raise ScopeError("empty or duplicate request binding")
        if not isinstance(config_id, str) or config_id not in registry:
            raise ScopeError("unknown request configuration")
        bound[name] = config_id
    configurations = {
        entry["id"]: entry["parameters"] for entry in payload["configurations"]
    }
    seen = set()
    for request in requests:
        if not isinstance(request, dict):
            raise ScopeError("request must be an object")
        name = request.get("id")
        if not isinstance(name, str) or not name or name in seen or name not in bound:
            raise ScopeError("request bindings must uniquely cover request IDs")
        seen.add(name)
        record = request.get("attribution")
        if not isinstance(record, dict):
            raise ScopeError("request attribution must be an object")
        if record.get("provider") == "nfs_mountstats":
            scope = validate_target_scope(record.get("target_scope"))
            if record.get("target_device_filtered") is not False:
                raise ScopeError("NFS service scope is not a device filter")
            matches = [
                arm
                for arm in configurations[bound[name]]["arms"]
                if arm["attribution_provider"] == "nfs_mountstats"
                and arm["tier_identity"] == scope
            ]
            if len(matches) != 1:
                raise ScopeError(
                    "request scope must match exactly one configured NFS arm"
                )
        elif "target_scope" in record:
            raise ScopeError("target_scope is reserved for nfs_mountstats")
    if seen != set(bound):
        raise ScopeError("request bindings must exactly cover requests")
    return registry
