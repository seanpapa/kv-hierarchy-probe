# SPDX-License-Identifier: Apache-2.0
"""Gate 3 producer retains every attempted store/reuse pair."""

# Standard
import importlib.util
import json
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "g3_admission_cost.py"
SPEC = importlib.util.spec_from_file_location("g3_admission_cost", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
CLIENT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CLIENT)


class AdmissionAttemptRetentionTest(unittest.TestCase):
    """Counter outcomes classify samples but never drop their timings."""

    def test_fallback_is_retained_beside_target_tier_hit(self) -> None:
        payload = {"messages": [{"content": "payload"}]}
        streams = [
            {"ttft_s": 0.5},
            {"ttft_s": 0.2},
            {"ttft_s": 0.6},
            {"ttft_s": 0.8},
        ]
        counters = [0, 64, 64, 128, 128, 192, 192, 192]
        with patch.object(CLIENT, "stream_ttft", side_effect=streams), patch.object(
            CLIENT, "nvfs_counter", side_effect=counters
        ), patch.object(CLIENT, "evict"):
            result = CLIENT.measure_store_and_retrieve(
                8102, payload, "entry", 64, 2, []
            )

        self.assertEqual(len(result["attempts"]), 2)
        self.assertEqual(
            [attempt["outcome"] for attempt in result["attempts"]],
            ["target_tier", "fallback"],
        )
        self.assertEqual(
            result["attempt_counts"],
            {
                "attempted": 2,
                "target_tier": 1,
                "fallback": 1,
                "store_unattributed": 0,
                "unattributed": 0,
            },
        )

    def test_missing_counters_remain_as_unattributed_attempt(self) -> None:
        payload = {"messages": [{"content": "payload"}]}
        with patch.object(
            CLIENT,
            "stream_ttft",
            side_effect=[{"ttft_s": 0.5}, {"ttft_s": 0.8}],
        ), patch.object(CLIENT, "nvfs_counter", return_value=-1), patch.object(
            CLIENT, "evict"
        ):
            result = CLIENT.measure_store_and_retrieve(
                8102, payload, "entry", 64, 1, []
            )

        self.assertEqual(len(result["attempts"]), 1)
        self.assertEqual(result["attempts"][0]["outcome"], "unattributed")
        self.assertEqual(result["attempt_counts"]["unattributed"], 1)

    def test_gds_warmup_pair_is_not_retained(self) -> None:
        """First-use initialization is excluded symmetrically on both arms."""
        payload = {"messages": [{"content": "payload"}]}
        streams = [
            {"ttft_s": 28.0},
            {"ttft_s": 0.2},
            {"ttft_s": 0.5},
            {"ttft_s": 0.1},
        ]
        with patch.object(CLIENT, "stream_ttft", side_effect=streams), patch.object(
            CLIENT, "nvfs_counter", side_effect=[0, 64, 64, 128]
        ), patch.object(CLIENT, "evict"):
            result = CLIENT.measure_store_and_retrieve(
                8102, payload, "entry", 64, 1, [], warmup=1
            )

        self.assertEqual(len(result["attempts"]), 1)
        self.assertEqual(result["attempts"][0]["store_ttft_s"], 0.5)
        self.assertEqual(result["attempts"][0]["reuse_ttft_s"], 0.1)

    def test_non_target_speedup_is_not_credited_to_target_admission(self) -> None:
        """Upper-tier speed cannot repay admission into the lower tier."""
        self.assertAlmostEqual(
            CLIENT.attributable_reuse_saving(0.3, 0.1, "target_tier"), 0.2
        )
        self.assertEqual(
            CLIENT.attributable_reuse_saving(0.3, 0.1, "fallback"), 0.0
        )
        self.assertAlmostEqual(
            CLIENT.attributable_reuse_saving(0.3, 0.4, "fallback"), -0.1
        )

    def test_main_applies_attributable_saving_to_raw_evidence(self) -> None:
        """The executable aggregation path must call the helper without shadowing it."""
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "admission.json"
            args = Namespace(
                gds_port=8102,
                recompute_port=8100,
                output=output,
                contexts=[256],
                samples=1,
                warmup=0,
                fillers=0,
                filler_tokens=8192,
            )
            measured = {
                "store_ttft_s": 0.35,
                "retrieve_ttft_s": 0.10,
                "attempts": [
                    {
                        "store_ttft_s": 0.35,
                        "reuse_ttft_s": 0.10,
                        "outcome": "fallback",
                    }
                ],
                "attempt_counts": {
                    "attempted": 1,
                    "target_tier": 0,
                    "fallback": 1,
                    "store_unattributed": 0,
                    "unattributed": 0,
                },
                "stored": 1,
                "retrieved": 0,
                "dropped_store": 0,
                "dropped_retrieve": 1,
            }
            with patch.object(CLIENT, "parse_args", return_value=args), patch.object(
                CLIENT,
                "build_payload",
                return_value=({"messages": [{"content": "payload"}]}, 256),
            ), patch.object(
                CLIENT, "measure_recompute", return_value=0.30
            ), patch.object(
                CLIENT, "measure_store_and_retrieve", return_value=measured
            ), patch.object(
                CLIENT, "gpu_identity", return_value={}
            ), patch.object(
                CLIENT, "report_device_identity"
            ):
                CLIENT.main()

            row = json.loads(output.read_text())["rows"][0]
            self.assertEqual(row["realized_reuse_saving_s"], 0.0)
            self.assertAlmostEqual(row["attempts"][0]["raw_reuse_delta_s"], 0.2)


if __name__ == "__main__":
    unittest.main()
