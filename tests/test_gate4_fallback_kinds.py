# SPDX-License-Identifier: Apache-2.0

"""Gate 4 splits its fallbacks by whether the entry's store write was observed.

A ``fallback`` beyond the upper tier's capacity needs one of two diagnoses: the
entry was never seen being written, or a write was seen and the load still did
not happen. The capacity producer keeps per-request ``stores`` and
``retrievals`` records; ingest reads each entry's history across the sweep and
the report names both counts and where to look first. A refresh of an entry the
tier already holds may write nothing, so the history, not the same row's store,
decides. Verdicts never change, and evidence without records keeps the
count-only result. All data here is synthetic.
"""

# Standard
import itertools
import json
import unittest
from pathlib import Path
from typing import Any

# First Party
from kvprobe.ingest import IngestError, build_capacity
from kvprobe.report import EvidenceError, assess_evidence

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "examples" / "evidence"
SCHEMAS = ROOT / "schemas"

PER_ENTRY_MIB = 64
#: Two entries fit; a working set of three or more is beyond capacity.
UPPER_TIER_BYTES = 2 * PER_ENTRY_MIB * 1024 * 1024
FULL = PER_ENTRY_MIB
COUNT_FIELD = {"dram": "dram_hits", "nvme": "nvme_hits", "fallback": "fallback", "miss": "miss"}


def _row(
    store_writes: list[float | None],
    retrievals: list[tuple[str, float | None]],
) -> dict[str, Any]:
    """Return one producer row from per-entry store writes and retrievals.

    Args:
        store_writes: Write MiB observed while storing entry 0, 1, ...
        retrievals: ``(kind, write MiB)`` for retrieving entry 0, 1, ...

    Returns:
        A row in the ``g4_capacity_sweep.py`` shape.
    """
    working_set = len(retrievals)
    row: dict[str, Any] = {
        "working_set": working_set,
        "resident_mib": working_set * PER_ENTRY_MIB,
        **{field: 0 for field in COUNT_FIELD.values()},
        "stores": [
            {"entry": index, "write_mib": write, "ttft_s": 0.1}
            for index, write in enumerate(store_writes)
        ],
        "retrievals": [],
    }
    for index, (kind, write) in enumerate(retrievals):
        row[COUNT_FIELD[kind]] += 1
        row["retrievals"].append(
            {
                "entry": index,
                "read_mib": FULL if kind == "nvme" else 0,
                "write_mib": write,
                "ttft_s": 0.9 if kind == "fallback" else 0.1,
                "kind": kind,
            }
        )
    return row


def _run(*rows: dict[str, Any]) -> dict[str, Any]:
    """Return a capacity run carrying the given rows."""
    return {
        "gate": "capacity_justification",
        "per_entry_mib": PER_ENTRY_MIB,
        "rows": list(rows),
    }


def _written_then_refreshed() -> dict[str, Any]:
    """Entries 0-1 are written once, then refreshed with no write and fall back.

    The second row's own stores wrote nothing, so a same-row rule would call both
    fallbacks "no store write"; the entry history says the write was observed.
    """
    return _run(
        _row([FULL, FULL], [("dram", 0), ("dram", 0)]),
        _row([0, 0, FULL], [("fallback", 0), ("fallback", 0), ("nvme", 0)]),
    )


def _beyond(block: dict[str, Any]) -> dict[str, int]:
    """Return the beyond-capacity counts of a capacity block."""
    return block["overflow_classification_counts"]


def _document(capacity: dict[str, Any]) -> dict[str, Any]:
    """Return the all-pass example at schema 0.3.2 with this capacity block."""
    document = json.loads((EXAMPLES / "pass-v0.3.json").read_text(encoding="utf-8"))
    document["schema_version"] = "0.3.2"
    document["evidence"]["capacity"] = capacity
    return document


def _gate4(document: dict[str, Any]) -> dict[str, Any]:
    """Return the Gate 4 verdict of a document."""
    gates = assess_evidence(document)["gates"]
    return next(gate for gate in gates if gate["id"] == 4)


