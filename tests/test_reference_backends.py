# SPDX-License-Identifier: Apache-2.0
"""Bundled runner and documentation backend names must not drift."""

# Standard
import json
import re
import unittest
from pathlib import Path

# First Party
from kvprobe.reference_backends import REFERENCE_LMCACHE_BACKEND_IDS


ROOT = Path(__file__).resolve().parents[1]
BACKEND_PATTERN = re.compile(r"lmcache_[a-z0-9_]+")


def _text(path: str) -> str:
    """Return one repository file as UTF-8 text.

    Args:
        path: Repository-relative path.

    Returns:
        File contents.
    """
    return (ROOT / path).read_text(encoding="utf-8")


class ReferenceBackendContractTests(unittest.TestCase):
    """Keep every bundled LMCache backend view aligned with the registry."""

    def test_registry_values_are_unique_and_self_describing(self) -> None:
        values = list(REFERENCE_LMCACHE_BACKEND_IDS.values())
        self.assertEqual(len(values), len(set(values)))
        for key, value in REFERENCE_LMCACHE_BACKEND_IDS.items():
            self.assertEqual(value, f"lmcache_{key}")

    def test_identity_reference_table_matches_registry(self) -> None:
        text = _text("docs/reference/CONFIGURATION-IDENTITY.md")
        section = text.split("### Backend identity for NIXL storage modes", 1)[1]
        section = section.split("\nThe raw run must record", 1)[0]
        self.assertEqual(
            BACKEND_PATTERN.findall(section),
            list(REFERENCE_LMCACHE_BACKEND_IDS.values()),
        )

    def test_nixl_matrix_is_a_complete_registry_view(self) -> None:
        text = _text("docs/NIXL-MODE-MATRIX.md")
        section = text.split("## Matrix", 1)[1].split(
            "## Why NIXL `GDS` is not in the primary matrix", 1
        )[0]
        self.assertEqual(
            set(BACKEND_PATTERN.findall(section)),
            set(REFERENCE_LMCACHE_BACKEND_IDS.values()),
        )

    def test_native_runner_uses_registered_ids(self) -> None:
        text = _text("scripts/g5_controlled_kv_experiment.sh")
        section = text.split("backend_id_for_arm()", 1)[1].split("\n}\n", 1)[0]
        self.assertEqual(
            set(BACKEND_PATTERN.findall(section)),
            {
                REFERENCE_LMCACHE_BACKEND_IDS["native_posix"],
                REFERENCE_LMCACHE_BACKEND_IDS["native_gds"],
            },
        )

    def test_workflow_example_names_native_gds(self) -> None:
        text = _text("docs/CONFIGURATION.md")
        expected = REFERENCE_LMCACHE_BACKEND_IDS["native_gds"]
        self.assertIn(f"native cuFile GDS arm records `{expected}`", text)

    def test_filtered_counter_example_names_native_gds(self) -> None:
        evidence = json.loads(
            _text("examples/evidence/gate1-pass-filtered-counter.json")
        )
        arms = evidence["configurations"][0]["parameters"]["arms"]
        gds = next(arm for arm in arms if arm["name"] == "gds")
        self.assertEqual(
            gds["backend"], REFERENCE_LMCACHE_BACKEND_IDS["native_gds"]
        )

    def test_client_help_example_names_dynamic_gds_mt(self) -> None:
        text = _text("scripts/g5_controlled_kv_experiment.py")
        expected = REFERENCE_LMCACHE_BACKEND_IDS["nixl_dynamic_gds_mt"]
        self.assertIn(f"example {expected}.", text)

    def test_nixl_runner_derivation_matches_registered_ids(self) -> None:
        text = _text("scripts/tools/nixl_mode_matrix.sh")
        self.assertIn('BACKEND_ID="lmcache_${ARM_ID}"', text)
        derived = {
            f"lmcache_nixl_{mode}_{io}"
            for mode in ("static", "dynamic")
            for io in ("posix", "gds_mt")
        }
        registered = {
            value
            for key, value in REFERENCE_LMCACHE_BACKEND_IDS.items()
            if key.startswith("nixl_")
        }
        self.assertEqual(derived, registered)

    def test_bundled_surfaces_use_only_registered_lmcache_ids(self) -> None:
        paths = (
            "docs/CONFIGURATION.md",
            "docs/NIXL-MODE-MATRIX.md",
            "docs/NIXL-STATIC-GDS-MATCHED.md",
            "docs/reference/CONFIGURATION-IDENTITY.md",
            "scripts/README.md",
            "scripts/g5_controlled_kv_experiment.py",
            "scripts/g5_controlled_kv_experiment.sh",
            "examples/evidence/gate1-pass-filtered-counter.json",
        )
        observed = {
            value
            for path in paths
            for value in BACKEND_PATTERN.findall(_text(path))
        }
        self.assertFalse(
            observed - set(REFERENCE_LMCACHE_BACKEND_IDS.values()),
            f"unregistered LMCache backend IDs: {sorted(observed)}",
        )


if __name__ == "__main__":
    unittest.main()
