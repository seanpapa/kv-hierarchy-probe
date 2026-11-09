# SPDX-License-Identifier: Apache-2.0
"""Synthetic reviewed-companion contract tests; no real serving PASS."""

# Standard
import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

# First Party
from kvprobe.cli import main
from kvprobe.nvme_capture import assess_capture_loss, summarize_capture
from kvprobe.nvme_trace import analyze_trace
from kvprobe.serving_verification import VerificationError, verify_serving


class ServingVerificationTests(unittest.TestCase):
    """Exercise the public decision matrix, input binding and explicit policy."""

    def setUp(self) -> None:
        """Create synthetic retained inputs and clearly fictional review evidence."""
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.review_path = self.root / "review.json"
        self.manifest = {
            "trace_contract_version": "0.2.0",
            "namespace": "nvme0n1",
            "controller_id": 0,
            "logical_block_size": 4096,
            "clock": "CLOCK_MONOTONIC",
            "capture_start_ns": 0,
            "capture_end_ns": 1000,
            "capture_ready": True,
            "capture_exit_code": 0,
            "process_returncode": 0,
            "stop_reason": None,
            "loss_events": 0,
        }
        self.roles = {
            "manifest.json": ["manifest"],
            "trace.csv": ["trace"],
            "phases.csv": ["phases"],
            "bpftrace.out": ["stdout"],
            "bpftrace.err": ["stderr"],
        }
        for role in (
            "acceptance",
            "runtime",
            "configuration",
            "lifecycle",
            "framework_log",
            "residency",
            "request",
            "counters",
            "geometry",
        ):
            name = f"synthetic-{role}.txt"
            self.roles[name] = [role]
            (self.root / name).write_text("SYNTHETIC assertion; no hardware tested")
        (self.root / "phases.csv").write_text(
            "request_id,phase,start_ns,end_ns,expected_read_bytes\n"
            "r1,retrieve,100,900,8192\n"
        )
        (self.root / "bpftrace.err").write_text("")
        self.records = "S,200,0,1,7,2,100,2,3\nC,300,0,1,7,0,3\n"
        self.write_capture()
        self.policy = {
            "expected_read_bytes": 8192,
            "read_min_bytes": 8192,
            "read_max_bytes": 8192,
            "write_max_bytes": 0,
            "declared_before_ns": 1,
            "artifact": "policy.json",
        }
        self.roles["policy.json"] = ["byte_policy"]
        self.write_policy()
        refs = list(self.roles)
        self.review = {
            "verification_contract_version": "0.1.0",
            "ordering_policy": "strict",
            "scope": {
                "namespace": "nvme0n1",
                "controller_id": 0,
                "request_id": "r1",
                "phase": "retrieve",
                "backend": "native_cufile_gds",
                "endpoint_id": "synthetic",
                "cache_domain_id": "synthetic",
                "concurrency": 1,
            },
            "reviewer": {
                "id": "synthetic-reviewer",
                "reviewed_at": "2026-10-04T21:00:00+09:00",
            },
            "artifacts": [],
            "input_sha256": {},
            "byte_policy": self.policy,
            "checks": [
                {
                    "id": check,
                    "status": "satisfied",
                    "reason": "Synthetic reviewed assertion; no hardware proof",
                    "artifacts": refs.copy(),
                }
                for check in (
                    "capture_integrity",
                    "filter_clock_acceptance",
                    "runtime_identity",
                    "admission_complete",
                    "residency_isolation",
                    "retrieval_correctness_path",
                    "byte_policy",
                    "audit_binding",
                )
            ],
        }
        self.rebind()

    def write_capture(self) -> None:
        """Retain matching raw records and scoped counters for this fixture."""
        setups = sum(line.startswith("S,") for line in self.records.splitlines())
        completions = sum(line.startswith("C,") for line in self.records.splitlines())
        raw = self.root / "bpftrace.out"
        raw.write_text(
            "# READY,0\n" + self.records + "# END,1000\n"
            f"@kvprobe_setup_count: {setups + 1}\n"
            f"@kvprobe_completion_count: {completions + 1}\n"
        )
        (self.root / "trace.csv").write_text(self.records)
        self.manifest["raw_stdout_sha256"] = hashlib.sha256(
            raw.read_bytes()
        ).hexdigest()
        self.manifest["loss_accounting"] = assess_capture_loss(
            summarize_capture(raw),
            self.root / "bpftrace.err",
            process_returncode=0,
            stop_reason=None,
        )
        (self.root / "manifest.json").write_text(json.dumps(self.manifest))

    def write_policy(self) -> None:
        """Retain the synthetic declaration separately from reviewer assertions."""
        (self.root / "policy.json").write_text(
            json.dumps(
                {key: value for key, value in self.policy.items() if key != "artifact"}
            )
        )

    def rebind(self) -> None:
        """Refresh hashes after explicitly modifying this synthetic input."""
        self.review["artifacts"] = [
            {
                "path": name,
                "sha256": hashlib.sha256((self.root / name).read_bytes()).hexdigest(),
                "roles": roles,
            }
            for name, roles in self.roles.items()
        ]
        self.review["input_sha256"] = {
            name: hashlib.sha256((self.root / name).read_bytes()).hexdigest()
            for name in (
                "manifest.json",
                "trace.csv",
                "phases.csv",
                "bpftrace.out",
                "bpftrace.err",
            )
        }
        self.save_review()

    def save_review(self) -> None:
        """Save only the separate synthetic review file."""
        self.review_path.write_text(json.dumps(self.review))

    def test_complete_review_is_separate_and_read_only(self) -> None:
        """All satisfied gives a reviewed verdict, without changing trace/evidence."""
        before = {path.name: path.read_bytes() for path in self.root.iterdir()}
        result = verify_serving(self.root, self.review_path)
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result, verify_serving(self.root, self.review_path))
        self.assertEqual(
            before, {path.name: path.read_bytes() for path in self.root.iterdir()}
        )
        self.assertEqual(
            analyze_trace(self.root)["serving_verification"], "not_assessed"
        )
        self.assertNotIn("confidence", result)

    def test_each_missing_check_is_incomplete(self) -> None:
        """No majority vote or byte equality can substitute for a missing check."""
        original = self.review["checks"]
        for omitted in original:
            with self.subTest(check=omitted["id"]):
                self.review["checks"] = [
                    c for c in original if c["id"] != omitted["id"]
                ]
                self.save_review()
                result = verify_serving(self.root, self.review_path)
                self.assertEqual(result["status"], "incomplete")
                self.assertEqual(
                    result["checks"][omitted["id"]]["status"], "unresolved"
                )

    def test_violation_wins_but_missing_checks_remain_visible(self) -> None:
        """Failed outranks incomplete without suppressing unknown residency."""
        self.review["checks"] = [
            c for c in self.review["checks"] if c["id"] != "residency_isolation"
        ]
        self.review["checks"][0]["status"] = "violated"
        self.save_review()
        result = verify_serving(self.root, self.review_path)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(
            result["checks"]["residency_isolation"]["status"], "unresolved"
        )

    def test_missing_artifact_and_missing_capture_are_incomplete(self) -> None:
        """Referenced evidence absence never turns a reviewed assertion into fact."""
        (self.root / "synthetic-residency.txt").unlink()
        self.assertEqual(
            verify_serving(self.root, self.review_path)["status"], "incomplete"
        )
        (self.root / "trace.csv").unlink()
        result = verify_serving(self.root, self.review_path)
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["missing_capture_inputs"], ["trace.csv"])

    def test_hash_and_run_binding_mismatch_fail(self) -> None:
        """Changed bytes and stale input fingerprints cannot reuse a review."""
        (self.root / "synthetic-runtime.txt").write_text("changed")
        self.assertEqual(
            verify_serving(self.root, self.review_path)["status"], "failed"
        )
        self.rebind()
        self.review["input_sha256"]["trace.csv"] = "0" * 64
        self.save_review()
        self.assertEqual(
            verify_serving(self.root, self.review_path)["status"], "failed"
        )

    def test_partial_input_binding_is_incomplete(self) -> None:
        """An empty hash map is not an accepted binding for a different run."""
        self.review["input_sha256"] = {}
        self.save_review()
        self.assertEqual(
            verify_serving(self.root, self.review_path)["status"], "incomplete"
        )

    def test_bad_scope_and_unsupported_backend_are_errors(self) -> None:
        """Namespace, phase, backend and concurrency claims cannot broaden scope."""
        for key, value in (
            ("namespace", "nvme1n1"),
            ("controller_id", True),
            ("request_id", "absent"),
            ("backend", "posix"),
            ("concurrency", 2),
        ):
            old = self.review["scope"][key]
            with self.subTest(key=key):
                self.review["scope"][key] = value
                self.save_review()
                with self.assertRaises(VerificationError):
                    verify_serving(self.root, self.review_path)
            self.review["scope"][key] = old

    def test_duplicate_unknown_and_undeclared_checks_are_errors(self) -> None:
        """Contract mistakes return no verdict instead of silently skipping checks."""
        check = self.review["checks"][0]
        for key, value in (
            ("id", "invented"),
            ("status", "pass"),
            ("reason", ""),
            ("artifacts", ["absent.txt"]),
        ):
            old = check[key]
            with self.subTest(key=key):
                check[key] = value
                self.save_review()
                with self.assertRaises(VerificationError):
                    verify_serving(self.root, self.review_path)
            check[key] = old
        self.review["checks"][1]["id"] = check["id"]
        self.save_review()
        with self.assertRaises(VerificationError):
            verify_serving(self.root, self.review_path)

    def test_path_traversal_absolute_and_symlink_are_errors(self) -> None:
        """Unsafe artifact paths are refused before any analysis."""
        entry = self.review["artifacts"][0]
        for value in ("../outside", "/etc/passwd", "a/./b", "a//b", "a\\b"):
            entry["path"] = value
            self.save_review()
            with self.assertRaises(VerificationError):
                verify_serving(self.root, self.review_path)
        entry["path"] = "link"
        (self.root / "link").symlink_to(self.root / "manifest.json")
        self.save_review()
        with self.assertRaises(VerificationError):
            verify_serving(self.root, self.review_path)

    def test_raw_integrity_is_checked_even_under_strict(self) -> None:
        """Matching review hashes cannot waive forged raw loss/digest assertions."""
        self.manifest["loss_accounting"] = {}
        (self.root / "manifest.json").write_text(json.dumps(self.manifest))
        self.rebind()
        result = verify_serving(self.root, self.review_path)
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(
            result["automatic_checks"]["record_delivery_integrity"], "unresolved"
        )

    def test_saved_usable_observation_cannot_override_reanalysis(self) -> None:
        """Saved quality strings have no authority over an unmatched command."""
        (self.root / "observation.json").write_text('{"quality":{"status":"usable"}}')
        self.records = "S,200,0,1,7,2,100,2,3\n"
        self.write_capture()
        self.rebind()
        result = verify_serving(self.root, self.review_path)
        self.assertEqual(result["status"], "incomplete")
        self.assertIn("unmatched_setup", result["observation_quality"]["reasons"])

    def test_byte_policy_declaration_and_bounds_are_checked(self) -> None:
        """Zero-read or a post-phase declaration cannot pass on reviewer prose."""
        self.policy["declared_before_ns"] = 100
        self.write_policy()
        self.rebind()
        self.assertEqual(
            verify_serving(self.root, self.review_path)["status"], "failed"
        )
        self.policy["declared_before_ns"] = 1
        self.write_policy()
        self.records = "S,200,0,1,7,2,100,1,3\nC,300,0,1,7,0,3\n"
        self.write_capture()
        self.rebind()
        self.assertEqual(
            verify_serving(self.root, self.review_path)["status"], "failed"
        )

    def test_policy_artifact_disagreement_and_missing_policy(self) -> None:
        """A retained policy must match the reviewed declaration, not just exist."""
        (self.root / "policy.json").write_text("{}")
        self.rebind()
        self.assertEqual(
            verify_serving(self.root, self.review_path)["status"], "failed"
        )
        self.review["byte_policy"] = None
        self.save_review()
        self.assertEqual(
            verify_serving(self.root, self.review_path)["status"], "incomplete"
        )

    def test_causal_requires_explicit_selection_and_preserves_strict(self) -> None:
        """Review metadata alone cannot opt the caller into causal ordering."""
        self.records = (
            "S,300,0,1,7,2,100,1,3\nS,200,0,1,8,2,200,1,4\n"
            "C,400,0,1,7,0,3\nC,410,0,1,8,0,4\n"
        )
        self.write_capture()
        self.rebind()
        self.assertEqual(
            verify_serving(self.root, self.review_path)["status"], "incomplete"
        )
        self.review["ordering_policy"] = "causal"
        self.save_review()
        with self.assertRaises(VerificationError):
            verify_serving(self.root, self.review_path)
        self.assertEqual(
            verify_serving(self.root, self.review_path, ordering_policy="causal")[
                "status"
            ],
            "verified",
        )

    def test_duplicate_json_keys_and_invalid_timestamp_are_errors(self) -> None:
        """Ambiguous JSON and reviewer dates cannot yield a reviewed PASS."""
        self.review_path.write_text('{"scope":{},"scope":{}}')
        with self.assertRaises(VerificationError):
            verify_serving(self.root, self.review_path)
        self.review["reviewer"]["reviewed_at"] = "2026-10-04T21:00:00"
        self.save_review()
        with self.assertRaises(VerificationError):
            verify_serving(self.root, self.review_path)

    def test_required_roles_and_capture_names_cannot_be_substituted(self) -> None:
        """Arbitrary review prose cannot substitute for named capture artifacts."""
        self.review["checks"][0]["artifacts"] = ["synthetic-runtime.txt"]
        self.save_review()
        result = verify_serving(self.root, self.review_path)
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["checks"]["capture_integrity"]["status"], "unresolved")

    def test_raw_normalized_disagreement_fails_with_rebound_review(self) -> None:
        """Strict analysis alone cannot excuse a changed normalized record."""
        (self.root / "trace.csv").write_text("S,200,0,1,7,2,999,2,3\nC,300,0,1,7,0,3\n")
        self.rebind()
        result = verify_serving(self.root, self.review_path)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["checks"]["capture_integrity"]["status"], "violated")

    def test_policy_invalid_ranges_types_and_expected_payload(self) -> None:
        """Booleans/ranges are contract errors; geometry mismatch is a failure."""
        for field, value in (
            ("read_min_bytes", 0),
            ("expected_read_bytes", True),
            ("write_max_bytes", -1),
        ):
            old = self.policy[field]
            self.policy[field] = value
            self.write_policy()
            self.rebind()
            with self.assertRaises(VerificationError):
                verify_serving(self.root, self.review_path)
            self.policy[field] = old
        self.policy.update(expected_read_bytes=4096, read_min_bytes=4096)
        self.write_policy()
        self.rebind()
        self.assertEqual(
            verify_serving(self.root, self.review_path)["status"], "failed"
        )

    def test_large_review_and_unreferenced_primary_symlink_are_refused(self) -> None:
        """Resource and symlink safety still apply when reviewers omit inputs."""
        self.review_path.write_text(" " * 65537)
        with self.assertRaises(VerificationError):
            verify_serving(self.root, self.review_path)
        self.review["artifacts"] = []
        self.review["input_sha256"] = {}
        for check in self.review["checks"]:
            check["artifacts"] = []
        self.save_review()
        (self.root / "trace.csv").unlink()
        (self.root / "trace.csv").symlink_to(self.root / "bpftrace.out")
        with self.assertRaises(VerificationError):
            verify_serving(self.root, self.review_path)

    def invoke_cli(self, *options: str) -> tuple[int, str, str]:
        """Invoke verify with redirected streams on the synthetic run.

        Args:
            options: Extra verify options following required input arguments.

        Returns:
            Exit code, stdout and stderr strings.
        """
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            status = main(
                [
                    "gate1",
                    "verify",
                    str(self.root),
                    "--review",
                    str(self.review_path),
                    *options,
                ]
            )
        return status, stdout.getvalue(), stderr.getvalue()

    def test_cli_verified_json_and_new_output(self) -> None:
        """JSON stdout stays parseable; summary is stderr and inputs are preserved."""
        before = {path.name: path.read_bytes() for path in self.root.iterdir()}
        output = self.root / "new" / "companion.json"
        status, stdout, stderr = self.invoke_cli("--json", "--output", str(output))
        self.assertEqual(status, 0, stderr)
        result = json.loads(stdout)
        self.assertEqual(result, json.loads(output.read_text()))
        self.assertEqual(result["status"], "verified")
        self.assertIn("not request-ID hardware attribution", stderr)
        self.assertIn("Synthetic verified fixtures are tool tests", stderr)
        self.assertEqual(
            before, {name: (self.root / name).read_bytes() for name in before}
        )

    def test_cli_incomplete_failed_and_error_statuses(self) -> None:
        """Exit one distinguishes valid negative verdicts from error exit two."""
        self.review["checks"].pop()
        self.save_review()
        status, stdout, stderr = self.invoke_cli("--json")
        self.assertEqual(status, 1, stderr)
        self.assertEqual(json.loads(stdout)["status"], "incomplete")
        self.review["checks"][0]["status"] = "violated"
        self.save_review()
        status, stdout, stderr = self.invoke_cli("--json")
        self.assertEqual(status, 1, stderr)
        self.assertEqual(json.loads(stdout)["status"], "failed")
        self.review_path.write_text("not JSON")
        status, stdout, stderr = self.invoke_cli("--json")
        self.assertEqual(status, 2)
        self.assertEqual(stdout, "")
        self.assertIn("serving verification error", stderr)

    def test_cli_existing_inputs_and_outputs_are_never_overwritten(self) -> None:
        """All policies refuse an existing file, including raw/review/hard links."""
        alias = self.root / "alias.json"
        alias.hardlink_to(self.review_path)
        before = {path.name: path.read_bytes() for path in self.root.iterdir()}
        for path in (self.review_path, self.root / "trace.csv", alias):
            with self.subTest(path=path.name):
                status, _, stderr = self.invoke_cli("--output", str(path))
                self.assertEqual(status, 2)
                self.assertIn("new file", stderr)
        self.assertEqual(
            before, {path.name: path.read_bytes() for path in self.root.iterdir()}
        )

    def test_cli_output_race_is_exclusive(self) -> None:
        """A file created after validation is preserved by exclusive open."""
        output = self.root / "companion.json"

        def race(*args: object, **kwargs: object) -> dict:
            result = verify_serving(self.root, self.review_path)
            output.write_text("other writer")
            return result

        with patch("kvprobe.cli.verify_serving", side_effect=race):
            status, _, stderr = self.invoke_cli("--output", str(output))
        self.assertEqual(status, 2, stderr)
        self.assertEqual(output.read_text(), "other writer")

    def test_cli_dangling_symlink_and_protected_output_are_refused(self) -> None:
        """No write through an output symlink or into kernel/device paths."""
        output = self.root / "dangling.json"
        target = self.root / "absent.json"
        output.symlink_to(target)
        self.assertEqual(self.invoke_cli("--output", str(output))[0], 2)
        self.assertFalse(target.exists())
        for path in (
            "/dev/kvprobe-companion-new.json",
            "/proc/kvprobe-companion-new.json",
            "/sys/kvprobe-companion-new.json",
        ):
            self.assertEqual(self.invoke_cli("--output", path)[0], 2)

    def test_cli_default_summary_and_explicit_policy(self) -> None:
        """Review policy does not select causal on behalf of the operator."""
        status, stdout, stderr = self.invoke_cli()
        self.assertEqual(status, 0, stderr)
        self.assertIn("Reviewed status: verified", stdout)
        self.assertEqual(stderr, "")
        self.review["ordering_policy"] = "causal"
        self.save_review()
        self.assertEqual(self.invoke_cli()[0], 2)
        status, stdout, stderr = self.invoke_cli(
            "--ordering-policy", "causal", "--json"
        )
        self.assertEqual(status, 0, stderr)
        self.assertEqual(json.loads(stdout)["ordering_policy"], "causal")

    def test_cli_malformed_inputs_write_no_output(self) -> None:
        """Contract errors cannot leave a PASS artifact at a requested destination."""
        output = self.root / "companion.json"
        self.review["scope"]["backend"] = "posix"
        self.save_review()
        status, stdout, stderr = self.invoke_cli("--json", "--output", str(output))
        self.assertEqual(status, 2, stderr)
        self.assertEqual(stdout, "")
        self.assertFalse(output.exists())

    def test_cli_help_and_required_review(self) -> None:
        """The subcommand is discoverable and missing review is an argument error."""
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout), self.assertRaises(SystemExit) as error:
            main(["gate1", "verify", "--help"])
        self.assertEqual(error.exception.code, 0)
        self.assertIn("--ordering-policy", stdout.getvalue())
        with (
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit) as error,
        ):
            main(["gate1", "verify", str(self.root)])
        self.assertEqual(error.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
