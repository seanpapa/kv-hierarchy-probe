# SPDX-License-Identifier: Apache-2.0
"""Public NFS adapter/report contracts, including controlled negative evidence."""

# Standard
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

# First Party
from kvprobe.ingest import IngestError
from kvprobe.nfs_ingest import build_nfs_document
from kvprobe.nfs_qualification import recognized_layout
from kvprobe.report import (
    EvidenceError,
    assess_evidence,
    gate1_next_steps,
    render_markdown,
    render_text,
)

# Local
from nfs_c3_fixtures import ROOT, normalized, raw_bundle
from test_nfs_client import seal
from test_nfs_scope import validator


class NfsPolicyTests(unittest.TestCase):
    """Unknown, contradicted, invalid and complete observations stay distinct."""

    def test_complete_gate1_is_partial_with_no_performance_claim(self) -> None:
        value = normalized()
        result = assess_evidence(value)
        self.assertEqual(result["gates"][0]["status"], "PASS")
        self.assertEqual(result["overall"], "PARTIAL")
        self.assertEqual(result["performance_claims"], "suppressed")
        self.assertTrue(
            all(x["status"] == "SKIP_UNSUPPORTED" for x in result["gates"][1:])
        )
        self.assertIn("NFS", render_text(result))
        self.assertIn("PARTIAL", render_markdown(result))

    def test_thirty_adverse_policy_cases(self) -> None:
        cases = [
            ("zero", "counter_after", 10000, "FAIL"),
            ("partial", "counter_after", 14096, "FAIL"),
            ("excess", "counter_after", 26384, "FAIL"),
            ("missing", "counter_after", None, "UNPROVEN"),
            ("reset", "counter_after", 0, "INVALID"),
            ("rpc_reset", "rpc_after", 0, "INVALID"),
            ("retry", "retransmissions", 1, "UNPROVEN"),
            ("timeout", "timeouts", 1, "INVALID"),
            ("error", "errors", 1, "INVALID"),
            ("rpc_disagreement", "rpc_after", 5, "INVALID"),
            ("wrong_counter", "counter", "READ_RPC_RECEIVED_BYTES", "UNPROVEN"),
            ("file_io", "workload", "isolated_file_io", "UNPROVEN"),
            ("identity_missing", "identity_binding", "missing", "UNPROVEN"),
            ("identity_changed", "identity_binding", "changed", "INVALID"),
            ("wrong_target", "identity_binding", "wrong_target", "INVALID"),
            ("shared", "namespace_binding", "shared", "UNPROVEN"),
            ("scope_unknown", "namespace_binding", "unknown", "UNPROVEN"),
            ("wrong_namespace", "namespace_binding", "mismatch", "INVALID"),
            ("client_missing", "client_binding", None, "UNPROVEN"),
            ("unrecognized_source", "source_binding", "unrecognized", "UNPROVEN"),
            ("shared_mount", "dedicated_mount", False, "UNPROVEN"),
            ("concurrency", "serialized", False, "UNPROVEN"),
            ("quiet_noise", "quiet_activity", True, "UNPROVEN"),
            ("gap_noise", "gap_activity", True, "UNPROVEN"),
            ("mode", "io_mode_match", False, "UNPROVEN"),
            ("unknown_layout", "payload_mapping", "unknown", "UNPROVEN"),
            ("unstable_file", "payload_mapping", "unstable", "UNPROVEN"),
            ("time", "interval_ns", [1, 3, 2, 4], "INVALID"),
            ("missing_interval", "interval_ns", None, "UNPROVEN"),
            ("capture_missing", "capture_status", "missing", "UNPROVEN"),
        ]
        for name, key, item, expected in cases:
            with self.subTest(name=name):
                value = normalized()
                r = value["evidence"]["attribution"]["requests"][0]
                r["nfs_measurement"][key] = item
                if key == "counter_after":
                    r["observed_bytes"] = (
                        None if item is None or item < 10000 else item - 10000
                    )
                result = assess_evidence(value)
                self.assertEqual(result["gates"][0]["status"], expected)
                self.assertEqual(result["performance_claims"], "suppressed")
                if expected in {"FAIL", "UNPROVEN"}:
                    self.assertTrue(gate1_next_steps(value, result))

    def test_wrong_output_unknown_output_and_missing_role(self) -> None:
        for correct, expected in [(False, "FAIL"), (None, "UNPROVEN")]:
            value = normalized()
            value["evidence"]["attribution"]["requests"][0]["correct"] = correct
            self.assertEqual(assess_evidence(value)["gates"][0]["status"], expected)
        value = normalized()
        value["evidence"]["attribution"]["requests"][0]["nfs_measurement"][
            "artifact_roles"
        ]["source"] = []
        self.assertEqual(assess_evidence(value)["gates"][0]["status"], "UNPROVEN")

    def test_scope_declaration_alone_and_malformed_fields_cannot_pass(self) -> None:
        for field, bad in [
            ("counter_after", True),
            ("serialized", []),
            ("interval_ns", [1, 2]),
            ("source_binding", {}),
            ("configuration_fingerprint", "bad"),
        ]:
            with self.subTest(field=field):
                value = normalized()
                value["evidence"]["attribution"]["requests"][0]["nfs_measurement"][
                    field
                ] = bad
                with self.assertRaises(EvidenceError):
                    assess_evidence(value)
        for action in [
            "missing_measurement",
            "device",
            "unknown_reference",
            "role_kind",
            "fingerprint",
            "unknown_field",
            "nonzero_tolerance",
            "hidden_g2",
        ]:
            with self.subTest(action=action):
                value = normalized()
                r = value["evidence"]["attribution"]["requests"][0]
                if action == "missing_measurement":
                    del r["nfs_measurement"]
                if action == "device":
                    r["attribution"]["target_device_filtered"] = True
                if action == "unknown_reference":
                    r["nfs_measurement"]["artifact_roles"]["source"] = ["not-declared"]
                if action == "role_kind":
                    value["provenance"]["artifacts"][0]["kind"] = "wrong"
                if action == "fingerprint":
                    r["nfs_measurement"]["configuration_fingerprint"] = "a" * 64
                if action == "unknown_field":
                    r["nfs_measurement"]["reviewed"] = True
                if action == "nonzero_tolerance":
                    value["evidence"]["attribution"]["tolerance_fraction"] = 0.1
                if action == "hidden_g2":
                    value["evidence"]["break_even"]["retrieval_ttft_s"] = [0.1]
                with self.assertRaises(EvidenceError):
                    assess_evidence(value)

    def test_batch_and_heuristic_are_unproven(self) -> None:
        for field, items in [
            ("granularity", ["per_batch", "per_run"]),
            ("confidence", ["heuristic", "direct"]),
        ]:
            for item in items:
                value = normalized()
                value["evidence"]["attribution"]["requests"][0]["attribution"][
                    field
                ] = item
                self.assertEqual(
                    assess_evidence(value)["gates"][0]["status"], "UNPROVEN"
                )

    def test_legacy_cannot_carry_nfs_measurement(self) -> None:
        value = json.loads(
            (ROOT / "examples/evidence/gate1-pass-filtered-counter.json").read_text()
        )
        value["evidence"]["attribution"]["requests"][0]["nfs_measurement"] = {}
        with self.assertRaises(EvidenceError):
            assess_evidence(value)

    def test_schema_and_manual_parser_match_new_field_boundaries(self) -> None:
        v = validator()
        self.assertFalse(list(v.iter_errors(normalized())))
        for value in [None, True, [], {}, "0", 0.5, -1]:
            doc = normalized()
            doc["evidence"]["attribution"]["requests"][0]["nfs_measurement"][
                "counter_after"
            ] = value
            schema_valid = not list(v.iter_errors(doc))
            try:
                assess_evidence(doc)
                manual_valid = True
            except EvidenceError:
                manual_valid = False
            self.assertEqual(manual_valid, schema_valid, repr(value))


