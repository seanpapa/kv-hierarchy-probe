# SPDX-License-Identifier: Apache-2.0
"""Canonical experiment configuration validation and fingerprinting."""

# Standard
import hashlib
import json
from typing import Any


#: Longest accepted runtime identity. These fields exist to distinguish one
#: build or device from another, not to carry a description, and the
#: fingerprint is taken over their text.
_IDENTITY_MAX = 200


class ConfigurationError(ValueError):
    """Raised when an experiment configuration is malformed."""


def _object(parent: dict[str, Any], name: str) -> dict[str, Any]:
    """Return a required child object."""
    value = parent.get(name)
    if not isinstance(value, dict):
        raise ConfigurationError(f"configuration {name} must be an object")
    return value


def _text(parent: dict[str, Any], name: str) -> str:
    """Return a required non-empty string."""
    value = parent.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(
            f"configuration {name} must be a non-empty string"
        )
    return value


def _identity(parent: dict[str, Any], name: str) -> str:
    """Return a required opaque identity string, recorded verbatim.

    Args:
        parent: Object holding the field.
        name: Field name, used in error messages.

    Returns:
        The identity exactly as supplied.

    Raises:
        ConfigurationError: If the value is missing, empty, longer than
            ``_IDENTITY_MAX``, or carries surrounding or control whitespace.
    """
    value = _text(parent, name)
    if len(value) > _IDENTITY_MAX:
        raise ConfigurationError(
            f"configuration {name} must be at most {_IDENTITY_MAX} characters"
        )
    if value != value.strip() or any(
        character.isspace() and character != " " for character in value
    ):
        raise ConfigurationError(
            f"configuration {name} must be recorded verbatim without "
            "surrounding or control whitespace; the fingerprint is taken over "
            "its text, so invisible drift would change the configuration"
        )
    return value


def _integer(
    parent: dict[str, Any], name: str, *, positive: bool = False
) -> int:
    """Return a required non-negative or positive integer."""
    value = parent.get(name)
    minimum = 1 if positive else 0
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < minimum
    ):
        qualifier = "positive" if positive else "non-negative"
        raise ConfigurationError(
            f"configuration {name} must be a {qualifier} integer"
        )
    return value


def _integer_list(parent: dict[str, Any], name: str) -> list[int]:
    """Return a required non-empty list of positive integers."""
    value = parent.get(name)
    if (
        not isinstance(value, list)
        or not value
        or not all(
            isinstance(item, int) and not isinstance(item, bool) and item > 0
            for item in value
        )
    ):
        raise ConfigurationError(
            f"configuration {name} must be a non-empty list of positive integers"
        )
    return value


def _exact_keys(
    value: dict[str, Any],
    required: set[str],
    context: str,
    *,
    optional: frozenset[str] = frozenset(),
) -> None:
    """Require an exact versioned configuration object shape.

    Args:
        value: The object to check.
        required: Keys that must be present.
        context: Name used in the error message.
        optional: Keys that may be present but are not required. A key omitted
            here is still reported as ``extra``, so the shape stays versioned.
    """
    missing = sorted(required - set(value))
    extra = sorted(set(value) - required - optional)
    if missing or extra:
        raise ConfigurationError(
            f"configuration {context} keys mismatch; missing={missing}, extra={extra}"
        )


