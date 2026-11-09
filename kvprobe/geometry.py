# SPDX-License-Identifier: Apache-2.0
"""Derive standard-attention KV-cache geometry from a model configuration."""

# Standard
from typing import Any


class GeometryError(ValueError):
    """Raised when KV geometry cannot be derived without guessing."""


_DTYPE_BYTES = {
    "bfloat16": 2,
    "float16": 2,
    "float32": 4,
    "float8_e4m3fn": 1,
    "float8_e5m2": 1,
    "int8": 1,
}

_DTYPE_ALIASES = {
    "bf16": "bfloat16",
    "fp16": "float16",
    "half": "float16",
    "fp32": "float32",
    "fp8": "float8_e4m3fn",
}


def _positive_integer(config: dict[str, Any], name: str) -> int:
    """Read a positive integer model field.

    Args:
        config: Parsed model configuration.
        name: Field name to read.

    Returns:
        Positive integer value.

    Raises:
        GeometryError: If the field is absent or not a positive integer.
    """
    value = config.get(name)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise GeometryError(f"{name} must be a positive integer")
    return value


def _runtime_positive_integer(value: int, name: str) -> int:
    """Validate a positive integer runtime setting.

    Args:
        value: Runtime value to validate.
        name: Setting name for an actionable error.

    Returns:
        The validated value.

    Raises:
        GeometryError: If the value is not a positive integer.
    """
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise GeometryError(f"{name} must be a positive integer")
    return value


def _normalize_dtype(dtype: str) -> tuple[str, int]:
    """Normalize one supported runtime KV-cache dtype.

    Args:
        dtype: Runtime dtype or a recognized short alias.

    Returns:
        Canonical dtype and bytes per scalar.

    Raises:
        GeometryError: If the dtype has no explicit size mapping.
    """
    normalized = dtype.lower().removeprefix("torch.")
    normalized = _DTYPE_ALIASES.get(normalized, normalized)
    if normalized not in _DTYPE_BYTES:
        supported = ", ".join(sorted(_DTYPE_BYTES))
        raise GeometryError(
            f"unsupported dtype {dtype!r}; choose one of: {supported}"
        )
    return normalized, _DTYPE_BYTES[normalized]