class NfsAdapterTests(unittest.TestCase):
    """The adapter parses originals instead of trusting a stored review status."""

    def test_full_raw_synthetic_bundle_roundtrip_and_schema(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cfg = raw_bundle(root)
            document = build_nfs_document(root, cfg, "synthetic-c3")
            result = assess_evidence(document)
            self.assertEqual(result["gates"][0]["status"], "PASS")
            self.assertEqual(result["overall"], "PARTIAL")
            self.assertFalse(list(validator().iter_errors(document)))
            self.assertNotIn("storage.invalid", json.dumps(document))

    def test_shutdown_state_does_not_change_measurement_or_verdict(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cfg = raw_bundle(root)
            before = build_nfs_document(root, cfg, "synthetic-c3")
            (root / "candidate/client/shutdown.json").unlink()
            seal(root / "candidate/client")
            after = build_nfs_document(root, cfg, "synthetic-c3")
            self.assertEqual(assess_evidence(before), assess_evidence(after))
            self.assertEqual(
                before["evidence"]["attribution"]["requests"][0]["nfs_measurement"][
                    "capture_status"
                ],
                "complete",
            )

    def test_missing_scope_and_tampered_file_are_not_pass(self) -> None:
        for action, expected in [
            ("scope", "UNPROVEN"),
            ("tamper", "INVALID"),
            ("missing_proof", "UNPROVEN"),
        ]:
            with self.subTest(action=action), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                cfg = raw_bundle(root)
                if action == "scope":
                    (root / "scope-before.json").unlink()
                elif action == "missing_proof":
                    (root / "candidate/client/proof/load_gpu-0-32-call1.bin").unlink()
                    seal(root / "candidate/client")
                else:
                    (root / "candidate/client/io/before-request-file.bin").write_bytes(
                        b"bad"
                    )
                document = build_nfs_document(root, cfg, "synthetic-c3")
                self.assertEqual(
                    assess_evidence(document)["gates"][0]["status"], expected
                )

    def test_malformed_raw_and_wrong_configuration_refuse(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cfg = raw_bundle(root)
            wrong = copy.deepcopy(cfg)
            wrong["model"]["revision"] = "other"
            with self.assertRaises(IngestError):
                build_nfs_document(root, wrong, "synthetic")
            (root / "scope-before.json").write_text("{broken")
            with self.assertRaises(IngestError):
                build_nfs_document(root, cfg, "synthetic")

    def test_buffered_zero_remote_payload_is_fail(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cfg = raw_bundle(root, io_mode="buffered", payload=0)
            result = assess_evidence(
                build_nfs_document(root, cfg, "synthetic-cached-read")
            )
            self.assertEqual(result["gates"][0]["status"], "FAIL")
            self.assertEqual(
                result["nfs_requests"][0]["reason_code"], "no_remote_payload"
            )

    def test_unrecognized_loaded_layout_never_becomes_pass(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source = (ROOT / "tests/fixtures/nfs/layout-source.txt").read_bytes()
            root = Path(temp)
            cfg = raw_bundle(
                root,
                layout_source=source.replace(
                    b"f.readinto(buffer)", b"f.readinto(buffer); f.seek(4)"
                ),
            )
            result = assess_evidence(
                build_nfs_document(root, cfg, "synthetic-unknown-layout")
            )
            self.assertEqual(result["gates"][0]["status"], "UNPROVEN")

    def test_rehashed_wrong_output_is_fail_not_input_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cfg = raw_bundle(root)
            client = root / "candidate/client"
            record = json.loads((client / "retrieve.json").read_text())
            record["output_token_ids"] = [7]
            (client / "retrieve.json").write_text(json.dumps(record))
            (client / "output.bin").write_bytes(b"[7]")
            (client / "serving/request.response.bin").write_bytes(b"[7]")
            from hashlib import sha256

            manifest = json.loads(
                (client / "serving/serving-manifest.json").read_text()
            )
            manifest["phases"]["request"]["response"]["sha256"] = sha256(
                b"[7]"
            ).hexdigest()
            (client / "serving/serving-manifest.json").write_text(json.dumps(manifest))
            seal(client)
            result = assess_evidence(
                build_nfs_document(root, cfg, "synthetic-wrong-output")
            )
            self.assertEqual(result["gates"][0]["status"], "FAIL")

    def test_source_contract_never_executes_and_refuses_changed_layout(self) -> None:
        source = (ROOT / "tests/fixtures/nfs/layout-source.txt").read_bytes()
        self.assertTrue(recognized_layout(source))
        self.assertFalse(
            recognized_layout(
                source.replace(b"f.readinto(buffer)", b"f.readinto(buffer); f.seek(4)")
            )
        )
        self.assertFalse(recognized_layout(b'__import__("os").abort()'))

    def test_cli_ingest_report_and_overwrite_refusal(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cfg = raw_bundle(root)
            config = root / "configuration.json"
            config.write_text(json.dumps(cfg))
            output = root / "normalized.json"
            command = [
                sys.executable,
                "-m",
                "kvprobe",
                "ingest",
                "--adapter",
                "nfs-reference",
                str(root),
                "--configuration",
                str(config),
                "--run-id",
                "synthetic-cli",
                "--output",
                str(output),
            ]

            def run(argv: list[str]) -> subprocess.CompletedProcess[str]:
                return subprocess.run(argv, cwd=ROOT, capture_output=True, text=True)

            created = run(command)
            self.assertEqual(created.returncode, 0, created.stderr)
            reported = run([sys.executable, "-m", "kvprobe", "report", str(output)])
            self.assertEqual(reported.returncode, 1, reported.stderr)
            self.assertIn("Overall: PARTIAL", reported.stdout)
            self.assertIn("Verification: PASS", reported.stdout)
            self.assertEqual(run(command).returncode, 2)
