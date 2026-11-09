# SPDX-License-Identifier: Apache-2.0
"""Tests for policy-free static-pool capacity calculations."""

# Standard
import unittest

# First Party
from kvprobe.pool_sizing import (
    PoolSizingError,
    bracket_pool_slots,
    calculate_pool_plan,
    derive_required_live_chunks,
)


class PoolSizingTest(unittest.TestCase):
    """Hold pool arithmetic and terminology stable."""

    def test_forced_eviction_experiment_is_below_the_live_set(self) -> None:
        """Thirty-six live chunks cannot fit in sixteen static slots."""
        plan = calculate_pool_plan(16, 67_108_864, 36)

        self.assertEqual(plan["logical_capacity_bytes"], 1_073_741_824)
        self.assertEqual(plan["required_live_bytes"], 2_415_919_104)
        self.assertEqual(plan["slot_headroom"], -20)
        self.assertEqual(plan["forced_evictions_lower_bound"], 20)
        self.assertEqual(plan["classification"], "below_live_set")

    def test_large_measured_pool_reports_headroom_without_recommending_it(self) -> None:
        """A roomy pool is described but never labeled optimal."""
        plan = calculate_pool_plan(512, 67_108_864, 16)

        self.assertEqual(plan["logical_capacity_bytes"], 34_359_738_368)
        self.assertEqual(plan["slot_headroom"], 496)
        self.assertEqual(plan["forced_evictions_lower_bound"], 0)
        self.assertEqual(plan["classification"], "above_live_set")
        self.assertNotIn("recommended", plan)
        self.assertNotIn("optimal", plan)

    def test_at_live_set_is_distinct_from_above_live_set(self) -> None:
        """An exact fit must not imply capacity margin."""
        plan = calculate_pool_plan(64, 1, 64)

        self.assertEqual(plan["classification"], "at_live_set")
        self.assertEqual(plan["slot_headroom"], 0)

    def test_rejects_boolean_and_non_positive_inputs(self) -> None:
        """Python booleans and zero are not valid capacities."""
        with self.assertRaisesRegex(PoolSizingError, "pool_slots"):
            calculate_pool_plan(True, 1, 1)
        with self.assertRaisesRegex(PoolSizingError, "bytes_per_chunk"):
            calculate_pool_plan(1, 0, 1)
        with self.assertRaisesRegex(PoolSizingError, "required_live_chunks"):
            calculate_pool_plan(1, 1, -1)


    def test_live_chunks_derive_from_contexts_and_chunk_geometry(self) -> None:
        """Three 4,096-token contexts of 256-token chunks need 48 live chunks."""
        derivation = derive_required_live_chunks(3, 4096, 256)

        self.assertEqual(derivation["chunks_per_context"], 16)
        self.assertEqual(derivation["required_live_chunks"], 48)
        self.assertEqual(derivation["partial_chunk"], "not_applicable")
        self.assertIn("no prefix shared", derivation["assumption"])

    def test_a_trailing_partial_chunk_needs_an_explicit_policy(self) -> None:
        """The backend's partial-chunk behavior is never assumed."""
        with self.assertRaisesRegex(PoolSizingError, "partial_chunk"):
            derive_required_live_chunks(1, 4100, 256)
        dropped = derive_required_live_chunks(2, 4100, 256, "drop")
        kept = derive_required_live_chunks(2, 4100, 256, "keep")
        self.assertEqual(dropped["required_live_chunks"], 32)
        self.assertEqual(kept["required_live_chunks"], 34)
        with self.assertRaisesRegex(PoolSizingError, "shorter than one chunk"):
            derive_required_live_chunks(1, 100, 256, "drop")
        with self.assertRaisesRegex(PoolSizingError, "'drop' or 'keep'"):
            derive_required_live_chunks(1, 4100, 256, "round")

    def test_live_chunk_derivation_rejects_invalid_inputs(self) -> None:
        """Booleans, zero and negatives are not valid counts."""
        for arguments, name in (
            ((True, 4096, 256), "concurrent_contexts"),
            ((1, 0, 256), "context_tokens"),
            ((1, 4096, -1), "chunk_tokens"),
        ):
            with self.subTest(name=name):
                with self.assertRaisesRegex(PoolSizingError, name):
                    derive_required_live_chunks(*arguments)

    def test_bracket_spans_the_live_set_without_choosing_a_value(self) -> None:
        """Values straddle the live set and carry no recommendation."""
        self.assertEqual(bracket_pool_slots(48), [24, 48, 96, 192])
        self.assertEqual(bracket_pool_slots(36), [18, 36, 72, 144])
        # An odd live set rounds the lower value up, still below the live set.
        self.assertEqual(bracket_pool_slots(5), [3, 5, 10, 20])
        # One live chunk has nothing below it; the values collapse honestly.
        self.assertEqual(bracket_pool_slots(1), [1, 2, 4])
        with self.assertRaisesRegex(PoolSizingError, "required_live_chunks"):
            bracket_pool_slots(0)


if __name__ == "__main__":
    unittest.main()
