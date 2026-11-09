# SPDX-License-Identifier: Apache-2.0
"""Tests for fail-closed KV-cache geometry derivation."""

# Standard
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# First Party
from kvprobe.geometry import GeometryError, derive_geometry
from kvprobe.model_source import ModelSourceError, load_model_reference

ROOT = Path(__file__).parents[1]


def _qwen_config() -> dict[str, object]:
    """Return the standard-attention fields used by Qwen2.5-32B."""
    return {
        "architectures": ["Qwen2ForCausalLM"],
        "hidden_size": 5120,
        "num_attention_heads": 40,
        "num_hidden_layers": 64,
        "num_key_value_heads": 8,
        "torch_dtype": "bfloat16",
    }


class _FakeResponse:
    """Provide the bounded HTTP response surface used by model resolution."""

    def __init__(self, payload: bytes, commit: str) -> None:
        """Build one fake pinned ``config.json`` response."""
        self._payload = payload
        self._commit = commit
        self.headers = {"X-Repo-Commit": commit, "ETag": '"config-etag"'}

    def __enter__(self) -> "_FakeResponse":
        """Return this response for a ``with`` block."""
        return self

    def __exit__(self, *args: object) -> None:
        """Close the fake response without suppressing exceptions."""

    def read(self, size: int = -1) -> bytes:
        """Return at most the requested number of payload bytes."""
        return self._payload if size < 0 else self._payload[:size]

    def geturl(self) -> str:
        """Return the immutable URL represented by the response."""
        return f"https://huggingface.co/api/models/model/{self._commit}/config.json"


