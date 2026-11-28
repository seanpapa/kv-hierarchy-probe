# SPDX-License-Identifier: Apache-2.0
"""Versioned tier scope identities, independent of measurements and verdicts."""

# Standard
import re
from typing import Any

_OPAQUE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")


class ScopeError(ValueError):
    """Raised for malformed or version-incompatible scope declarations."""


def validate_target_scope(value: Any) -> dict[str, str]:
    """Validate one opaque NFS-service identity without claiming it was observed.

    Args:
        value: Exact mapping of kind, id, service_id, export_id, client_id,
            transport and io_mode. IDs are operator-assigned opaque labels.

    Returns:
        The validated mapping, unchanged. No network or proc data is consulted.

    Raises:
        ScopeError: Missing/extra fields, unsupported transport/mode or unsafe IDs.
    """
    if not isinstance(value, dict) or set(value) != {
        "kind",
        "id",
        "service_id",
        "export_id",
        "client_id",
        "transport",
        "io_mode",
    }:
        raise ScopeError("target scope requires the exact NFS-service identity fields")
    if value["kind"] != "nfs_service" or value["transport"] != "rdma":
        raise ScopeError("only nfs_service over rdma is defined")
    if not isinstance(value["io_mode"], str) or value["io_mode"] not in {
        "direct",
        "buffered",
    }:
        raise ScopeError("NFS io_mode must be direct or buffered")
    for name in ("id", "service_id", "export_id", "client_id"):
        if not isinstance(value[name], str) or not _OPAQUE_ID.fullmatch(value[name]):
            raise ScopeError(
                f"{name} must be an opaque label of 1-128 ASCII characters"
            )
    return value


def reject_legacy_scope(payload: dict[str, Any]) -> None:
    """Refuse new service-scope declarations inside a legacy evidence document.

    Args:
        payload: Evidence declaring a legacy 0.3.x version. The caller dispatches
            versions before using this guard.

    Returns:
        None when no new NFS scope declaration occurs at a reserved location.

    Raises:
        ScopeError: New NFS provider/identity/scope under a legacy version,
            including records that would otherwise short-circuit as fatal.
    """
    evidence = payload.get("evidence")
    attribution = evidence.get("attribution") if isinstance(evidence, dict) else None
    if isinstance(attribution, dict):
        records = [attribution]
        for name in ("requests", "corroborating_observations"):
            values = attribution.get(name)
            if isinstance(values, list):
                for value in values:
                    if isinstance(value, dict):
                        if "nfs_measurement" in value:
                            raise ScopeError("NFS measurement requires evidence 0.4.0")
                        records.append(value)
                        if isinstance(value.get("attribution"), dict):
                            records.append(value["attribution"])
        for record in records:
            if "target_scope" in record or record.get("provider") == "nfs_mountstats":
                raise ScopeError(
                    "NFS service scope requires evidence 0.4.0; "
                    "legacy scope mixing is refused"
                )
    entries = payload.get("configurations")
    if isinstance(entries, list):
        for entry in entries:
            params = entry.get("parameters") if isinstance(entry, dict) else None
            arms = params.get("arms") if isinstance(params, dict) else None
            if isinstance(arms, list):
                for arm in arms:
                    if isinstance(arm, dict) and (
                        "tier_identity" in arm
                        or arm.get("attribution_provider") == "nfs_mountstats"
                    ):
                        raise ScopeError("NFS configuration requires evidence 0.4.0")
