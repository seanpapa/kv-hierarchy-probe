# SPDX-License-Identifier: Apache-2.0
"""Evidence 0.4.0 NFS-service Gate 1 policy; independent of stack teardown."""

# Standard
from typing import Any

ROLES = ("client", "source", "file_layout", "namespace", "counters", "correctness")
ENUMS = {
    "capture_status": {"complete", "missing", "invalid"},
    "workload": {"serving_retrieval", "isolated_file_io"},
    "identity_binding": {"matched", "missing", "changed", "wrong_target"},
    "source_binding": {"matched", "missing", "unrecognized"},
    "payload_mapping": {"identity", "unknown", "unstable"},
    "namespace_binding": {"bounded", "unknown", "shared", "mismatch"},
}
COUNTS = (
    "counter_before",
    "counter_after",
    "rpc_before",
    "rpc_after",
    "retransmissions",
    "timeouts",
    "errors",
    "expected_payload_bytes",
)
FLAGS = (
    "client_binding",
    "dedicated_mount",
    "serialized",
    "quiet_activity",
    "gap_activity",
    "io_mode_match",
)


class NfsPolicyError(ValueError):
    """Raised for malformed normalized NFS evidence, not an adverse measurement."""


def validate_measurement(value: Any) -> dict[str, Any]:
    """Validate all fields of one normalized NFS observation.

    Args:
        value: NFS measurement object carried by an evidence 0.4.0 request.

    Returns:
        The unmodified object. Null counts/flags/intervals mean unavailable data.

    Raises:
        NfsPolicyError: Unknown/missing fields, types, enums or malformed references.
    """
    keys = (
        set(ENUMS)
        | set(COUNTS)
        | set(FLAGS)
        | {
            "configuration_fingerprint",
            "clock",
            "interval_ns",
            "counter",
            "artifact_roles",
            "capture_reason",
        }
    )
    if not isinstance(value, dict) or set(value) != keys:
        raise NfsPolicyError("nfs_measurement requires its exact versioned fields")
    for name, choices in ENUMS.items():
        if not isinstance(value[name], str) or value[name] not in choices:
            raise NfsPolicyError(f"invalid NFS {name}")
    for name in COUNTS:
        item = value[name]
        if item is not None and (type(item) is not int or item < 0):
            raise NfsPolicyError(f"NFS {name} must be a nonnegative integer or null")
    for name in FLAGS:
        if value[name] is not None and type(value[name]) is not bool:
            raise NfsPolicyError(f"NFS {name} must be a boolean or null")
    for name in ("configuration_fingerprint", "clock", "counter", "capture_reason"):
        if not isinstance(value[name], str) or not value[name].strip():
            raise NfsPolicyError(f"NFS {name} must be nonempty text")
    fingerprint = value["configuration_fingerprint"]
    if len(fingerprint) != 64 or any(c not in "0123456789abcdef" for c in fingerprint):
        raise NfsPolicyError("malformed NFS configuration fingerprint")
    interval = value["interval_ns"]
    if interval is not None and (
        not isinstance(interval, list)
        or len(interval) != 4
        or any(type(x) is not int or x < 0 for x in interval)
    ):
        raise NfsPolicyError("NFS interval_ns must be four integers or null")
    roles = value["artifact_roles"]
    if not isinstance(roles, dict) or set(roles) != set(ROLES):
        raise NfsPolicyError("NFS artifact_roles must declare every evidence role")
    for refs in roles.values():
        if (
            not isinstance(refs, list)
            or any(not isinstance(x, str) or not x for x in refs)
            or len(set(refs)) != len(refs)
        ):
            raise NfsPolicyError("malformed NFS role artifact references")
    return value