def _capacity(
    beyond: dict[str, int],
    total: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Return a supported capacity block from beyond-capacity counts."""
    return {
        "availability": {"status": "supported"},
        "upper_tier_overflow_observed": beyond["retrievals_expected"] > 0,
        "target_tier_served": beyond["target_tier_served"] > 0,
        "classification_counts": dict(total or beyond),
        "overflow_classification_counts": dict(beyond),
    }


def _counts(
    served: int, fallback: int, miss: int, observed: int | None = None
) -> dict[str, int]:
    """Return counts with one upper-tier hit, optionally split."""
    counts = {
        "retrievals_expected": 1 + served + fallback + miss,
        "upper_tier_served": 1,
        "target_tier_served": served,
        "fallback": fallback,
        "miss": miss,
    }
    if observed is not None:
        counts["fallback_store_write_observed"] = observed
        counts["fallback_store_write_not_observed"] = fallback - observed
    return counts


class IngestSplit(unittest.TestCase):
    """Ingest reads each entry's history across the sweep."""

    def test_a_refresh_with_no_write_keeps_the_earlier_write(self) -> None:
        beyond = _beyond(build_capacity(_written_then_refreshed(), UPPER_TIER_BYTES))
        self.assertEqual(beyond["fallback"], 2)
        self.assertEqual(beyond["fallback_store_write_observed"], 2)
        self.assertEqual(beyond["fallback_store_write_not_observed"], 0)

    def test_an_entry_never_seen_written_is_not_observed(self) -> None:
        run = _run(_row([0, 0, 0], [("fallback", 0), ("fallback", 0), ("dram", 0)]))
        beyond = _beyond(build_capacity(run, UPPER_TIER_BYTES))
        self.assertEqual(beyond["fallback_store_write_observed"], 0)
        self.assertEqual(beyond["fallback_store_write_not_observed"], 2)

    def test_mixed_history_counts_each_kind(self) -> None:
        run = _run(
            _row([FULL, 0, 0], [("fallback", 0), ("fallback", 0), ("nvme", 0)])
        )
        beyond = _beyond(build_capacity(run, UPPER_TIER_BYTES))
        self.assertEqual(beyond["fallback_store_write_observed"], 1)
        self.assertEqual(beyond["fallback_store_write_not_observed"], 1)

    def test_a_partial_write_is_not_an_observed_store_write(self) -> None:
        """At least 95% of one entry counts; less does not."""
        for write, observed in ((FULL * 0.95, 1), (FULL * 0.94, 0)):
            with self.subTest(write=write):
                run = _run(
                    _row([write, 0, 0], [("fallback", 0), ("dram", 0), ("dram", 0)])
                )
                beyond = _beyond(build_capacity(run, UPPER_TIER_BYTES))
                self.assertEqual(beyond["fallback_store_write_observed"], observed)

    def test_the_fallbacks_own_write_does_not_count_for_it(self) -> None:
        """A recompute may store the entry after the load failed; a later
        fallback of that entry then follows an observed write."""
        run = _run(
            _row([0, 0, 0], [("fallback", FULL), ("dram", 0), ("dram", 0)]),
            _row([0, 0, 0, 0], [("fallback", 0), ("dram", 0), ("dram", 0), ("dram", 0)]),
        )
        beyond = _beyond(build_capacity(run, UPPER_TIER_BYTES))
        self.assertEqual(beyond["fallback_store_write_observed"], 1)
        self.assertEqual(beyond["fallback_store_write_not_observed"], 1)

    def test_fallbacks_within_capacity_count_only_in_the_sweep_total(self) -> None:
        run = _run(
            _row([0, 0], [("fallback", 0), ("dram", 0)]),
            _row([FULL, FULL, FULL], [("fallback", 0), ("nvme", 0), ("dram", 0)]),
        )
        block = build_capacity(run, UPPER_TIER_BYTES)
        self.assertEqual(
            (
                block["classification_counts"]["fallback_store_write_observed"],
                block["classification_counts"]["fallback_store_write_not_observed"],
            ),
            (1, 1),
        )
        self.assertEqual(_beyond(block)["fallback_store_write_observed"], 1)
        self.assertEqual(_beyond(block)["fallback_store_write_not_observed"], 0)

    def test_an_unavailable_write_reading_omits_the_split(self) -> None:
        """A fallback whose entry has only an unreadable write cannot be split."""
        run = _run(_row([None, 0, 0], [("fallback", 0), ("dram", 0), ("dram", 0)]))
        block = build_capacity(run, UPPER_TIER_BYTES)
        self.assertEqual(_beyond(block)["fallback"], 1)
        for name in ("classification_counts", "overflow_classification_counts"):
            self.assertNotIn("fallback_store_write_observed", block[name])
            self.assertNotIn("fallback_store_write_not_observed", block[name])

    def test_an_unavailable_reading_after_an_observed_write_still_splits(self) -> None:
        run = _run(
            _row([FULL, 0], [("dram", 0), ("dram", 0)]),
            _row([None, 0, 0], [("fallback", 0), ("dram", 0), ("dram", 0)]),
        )
        beyond = _beyond(build_capacity(run, UPPER_TIER_BYTES))
        self.assertEqual(beyond["fallback_store_write_observed"], 1)

    def test_a_run_without_records_carries_counts_only(self) -> None:
        run = _written_then_refreshed()
        for row in run["rows"]:
            del row["stores"], row["retrievals"]
        block = build_capacity(run, UPPER_TIER_BYTES)
        self.assertEqual(_beyond(block)["fallback"], 2)
        self.assertNotIn("fallback_store_write_observed", _beyond(block))

    def test_records_that_contradict_the_run_are_refused(self) -> None:
        def some_rows_only(run: dict[str, Any]) -> None:
            del run["rows"][0]["stores"], run["rows"][0]["retrievals"]

        def stores_without_retrievals(run: dict[str, Any]) -> None:
            del run["rows"][1]["retrievals"]

        def no_entry_size(run: dict[str, Any]) -> None:
            del run["per_entry_mib"]

        def kinds_disagree(run: dict[str, Any]) -> None:
            run["rows"][1]["retrievals"][0]["kind"] = "nvme"

        def unknown_kind(run: dict[str, Any]) -> None:
            run["rows"][1]["retrievals"][0]["kind"] = "lost"

        def one_retrieval_short(run: dict[str, Any]) -> None:
            run["rows"][1]["retrievals"].pop()

        def entry_twice(run: dict[str, Any]) -> None:
            run["rows"][1]["retrievals"][1]["entry"] = 0

        def negative_write(run: dict[str, Any]) -> None:
            run["rows"][0]["stores"][0]["write_mib"] = -1

        def write_missing(run: dict[str, Any]) -> None:
            del run["rows"][0]["stores"][0]["write_mib"]

        def entry_not_integer(run: dict[str, Any]) -> None:
            run["rows"][0]["stores"][0]["entry"] = "0"

        for change in (
            some_rows_only,
            stores_without_retrievals,
            no_entry_size,
            kinds_disagree,
            unknown_kind,
            one_retrieval_short,
            entry_twice,
            negative_write,
            write_missing,
            entry_not_integer,
        ):
            with self.subTest(change=change.__name__):
                run = _written_then_refreshed()
                change(run)
                with self.assertRaises(IngestError):
                    build_capacity(run, UPPER_TIER_BYTES)


class ReportDirection(unittest.TestCase):
    """The report names both counts and where to look first."""

    def test_all_observed_points_to_the_load_path(self) -> None:
        gate = _gate4(_document(_capacity(_counts(served=4, fallback=8, miss=0, observed=8))))
        self.assertEqual(gate["status"], "PASS")
        self.assertIn("served only 4 of 13", gate["result"])
        self.assertIn("all 8 fallbacks followed an observed store write", gate["result"])
        self.assertIn("load path and the staging pool", gate["result"])
        self.assertIn("docs/FAILURE-CATALOG.md#staging-buffer-full", gate["result"])
        self.assertNotIn("store path", gate["result"])

    def test_none_observed_points_to_the_store_path(self) -> None:
        result = _gate4(_document(_capacity(_counts(4, 3, 0, observed=0))))["result"]
        self.assertIn("none of the 3 fallbacks followed an observed store write", result)
        self.assertIn("look first at the store path", result)
        self.assertNotIn("staging pool", result)

    def test_mixed_points_to_both(self) -> None:
        result = _gate4(_document(_capacity(_counts(4, 5, 0, observed=2))))["result"]
        self.assertIn("2 of the 5 fallbacks followed an observed store write", result)
        self.assertIn("and 3 did not", result)
        self.assertIn("staging pool", result)
        self.assertIn("the store path", result)

    def test_a_single_fallback_reads_as_one(self) -> None:
        observed = _gate4(_document(_capacity(_counts(4, 1, 0, observed=1))))["result"]
        self.assertIn("the fallback followed an observed store write", observed)
        missing = _gate4(_document(_capacity(_counts(4, 1, 0, observed=0))))["result"]
        self.assertIn("the fallback did not follow an observed store write", missing)

    def test_every_direction_keeps_the_counter_scope(self) -> None:
        for observed in (0, 1, 2):
            with self.subTest(observed=observed):
                result = _gate4(
                    _document(_capacity(_counts(4, 2, 0, observed=observed)))
                )["result"]
                self.assertIn("the write counter is host-wide", result)
                self.assertIn(
                    "does not show that an entry reached the target device", result
                )

    def test_misses_are_named_as_not_split(self) -> None:
        many = _gate4(_document(_capacity(_counts(4, 2, 3, observed=2))))["result"]
        self.assertIn("5 fell back to recompute or missed", many)
        self.assertIn("the 3 misses are not split this way", many)
        one = _gate4(_document(_capacity(_counts(4, 2, 1, observed=2))))["result"]
        self.assertIn("the miss is not split this way", one)

    def test_only_misses_keep_the_count_only_result(self) -> None:
        split = _gate4(_document(_capacity(_counts(4, 0, 2, observed=0))))["result"]
        plain = _gate4(_document(_capacity(_counts(4, 0, 2))))["result"]
        self.assertEqual(split, plain)

    def test_no_split_keeps_the_count_only_result(self) -> None:
        result = _gate4(_document(_capacity(_counts(4, 8, 0))))["result"]
        self.assertIn("read the producer's per-request store and retrieval records", result)
        self.assertNotIn("followed an observed store write", result)

    def test_a_failed_gate_names_the_direction_in_its_reason(self) -> None:
        gate = _gate4(_document(_capacity(_counts(0, 3, 0, observed=3))))
        self.assertEqual(gate["status"], "FAIL")
        self.assertTrue(
            gate["reason"].startswith(
                "the upper tier overflowed but no retrieval was served by the "
                "target tier — all 3 fallbacks followed"
            )
        )
        self.assertEqual(gate["result"], "")

    def test_a_failed_gate_without_a_split_keeps_its_reason(self) -> None:
        gate = _gate4(_document(_capacity(_counts(0, 3, 0))))
        self.assertEqual(
            gate["reason"],
            "the upper tier overflowed but no retrieval was served by the target tier",
        )

    def test_the_split_never_changes_a_verdict(self) -> None:
        """Every combination of service, fallback, miss and split, with and
        without overflow, gives the same status and claim state as its
        count-only twin."""
        for served, fallback, miss in itertools.product(range(3), range(3), range(2)):
            for observed in range(fallback + 1):
                with self.subTest(served=served, fallback=fallback, miss=miss, observed=observed):
                    split = _document(_capacity(_counts(served, fallback, miss, observed)))
                    plain = _document(_capacity(_counts(served, fallback, miss)))
                    with_split = assess_evidence(split)
                    without = assess_evidence(plain)
                    self.assertEqual(
                        [gate["status"] for gate in with_split["gates"]],
                        [gate["status"] for gate in without["gates"]],
                    )
                    self.assertEqual(
                        with_split["performance_claims"], without["performance_claims"]
                    )
                    self.assertEqual(with_split["overall"], without["overall"])

    def test_an_inconsistent_split_is_refused(self) -> None:
        def does_not_add_up(block: dict[str, Any]) -> None:
            block["overflow_classification_counts"]["fallback_store_write_observed"] = 1

        def one_key_only(block: dict[str, Any]) -> None:
            for name in ("classification_counts", "overflow_classification_counts"):
                del block[name]["fallback_store_write_not_observed"]

        def one_block_only(block: dict[str, Any]) -> None:
            del block["classification_counts"]["fallback_store_write_observed"]
            del block["classification_counts"]["fallback_store_write_not_observed"]

        def beyond_exceeds_total(block: dict[str, Any]) -> None:
            block["classification_counts"]["fallback_store_write_observed"] = 0
            block["classification_counts"]["fallback_store_write_not_observed"] = 2

        def not_an_integer(block: dict[str, Any]) -> None:
            for name in ("classification_counts", "overflow_classification_counts"):
                block[name]["fallback_store_write_observed"] = 2.0

        for change in (
            does_not_add_up,
            one_key_only,
            one_block_only,
            beyond_exceeds_total,
            not_an_integer,
        ):
            with self.subTest(change=change.__name__):
                block = _capacity(_counts(4, 2, 0, observed=2))
                change(block)
                with self.assertRaises(EvidenceError):
                    assess_evidence(_document(block))

    def test_ingested_records_reach_the_result(self) -> None:
        block = build_capacity(_written_then_refreshed(), UPPER_TIER_BYTES)
        result = _gate4(_document(block))["result"]
        self.assertIn("all 2 fallbacks followed an observed store write", result)


def _jsonschema_usable() -> bool:
    """Return True only if the installed jsonschema can run these checks."""
    try:
        # Third Party
        from jsonschema import Draft202012Validator  # noqa: F401
        from referencing import Registry, Resource  # noqa: F401
    except ImportError:
        return False
    return True


def _validator(schema_version: str) -> Any:
    """Return a validator for one evidence schema, resolving bundled refs offline."""
    # Third Party
    from jsonschema import Draft202012Validator
    from referencing import Registry, Resource

    registry = Registry().with_resources(
        (document["$id"], Resource.from_contents(document))
        for document in (
            json.loads(path.read_text(encoding="utf-8"))
            for path in sorted(SCHEMAS.glob("evidence-v*.schema.json"))
        )
    )
    schema = json.loads(
        (SCHEMAS / f"evidence-v{schema_version}.schema.json").read_text(encoding="utf-8")
    )
    return Draft202012Validator(schema, registry=registry)


@unittest.skipUnless(_jsonschema_usable(), "jsonschema is a test-only dependency")
class SchemaSplit(unittest.TestCase):
    """Only 0.3.2 carries the split, and only as a pair."""

    def test_ingested_split_satisfies_0_3_2(self) -> None:
        document = _document(build_capacity(_written_then_refreshed(), UPPER_TIER_BYTES))
        self.assertEqual([], [e.message for e in _validator("0.3.2").iter_errors(document)])

    def test_0_3_1_stays_closed_to_the_split(self) -> None:
        document = _document(build_capacity(_written_then_refreshed(), UPPER_TIER_BYTES))
        document["schema_version"] = "0.3.1"
        self.assertTrue(list(_validator("0.3.1").iter_errors(document)))

    def test_one_key_of_the_pair_is_refused(self) -> None:
        document = _document(build_capacity(_written_then_refreshed(), UPPER_TIER_BYTES))
        counts = document["evidence"]["capacity"]["overflow_classification_counts"]
        del counts["fallback_store_write_not_observed"]
        self.assertTrue(list(_validator("0.3.2").iter_errors(document)))

    def test_count_only_capacity_satisfies_0_3_2(self) -> None:
        document = _document(_capacity(_counts(4, 2, 0)))
        self.assertEqual([], [e.message for e in _validator("0.3.2").iter_errors(document)])


if __name__ == "__main__":
    unittest.main()
