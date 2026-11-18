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


def _qwen35_wrapper_config() -> dict[str, object]:
    """Return the shape of a Qwen3.5 config: a wrapper with a hybrid text_config."""
    return {
        "architectures": ["Qwen3_5ForConditionalGeneration"],
        "model_type": "qwen3_5",
        "text_config": {
            "model_type": "qwen3_5_text",
            "hidden_size": 4096,
            "num_attention_heads": 16,
            "num_hidden_layers": 32,
            "num_key_value_heads": 4,
            "head_dim": 256,
            "dtype": "bfloat16",
            "layer_types": (["linear_attention"] * 3 + ["full_attention"]) * 8,
        },
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
        self.assertEqual(payload["geometry_version"], "0.2.0")
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

    def test_rejects_mixed_layer_types_with_their_counts(self) -> None:
        """A hybrid layout must not get the all-layer full-attention number."""
        hybrid = _qwen_config() | {
            "layer_types": ["linear_attention"] * 48 + ["full_attention"] * 16
        }
        with self.assertRaisesRegex(
            GeometryError,
            r"hybrid layer layout \(config\.layer_types: 48 linear_attention, "
            r"16 full_attention\)",
        ):
            derive_geometry(hybrid, chunk_tokens=256)

    def test_rejects_uniform_non_full_attention_layers(self) -> None:
        """A uniform layout is not necessarily the full-attention KV layout."""
        for kind in ("linear_attention", "sliding_attention", "mamba", "unknown"):
            with self.subTest(kind=kind):
                config = _qwen_config() | {"layer_types": [kind] * 64}
                with self.assertRaisesRegex(
                    GeometryError, "supports only full_attention layers"
                ):
                    derive_geometry(config, chunk_tokens=256)

    def test_rejects_uniform_non_full_layers_in_text_config(self) -> None:
        """A wrapper cannot hide unsupported uniform layers behind text_config."""
        config = _qwen_config() | {
            "text_config": {
                "num_hidden_layers": 32,
                "layer_types": ["linear_attention"] * 32,
            }
        }
        with self.assertRaisesRegex(
            GeometryError, r"unsupported layer layout \(text_config.layer_types"
        ):
            derive_geometry(config, chunk_tokens=256)

    def test_rejects_malformed_explicit_layer_types(self) -> None:
        """An invalid layer declaration cannot stand in for a supported layout."""
        for kinds in ([], "full_attention", {}, 1, True, [None] * 64, [""] * 64):
            with self.subTest(layer_types=kinds):
                config = _qwen_config() | {"layer_types": kinds}
                with self.assertRaisesRegex(
                    GeometryError, "non-empty list of layer-type names"
                ):
                    derive_geometry(config, chunk_tokens=256)

    def test_rejects_layer_types_count_mismatch(self) -> None:
        """One full-attention label cannot certify all the model's layers."""
        for count in (1, 63, 65):
            with self.subTest(count=count):
                config = _qwen_config() | {"layer_types": ["full_attention"] * count}
                with self.assertRaisesRegex(
                    GeometryError, rf"has {count} entries.*num_hidden_layers is 64"
                ):
                    derive_geometry(config, chunk_tokens=256)

    def test_absent_or_null_layer_types_keep_standard_formula(self) -> None:
        """Legacy configs without an explicit layout keep the same arithmetic."""
        config = _qwen_config()
        self.assertEqual(
            derive_geometry(config | {"layer_types": None}, chunk_tokens=256),
            derive_geometry(config, chunk_tokens=256),
        )

    def test_rejects_each_known_hybrid_field(self) -> None:
        """Hybrid families that mark their layout with a field are refused."""
        for marker, value in (
            ("full_attention_interval", 4),
            ("hybrid_override_pattern", "M-M-M*-"),
            ("attn_layer_period", 8),
            ("sliding_window_pattern", 6),
        ):
            with self.subTest(marker=marker):
                with self.assertRaisesRegex(
                    GeometryError, rf"hybrid layer layout \(config\.{marker} is set\)"
                ):
                    derive_geometry(_qwen_config() | {marker: value}, chunk_tokens=256)

    def test_rejects_hybrid_cache_implementation_only(self) -> None:
        """Alternating sliding and full layers declared only by the cache type are refused."""
        alternating = _qwen_config() | {
            "cache_implementation": "hybrid",
            "sliding_window": 4096,
        }
        with self.assertRaisesRegex(
            GeometryError,
            r'hybrid layer layout \(config\.cache_implementation is "hybrid"\)',
        ):
            derive_geometry(alternating, chunk_tokens=256)
        other = _qwen_config() | {"cache_implementation": "static"}
        self.assertEqual(
            derive_geometry(other, chunk_tokens=256),
            derive_geometry(_qwen_config(), chunk_tokens=256),
        )

    def test_rejects_hybrid_text_config_inside_a_wrapper(self) -> None:
        """A Qwen3.5-shaped wrapper is refused for its layout, not a missing field."""
        with self.assertRaisesRegex(
            GeometryError,
            r"text_config\.layer_types: 24 linear_attention, 8 full_attention",
        ):
            derive_geometry(_qwen35_wrapper_config(), chunk_tokens=256)

    def test_flattened_hybrid_text_config_is_refused_too(self) -> None:
        """The same layer fields given as a flat config do not slip through."""
        flat = dict(_qwen35_wrapper_config()["text_config"])
        with self.assertRaisesRegex(GeometryError, "hybrid layer layout"):
            derive_geometry(flat, chunk_tokens=256)

    def test_wrapper_without_top_level_layers_names_text_config(self) -> None:
        """A standard text_config under a wrapper gets a message that says where."""
        wrapper = {
            "architectures": ["SomeVisionLanguageModel"],
            "text_config": _qwen_config(),
        }
        with self.assertRaisesRegex(
            GeometryError, "num_hidden_layers is under text_config"
        ):
            derive_geometry(wrapper, chunk_tokens=256)

    def test_uniform_layer_types_and_unused_sliding_window_are_accepted(self) -> None:
        """Standard models that carry these fields keep their geometry."""
        uniform = _qwen_config() | {"layer_types": ["full_attention"] * 64}
        self.assertEqual(
            derive_geometry(uniform, chunk_tokens=256),
            derive_geometry(_qwen_config(), chunk_tokens=256),
        )
        qwen25_7b = {
            "architectures": ["Qwen2ForCausalLM"],
            "hidden_size": 3584,
            "max_window_layers": 28,
            "num_attention_heads": 28,
            "num_hidden_layers": 28,
            "num_key_value_heads": 4,
            "sliding_window": 131072,
            "torch_dtype": "bfloat16",
            "use_sliding_window": False,
        }
        result = derive_geometry(qwen25_7b, chunk_tokens=256)
        self.assertEqual(result["geometry"]["full_model"]["bytes_per_token"], 57344)
        self.assertEqual(result["geometry"]["full_model"]["bytes_per_chunk"], 14680064)

    def test_cli_refuses_a_hybrid_config_with_exit_two(self) -> None:
        """The command line prints the reason and writes no geometry."""
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.json"
            config.write_text(json.dumps(_qwen35_wrapper_config()), encoding="utf-8")
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
                    "--json",
                ],
                cwd=ROOT,
                capture_output=True,
                check=False,
                text=True,
            )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("geometry error: hybrid layer layout (text_config.layer_types", result.stderr)

    def test_cli_writes_no_geometry_for_unsupported_uniform_layers(self) -> None:
        """Unsupported uniform layers exit with an explanation and no artifact."""
        config = _qwen_config() | {"layer_types": ["linear_attention"] * 64}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            output = Path(directory) / "geometry.json"
            path.write_text(json.dumps(config), encoding="utf-8")
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "kvprobe",
                    "geometry",
                    "--model-config",
                    str(path),
                    "--chunk-tokens",
                    "256",
                    "--output",
                    str(output),
                    "--json",
                ],
                cwd=ROOT,
                capture_output=True,
                check=False,
                text=True,
            )
            self.assertFalse(output.exists())
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("supports only full_attention layers", result.stderr)

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