def judge_request(
    request: dict[str, Any], artifacts: dict[str, str], fingerprint: str
) -> dict[str, str]:
    """Judge typed observations, preserving invalid/missing/contradicted evidence.

    Args:
        request: One normalized NFS request with bytes, correctness and attribution.
        artifacts: Declared artifact ID to kind mapping, validated by the report.
        fingerprint: Independently checked configuration binding for this request.

    Returns:
        Gate 1 status, stable reason code and human-readable reason. No process
        exit or shutdown field participates. Unknown scope is checked before bytes.

    Raises:
        NfsPolicyError: Malformed request, bad binding or unknown artifact references.
    """
    expected_keys = {
        "id",
        "correct",
        "source",
        "expected_bytes",
        "observed_bytes",
        "attribution",
        "nfs_measurement",
    }
    if not isinstance(request, dict) or set(request) != expected_keys:
        raise NfsPolicyError("NFS request requires its exact versioned fields")
    if not isinstance(request["id"], str) or not request["id"].strip():
        raise NfsPolicyError("NFS request id must be nonempty text")
    if request["correct"] is not None and type(request["correct"]) is not bool:
        raise NfsPolicyError("NFS correctness must be boolean or null")
    if request["source"] != "target_tier":
        raise NfsPolicyError(
            "NFS checkpoint records intended target-tier retrievals only"
        )
    for name in ("expected_bytes", "observed_bytes"):
        value = request[name]
        if value is not None and (type(value) is not int or value < 0):
            raise NfsPolicyError(f"NFS {name} must be a nonnegative integer or null")
    attribution = request["attribution"]
    if not isinstance(attribution, dict) or set(attribution) != {
        "provider",
        "granularity",
        "confidence",
        "target_device_filtered",
        "target_scope",
        "artifact_refs",
    }:
        raise NfsPolicyError("NFS attribution requires its exact fields")
    if (
        attribution["provider"] != "nfs_mountstats"
        or attribution["target_device_filtered"] is not False
    ):
        raise NfsPolicyError("NFS service scope cannot claim a block-device filter")
    if not isinstance(attribution["granularity"], str) or attribution[
        "granularity"
    ] not in {"per_request", "per_run", "per_batch"}:
        raise NfsPolicyError("unknown NFS observation granularity")
    if not isinstance(attribution["confidence"], str) or attribution[
        "confidence"
    ] not in {"direct", "corroborating", "heuristic"}:
        raise NfsPolicyError("unknown NFS observation confidence")
    refs = attribution["artifact_refs"]
    if not isinstance(refs, list) or any(
        not isinstance(x, str) or x not in artifacts for x in refs
    ):
        raise NfsPolicyError("NFS attribution references unknown artifacts")
    if len(set(refs)) != len(refs):
        raise NfsPolicyError("duplicate NFS attribution artifact reference")
    measurement = validate_measurement(request["nfs_measurement"])
    if measurement["configuration_fingerprint"] != fingerprint:
        raise NfsPolicyError("NFS observation/configuration fingerprint mismatch")
    roles = measurement["artifact_roles"]
    for role, references in roles.items():
        for ref in references:
            if ref not in refs or artifacts.get(ref) != "nfs_" + role:
                raise NfsPolicyError(
                    "NFS role does not match its declared artifact kind"
                )

    def result(status: str, code: str, reason: str) -> dict[str, str]:
        return {"status": status, "reason_code": code, "reason": reason}

    def invalid(code: str) -> dict[str, str]:
        return result("INVALID", code, "invalid NFS measurement: " + code)

    def missing(code: str) -> dict[str, str]:
        return result("UNPROVEN", code, "NFS evidence is insufficient: " + code)

    if measurement["capture_status"] == "invalid":
        return invalid(measurement["capture_reason"])
    if measurement["capture_status"] == "missing":
        return missing(measurement["capture_reason"])
    if measurement["identity_binding"] in {"changed", "wrong_target"}:
        return invalid("target_scope_changed_or_wrong")
    if measurement["namespace_binding"] == "mismatch":
        return invalid("namespace_mismatch")
    if measurement["clock"] != "CLOCK_MONOTONIC":
        return invalid("unsupported_clock")
    interval = measurement["interval_ns"]
    if interval is not None and not (
        interval[0] <= interval[1] < interval[2] <= interval[3]
    ):
        return invalid("request_interval_not_ordered")
    if any(measurement[k] is None for k in COUNTS):
        return missing("counter_or_payload_expectation_missing")
    before, after = measurement["counter_before"], measurement["counter_after"]
    rb, ra = measurement["rpc_before"], measurement["rpc_after"]
    if after < before or ra < rb:
        return invalid("counter_reset")
    moved = after - before
    if request["observed_bytes"] != moved:
        raise NfsPolicyError("NFS observed_bytes contradicts its counter originals")
    if measurement["timeouts"] or measurement["errors"]:
        return invalid("rpc_error_or_timeout")
    if moved and ra == rb:
        return invalid("payload_rpc_disagreement")
    if measurement["retransmissions"]:
        return missing("retransmission_unqualified")
    if measurement["counter"] != "NFSIOS_SERVERREADBYTES":
        return missing("payload_counter_not_established")
    if measurement["workload"] != "serving_retrieval":
        return missing("not_a_serving_request")
    if attribution["granularity"] != "per_request" or interval is None:
        return missing("request_interval_missing")
    if attribution["confidence"] != "corroborating":
        return missing("mount_counter_requires_scoped_corroboration")
    if (
        measurement["identity_binding"] != "matched"
        or measurement["client_binding"] is not True
    ):
        return missing("client_or_target_binding_missing")
    if measurement["source_binding"] != "matched":
        return missing("source_contract_unqualified")
    if (
        measurement["namespace_binding"] != "bounded"
        or measurement["dedicated_mount"] is not True
        or measurement["serialized"] is not True
        or measurement["quiet_activity"] is not False
        or measurement["gap_activity"] is not False
        or measurement["io_mode_match"] is not True
    ):
        return missing("counter_scope_not_bounded")
    if any(not references for references in roles.values()):
        return missing("original_role_missing")
    if (
        measurement["payload_mapping"] != "identity"
        or measurement["expected_payload_bytes"] != request["expected_bytes"]
        or not request["expected_bytes"]
    ):
        return missing("payload_mapping_unestablished")
    if request["correct"] is None:
        return missing("correctness_missing")
    if request["correct"] is False:
        return result(
            "FAIL",
            "incorrect_output_or_kv",
            "output or restored KV differs from its original",
        )
    if moved == 0:
        return result(
            "FAIL",
            "no_remote_payload",
            "intended NFS retrieval recorded zero remote payload",
        )
    if moved != measurement["expected_payload_bytes"]:
        return result(
            "FAIL",
            "payload_mismatch",
            "NFS payload differs from the independently established file range",
        )
    return result(
        "PASS",
        "nfs_service_verified",
        "request output/KV and scoped NFS-service payload agree; "
        "not server SSD or GPU-direct proof",
    )
