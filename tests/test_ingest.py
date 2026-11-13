# SPDX-License-Identifier: Apache-2.0

"""The adapter must normalize what a run recorded and refuse the rest.

Its value is not the conversion; it is that the evidence contract then states
the attribution quality mechanically. Host-wide NVFS and an unlabeled cgroup
total do not prove target-device filtering merely by being kernel counters.
"""

# Standard
import copy
import json
import unittest
from pathlib import Path

# First Party
from kvprobe.configuration import ConfigurationError
from kvprobe.ingest import (
    IngestError,
    build_admission,
    build_break_even,
    build_capacity,
    build_document,
    build_envelope,
    classify,
)
from kvprobe.report import assess_evidence

CONFIGURATION = {
    "model": {"root": "/models/m", "revision": "unrecorded", "served_name": "m"},
    "kv_geometry": {"chunk_tokens": 256, "bytes_per_chunk": 67108864, "dtype": "bfloat16"},
    "cache": {"upper_tier_capacity_bytes": 3221225472},
    "runtime": {
            "image_digest": "sha256:" + "a" * 64,
            "device_id": "00000000:01:00.0",
            "gpu_index": 0,
        },
    "measurement": {"contexts": [4096], "concurrency": [1], "samples": 2, "warmup": 1},
    "arms": [
        {
            "name": "posix",
            "backend": "reference",
            "tier_path": "/tier",
            "attribution_provider": "cgroup_blk",
            "staging_capacity_bytes": 0,
            "eviction_policy": "LRU",
            "eviction_parameters": {},
        }
    ],
}
CONFIGURATION["arms"].append(
    CONFIGURATION["arms"][0] | {"name": "gds", "attribution_provider": "nvidia_fs"}
)


def _run(rows: list[dict[str, object]]) -> dict[str, object]:
    """Return a context-run envelope carrying the given rows."""
    return {
        "mode": "context",
        "arm": "posix",
        "arguments": {"contexts": [4096], "samples": 2, "warmup": 1},
        "result": {"4096": {"summary": {"n": len(rows)}, "rows": rows}},
    }


def _row(**overrides: object) -> dict[str, object]:
    """Return a measured row with device bytes, overridden as needed."""
    row = {
        "phase": "measure",
        "index": 0,
        "correct": True,
        "tier_ok": True,
        "nvfs_read_mib": 1024,
        "cgroup_read_bytes": 1073741824,
        "cacheable_tokens": 4096,
    }
    row.update(overrides)
    return row


def _build(rows: list[dict[str, object]]) -> dict[str, object]:
    """Normalize the given rows with the shared configuration."""
    return _document([("run", _run(rows))])


def _document(
    runs: list[tuple[str, dict[str, object]]],
    configuration: dict[str, object] | None = None,
    **kwargs: object,
) -> dict[str, object]:
    """Normalize the given runs with the shared configuration."""
    triples = [(name, run, "b" * 64) for name, run in runs]
    return build_document(
        triples,
        copy.deepcopy(CONFIGURATION if configuration is None else configuration),
        run_id="t",
        **kwargs,
    )


def _concurrency_run(levels: dict[int, list[bool]]) -> dict[str, object]:
    """Return a concurrency run whose batches pass or fail as given."""
    result = {}
    for concurrency, gates in levels.items():
        result[str(concurrency)] = {
            "batches": [
                {
                    "phase": "measure",
                    "tier_ok": ok,
                    "rows": [{"correct": True}] * concurrency,
                }
                for ok in gates
            ]
        }
    return {
        "mode": "concurrency",
        "arm": "gds",
        "arguments": {"concurrency": [1], "warmup": 1},
        "result": result,
    }


