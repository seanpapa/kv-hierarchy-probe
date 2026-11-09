# SPDX-License-Identifier: Apache-2.0
"""Deterministic capacity calculations for a static KV-cache slot pool."""

# Standard
from typing import TypedDict


class PoolSizingError(ValueError):
    """Raised when static-pool sizing inputs are invalid."""


class PoolPlan(TypedDict):
    """Machine-readable result of a static-pool capacity calculation."""

    pool_slots: int
    bytes_per_chunk: int
    logical_capacity_bytes: int
    required_live_chunks: int
    required_live_bytes: int
    minimum_slots: int
    slot_headroom: int
    forced_evictions_lower_bound: int
    classification: str


def calculate_pool_plan(
    pool_slots: int,
    bytes_per_chunk: int,
    required_live_chunks: int,
) -> PoolPlan:
    """Calculate capacity and unavoidable eviction for a static slot pool.

    The calculation is deliberately policy-free. It does not call a pool
    optimal or recommended, and it does not add an arbitrary safety margin.
    ``required_live_chunks`` means the peak number of distinct chunks the
    experiment expects to remain retrievable at the same time. If no entry is
    explicitly removed before that peak, every chunk beyond ``pool_slots``
    requires at least one eviction and slot reuse.

    Args:
        pool_slots: Number of pre-created static storage slots.
        bytes_per_chunk: Exact stored bytes occupied by one KV-cache chunk.
        required_live_chunks: Peak number of chunks intended to remain live.

    Returns:
        Exact byte capacity, slot headroom, a lower bound on forced evictions,
        and where the pool capacity stands relative to the live set
        (``below_live_set``, ``at_live_set`` or ``above_live_set``).

    Raises:
        PoolSizingError: If any argument is not a positive integer.
    """
    values = {
        "pool_slots": pool_slots,
        "bytes_per_chunk": bytes_per_chunk,
        "required_live_chunks": required_live_chunks,
    }
    for name, value in values.items():
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise PoolSizingError(f"{name} must be a positive integer")

    slot_headroom = pool_slots - required_live_chunks
    if slot_headroom < 0:
        classification = "below_live_set"
    elif slot_headroom == 0:
        classification = "at_live_set"
    else:
        classification = "above_live_set"

    return {
        "pool_slots": pool_slots,
        "bytes_per_chunk": bytes_per_chunk,
        "logical_capacity_bytes": pool_slots * bytes_per_chunk,
        "required_live_chunks": required_live_chunks,
        "required_live_bytes": required_live_chunks * bytes_per_chunk,
        "minimum_slots": required_live_chunks,
        "slot_headroom": slot_headroom,
        "forced_evictions_lower_bound": max(0, -slot_headroom),
        "classification": classification,
    }


class LiveChunkDerivation(TypedDict):
    """How a live-chunk count was derived, kept so the assumptions stay visible."""

    method: str
    concurrent_contexts: int
    context_tokens: int
    chunk_tokens: int
    partial_chunk: str
    chunks_per_context: int
    required_live_chunks: int
    assumption: str


def _positive(name: str, value: object) -> int:
    """Return ``value`` if it is a positive integer, else raise."""
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise PoolSizingError(f"{name} must be a positive integer")
    return value


def derive_required_live_chunks(
    concurrent_contexts: int,
    context_tokens: int,
    chunk_tokens: int,
    partial_chunk: str | None = None,
) -> LiveChunkDerivation:
    """Derive the live-chunk count from concurrent contexts and chunk geometry.

    The derivation assumes every context needs its own distinct chunks, so no
    prefix is shared between contexts. It does not know how a backend counts
    slots under tensor parallelism; the operator must state that separately.

    Args:
        concurrent_contexts: Contexts that must stay retrievable at once.
        context_tokens: Prompt tokens per context.
        chunk_tokens: Tokens held by one chunk.
        partial_chunk: ``"drop"`` or ``"keep"`` for a trailing partial chunk.
            Required when ``context_tokens`` is not a multiple of
            ``chunk_tokens``, because the backend's behavior is not assumed.

    Returns:
        The derived count with every input and assumption it depends on.

    Raises:
        PoolSizingError: If an input is invalid, the partial-chunk policy is
            missing or unknown, or the context holds no complete chunk to keep.
    """
    contexts = _positive("concurrent_contexts", concurrent_contexts)
    tokens = _positive("context_tokens", context_tokens)
    chunk = _positive("chunk_tokens", chunk_tokens)
    if partial_chunk not in (None, "drop", "keep"):
        raise PoolSizingError("partial_chunk must be 'drop' or 'keep'")
    whole, remainder = divmod(tokens, chunk)
    if remainder == 0:
        policy = "not_applicable"
        per_context = whole
    elif partial_chunk is None:
        raise PoolSizingError(
            f"context_tokens ({tokens}) is not a multiple of chunk_tokens "
            f"({chunk}); state whether a trailing partial chunk is stored "
            "with partial_chunk='drop' or 'keep'"
        )
    else:
        policy = partial_chunk
        per_context = whole + (1 if partial_chunk == "keep" else 0)
    if per_context == 0:
        raise PoolSizingError(
            "context_tokens is shorter than one chunk and the partial chunk "
            "is dropped, so no chunk would be stored"
        )
    return {
        "method": "concurrent_contexts x chunks_per_context",
        "concurrent_contexts": contexts,
        "context_tokens": tokens,
        "chunk_tokens": chunk,
        "partial_chunk": policy,
        "chunks_per_context": per_context,
        "required_live_chunks": contexts * per_context,
        "assumption": "distinct chunks per context; no prefix shared between contexts",
    }


def bracket_pool_slots(required_live_chunks: int) -> list[int]:
    """Return pool sizes that bracket a live set, without choosing one.

    The values are ``ceil(live / 2)``, ``live``, ``2 * live`` and ``4 * live``,
    deduplicated and sorted: at least one below, one at, and two above the live
    set where a value below exists. They only span the region a sweep must
    cover; none is a recommendation.

    Args:
        required_live_chunks: Peak number of chunks intended to stay live.

    Returns:
        Ascending pool sizes in slots.

    Raises:
        PoolSizingError: If ``required_live_chunks`` is not a positive integer.
    """
    live = _positive("required_live_chunks", required_live_chunks)
    return sorted({-(-live // 2), live, 2 * live, 4 * live})
