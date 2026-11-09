# SPDX-License-Identifier: Apache-2.0
"""Opt-in serial phase recording; no workload control or serving verdict."""

# Standard
import csv
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path


class PhaseWriter:
    """Write new normalized phase CSV with explicit error outcomes.

    Args:
        path: New CSV destination; its parent must already exist.
        clock: Monotonic nanosecond provider, injectable for deterministic tests.

    Raises:
        FileExistsError: If the destination exists (including a symlink).
        OSError: If the destination cannot be created.

    Notes:
        Use as a context manager. Additional outcome/error columns are accepted
        by the offline analyzer but do not establish correctness. No capture
        coverage, runtime identity or serving verification is inferred.
    """

    def __init__(
        self, path: Path, *, clock: Callable[[], int] = time.monotonic_ns
    ) -> None:
        self._clock = clock
        self._stream = path.open("x", encoding="utf-8", newline="")
        self._writer = csv.writer(self._stream)
        self._writer.writerow(
            [
                "request_id",
                "phase",
                "start_ns",
                "end_ns",
                "expected_read_bytes",
                "outcome",
                "error_type",
            ]
        )
        self._stream.flush()
        self._labels: set[tuple[str, str]] = set()
        self._active = False

    def __enter__(self) -> "PhaseWriter":
        """Return this open writer for a bounded export session."""
        return self

    def __exit__(self, *exc: object) -> None:
        """Close the output without suppressing the caller's exception."""
        self._stream.close()

    @contextmanager
    def record(
        self, request_id: str, phase: str, *, expected_read_bytes: int | None = None
    ) -> Iterator[None]:
        """Record a nonnested serial interval, even if the body raises.

        Args:
            request_id: Nonempty operator-generated interval label.
            phase: Nonempty phase name; request/phase pairs must be unique.
            expected_read_bytes: Independently derived nonnegative payload bytes.

        Yields:
            None; run the controlled action inside the context.

        Raises:
            ValueError: Invalid labels, bytes, nesting, duplicates or clock span.
            OSError: Output write failure.
            BaseException: Any body exception is retained and re-raised.

        Notes:
            Failed intervals keep only the exception class, never its message
            (which may contain a prompt or credential). A bad clock span is not
            repaired into fabricated timestamps; on body failure that original
            exception is preserved and no invalid interval is emitted.
        """
        if self._stream.closed:
            raise ValueError("phase writer is closed")
        if not isinstance(request_id, str) or not request_id:
            raise ValueError("request_id must be nonempty")
        if not isinstance(phase, str) or not phase:
            raise ValueError("phase must be nonempty")
        if expected_read_bytes is not None and (
            isinstance(expected_read_bytes, bool)
            or not isinstance(expected_read_bytes, int)
            or expected_read_bytes < 0
        ):
            raise ValueError("expected_read_bytes must be a nonnegative integer")
        label = (request_id, phase)
        if self._active or label in self._labels:
            raise ValueError("nested or duplicate phase")
        start = self._clock()
        if isinstance(start, bool) or not isinstance(start, int) or start < 0:
            raise ValueError("invalid monotonic timestamp")
        self._labels.add(label)
        self._active = True
        error = ""
        try:
            yield
        except BaseException as exc:
            error = type(exc).__name__
            raise
        finally:
            self._active = False
            end = self._clock()
            valid = isinstance(end, int) and not isinstance(end, bool) and end > start
            if valid:
                self._writer.writerow(
                    [
                        request_id,
                        phase,
                        start,
                        end,
                        "" if expected_read_bytes is None else expected_read_bytes,
                        "error" if error else "completed",
                        error,
                    ]
                )
                self._stream.flush()
            elif not error:
                raise ValueError("phase clock must advance")