class Normalization(unittest.TestCase):
    def test_warmups_are_excluded(self) -> None:
        document = _build([_row(phase="warmup"), _row(index=1)])
        self.assertEqual(len(document["evidence"]["attribution"]["requests"]), 1)

    def test_host_nvfs_counter_is_not_device_filtered(self) -> None:
        run = _run([_row()]) | {"arm": "gds"}
        record = _document([("run", run)])["evidence"]["attribution"]["requests"][0]["attribution"]
        self.assertEqual(record["provider"], "nvidia_fs")
        self.assertEqual(record["confidence"], "corroborating")
        self.assertFalse(record["target_device_filtered"])

    def test_cgroup_total_is_corroborating_and_unfiltered(self) -> None:
        """A run that records no device came from the summing reader: unfiltered."""
        record = _build([_row(nvfs_read_mib=None)])["evidence"]["attribution"]["requests"][0]
        self.assertEqual(record["attribution"]["provider"], "cgroup_blk")
        self.assertEqual(record["attribution"]["confidence"], "corroborating")
        self.assertFalse(record["attribution"]["target_device_filtered"])

    def test_cgroup_counter_for_a_recorded_device_is_filtered(self) -> None:
        """The per-device reader records its device and scope; then it is filtered."""
        for scope in ("whole_disk", "widened_to_parent_disk"):
            with self.subTest(scope=scope):
                run = _run([_row()]) | {
                    "cgroup_io_device": "259:7",
                    "cgroup_io_device_scope": scope,
                }
                record = _document([("run", run)])["evidence"]["attribution"]["requests"][0]
                self.assertEqual(record["attribution"]["confidence"], "corroborating")
                self.assertTrue(record["attribution"]["target_device_filtered"])

    def test_recorded_device_does_not_filter_the_host_wide_counter(self) -> None:
        """nvidia-fs stays host-wide whatever the run records about cgroup."""
        run = _run([_row()]) | {
            "arm": "gds",
            "cgroup_io_device": "259:7",
            "cgroup_io_device_scope": "whole_disk",
        }
        record = _document([("run", run)])["evidence"]["attribution"]["requests"][0]
        self.assertEqual(record["attribution"]["provider"], "nvidia_fs")
        self.assertFalse(record["attribution"]["target_device_filtered"])

    def test_unmeasured_gates_are_reported_as_unsupported(self) -> None:
        evidence = _build([_row()])["evidence"]
        for gate in ("break_even", "admission", "capacity", "envelope"):
            with self.subTest(gate=gate):
                self.assertEqual(evidence[gate]["availability"]["status"], "unsupported")

    def test_output_is_assessable(self) -> None:
        assessment = assess_evidence(_build([_row()]))
        self.assertEqual(assessment["assessment_version"], "0.6.2")
        attribution = next(g for g in assessment["gates"] if g["id"] == 1)
        self.assertEqual(attribution["status"], "UNPROVEN")
        self.assertIsNone(assessment.get("performance"))


class CounterSelection(unittest.TestCase):
    """Unrelated counters must never replace the configured provider."""

    def test_posix_uses_cgroup_even_when_nvfs_is_zero_or_nonzero(self) -> None:
        for nvfs in (0, 2048):
            with self.subTest(nvfs=nvfs):
                request = _build([_row(nvfs_read_mib=nvfs)])["evidence"]["attribution"]["requests"][0]
                self.assertEqual(request["attribution"]["provider"], "cgroup_blk")
                self.assertEqual(request["observed_bytes"], 1073741824)

    def test_gds_uses_nvfs_even_when_cgroup_is_larger(self) -> None:
        run = _run([_row(nvfs_read_mib=512, cgroup_read_bytes=2147483648)])
        run["arm"] = "gds"
        request = _document([("gds", run)])["evidence"]["attribution"]["requests"][0]
        self.assertEqual(request["attribution"]["provider"], "nvidia_fs")
        self.assertEqual(request["observed_bytes"], 536870912)

    def test_missing_selected_counter_does_not_fall_back(self) -> None:
        with self.assertRaises(IngestError):
            _build([_row(cgroup_read_bytes=None)])

    def test_invalid_selected_counters_are_refused(self) -> None:
        for value in (-1, float("nan"), float("inf"), True, "1024"):
            with self.subTest(value=value), self.assertRaises(IngestError):
                _build([_row(cgroup_read_bytes=value)])

    def test_unverified_filtered_boolean_does_not_upgrade_evidence(self) -> None:
        request = _build([_row(target_device_filtered=True)])["evidence"]["attribution"]["requests"][0]
        self.assertFalse(request["attribution"]["target_device_filtered"])

    def test_unknown_provider_does_not_fall_back(self) -> None:
        config = copy.deepcopy(CONFIGURATION)
        config["arms"][0]["attribution_provider"] = "unknown"
        with self.assertRaises(IngestError):
            build_document([("run", _run([_row()]), "b" * 64)], config, run_id="t")