def validate_configuration(parameters: Any) -> dict[str, Any]:
    """Validate the fields that define one comparable experiment.

    Args:
        parameters: Parsed configuration parameter object.

    Returns:
        The validated object without modification.

    Raises:
        ConfigurationError: If a required identity, capacity, schedule, or arm
            field is missing or malformed.
    """
    if not isinstance(parameters, dict):
        raise ConfigurationError("configuration parameters must be an object")
    _exact_keys(
        parameters,
        {"model", "kv_geometry", "cache", "runtime", "measurement", "arms"},
        "parameters",
    )

    model = _object(parameters, "model")
    _exact_keys(model, {"root", "revision", "served_name"}, "model")
    _text(model, "root")
    _text(model, "revision")
    _text(model, "served_name")

    geometry = _object(parameters, "kv_geometry")
    _exact_keys(
        geometry,
        {"chunk_tokens", "bytes_per_chunk", "dtype"},
        "kv_geometry",
    )
    _integer(geometry, "chunk_tokens", positive=True)
    _integer(geometry, "bytes_per_chunk", positive=True)
    _text(geometry, "dtype")

    cache = _object(parameters, "cache")
    _exact_keys(cache, {"upper_tier_capacity_bytes"}, "cache")
    _integer(cache, "upper_tier_capacity_bytes", positive=True)

    runtime = _object(parameters, "runtime")
    _exact_keys(
        runtime,
        {"image_digest", "device_id"},
        "runtime",
        optional=frozenset({"gpu_index"}),
    )
    # The serving software is the other half of a configuration's identity: two
    # runs on the same device and the same parameters still differ if the image
    # changed under them, and a fingerprint that did not bind the build would
    # merge them. A content digest is the strongest form of that identity and
    # stays the recommended value, but it is not required: an operator who
    # publishes evidence should not have to disclose a private build to be
    # inside the contract, and a fabricated digest would read as authentic
    # where an opaque label reads as what it is.
    _identity(runtime, "image_digest")
    # Storage counters do not identify which accelerator issued the I/O:
    # nvidia-fs is host-wide, while current cgroup accounting can be filtered
    # to one block device. On a multi-device host two accelerators can sit
    # behind different root complexes and reach the tier at different cost, so
    # a configuration that does not name its accelerator would let those runs
    # merge under one fingerprint. The identity is opaque on purpose: what the
    # method needs is a value that stays stable across the campaign and differs
    # between devices, not any particular vendor tool's output format.
    _identity(runtime, "device_id")
    # Optional, and never a substitute: enumeration order is not stable across
    # reboots or driver changes, which is why the identity above is required.
    if "gpu_index" in runtime:
        _integer(runtime, "gpu_index")

    measurement = _object(parameters, "measurement")
    _exact_keys(
        measurement,
        {"contexts", "concurrency", "samples", "warmup"},
        "measurement",
    )
    _integer_list(measurement, "contexts")
    _integer_list(measurement, "concurrency")
    _integer(measurement, "samples", positive=True)
    _integer(measurement, "warmup")

    arms = parameters.get("arms")
    if not isinstance(arms, list) or not arms:
        raise ConfigurationError("configuration arms must be a non-empty list")
    arm_names: set[str] = set()
    for index, arm in enumerate(arms):
        if not isinstance(arm, dict):
            raise ConfigurationError(f"configuration arm {index} must be an object")
        _exact_keys(
            arm,
            {
                "name",
                "backend",
                "tier_path",
                "attribution_provider",
                "staging_capacity_bytes",
                "eviction_policy",
                "eviction_parameters",
            },
            f"arm {index}",
        )
        name = _text(arm, "name")
        if name in arm_names:
            raise ConfigurationError(f"duplicate configuration arm name: {name}")
        arm_names.add(name)
        _text(arm, "backend")
        tier_path = arm.get("tier_path")
        if tier_path is not None and (
            not isinstance(tier_path, str) or not tier_path.strip()
        ):
            raise ConfigurationError(
                f"configuration arm {index} tier_path must be null or non-empty"
            )
        _text(arm, "attribution_provider")
        _integer(arm, "staging_capacity_bytes")
        _text(arm, "eviction_policy")
        if not isinstance(arm.get("eviction_parameters"), dict):
            raise ConfigurationError(
                f"configuration arm {index} eviction_parameters must be an object"
            )
    return parameters


def fingerprint_configuration(parameters: Any) -> str:
    """Return the SHA-256 fingerprint of canonical configuration JSON.

    Args:
        parameters: Parsed configuration parameter object.

    Returns:
        Lowercase SHA-256 digest over UTF-8 canonical JSON with sorted keys and
        no insignificant whitespace.

    Raises:
        ConfigurationError: If the configuration is malformed or contains a
            non-finite number that canonical JSON must reject.
    """
    validated = validate_configuration(parameters)
    try:
        canonical = json.dumps(
            validated,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(
            f"configuration is not canonical JSON: {exc}"
        ) from exc
    return hashlib.sha256(canonical).hexdigest()


def validate_registry(entries: Any) -> dict[str, str]:
    """Validate a configuration registry and return ID-to-fingerprint mapping.

    Args:
        entries: Parsed list of configuration registry entries.

    Returns:
        Mapping from unique configuration IDs to verified fingerprints.

    Raises:
        ConfigurationError: If the registry is empty, duplicated, malformed,
            or contains a fingerprint that does not match its parameters.
    """
    if not isinstance(entries, list) or not entries:
        raise ConfigurationError("configurations must be a non-empty list")
    registry: dict[str, str] = {}
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ConfigurationError(f"configuration entry {index} must be an object")
        _exact_keys(entry, {"id", "fingerprint", "parameters"}, f"entry {index}")
        configuration_id = _text(entry, "id")
        if configuration_id in registry:
            raise ConfigurationError(
                f"duplicate configuration id: {configuration_id}"
            )
        supplied = _text(entry, "fingerprint")
        expected = fingerprint_configuration(entry.get("parameters"))
        if supplied != expected:
            raise ConfigurationError(
                f"configuration {configuration_id} fingerprint mismatch: "
                f"expected {expected}"
            )
        registry[configuration_id] = supplied
    return registry