def derive_geometry(
    model_config: dict[str, Any],
    *,
    chunk_tokens: int,
    dtype: str | None = None,
    tensor_parallel_size: int = 1,
    pipeline_parallel_size: int = 1,
) -> dict[str, Any]:
    """Derive full-model and TP-rank KV-cache byte geometry.

    The standard attention formula is ``2 * layers * kv_heads * head_dim *
    dtype_bytes`` per token. Runtime settings are explicit because a model
    file cannot prove the dtype or parallelism selected by the serving engine.
    Pipeline parallelism other than one is refused because the layer placement
    per rank is required for an exact result.

    ``num_attention_heads`` may be absent when both ``num_key_value_heads``
    and ``head_dim`` are given, because neither is then derived from it; the
    result's ``model`` block then omits it rather than inventing a value.

    Args:
        model_config: Parsed Hugging Face-style ``config.json`` object, or the
            equivalent fields entered as numbers.
        chunk_tokens: Cache chunk size selected by the serving runtime.
        dtype: Runtime KV-cache dtype. When omitted, use ``torch_dtype`` from
            the model configuration.
        tensor_parallel_size: Number of tensor-parallel ranks.
        pipeline_parallel_size: Number of pipeline-parallel stages. Only one
            is currently supported.

    Returns:
        Machine-readable model inputs, runtime settings, and byte geometry.

    Raises:
        GeometryError: If required fields are missing, the layout is
            unsupported, or a result would require guessing.
    """
    if not isinstance(model_config, dict):
        raise GeometryError("model config must be a JSON object")
    if model_config.get("kv_lora_rank") is not None:
        raise GeometryError(
            "MLA geometry is not supported by the standard KV-head formula"
        )

    chunk_tokens = _runtime_positive_integer(chunk_tokens, "chunk_tokens")
    tensor_parallel_size = _runtime_positive_integer(
        tensor_parallel_size, "tensor_parallel_size"
    )
    pipeline_parallel_size = _runtime_positive_integer(
        pipeline_parallel_size, "pipeline_parallel_size"
    )
    if pipeline_parallel_size != 1:
        raise GeometryError(
            "pipeline_parallel_size > 1 requires exact layer placement per rank"
        )

    layers = _positive_integer(model_config, "num_hidden_layers")
    needs_attention_heads = (
        model_config.get("num_key_value_heads") is None
        or model_config.get("head_dim") is None
    )
    attention_heads = (
        _positive_integer(model_config, "num_attention_heads")
        if needs_attention_heads or "num_attention_heads" in model_config
        else None
    )
    kv_heads_value = model_config.get("num_key_value_heads", attention_heads)
    if (
        not isinstance(kv_heads_value, int)
        or isinstance(kv_heads_value, bool)
        or kv_heads_value <= 0
    ):
        raise GeometryError("num_key_value_heads must be a positive integer")
    kv_heads = kv_heads_value

    explicit_head_dim = model_config.get("head_dim")
    if explicit_head_dim is not None:
        if (
            not isinstance(explicit_head_dim, int)
            or isinstance(explicit_head_dim, bool)
            or explicit_head_dim <= 0
        ):
            raise GeometryError("head_dim must be a positive integer")
        head_dim = explicit_head_dim
        head_dim_source = "head_dim"
    else:
        hidden_size = _positive_integer(model_config, "hidden_size")
        if attention_heads is None:
            raise GeometryError("num_attention_heads must be a positive integer")
        if hidden_size % attention_heads != 0:
            raise GeometryError(
                "hidden_size is not divisible by num_attention_heads; "
                "an explicit head_dim is required"
            )
        head_dim = hidden_size // attention_heads
        head_dim_source = "hidden_size / num_attention_heads"

    dtype_source = "argument"
    selected_dtype = dtype
    if selected_dtype is None:
        if model_config.get("dtype") is not None:
            selected_dtype = model_config.get("dtype")
            dtype_source = "model_config.dtype"
        else:
            selected_dtype = model_config.get("torch_dtype")
            dtype_source = "model_config.torch_dtype"
    if not isinstance(selected_dtype, str) or not selected_dtype:
        raise GeometryError(
            "dtype is required because model_config.torch_dtype is absent"
        )
    canonical_dtype, dtype_bytes = _normalize_dtype(selected_dtype)

    if kv_heads % tensor_parallel_size == 0:
        per_rank_kv_heads = kv_heads // tensor_parallel_size
        placement = "single" if tensor_parallel_size == 1 else "sharded"
    elif tensor_parallel_size % kv_heads == 0:
        per_rank_kv_heads = 1
        placement = "replicated"
    else:
        raise GeometryError(
            "num_key_value_heads and tensor_parallel_size do not describe "
            "an even sharded or replicated layout"
        )

    def byte_geometry(heads: int) -> dict[str, int]:
        """Calculate exact bytes for one KV-head count."""
        bytes_per_token = 2 * layers * heads * head_dim * dtype_bytes
        return {
            "bytes_per_token": bytes_per_token,
            "bytes_per_chunk": bytes_per_token * chunk_tokens,
        }

    full_model = byte_geometry(kv_heads)
    per_rank = byte_geometry(per_rank_kv_heads)
    tp_group = {
        name: value * tensor_parallel_size for name, value in per_rank.items()
    }

    def exact_mib(value: int) -> int | None:
        """Return exact MiB or None when integer-MiB scripts cannot represent it."""
        mib = 1024 * 1024
        return value // mib if value % mib == 0 else None

    architectures = model_config.get("architectures", [])
    if not isinstance(architectures, list) or not all(
        isinstance(item, str) for item in architectures
    ):
        architectures = []

    return {
        "geometry_version": "0.1.1",
        "model": {
            "architectures": architectures,
            "num_hidden_layers": layers,
            **(
                {"num_attention_heads": attention_heads}
                if attention_heads is not None
                else {}
            ),
            "num_key_value_heads": kv_heads,
            "head_dim": head_dim,
            "head_dim_source": head_dim_source,
        },
        "runtime": {
            "dtype": canonical_dtype,
            "dtype_bytes": dtype_bytes,
            "dtype_source": dtype_source,
            "chunk_tokens": chunk_tokens,
            "tensor_parallel_size": tensor_parallel_size,
            "pipeline_parallel_size": pipeline_parallel_size,
            "kv_head_placement": placement,
            "kv_head_placement_source": "derived_from_counts_not_observed",
        },
        "geometry": {
            "full_model": full_model,
            "per_tp_rank": per_rank,
            "tp_group_aggregate": tp_group,
        },
        "selection": {
            "per_device_counter_bytes_per_chunk": per_rank["bytes_per_chunk"],
            "tp_group_counter_bytes_per_chunk": tp_group["bytes_per_chunk"],
            "per_device_counter_mib_per_chunk": exact_mib(
                per_rank["bytes_per_chunk"]
            ),
            "tp_group_counter_mib_per_chunk": exact_mib(
                tp_group["bytes_per_chunk"]
            ),
            "rule": (
                "Select the value matching the counter scope; do not mix "
                "per-device and TP-group evidence."
            ),
        },
    }