class ConfigurationMatching(unittest.TestCase):
    """Fingerprinting cannot replace checking the recorded arm and metadata."""

    def test_undeclared_arm_is_refused(self) -> None:
        config = copy.deepcopy(CONFIGURATION)
        config["arms"] = [config["arms"][0]]
        run = _run([_row()]) | {"arm": "gds"}
        with self.assertRaises(IngestError):
            build_document([("run", run, "b" * 64)], config, run_id="t")

    def test_missing_arm_is_refused(self) -> None:
        run = _run([_row()])
        del run["arm"]
        with self.assertRaises(IngestError):
            _document([("run", run)])

    def test_argument_and_envelope_arm_must_match(self) -> None:
        run = _run([_row()])
        run["arguments"]["arm"] = "gds"
        with self.assertRaises(IngestError):
            _document([("run", run)])

    def test_recorded_identity_conflicts_are_refused(self) -> None:
        fields = {
            "model_root": "/models/other", "model_revision": "other-revision",
            "served_name": "other-name", "image_digest": "sha256:" + "c" * 64,
            "chunk_tokens": 512, "bytes_per_chunk": 32,
            "dtype": "float16", "upper_tier_capacity_bytes": 1,
            "backend": "other-backend", "tier_path": "/other-tier",
            "attribution_provider": "nvidia_fs", "staging_capacity_bytes": 1,
            "eviction_policy": "FIFO", "eviction_parameters": {"fillers": 99},
            "device_id": "00000000:65:00.0", "gpu_index": 3,
        }
        for field, value in fields.items():
            for location in ("run", "arguments"):
                with self.subTest(field=field, location=location):
                    run = _run([_row()])
                    recorded = run if location == "run" else run["arguments"]
                    recorded[field] = value
                    with self.assertRaises(IngestError):
                        _document([("run", run)])

    def test_matching_recorded_identity_is_accepted(self) -> None:
        run = _run([_row()])
        run["arguments"]["model_root"] = CONFIGURATION["model"]["root"]
        run["attribution_provider"] = "cgroup_blk"
        self.assertEqual(_document([("run", run)])["schema_version"], "0.3.1")

    def test_a_run_from_another_device_is_refused(self) -> None:
        """A configuration cannot claim a device the run did not use.

        Storage counters do not identify which accelerator issued the I/O, so a
        normalized document cannot recover it. Naming the accelerator in the
        configuration is only worth requiring if the run is held to it —
        otherwise the field is a transcription with nothing checking it.
        """
        run = _run([_row()])
        run["device_id"] = "00000000:65:00.0"

        with self.assertRaises(IngestError) as caught:
            _document([("run", run)])

        self.assertIn("device_id", str(caught.exception))

    def test_a_run_that_names_the_same_device_is_accepted(self) -> None:
        """A run recording the device it used must not become harder to ingest."""
        run = _run([_row()])
        run["device_id"] = CONFIGURATION["runtime"]["device_id"]
        run["gpu_index"] = CONFIGURATION["runtime"]["gpu_index"]

        self.assertEqual(_document([("run", run)])["schema_version"], "0.3.1")

    def test_a_run_device_is_not_checked_against_an_unstated_index(self) -> None:
        """An optional field the configuration omits has nothing to contradict.

        A platform may not expose a stable enumeration index. Leaving it out
        must not turn into a silent mismatch against whatever the run recorded.
        """
        config = copy.deepcopy(CONFIGURATION)
        del config["runtime"]["gpu_index"]
        run = _run([_row()])
        run["gpu_index"] = 7
        run["device_id"] = config["runtime"]["device_id"]

        document = _document([("run", run)], configuration=config)

        self.assertEqual(document["schema_version"], "0.3.1")

    def test_recompute_arm_in_arguments_is_not_treated_as_tier(self) -> None:
        config = copy.deepcopy(CONFIGURATION)
        config["arms"].append(config["arms"][0] | {"name": "recompute"})
        run = _run([_row()])
        del run["arm"]
        run["arguments"]["arm"] = "recompute"
        document = build_document([("run", run, "b" * 64)], config, run_id="t")
        self.assertEqual(
            document["evidence"]["attribution"]["availability"]["status"], "unsupported"
        )

    def test_concurrency_batch_count_is_checked(self) -> None:
        run = _concurrency_run({1: [True]})
        run["arguments"]["batches"] = 99
        with self.assertRaises(IngestError):
            _document([("run", run)])


