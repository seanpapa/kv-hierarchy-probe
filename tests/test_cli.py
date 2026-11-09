# SPDX-License-Identifier: Apache-2.0
"""Public CLI contract tests that require neither a GPU nor Docker."""

# Standard
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

# First Party
from kvprobe.doctor import build_manifest, render_manifest

ROOT = Path(__file__).parents[1]
EXAMPLES = ROOT / "examples" / "evidence"


def _run(*arguments: str) -> subprocess.CompletedProcess[str]:
    """Run kvprobe from the repository root.

    Args:
        arguments: CLI arguments after ``python -m kvprobe``.

    Returns:
        Completed subprocess result.
    """
    return subprocess.run(
        [sys.executable, "-m", "kvprobe", *arguments],
        cwd=ROOT,
        capture_output=True,
        check=False,
        text=True,
    )


class CliTest(unittest.TestCase):
    """Verify the dependency-free first-visit path."""

    def test_doctor_writes_manifest_without_requiring_gpu(self) -> None:
        """Doctor must distinguish missing capability rather than fail."""
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "doctor.json"
            result = _run("doctor", "--json-output", str(output))

            self.assertEqual(result.returncode, 0, result.stderr)
            manifest = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(manifest["manifest_version"], "0.2.1")
            self.assertIn("gpu_memory_usage_query", manifest["capabilities"])
            self.assertIn("measurement_ready", manifest["current_adapter"])
            self.assertIn("route_status", manifest["current_adapter"])
            self.assertEqual(
                manifest["current_adapter"]["measurement_ready_scope"],
                "host_prerequisites_only",
            )
            self.assertEqual(
                manifest["current_adapter"]["serving_verification"],
                "not_assessed",
            )
            self.assertIn("multi_gpu", manifest)

    def test_doctor_keeps_gb10_when_memory_usage_query_is_unsupported(self) -> None:
        """DGX Spark must not become CPU-only when one telemetry field is absent."""
        command_results = [
            subprocess.CompletedProcess(
                args=["nvidia-smi"],
                returncode=0,
                stdout="NVIDIA GB10\n",
                stderr="",
            ),
            subprocess.CompletedProcess(
                args=["nvidia-smi"],
                returncode=0,
                stdout="Not Supported\n",
                stderr="",
            ),
        ]

        def executable(name: str) -> str | None:
            """Expose only the commands relevant to this simulated host."""
            return "/usr/bin/nvidia-smi" if name == "nvidia-smi" else None

        with (
            patch("kvprobe.doctor.shutil.which", side_effect=executable),
            patch("kvprobe.doctor.subprocess.run", side_effect=command_results),
        ):
            manifest = build_manifest()

        self.assertTrue(manifest["gpu"]["present"])
        self.assertEqual(manifest["memory_architecture"]["kind"], "unified")
        self.assertEqual(
            manifest["capabilities"]["gpu_memory_usage_query"]["status"],
            "unsupported",
        )
        self.assertEqual(
            manifest["multi_gpu"]["topology"]["status"],
            "not_applicable",
        )

    def test_doctor_separates_multi_gpu_topology_from_runtime_path(self) -> None:
        """Host links must not be presented as observed TP communication."""
        topology = """\
        GPU0    GPU1    CPU Affinity
GPU0     X      SYS     0-31
GPU1    SYS      X      32-63
"""
        command_results = [
            subprocess.CompletedProcess(
                args=["nvidia-smi"],
                returncode=0,
                stdout="GPU A\nGPU B\n",
                stderr="",
            ),
            subprocess.CompletedProcess(
                args=["nvidia-smi"],
                returncode=0,
                stdout="1024\n1024\n",
                stderr="",
            ),
            subprocess.CompletedProcess(
                args=["nvidia-smi"],
                returncode=0,
                stdout=topology,
                stderr="",
            ),
        ]

        def executable(name: str) -> str | None:
            """Expose only NVIDIA discovery for this simulated host."""
            return "/usr/bin/nvidia-smi" if name == "nvidia-smi" else None

        with (
            patch("kvprobe.doctor.shutil.which", side_effect=executable),
            patch("kvprobe.doctor.subprocess.run", side_effect=command_results),
        ):
            manifest = build_manifest()

        self.assertEqual(manifest["multi_gpu"]["gpu_count"], 2)
        self.assertEqual(
            manifest["multi_gpu"]["topology"]["links"],
            [{"left": "GPU0", "right": "GPU1", "connection": "SYS"}],
        )
        self.assertEqual(
            manifest["multi_gpu"]["runtime_collective"]["status"],
            "not_observed",
        )
        rendered = render_manifest(manifest)
        self.assertIn("inter-GPU topology: available (SYS=1 pair)", rendered)
        self.assertIn("runtime collective: not_observed", rendered)

    def test_doctor_finds_gdscheck_outside_path(self) -> None:
        """A toolkit-installed gdscheck must not be reported as absent."""
        toolkit_path = "/usr/local/cuda-13.3/gds/tools/gdscheck"
        with patch("kvprobe.doctor._find_gdscheck", return_value=toolkit_path):
            manifest = build_manifest()

        capability = manifest["capabilities"]["gds"]
        self.assertEqual(capability["status"], "not_tested")
        self.assertIn(toolkit_path, capability["detail"])

    def test_demo_matches_all_expected_verdicts(self) -> None:
        """Bundled examples must remain executable golden cases."""
        result = _run("demo")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.count("[PASS]"), 4)
        self.assertEqual(result.stdout.count("       check:   "), 4)
        self.assertEqual(result.stdout.count("       result:  "), 4)
        self.assertNotIn("MISMATCH", result.stdout)

    def test_first_visit_routes_without_claiming_a_hardware_pass(self) -> None:
        """First visit must finish on CPU-only hosts without faking Gate 1."""
        result = _run("first-visit")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("STEP 1/4 — See the five decisions", result.stdout)
        self.assertIn("Gate 2 · BENEFIT", result.stdout)
        self.assertIn("Gate 3 · ECONOMICS", result.stdout)
        self.assertIn("STEP 2/4 — Explore one end-to-end result", result.stdout)
        self.assertIn("SYNTHETIC EXAMPLE", result.stdout)
        self.assertIn("Reporter self-check: 4/4", result.stdout)
        self.assertIn("WHY GATE 1 MATTERS", result.stdout)
        self.assertIn("phantom tier", result.stdout)
        self.assertIn("reporter self-check: PASS", result.stdout)
        self.assertIn("hardware measurement: NOT_RUN", result.stdout)
        self.assertIn(
            "Gate 1 serving-tier verification: NOT_ESTABLISHED",
            result.stdout,
        )
        # The gate list appears once, in STEP 1, instead of being repeated.
        self.assertNotIn("WHAT GATE 1 UNLOCKS", result.stdout)
        self.assertIn("NEXT CHECKPOINT", result.stdout)
        self.assertIn("continue with docs/GATE1.md", result.stdout)
        self.assertNotIn("Next: docs/GATE1.md", result.stdout)
        self.assertIn("rerun python3 -m kvprobe geometry", result.stdout)
        # Started with -m, every suggested command must work before installation.
        self.assertNotIn("\n    kvprobe ", result.stdout)
        # The planning path comes first: it is useful with no GPU at all.
        self.assertLess(
            result.stdout.index("AVAILABLE NOW"),
            result.stdout.index("NEXT CHECKPOINT"),
        )
        self.assertIn(
            "kvprobe pool-plan --sweep --geometry geometry.json", result.stdout
        )
        # The model source is a visible placeholder, not a hidden default.
        self.assertIn("replace ORG/MODEL_NAME", result.stdout)
        self.assertIn("--model ORG/MODEL_NAME", result.stdout)
        # geometry is the common path; pool-plan states its generic scope and
        # the currently documented backend reference.
        self.assertIn("Any stack", result.stdout)
        self.assertIn(
            "fixed-slot pool planning; documented reference: LMCache NIXL static",
            result.stdout,
        )
        self.assertLess(
            result.stdout.index("geometry --model"),
            result.stdout.index("pool-plan --sweep"),
        )
        self.assertNotIn("DETAILED BUNDLED CASES", result.stdout)

    def test_first_visit_verbose_exposes_detailed_diagnostics(self) -> None:
        """Verbose first visit must retain the full host and demo diagnostics."""
        result = _run("first-visit", "--verbose")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("kvprobe doctor", result.stdout)
        self.assertIn("DETAILED BUNDLED CASES", result.stdout)
        self.assertEqual(result.stdout.count("       check:   "), 4)
        self.assertEqual(result.stdout.count("       result:  "), 4)

    def test_first_visit_can_preserve_reviewable_artifacts(self) -> None:
        """The guided path must optionally preserve its manifest and reports."""
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "first-visit"
            result = _run("first-visit", "--output-dir", str(output))

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((output / "doctor.json").is_file())
            reports = sorted(output.glob("*.report.md"))
            self.assertEqual(len(reports), 4)
            index = output / "README.md"
            self.assertTrue(index.is_file())
            index_text = index.read_text(encoding="utf-8")
            self.assertIn(
                "batches without complete tier-byte and correctness evidence", index_text
            )
            self.assertNotIn("target-tier service stops", index_text)
            reading_order = (
                "pass-all-attempts.report.md",
                "attribution-unfiltered-counter.report.md",
                "phantom-tier-framework-hit.report.md",
                "envelope-collapse-small-pool.report.md",
            )
            positions = [index_text.index(filename) for filename in reading_order]
            self.assertEqual(positions, sorted(positions))
            self.assertIn("do **not** measure this host", index_text)
            self.assertIn("four independent bundled examples", index_text)
            self.assertIn("not four phases", index_text)
            self.assertIn("`doctor.json` is optional", index_text)
            self.assertIn("**Reference adapter:**", index_text)
            self.assertIn("host prerequisites only", index_text)
            self.assertIn("**Serving verification:** `NOT_ASSESSED`", index_text)
            self.assertIn("`docs/GATE1.md`", index_text)
            expected_origins = {
                "pass-all-attempts.report.md": "synthetic, hand-written sample",
                "attribution-unfiltered-counter.report.md": (
                    "measured, 4096-token context run on a unified-memory "
                    "aarch64 host"
                ),
                "phantom-tier-framework-hit.report.md": (
                    "synthetic, from a block-tier test fixture"
                ),
                "envelope-collapse-small-pool.report.md": (
                    "measured, concurrency sweep with a small staging pool"
                ),
            }
            for report in reports:
                report_text = report.read_text(encoding="utf-8")
                self.assertIn(
                    "Bundled example — not a measurement of the current host",
                    report_text,
                )
                self.assertIn("independent case", report_text)
                self.assertIn(expected_origins[report.name], report_text)
            self.assertIn(f"first-visit artifacts: {output}", result.stdout)
            self.assertIn(f"start with: {index}", result.stdout)

    def test_pool_plan_exposes_forced_eviction_without_optimality_claim(self) -> None:
        """Pool planning must report arithmetic, not invent a recommendation."""
        result = _run(
            "pool-plan",
                "--json",
            "--pool-slots",
            "16",
            "--bytes-per-chunk",
            "67108864",
            "--required-live-chunks",
            "36",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        plan = json.loads(result.stdout)
        self.assertEqual(plan["classification"], "below_live_set")
        self.assertEqual(plan["forced_evictions_lower_bound"], 20)
        self.assertNotIn("recommended", plan)

    def test_pool_plan_default_output_is_the_summary_alone(self) -> None:
        """Without --json the plan is explained, and a saved file is named."""
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "plan.json"
            result = _run(
                "pool-plan",
                "--pool-slots",
                "16",
                "--bytes-per-chunk",
                "67108864",
                "--required-live-chunks",
                "36",
                "--output",
                str(output),
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("forced evictions >= 20", result.stdout)
            self.assertIn(f"Saved JSON: {output}", result.stdout)
            self.assertNotIn('"classification"', result.stdout)
            self.assertEqual(
                json.loads(output.read_text(encoding="utf-8"))["classification"],
                "below_live_set",
            )

    def _geometry_file(self, directory: str, tensor_parallel_size: int) -> Path:
        """Write a geometry document produced by the real CLI."""
        config = Path(directory) / "config.json"
        config.write_text(
            json.dumps(
                {
                    "architectures": ["Qwen2ForCausalLM"],
                    "hidden_size": 5120,
                    "num_attention_heads": 40,
                    "num_key_value_heads": 8,
                    "num_hidden_layers": 64,
                    "torch_dtype": "bfloat16",
                }
            ),
            encoding="utf-8",
        )
        output = Path(directory) / f"geometry-tp{tensor_parallel_size}.json"
        result = _run(
            "geometry",
            "--model-config",
            str(config),
            "--chunk-tokens",
            "256",
            "--tensor-parallel-size",
            str(tensor_parallel_size),
            "--output",
            str(output),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return output

    def test_pool_plan_reads_bytes_per_chunk_from_geometry(self) -> None:
        """A geometry document replaces the hand-copied byte count."""
        with tempfile.TemporaryDirectory() as directory:
            geometry = self._geometry_file(directory, 1)
            result = _run(
                "pool-plan",
                "--json",
                "--pool-slots",
                "16",
                "--geometry",
                str(geometry),
                "--required-live-chunks",
                "36",
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        plan = json.loads(result.stdout)
        self.assertEqual(plan["bytes_per_chunk"], 67_108_864)
        self.assertEqual(plan["bytes_per_chunk_source"], "geometry:all_scopes_equal")
        self.assertIn("2.25 GiB", result.stderr)
        self.assertIn("Pool 16 slots = 1 GiB", result.stderr)

    def test_pool_plan_requires_an_explicit_scope_when_scopes_differ(self) -> None:
        """Per-rank and TP-group bytes must never be mixed silently."""
        with tempfile.TemporaryDirectory() as directory:
            geometry = self._geometry_file(directory, 2)
            base = (
                "pool-plan",
                "--json",
                "--pool-slots",
                "16",
                "--geometry",
                str(geometry),
                "--required-live-chunks",
                "36",
            )
            ambiguous = _run(*base)
            per_rank = _run(*base, "--geometry-scope", "per_tp_rank")
            group = _run(*base, "--geometry-scope", "tp_group_aggregate")

        self.assertEqual(ambiguous.returncode, 2)
        self.assertIn("geometry scopes differ", ambiguous.stderr)
        self.assertEqual(per_rank.returncode, 0, per_rank.stderr)
        self.assertEqual(group.returncode, 0, group.stderr)
        self.assertEqual(
            json.loads(per_rank.stdout)["bytes_per_chunk_source"],
            "geometry:per_tp_rank",
        )
        self.assertLess(
            json.loads(per_rank.stdout)["bytes_per_chunk"],
            json.loads(group.stdout)["bytes_per_chunk"],
        )

    def test_pool_plan_rejects_ambiguous_or_invalid_byte_sources(self) -> None:
        """Exactly one byte source is accepted, and it must be a geometry file."""
        common = ("pool-plan",
                "--json", "--pool-slots", "16", "--required-live-chunks", "36")
        with tempfile.TemporaryDirectory() as directory:
            geometry = self._geometry_file(directory, 1)
            not_geometry = Path(directory) / "other.json"
            not_geometry.write_text("{}", encoding="utf-8")
            both = _run(
                *common, "--bytes-per-chunk", "1", "--geometry", str(geometry)
            )
            neither = _run(*common)
            scope_only = _run(
                *common, "--bytes-per-chunk", "1", "--geometry-scope", "per_tp_rank"
            )
            wrong_file = _run(*common, "--geometry", str(not_geometry))

        for result in (both, neither, scope_only, wrong_file):
            self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("not a kvprobe geometry document", wrong_file.stderr)

    def test_pool_plan_derives_the_live_set_from_geometry(self) -> None:
        """Chunk tokens come from the geometry file; the derivation is recorded."""
        with tempfile.TemporaryDirectory() as directory:
            geometry = self._geometry_file(directory, 1)
            result = _run(
                "pool-plan",
                "--json",
                "--pool-slots",
                "16",
                "--geometry",
                str(geometry),
                "--concurrent-contexts",
                "3",
                "--context-tokens",
                "4096",
            )
            conflict = _run(
                "pool-plan",
                "--json",
                "--pool-slots",
                "16",
                "--geometry",
                str(geometry),
                "--concurrent-contexts",
                "3",
                "--context-tokens",
                "4096",
                "--chunk-tokens",
                "128",
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        plan = json.loads(result.stdout)
        self.assertEqual(plan["required_live_chunks"], 48)
        self.assertEqual(plan["required_live_chunks_source"], "derived")
        self.assertEqual(plan["derivation"]["chunks_per_context"], 16)
        self.assertIn("3 contexts × 16 chunks", result.stderr)
        self.assertIn("no prefix shared", result.stderr)
        self.assertEqual(conflict.returncode, 2)
        self.assertIn("differs from the geometry", conflict.stderr)

    def test_pool_plan_derivation_inputs_are_validated(self) -> None:
        """Derivation flags need their partners and an explicit partial policy."""
        base = ("pool-plan",
                "--json", "--pool-slots", "16", "--bytes-per-chunk", "1")
        no_contexts = _run(*base, "--required-live-chunks", "4", "--context-tokens", "9")
        no_tokens = _run(*base, "--concurrent-contexts", "2", "--chunk-tokens", "4")
        no_chunk = _run(
            *base, "--concurrent-contexts", "2", "--context-tokens", "8"
        )
        partial = _run(
            *base,
            "--concurrent-contexts",
            "2",
            "--context-tokens",
            "10",
            "--chunk-tokens",
            "4",
        )
        keep = _run(
            *base,
            "--concurrent-contexts",
            "2",
            "--context-tokens",
            "10",
            "--chunk-tokens",
            "4",
            "--partial-chunk",
            "keep",
        )
        both = _run(
            *base,
            "--required-live-chunks",
            "4",
            "--concurrent-contexts",
            "2",
        )

        for result in (no_contexts, no_tokens, no_chunk, partial, both):
            self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("apply only with --concurrent-contexts", no_contexts.stderr)
        self.assertIn("requires --context-tokens", no_tokens.stderr)
        self.assertIn("chunk tokens are unknown", no_chunk.stderr)
        self.assertIn("partial_chunk", partial.stderr)
        self.assertEqual(keep.returncode, 0, keep.stderr)
        self.assertEqual(json.loads(keep.stdout)["required_live_chunks"], 6)

    def test_pool_plan_sweep_brackets_the_live_set_without_recommending(self) -> None:
        """A sweep lists plans below, at and above the live set."""
        result = _run(
            "pool-plan",
                "--json",
            "--sweep",
            "--bytes-per-chunk",
            "67108864",
            "--required-live-chunks",
            "48",
        )
        conflict = _run(
            "pool-plan",
                "--json",
            "--sweep",
            "--pool-slots",
            "16",
            "--bytes-per-chunk",
            "1",
            "--required-live-chunks",
            "4",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        document = json.loads(result.stdout)
        self.assertEqual(
            [plan["pool_slots"] for plan in document["sweep"]], [24, 48, 96, 192]
        )
        self.assertEqual(
            [plan["classification"] for plan in document["sweep"]],
            ["below_live_set", "at_live_set", "above_live_set", "above_live_set"],
        )
        self.assertIn("not a recommendation", document["sweep_note"])
        self.assertNotIn("recommended", document)
        self.assertIn("none is a recommendation", result.stderr)
        self.assertEqual(conflict.returncode, 2)

    def test_pool_plan_reports_token_capacity_when_chunk_tokens_are_known(self) -> None:
        """Slots and the live set are also stated in tokens, never guessed."""
        explicit = ("pool-plan",
                "--json", "--pool-slots", "16", "--bytes-per-chunk", "1")
        with tempfile.TemporaryDirectory() as directory:
            geometry = self._geometry_file(directory, 1)
            from_geometry = _run(
                "pool-plan",
                "--json",
                "--pool-slots",
                "16",
                "--geometry",
                str(geometry),
                "--required-live-chunks",
                "36",
            )
            sweep = _run(
                "pool-plan",
                "--json",
                "--sweep",
                "--geometry",
                str(geometry),
                "--concurrent-contexts",
                "3",
                "--context-tokens",
                "4096",
            )
            conflict = _run(
                "pool-plan",
                "--json",
                "--pool-slots",
                "16",
                "--geometry",
                str(geometry),
                "--required-live-chunks",
                "36",
                "--chunk-tokens",
                "128",
            )
        unknown = _run(*explicit, "--required-live-chunks", "4")
        given = _run(*explicit, "--required-live-chunks", "4", "--chunk-tokens", "256")

        plan = json.loads(from_geometry.stdout)
        self.assertEqual(plan["chunk_tokens"], 256)
        self.assertEqual(plan["logical_capacity_tokens"], 16 * 256)
        self.assertEqual(plan["required_live_tokens"], 36 * 256)
        self.assertIn("(4,096 tokens)", from_geometry.stderr)
        self.assertIn("(9,216 tokens)", from_geometry.stderr)
        for entry in json.loads(sweep.stdout)["sweep"]:
            self.assertEqual(entry["logical_capacity_tokens"], entry["pool_slots"] * 256)
        self.assertEqual(conflict.returncode, 2)
        self.assertIn("differs from the geometry", conflict.stderr)
        # Without chunk tokens no token figure is invented.
        self.assertEqual(unknown.returncode, 0, unknown.stderr)
        self.assertNotIn("logical_capacity_tokens", json.loads(unknown.stdout))
        self.assertNotIn("tokens)", unknown.stderr)
        self.assertEqual(json.loads(given.stdout)["logical_capacity_tokens"], 16 * 256)

    def test_pool_plan_classification_describes_without_judging(self) -> None:
        """Classifications state the relation to the live set and nothing more."""
        for slots, expected in (
            (8, "below_live_set"),
            (16, "at_live_set"),
            (32, "above_live_set"),
        ):
            result = _run(
                "pool-plan",
                "--json",
                "--pool-slots",
                str(slots),
                "--bytes-per-chunk",
                "1",
                "--required-live-chunks",
                "16",
            )
            self.assertEqual(json.loads(result.stdout)["classification"], expected)

    def test_passing_evidence_allows_performance_claims(self) -> None:
        """All five gates permit the report to expose performance data."""
        result = _run("report", str(EXAMPLES / "pass-v0.3.json"), "--format", "json")

        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["overall"], "PASS")
        self.assertEqual(report["performance_claims"], "allowed")
        self.assertIn("performance", report)

    def test_non_pass_report_explains_its_exit_code(self) -> None:
        """Exit 1 is a verdict; stderr says so and stdout stays the report."""
        result = _run("report", str(EXAMPLES / "gate1-pass-filtered-counter.json"))

        self.assertEqual(result.returncode, 1)
        self.assertIn("exit 1: overall PARTIAL", result.stderr)
        self.assertIn("not an installation or runtime error", result.stderr)
        self.assertNotIn("exit 1:", result.stdout)
        passing = _run("report", str(EXAMPLES / "pass-v0.3.json"))
        self.assertEqual(passing.returncode, 0)
        self.assertNotIn("exit", passing.stderr)

    def test_unfiltered_counter_suppresses_performance_claims(self) -> None:
        """A counter not filtered to the tier's device leaves gate 1 unproven."""
        result = _run(
            "report",
            str(EXAMPLES / "attribution-unfiltered-counter.json"),
            "--format",
            "json",
        )

        self.assertEqual(result.returncode, 1, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["gates"][0]["status"], "UNPROVEN")
        self.assertEqual(report["overall"], "UNPROVEN")
        self.assertEqual(report["performance_claims"], "suppressed")
        self.assertNotIn("performance", report)
        self.assertNotIn("reuses to repay", json.dumps(report))

    def test_envelope_collapse_preserves_last_valid_level(self) -> None:
        """A level whose batches were not credited ends the envelope, not the sweep."""
        result = _run(
            "report",
            str(EXAMPLES / "envelope-collapse-small-pool.json"),
            "--format",
            "json",
        )

        self.assertEqual(result.returncode, 1, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["gates"][4]["status"], "PASS")
        self.assertIn("validated envelope is concurrency 4", report["gates"][4]["result"])
        self.assertEqual(report["validated_envelope"]["max_concurrency"], 4)
        self.assertNotIn("performance", report)

    def test_envelope_does_not_resume_after_a_failed_level(self) -> None:
        """A later pass cannot erase an earlier hole in the operating envelope."""
        payload = json.loads(
            (EXAMPLES / "envelope-collapse-small-pool.json").read_text(
                encoding="utf-8"
            )
        )
        payload["evidence"]["envelope"]["levels"].append(
            {"concurrency": 16, "requests": 20, "target_tier_served": 20}
        )
        payload["bindings"]["envelope_levels"].append(
            {"level_index": 4, "configuration_id": "config-a"}
        )
        with tempfile.TemporaryDirectory() as directory:
            evidence = Path(directory) / "non-monotonic.json"
            evidence.write_text(json.dumps(payload), encoding="utf-8")
            result = _run("report", str(evidence), "--format", "json")

        self.assertEqual(result.returncode, 1, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["validated_envelope"]["max_concurrency"], 4)


if __name__ == "__main__":
    unittest.main()


class InvocationTest(unittest.TestCase):
    """Suggested commands follow the form the user ran."""

    def test_installed_command_suggests_the_installed_form(self) -> None:
        from unittest import mock

        from kvprobe import cli

        with mock.patch.object(sys, "argv", ["/usr/local/bin/kvprobe", "first-visit"]):
            self.assertEqual(cli._invocation(), "kvprobe")
        with mock.patch.object(sys, "argv", ["/repo/kvprobe/__main__.py", "first-visit"]):
            self.assertEqual(cli._invocation(), "python3 -m kvprobe")
