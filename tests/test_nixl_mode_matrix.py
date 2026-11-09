# SPDX-License-Identifier: Apache-2.0
"""The NIXL matrix runner must preserve mode and plug-in boundaries."""

# Standard
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
MATRIX = (ROOT / "scripts" / "tools" / "nixl_mode_matrix.sh").read_text(
    encoding="utf-8"
)
HISTORICAL = (ROOT / "scripts" / "tools" / "nixl_matched.sh").read_text(
    encoding="utf-8"
)


class NixlModeMatrixContractTest(unittest.TestCase):
    """Hold the experiment's critical configuration choices in CI."""

    def test_dynamic_mode_selects_zero_pool(self) -> None:
        self.assertIn("dynamic) NIXL_POOL_SIZE=0", MATRIX)

    def test_gds_mt_is_the_primary_default(self) -> None:
        self.assertIn("NIXL_BACKEND=${NIXL_BACKEND:-GDS_MT}", MATRIX)
        self.assertIn("NIXL_BACKEND=${NIXL_BACKEND:-GDS_MT}", HISTORICAL)

    def test_single_threaded_gds_is_smoke_only(self) -> None:
        self.assertIn('[[ "$NIXL_BACKEND" != GDS ]]', MATRIX)
        self.assertIn("NIXL GDS is smoke-only", MATRIX)

    def test_presence_cache_is_explicit_and_disabled_by_default(self) -> None:
        self.assertIn(
            "NIXL_PRESENCE_CACHE=${NIXL_PRESENCE_CACHE:-false}", MATRIX
        )
        self.assertIn('"nixl_presence_cache":%s', MATRIX)

    def test_direct_io_default_matches_the_io_family(self) -> None:
        self.assertIn("NIXL_USE_DIRECT_IO=${NIXL_USE_DIRECT_IO:-false}", MATRIX)
        self.assertIn("NIXL_USE_DIRECT_IO=${NIXL_USE_DIRECT_IO:-true}", MATRIX)

    def test_raw_evidence_records_exact_mode_plugin_identity(self) -> None:
        self.assertIn('ARM_ID="nixl_${NIXL_STORAGE_MODE}_${BACKEND_LOWER}"', MATRIX)
        self.assertIn('--backend "$BACKEND_ID"', MATRIX)
        self.assertIn('--attribution "$ATTRIBUTION"', MATRIX)

    def test_matrix_reuses_a_bounded_filler_set(self) -> None:
        self.assertIn("--filler-key-policy reuse", MATRIX)


if __name__ == "__main__":
    unittest.main()