class ExpectedBytes(unittest.TestCase):
    """What a request should have moved comes from geometry, not from itself.

    Taking it from the observation would make every request match its own
    reading, and gate 1 could never catch a short read.
    """

    def test_expected_comes_from_configuration_geometry(self) -> None:
        request = _build([_row()])["evidence"]["attribution"]["requests"][0]
        chunks = 4096 // CONFIGURATION["kv_geometry"]["chunk_tokens"]
        self.assertEqual(
            request["expected_bytes"],
            chunks * CONFIGURATION["kv_geometry"]["bytes_per_chunk"],
        )

    def test_a_short_read_does_not_match_its_expectation(self) -> None:
        request = _build([_row(cgroup_read_bytes=536870912)])["evidence"]["attribution"][
            "requests"
        ][0]
        self.assertLess(request["observed_bytes"], request["expected_bytes"] * 0.9)

    def test_refuses_a_row_that_did_not_record_cacheable_tokens(self) -> None:
        with self.assertRaises(IngestError):
            _build([_row(cacheable_tokens=None)])


class Refusals(unittest.TestCase):
    def test_rejects_an_unknown_run_shape(self) -> None:
        run = _run([_row()]) | {"mode": "something-else"}
        with self.assertRaises(IngestError):
            _document([("run", run)])

    def test_rejects_a_row_with_no_byte_counter(self) -> None:
        with self.assertRaises(IngestError):
            _build([_row(nvfs_read_mib=None, cgroup_read_bytes=None)])

    def test_refuses_to_guess_between_upper_tier_and_recompute(self) -> None:
        with self.assertRaises(IngestError):
            _build([_row(tier_ok=False, nvfs_read_mib=0, cgroup_read_bytes=0)])

    def test_rejects_a_configuration_that_contradicts_the_run(self) -> None:
        configuration = copy.deepcopy(CONFIGURATION)
        configuration["measurement"]["samples"] = 99
        with self.assertRaises(IngestError):
            build_document(
                [("run", _run([_row()]), "b" * 64)], configuration, run_id="t"
            )

    def test_rejects_a_malformed_configuration(self) -> None:
        configuration = copy.deepcopy(CONFIGURATION)
        del configuration["runtime"]
        with self.assertRaises(ConfigurationError):
            build_document(
                [("run", _run([_row()]), "b" * 64)], configuration, run_id="t"
            )


if __name__ == "__main__":
    unittest.main()


class Shapes(unittest.TestCase):
    def test_classify_knows_the_four_run_shapes(self) -> None:
        cases = {
            "context": {"mode": "context"},
            "concurrency": {"mode": "concurrency"},
            "admission": {"gate": "g3_admission_cost"},
            "capacity": {"gate": "capacity_justification"},
        }
        for expected, run in cases.items():
            with self.subTest(kind=expected):
                self.assertEqual(classify(run), expected)

    def test_classify_refuses_an_unknown_shape(self) -> None:
        with self.assertRaises(IngestError):
            classify({"mode": "sweep-of-some-kind"})


