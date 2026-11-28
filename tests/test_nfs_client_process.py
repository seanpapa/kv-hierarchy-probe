# SPDX-License-Identifier: Apache-2.0
"""Parent wait status is authoritative even when the child aborts or forges success."""

# Standard
import json
from pathlib import Path
import signal
import sys
import tempfile
from typing import Any
import unittest

# First Party
from kvprobe.nfs_client import replay_client_execution
from kvprobe.nfs_client_process import (
    capture_client_process,
    checkpoint_client_workload,
    replay_client_process,
)
from kvprobe.nfs_observation import NfsObservationError

ROOT = Path(__file__).resolve().parents[1]


def run(root: Path, action: str = "normal", timeout: float = 5) -> dict[str, Any]:
    """Run one bounded synthetic child and return the public replay result."""
    capture_client_process(
        root,
        [
            sys.executable,
            str(ROOT / "tests/client_process_helper.py"),
            "{client_dir}",
            action,
        ],
        timeout_seconds=timeout,
        grace_seconds=1,
        environment={
            "PYTHONPATH": str(ROOT) + ":" + str(ROOT / "tests"),
            "PYTHONDONTWRITEBYTECODE": "1",
        },
    )
    return replay_client_execution(root)


class ClientProcessTests(unittest.TestCase):
    """Real POSIX process tests use synthetic artifacts and never access a tier."""

    def test_normal_exit_and_cleanup_are_recorded_but_unqualified(self) -> None:
        """Normal process completion does not qualify serving or isolation."""
        with tempfile.TemporaryDirectory() as temp:
            result = run(Path(temp))
        self.assertEqual(result["execution"]["process_returncode"], 0)
        self.assertEqual(
            result["execution"]["client_completion"], "recorded_unqualified"
        )
        self.assertEqual(result["serving_verification"], "not_assessed")

    def test_abort_retains_workload_and_real_signal(self) -> None:
        """A pre-teardown checkpoint survives SIGABRT without a shutdown receipt."""
        with tempfile.TemporaryDirectory() as temp:
            result = run(Path(temp), "abort")
            self.assertTrue((Path(temp) / "client/workload-manifest.json").is_file())
            self.assertFalse((Path(temp) / "client/client-manifest.json").exists())
        self.assertEqual(result["actual_input_binding"], "consistent")
        self.assertEqual(result["measurement_evidence"]["status"], "available")
        self.assertEqual(result["stack_health"]["status"], "error")
        self.assertNotIn("client_execution_incomplete", result["review_reasons"])
        self.assertEqual(result["execution"]["process_returncode"], -signal.SIGABRT)
        self.assertEqual(result["execution"]["shell_exit_code"], 134)
        self.assertIsNone(result["execution"]["public_shutdown_returned"])
        self.assertEqual(result["execution"]["client_completion"], "incomplete")
        self.assertEqual(result["performance_claims"], "suppressed")

    def test_early_failure_retains_launch_and_unavailable_workload(self) -> None:
        """Failure before a checkpoint keeps logs without inventing an observation."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            result = run(root, "early")
            self.assertTrue((root / "process-manifest.json").exists())
        self.assertEqual(result["execution"]["process_returncode"], 7)
        self.assertEqual(result["actual_input_binding"], "unavailable")
        self.assertIsNone(result["baseline_output_equal"])

    def test_child_success_text_does_not_override_actual_failure(self) -> None:
        """A child-written zero exit file cannot replace the parent's wait result."""
        with tempfile.TemporaryDirectory() as temp:
            result = run(Path(temp), "forged")
        self.assertEqual(result["execution"]["process_returncode"], 9)
        self.assertEqual(result["execution"]["process_status"], "failed")
        self.assertEqual(result["execution"]["client_completion"], "incomplete")

    def test_timeout_and_forced_kill_are_distinct_real_outcomes(self) -> None:
        """Timeouts keep their actual termination signal and scoped files."""
        for action, sig in [("timeout", signal.SIGTERM), ("kill", signal.SIGKILL)]:
            with self.subTest(action=action), tempfile.TemporaryDirectory() as temp:
                result = run(Path(temp), action, timeout=0.8)
                state = result["execution"]
                self.assertTrue(state["timed_out"])
                self.assertEqual(state["process_status"], "timed_out")
                self.assertEqual(state["process_returncode"], -sig)
                self.assertTrue(state["workload_checkpoint_present"])
                self.assertEqual(state["client_completion"], "incomplete")

    def test_launch_failure_is_retained(self) -> None:
        """Missing executable retains an actual launch error, not a fake child exit."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            capture_client_process(
                root, ["/nonexistent/kvprobe-client", "{client_dir}"]
            )
            result = replay_client_execution(root)
        self.assertEqual(result["execution"]["process_status"], "launch_failed")
        self.assertIsNone(result["execution"]["process_returncode"])

    def test_live_child_pid_binding_is_checked(self) -> None:
        """Internally consistent fixture PIDs must still match the real child."""
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(NfsObservationError, "actual child PID"):
                run(Path(temp), "wrong-pid")

    def test_tampered_supervisor_original_is_refused(self) -> None:
        """A changed output log invalidates the parent artifact seal."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run(root)
            (root / "stdout.bin").write_bytes(b"changed")
            with self.assertRaises(NfsObservationError):
                replay_client_process(root)

    def test_symlink_prevents_sealing_even_after_exit_zero(self) -> None:
        """A normal exit cannot turn an unsafe artifact tree into usable evidence."""
        with tempfile.TemporaryDirectory() as temp:
            result = run(Path(temp), "link")
        self.assertEqual(result["execution"]["process_returncode"], 0)
        self.assertEqual(result["execution"]["artifact_seal"], "incomplete")
        self.assertEqual(result["actual_input_binding"], "unavailable")
        self.assertEqual(result["execution"]["client_completion"], "incomplete")

    def test_checkpoint_and_supervisor_refuse_overwrite(self) -> None:
        """A second writer cannot replace either existing receipt."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            checkpoint_client_workload(root)
            with self.assertRaises(NfsObservationError):
                checkpoint_client_workload(root)
            with self.assertRaises(NfsObservationError):
                capture_client_process(root, [sys.executable, "{client_dir}"])

    def test_timeout_with_zero_child_exit_is_still_incomplete(self) -> None:
        """A graceful zero after timeout cannot be called normal completion."""
        with tempfile.TemporaryDirectory() as temp:
            result = run(Path(temp), "timeout-zero", timeout=0.8)
        self.assertEqual(result["execution"]["process_returncode"], 0)
        self.assertEqual(result["execution"]["process_status"], "timed_out")
        self.assertEqual(result["execution"]["client_completion"], "incomplete")

    def test_zero_without_cleanup_and_abort_after_seal_are_incomplete(self) -> None:
        """Neither zero alone nor a child seal alone overrides missing completion."""
        for action, code in [("no-cleanup", 0), ("abort-after-seal", -signal.SIGABRT)]:
            with self.subTest(action=action), tempfile.TemporaryDirectory() as temp:
                result = run(Path(temp), action)
                self.assertEqual(result["execution"]["process_returncode"], code)
                self.assertEqual(result["execution"]["client_completion"], "incomplete")
                self.assertEqual(result["serving_verification"], "not_assessed")

    def test_invalid_new_receipt_never_falls_back_to_valid_legacy(self) -> None:
        """A malformed or dangling parent receipt cannot be ignored."""
        # Local
        from test_nfs_client import fixture

        for action in ("malformed", "dangling"):
            with self.subTest(action=action), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                fixture(root, "baseline")
                path = root / "process-manifest.json"
                if action == "malformed":
                    path.write_text("{}")
                else:
                    path.symlink_to(root / "missing-receipt.json")
                with self.assertRaises((NfsObservationError, OSError)):
                    replay_client_execution(root)

    def test_measurement_view_is_independent_of_post_request_exit(self) -> None:
        """Available request evidence is identical across normal and aborted clients."""
        views = []
        for action in ("normal", "abort", "no-cleanup", "abort-after-seal"):
            with self.subTest(action=action), tempfile.TemporaryDirectory() as temp:
                result = run(Path(temp), action)
                views.append(
                    {
                        k: v
                        for k, v in result.items()
                        if k not in {"execution", "stack_health"}
                    }
                )
        self.assertTrue(all(view == views[0] for view in views))
        self.assertEqual(views[0]["measurement_evidence"]["status"], "available")

    def test_invalid_timeout_and_argv_are_refused_before_launch(self) -> None:
        """Invalid controls never launch a process or create workload files."""
        for timeout in [0, True, float("nan"), float("inf"), 601]:
            with self.subTest(timeout=timeout), tempfile.TemporaryDirectory() as temp:
                with self.assertRaises(NfsObservationError):
                    capture_client_process(
                        Path(temp),
                        [sys.executable, "{client_dir}"],
                        timeout_seconds=timeout,
                    )
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(NfsObservationError):
                capture_client_process(Path(temp), [sys.executable, "no-placeholder"])

    def test_bad_version_boolean_exit_and_escaped_ref_are_refused(self) -> None:
        """Malformed receipt fields do not become successful process evidence."""
        for key, value in [
            ("nfs_client_process_version", "99"),
            ("process_returncode", False),
        ]:
            with self.subTest(key=key), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                run(root)
                record = json.loads((root / "process-manifest.json").read_bytes())
                record[key] = value
                (root / "process-manifest.json").write_text(json.dumps(record))
                with self.assertRaises(NfsObservationError):
                    replay_client_process(root)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run(root)
            record = json.loads((root / "process-manifest.json").read_bytes())
            record["artifacts"][0]["path"] = "../outside"
            (root / "process-manifest.json").write_text(json.dumps(record))
            with self.assertRaises((NfsObservationError, OSError)):
                replay_client_process(root)


if __name__ == "__main__":
    unittest.main()
