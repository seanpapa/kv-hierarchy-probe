# SPDX-License-Identifier: Apache-2.0
"""Load one model configuration without downloading model weights."""

# Standard
import hashlib
import json
import re
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


class ModelSourceError(ValueError):
    """Raised when a model reference cannot yield a reviewable configuration."""


_MAX_CONFIG_BYTES = 4 * 1024 * 1024
_REPO_ID = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]*(?:/[A-Za-z0-9][A-Za-z0-9._-]*)?$"
)
_FULL_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_COMMIT_IN_URL = re.compile(r"/([0-9a-f]{40})/config\.json(?:\?|$)")


def _parse_config(raw: bytes, source: str) -> dict[str, Any]:
    """Parse a bounded UTF-8 model configuration.

    Args:
        raw: Raw ``config.json`` bytes.
        source: Human-readable source used in errors.

    Returns:
        Parsed JSON object.

    Raises:
        ModelSourceError: If the payload is too large, invalid UTF-8/JSON, or
            is not a JSON object.
    """
    if len(raw) > _MAX_CONFIG_BYTES:
        raise ModelSourceError(
            f"model config from {source} exceeds {_MAX_CONFIG_BYTES} bytes"
        )
    try:
        config = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ModelSourceError(f"invalid model config from {source}: {exc}") from exc
    if not isinstance(config, dict):
        raise ModelSourceError(f"model config from {source} must be a JSON object")
    return config


def _config_sha256(raw: bytes) -> str:
    """Return the stable content identity for one config payload."""
    return hashlib.sha256(raw).hexdigest()


def load_model_config_file(
    path: Path, *, input_source: str = "model_config"
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Load a local model config and return non-sensitive provenance.

    Args:
        path: Path to ``config.json``.
        input_source: Public source label recorded in the geometry document.

    Returns:
        Parsed configuration and provenance fields for ``geometry.model``.

    Raises:
        ModelSourceError: If the file is missing, unreadable, invalid, or too
            large.
    """
    try:
        raw = path.read_bytes()
    except FileNotFoundError as exc:
        raise ModelSourceError(
            f"model config file not found: {path}; use a config.json readable "
            "from this shell (copy it from the serving container or use the "
            "host-mounted path). Model weights are not required. See "
            "docs/GEOMETRY.md"
        ) from exc
    except OSError as exc:
        raise ModelSourceError(f"cannot read model config {path}: {exc}") from exc
    config = _parse_config(raw, str(path))
    return config, {
        "input_source": input_source,
        "config_source": {
            "kind": "local_file",
            "file_name": path.name,
            "config_sha256": _config_sha256(raw),
        },
    }


def _looks_like_missing_local_path(model: str) -> bool:
    """Return whether a missing model reference was clearly intended as a path."""
    return model.startswith(("/", "./", "../", "~")) or model.endswith(".json")


def _resolved_revision(requested: str, headers: Any, final_url: str) -> str:
    """Extract the immutable Hub commit represented by one response."""
    header = headers.get("X-Repo-Commit") or headers.get("x-repo-commit")
    if isinstance(header, str) and _FULL_COMMIT.fullmatch(header.lower()):
        return header.lower()
    if _FULL_COMMIT.fullmatch(requested.lower()):
        return requested.lower()
    match = _COMMIT_IN_URL.search(final_url)
    if match is not None:
        return match.group(1)
    raise ModelSourceError(
        "Hugging Face response did not expose a resolved commit; retry with "
        "--revision set to the full 40-character commit SHA"
    )


def _load_huggingface_config(
    model_id: str, revision: str | None
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Fetch only ``config.json`` for one public Hugging Face model."""
    if not _REPO_ID.fullmatch(model_id):
        raise ModelSourceError(
            f"invalid model reference {model_id!r}; use an existing local model "
            "directory or a Hugging Face model ID such as owner/model"
        )
    requested_revision = revision or "main"
    quoted_model = "/".join(quote(part, safe="") for part in model_id.split("/"))
    quoted_revision = quote(requested_revision, safe="")
    url = (
        f"https://huggingface.co/{quoted_model}/resolve/"
        f"{quoted_revision}/config.json"
    )
    request = Request(
        url,
        headers={"User-Agent": "kv-hierarchy-probe/0.1 (config-only)"},
    )
    try:
        with urlopen(request, timeout=15) as response:
            raw = response.read(_MAX_CONFIG_BYTES + 1)
            resolved = _resolved_revision(
                requested_revision, response.headers, response.geturl()
            )
            etag = response.headers.get("ETag")
    except HTTPError as exc:
        if exc.code in {401, 403}:
            detail = (
                "the model is gated or private; download config.json with your "
                "approved credentials and use --model-config"
            )
        elif exc.code == 404:
            detail = "model, revision, or config.json was not found"
        else:
            detail = f"HTTP {exc.code}"
        raise ModelSourceError(
            f"cannot fetch Hugging Face config for {model_id}@{requested_revision}: "
            f"{detail}"
        ) from exc
    except URLError as exc:
        raise ModelSourceError(
            f"cannot fetch Hugging Face config for {model_id}@{requested_revision}: "
            f"{exc.reason}"
        ) from exc
    except OSError as exc:
        raise ModelSourceError(
            "cannot fetch Hugging Face config for "
            f"{model_id}@{requested_revision}: {exc}"
        ) from exc

    config = _parse_config(raw, f"{model_id}@{resolved}")
    source: dict[str, Any] = {
        "kind": "huggingface",
        "model_id": model_id,
        "requested_revision": requested_revision,
        "resolved_revision": resolved,
        "config_sha256": _config_sha256(raw),
        "weights_downloaded": False,
    }
    if isinstance(etag, str) and etag:
        source["etag"] = etag
    return config, {"input_source": "model", "config_source": source}


def load_model_reference(
    model: str, *, revision: str | None = None
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Resolve a local model directory/file or a public Hugging Face model ID.

    Args:
        model: Existing local model directory or ``config.json`` path, or a
            public Hugging Face model ID.
        revision: Optional Hugging Face branch, tag, or full commit SHA.

    Returns:
        Parsed model configuration and provenance fields for
        ``geometry.model``. Remote resolution fetches only ``config.json``.

    Raises:
        ModelSourceError: If the reference is invalid, inaccessible, or does
            not resolve to a pinned configuration.
    """
    local = Path(model).expanduser()
    if local.exists():
        if revision is not None:
            raise ModelSourceError(
                "--revision applies only when --model is a Hugging Face model ID"
            )
        config_path = local / "config.json" if local.is_dir() else local
        config, provenance = load_model_config_file(
            config_path, input_source="model"
        )
        provenance["config_source"].update(
            {
                "kind": "local_model_directory" if local.is_dir() else "local_file",
                "model_name": local.name,
            }
        )
        return config, provenance
    if _looks_like_missing_local_path(model):
        raise ModelSourceError(f"local model path not found: {model}")
    return _load_huggingface_config(model, revision)