class Envelope(unittest.TestCase):
    def test_a_failed_batch_contributes_no_served_requests(self) -> None:
        """A batch that failed its gate cannot be split into served and not."""
        block = build_envelope(_concurrency_run({4: [True, True], 8: [False, False]}))
        levels = {level["concurrency"]: level for level in block["levels"]}
        self.assertEqual(levels[4]["target_tier_served"], levels[4]["requests"])
        self.assertEqual(levels[8]["target_tier_served"], 0)
        self.assertEqual(levels[8]["requests"], 16)

    def test_an_incorrect_response_withholds_its_batch(self) -> None:
        """Bytes alone do not serve a batch; every response must also be correct."""
        run = _concurrency_run({1: [True, True], 2: [True, True]})
        run["result"]["2"]["batches"][1]["rows"] = [{"correct": True}, {"correct": False}]
        levels = {level["concurrency"]: level for level in build_envelope(run)["levels"]}
        self.assertEqual(levels[1]["target_tier_served"], 2)
        self.assertEqual(levels[2]["requests"], 4)
        self.assertEqual(levels[2]["target_tier_served"], 2)

    def test_a_row_without_recorded_correctness_counts_as_incorrect(self) -> None:
        run = _concurrency_run({1: [True]})
        run["result"]["1"]["batches"][0]["rows"] = [{}]
        block = build_envelope(run)
        self.assertEqual(block["levels"][0]["target_tier_served"], 0)

    def test_records_the_per_request_input_size(self) -> None:
        """The envelope holds for one input size, so the block names it."""
        run = _concurrency_run({1: [True], 2: [True]})
        for block in run["result"].values():
            for batch in block["batches"]:
                batch["rows"] = [
                    {"correct": True, "prompt": {"cacheable_tokens": 2048}}
                    for _ in batch["rows"]
                ]
        self.assertEqual(build_envelope(run)["request_cacheable_tokens"], 2048)

    def test_omits_the_input_size_when_rows_do_not_record_it(self) -> None:
        """An older run that never recorded the size gets no invented one."""
        self.assertNotIn(
            "request_cacheable_tokens",
            build_envelope(_concurrency_run({1: [True], 2: [True]})),
        )

    def test_omits_the_input_size_when_only_some_rows_record_it(self) -> None:
        run = _concurrency_run({1: [True], 2: [True]})
        run["result"]["2"]["batches"][0]["rows"] = [
            {"correct": True, "prompt": {"cacheable_tokens": 2048}},
            {"correct": True},
        ]
        self.assertNotIn("request_cacheable_tokens", build_envelope(run))

    def test_refuses_a_run_that_mixes_input_sizes(self) -> None:
        """One envelope cannot describe two input sizes."""
        run = _concurrency_run({1: [True], 2: [True]})
        run["result"]["1"]["batches"][0]["rows"] = [
            {"correct": True, "prompt": {"cacheable_tokens": 2048}}
        ]
        run["result"]["2"]["batches"][0]["rows"] = [
            {"correct": True, "prompt": {"cacheable_tokens": 4096}}
        ] * 2
        with self.assertRaisesRegex(IngestError, "mixes per-request input sizes"):
            build_envelope(run)

    def test_rejects_a_level_with_no_measured_batches(self) -> None:
        run = _concurrency_run({1: [True]})
        run["result"]["1"]["batches"][0]["phase"] = "warmup"
        with self.assertRaises(IngestError):
            build_envelope(run)


class BreakEven(unittest.TestCase):
    def _context(self, arm: str, medians: dict[str, float]) -> dict[str, object]:
        return {
            "mode": "context",
            "arm": arm,
            "result": {
                key: {"summary": {"ttft_p50_s": value}} for key, value in medians.items()
            },
        }

    def test_pairs_the_arms_by_context(self) -> None:
        block = build_break_even(
            self._context("gds", {"256": 0.1, "4096": 0.5}),
            self._context("recompute", {"256": 0.3, "4096": 2.0}),
        )
        self.assertEqual(block["retrieval_ttft_s"], [0.1, 0.5])
        self.assertEqual(block["recompute_ttft_s"], [0.3, 2.0])

    def test_refuses_arms_that_cover_different_contexts(self) -> None:
        with self.assertRaises(IngestError):
            build_break_even(
                self._context("gds", {"256": 0.1}),
                self._context("recompute", {"4096": 2.0}),
            )