class GeometryTest(unittest.TestCase):
    """Hold model, dtype, and parallel-scope arithmetic stable."""

    def test_qwen_geometry_matches_reference_chunk(self) -> None:
        """The recorded Qwen BF16 configuration must remain 64 MiB/chunk."""
        result = derive_geometry(_qwen_config(), chunk_tokens=256)

        self.assertEqual(result["geometry"]["full_model"]["bytes_per_token"], 262_144)
        self.assertEqual(
            result["geometry"]["full_model"]["bytes_per_chunk"],
            67_108_864,
        )
        self.assertEqual(result["runtime"]["dtype_source"], "model_config.torch_dtype")

    def test_new_model_dtype_field_is_supported(self) -> None:
        """A current config dtype field must take precedence over torch_dtype."""
        config = _qwen_config() | {"dtype": "float16"}
        result = derive_geometry(config, chunk_tokens=256)

        self.assertEqual(result["runtime"]["dtype"], "float16")
        self.assertEqual(result["runtime"]["dtype_source"], "model_config.dtype")

    def test_tp_two_separates_rank_and_group_counter_scope(self) -> None:
        """TP=2 halves per-rank bytes while preserving the group aggregate."""
        result = derive_geometry(
            _qwen_config(), chunk_tokens=256, tensor_parallel_size=2
        )

        self.assertEqual(result["runtime"]["kv_head_placement"], "sharded")
        self.assertEqual(
            result["runtime"]["kv_head_placement_source"],
            "derived_from_counts_not_observed",
        )
        self.assertEqual(
            result["geometry"]["per_tp_rank"]["bytes_per_chunk"],
            33_554_432,
        )
        self.assertEqual(
            result["geometry"]["tp_group_aggregate"]["bytes_per_chunk"],
            67_108_864,
        )
        self.assertEqual(result["selection"]["per_device_counter_mib_per_chunk"], 32)

    def test_mha_falls_back_to_attention_head_count(self) -> None:
        """A config without num_key_value_heads represents ordinary MHA."""
        config = {
            "hidden_size": 4096,
            "num_attention_heads": 32,
            "num_hidden_layers": 32,
            "torch_dtype": "float16",
        }
        result = derive_geometry(config, chunk_tokens=128)

        self.assertEqual(result["model"]["num_key_value_heads"], 32)
        self.assertEqual(result["model"]["head_dim"], 128)

    def test_rejects_mla_and_pipeline_parallel_guessing(self) -> None:
        """Layouts requiring architecture-specific placement must fail closed."""
        mla = _qwen_config() | {"kv_lora_rank": 512}
        with self.assertRaisesRegex(GeometryError, "MLA"):
            derive_geometry(mla, chunk_tokens=256)
        with self.assertRaisesRegex(GeometryError, "layer placement"):
            derive_geometry(
                _qwen_config(),
                chunk_tokens=256,
                pipeline_parallel_size=2,
            )

    def test_cli_missing_config_explains_how_to_retry(self) -> None:
        """A missing file gives actionable guidance without producing geometry."""
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "missing" / "config.json"
            output = Path(directory) / "geometry.json"
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "kvprobe",
                    "geometry",
                    "--model-config",
                    str(config),
                    "--chunk-tokens",
                    "256",
                    "--output",
                    str(output),
                ],
                cwd=ROOT,
                capture_output=True,
                check=False,
                text=True,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn(str(config), result.stderr)
            self.assertIn("readable from this shell", result.stderr)
            self.assertIn("container", result.stderr)
            self.assertIn("docs/GEOMETRY.md", result.stderr)
            self.assertNotIn("Traceback", result.stderr)
            self.assertEqual(result.stdout, "")
            self.assertFalse(output.exists())

    def test_cli_accepts_local_model_directory(self) -> None:
        """--model must find config.json without hand-copying model dimensions."""
        with tempfile.TemporaryDirectory() as directory:
            model = Path(directory) / "Qwen2.5-32B-Instruct"
            model.mkdir()
            raw = json.dumps(_qwen_config()).encode("utf-8")
            (model / "config.json").write_bytes(raw)
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "kvprobe",
                    "geometry",
                    "--model",
                    str(model),
                    "--chunk-tokens",
                    "256",
                    "--json",
                ],
                cwd=ROOT,
                capture_output=True,
                check=False,
                text=True,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        source = payload["model"]["config_source"]
        self.assertEqual(payload["geometry_version"], "0.1.1")
        self.assertEqual(payload["model"]["input_source"], "model")
        self.assertEqual(source["kind"], "local_model_directory")
        self.assertEqual(source["model_name"], "Qwen2.5-32B-Instruct")
        self.assertEqual(source["config_sha256"], hashlib.sha256(raw).hexdigest())
        self.assertNotIn(str(model.parent), result.stdout)

    @patch("kvprobe.model_source.urlopen")
    def test_model_id_fetches_only_pinned_config(
        self, mocked_urlopen: MagicMock
    ) -> None:
        """A Hub model records immutable provenance without requesting weights."""
        raw = json.dumps(_qwen_config()).encode("utf-8")
        commit = "a" * 40
        mocked_urlopen.return_value = _FakeResponse(raw, commit)

        config, provenance = load_model_reference(
            "Qwen/Qwen2.5-32B-Instruct", revision="main"
        )

        self.assertEqual(config["num_hidden_layers"], 64)
        source = provenance["config_source"]
        self.assertEqual(provenance["input_source"], "model")
        self.assertEqual(source["model_id"], "Qwen/Qwen2.5-32B-Instruct")
        self.assertEqual(source["requested_revision"], "main")
        self.assertEqual(source["resolved_revision"], commit)
        self.assertEqual(source["config_sha256"], hashlib.sha256(raw).hexdigest())
        self.assertFalse(source["weights_downloaded"])
        request = mocked_urlopen.call_args.args[0]
        self.assertEqual(
            request.full_url,
            "https://huggingface.co/Qwen/Qwen2.5-32B-Instruct/resolve/main/config.json",
        )

    def test_local_model_rejects_remote_revision(self) -> None:
        """A Hub-only revision must not be silently applied to a local directory."""
        with tempfile.TemporaryDirectory() as directory:
            model = Path(directory)
            (model / "config.json").write_text(
                json.dumps(_qwen_config()), encoding="utf-8"
            )
            with self.assertRaisesRegex(ModelSourceError, "Hugging Face"):
                load_model_reference(str(model), revision="main")

    @patch("kvprobe.model_source.urlopen")
    def test_model_id_requires_immutable_revision(
        self, mocked_urlopen: MagicMock
    ) -> None:
        """A mutable Hub response without its commit must fail closed."""
        raw = json.dumps(_qwen_config()).encode("utf-8")
        mocked_urlopen.return_value = _FakeResponse(raw, "not-a-commit")

        with self.assertRaisesRegex(ModelSourceError, "resolved commit"):
            load_model_reference("Qwen/Qwen2.5-32B-Instruct")

    def test_model_reference_rejects_arbitrary_url(self) -> None:
        """The model argument must not become an arbitrary URL fetcher."""
        with self.assertRaisesRegex(ModelSourceError, "invalid model reference"):
            load_model_reference("https://example.com/model")

    def _geometry_cli(self, *extra: str) -> subprocess.CompletedProcess:
        """Run ``kvprobe geometry`` on the Qwen reference config."""
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.json"
            config.write_text(json.dumps(_qwen_config()), encoding="utf-8")
            return subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "kvprobe",
                    "geometry",
                    "--model-config",
                    str(config),
                    "--chunk-tokens",
                    "256",
                    *extra,
                ],
                cwd=ROOT,
                capture_output=True,
                check=False,
                text=True,
            )

    def test_cli_default_output_is_the_summary_alone(self) -> None:
        """Without --json the operator sees the answer, not a JSON dump."""
        result = self._geometry_cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("67,108,864 bytes/chunk (64 MiB)", result.stdout)
        self.assertIn("does not establish tier service or a gate PASS", result.stdout)
        self.assertNotIn('"geometry_version"', result.stdout)
        self.assertNotIn("Saved JSON:", result.stdout)

    def test_cli_json_flag_keeps_stdout_machine_readable(self) -> None:
        """With --json, stdout is JSON alone and the summary moves to stderr."""
        result = self._geometry_cli("--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["geometry"]["full_model"]["bytes_per_token"], 262144)
        self.assertEqual(payload["model"]["input_source"], "model_config")
        self.assertIn("67,108,864 bytes/chunk (64 MiB)", result.stderr)

    def test_cli_accepts_dimensions_without_a_model_file(self) -> None:
        """Numbers on the command line give the same bytes as the config file."""
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "kvprobe",
                "geometry",
                "--layers",
                "64",
                "--kv-heads",
                "8",
                "--head-dim",
                "128",
                "--dtype",
                "bf16",
                "--chunk-tokens",
                "256",
                "--json",
            ],
            cwd=ROOT,
            capture_output=True,
            check=False,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["geometry"]["full_model"]["bytes_per_chunk"], 67_108_864)
        self.assertEqual(payload["model"]["input_source"], "dimensions")
        self.assertNotIn("num_attention_heads", payload["model"])
        self.assertIn("entered as numbers, not read from a model file", result.stderr)

    def test_cli_dimensions_refuse_to_guess(self) -> None:
        """A missing dimension or dtype is an error, never a default."""
        for extra, message in (
            (("--layers", "64", "--kv-heads", "8", "--dtype", "bf16"), "--head-dim"),
            (("--layers", "64", "--kv-heads", "8", "--head-dim", "128"), "--dtype"),
        ):
            with self.subTest(missing=message):
                result = subprocess.run(
                    [sys.executable, "-m", "kvprobe", "geometry", *extra, "--chunk-tokens", "256"],
                    cwd=ROOT,
                    capture_output=True,
                    check=False,
                    text=True,
                )
                self.assertEqual(result.returncode, 2)
                self.assertIn(message, result.stderr)
                self.assertEqual(result.stdout, "")

    def test_cli_config_and_dimensions_do_not_mix(self) -> None:
        """--kv-heads with --model-config would silently compete with the file."""
        result = self._geometry_cli("--kv-heads", "4")
        self.assertEqual(result.returncode, 2)
        self.assertIn("go with --layers", result.stderr)

    def test_cli_writes_machine_readable_geometry(self) -> None:
        """The public command must emit the exact evidence-input candidates."""
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.json"
            output = Path(directory) / "geometry.json"
            config.write_text(json.dumps(_qwen_config()), encoding="utf-8")
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "kvprobe",
                    "geometry",
                    "--model-config",
                    str(config),
                    "--chunk-tokens",
                    "256",
                    "--tensor-parallel-size",
                    "2",
                    "--output",
                    str(output),
                    "--json",
                ],
                cwd=ROOT,
                capture_output=True,
                check=False,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(json.loads(result.stdout), payload)
            self.assertIn(f"Saved JSON: {output}", result.stderr)
            self.assertIn("33,554,432 bytes/chunk (32 MiB)", result.stderr)
            self.assertIn("per-device=33,554,432; TP-group=67,108,864", result.stderr)
            self.assertIn(
                "live serving settings and rank placement were not observed",
                result.stderr,
            )
            self.assertIn("docs/CONFIGURATION.md", result.stderr)
            self.assertEqual(
                payload["selection"]["per_device_counter_bytes_per_chunk"],
                33_554_432,
            )


if __name__ == "__main__":
    unittest.main()