class AdmissionAndCapacity(unittest.TestCase):
    def test_admission_includes_every_reuse_outcome(self) -> None:
        """Fallbacks reduce realized saving instead of disappearing."""
        run = {
            "gate": "g3_admission_cost",
            "rows": [
                {
                    "attempts": [
                        {
                            "admission_cost_s": 0.2,
                            "realized_reuse_saving_s": 0.4,
                            "outcome": "target_tier",
                        },
                        {
                            "admission_cost_s": 0.4,
                            "realized_reuse_saving_s": -0.2,
                            "outcome": "fallback",
                        },
                    ]
                }
            ],
        }
        block = build_admission(run, expected_reuses=3)
        self.assertAlmostEqual(block["store_cost_s"], 0.3)
        self.assertAlmostEqual(block["realized_reuse_saving_s"], 0.1)
        self.assertEqual(block["calculation_basis"], "all_attempts")
        self.assertEqual(
            block["attempt_counts"],
            {
                "attempted": 2,
                "target_tier": 1,
                "fallback": 1,
                "store_unattributed": 0,
                "unattributed": 0,
            },
        )

    def test_admission_refuses_a_row_missing_requested_attempts(self) -> None:
        """A lost attempt would silently shrink the repayment sample."""
        attempt = {
            "admission_cost_s": 0.2,
            "realized_reuse_saving_s": 0.4,
            "outcome": "target_tier",
        }
        run = {"arguments": {"samples": 2}, "rows": [{"attempts": [attempt]}]}
        with self.assertRaisesRegex(IngestError, "holds 1 attempts but the run requested 2"):
            build_admission(run, expected_reuses=1)
        run["rows"][0]["attempts"].append(dict(attempt))
        self.assertEqual(
            build_admission(run, expected_reuses=1)["attempt_counts"]["attempted"], 2
        )

    def test_admission_refuses_mixed_attempt_contracts(self) -> None:
        run = {
            "rows": [
                {
                    "attempts": [
                        {
                            "admission_cost_s": 0.2,
                            "realized_reuse_saving_s": 0.1,
                            "outcome": "target_tier",
                        }
                    ]
                },
                {"g3_admission_cost_s": 0.4},
            ]
        }
        with self.assertRaisesRegex(IngestError, "mixes all-attempt"):
            build_admission(run, expected_reuses=3)

    def test_admission_takes_the_median_measured_cost(self) -> None:
        run = {
            "gate": "g3_admission_cost",
            "rows": [
                {"g3_admission_cost_s": 0.2},
                {"g3_admission_cost_s": 0.4},
                {"g3_admission_cost_s": None},
            ],
        }
        block = build_admission(run, expected_reuses=3)
        self.assertAlmostEqual(block["store_cost_s"], 0.3)
        self.assertEqual(block["expected_reuses"], 3.0)

    def test_admission_refuses_a_run_where_every_row_was_dropped(self) -> None:
        with self.assertRaises(IngestError):
            build_admission({"gate": "g3_admission_cost", "rows": [{}]}, 1)

    def test_expected_reuses_must_come_from_the_caller(self) -> None:
        """The workload's reuse rate is not something a run can measure."""
        run = {"gate": "g3_admission_cost", "rows": [{"g3_admission_cost_s": 0.2}]}
        with self.assertRaises(IngestError):
            _document([("admission", run)])

    def test_capacity_reads_producer_rows_and_device_service(self) -> None:
        run = {
            "gate": "capacity_justification",
            "crossover_working_set": 4,
            "rows": [
                {
                    "working_set": 2,
                    "resident_mib": 128,
                    "dram_hits": 2,
                    "nvme_hits": 0,
                    "fallback": 0,
                    "miss": 0,
                },
                {
                    "working_set": 4,
                    "resident_mib": 256,
                    "dram_hits": 1,
                    "nvme_hits": 3,
                    "fallback": 0,
                    "miss": 0,
                },
            ],
        }
        block = build_capacity(run, upper_tier_capacity_bytes=128 * 1024 * 1024)
        self.assertTrue(block["upper_tier_overflow_observed"])
        self.assertTrue(block["target_tier_served"])
        self.assertEqual(block["classification_counts"]["retrievals_expected"], 6)
        self.assertEqual(
            block["overflow_classification_counts"]["target_tier_served"], 3
        )

    def test_capacity_without_a_crossover_reports_no_overflow(self) -> None:
        run = {
            "gate": "capacity_justification",
            "crossover_working_set": None,
            "rows": [
                {
                    "working_set": 2,
                    "resident_mib": 128,
                    "dram_hits": 2,
                    "nvme_hits": 0,
                    "fallback": 0,
                    "miss": 0,
                }
            ],
        }
        block = build_capacity(run, upper_tier_capacity_bytes=256 * 1024 * 1024)
        self.assertFalse(block["upper_tier_overflow_observed"])
        self.assertFalse(block["target_tier_served"])

    def test_capacity_detects_overflow_without_target_service(self) -> None:
        """Declared capacity, not an NVMe hit, establishes overflow."""
        run = {
            "gate": "capacity_justification",
            "crossover_working_set": None,
            "rows": [
                {
                    "working_set": 4,
                    "resident_mib": 256,
                    "dram_hits": 2,
                    "nvme_hits": 0,
                    "fallback": 1,
                    "miss": 1,
                }
            ],
        }
        block = build_capacity(run, upper_tier_capacity_bytes=128 * 1024 * 1024)
        self.assertTrue(block["upper_tier_overflow_observed"])
        self.assertFalse(block["target_tier_served"])

    def test_capacity_refuses_incomplete_classification(self) -> None:
        """Every retrieval in a working set must land in exactly one bucket."""
        run = {
            "gate": "capacity_justification",
            "rows": [
                {
                    "working_set": 4,
                    "resident_mib": 256,
                    "dram_hits": 1,
                    "nvme_hits": 1,
                    "fallback": 0,
                    "miss": 0,
                }
            ],
        }
        with self.assertRaisesRegex(IngestError, "classified 2 retrievals"):
            build_capacity(run, upper_tier_capacity_bytes=128 * 1024 * 1024)

    def test_capacity_producer_shape_flows_through_report(self) -> None:
        """The real flat producer fields survive ingest and support Gate 4."""
        run = {
            "gate": "capacity_justification",
            "crossover_working_set": 4,
            "rows": [
                {
                    "working_set": 4,
                    "resident_mib": 4096,
                    "dram_hits": 3,
                    "nvme_hits": 1,
                    "fallback": 0,
                    "miss": 0,
                }
            ],
        }
        document = _document([("capacity", run)])
        assessment = assess_evidence(document)
        gate = next(item for item in assessment["gates"] if item["id"] == 4)
        self.assertEqual(gate["status"], "PASS")
        self.assertTrue(document["evidence"]["capacity"]["target_tier_served"])


class Merging(unittest.TestCase):
    def test_gates_from_several_runs_land_in_one_document(self) -> None:
        document = _document(
            [
                ("ctx", _run([_row()])),
                ("conc", _concurrency_run({1: [True]})),
            ]
        )
        evidence = document["evidence"]
        self.assertEqual(evidence["attribution"]["availability"]["status"], "supported")
        self.assertEqual(evidence["envelope"]["availability"]["status"], "supported")
        self.assertEqual(len(document["bindings"]["envelope_levels"]), 1)

    def test_refuses_two_runs_of_the_same_kind(self) -> None:
        run = _concurrency_run({1: [True]})
        with self.assertRaises(IngestError):
            _document([("a", run), ("b", copy.deepcopy(run))])

    def test_unmeasured_gates_are_bound_to_nothing(self) -> None:
        bindings = _document([("ctx", _run([_row()]))])["bindings"]
        self.assertIsNone(bindings["capacity"])
        self.assertEqual(bindings["envelope_levels"], [])


class AdmissionSampleCounts(unittest.TestCase):
    """The admission block keeps how many stores its cost rests on."""

    def test_sample_counts_come_from_stored_and_dropped(self) -> None:
        from kvprobe.ingest import build_admission

        run = {
            "rows": [
                {"g3_admission_cost_s": 0.06, "stored": 3, "dropped_store": 7},
                {"g3_admission_cost_s": 0.08, "stored": 10, "dropped_store": 0},
            ]
        }
        block = build_admission(run, 1)
        self.assertEqual(block["sample_counts"], {"requested": 20, "valid": 13, "dropped": 7})

    def test_older_runs_without_counts_stay_readable(self) -> None:
        from kvprobe.ingest import build_admission

        block = build_admission({"rows": [{"g3_admission_cost_s": 0.06}]}, 1)
        self.assertNotIn("sample_counts", block)
