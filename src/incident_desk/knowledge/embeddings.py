"""Explicitly installed local E5 embeddings; inference never downloads a model."""

import importlib.util
import json
import math
import os
import threading
from pathlib import Path

MODEL_ID = "intfloat/multilingual-e5-small"
MODEL_REVISION = "fd1525a9fd15316a2d503bf26ab031a61d056e98"
DIMENSIONS = 384
FINGERPRINT = f"{MODEL_ID}@{MODEL_REVISION}:384:l2:query-passage-v1"
_LOCK = threading.RLock()
_MODELS: dict[str, object] = {}


class EmbeddingUnavailable(RuntimeError):
    """A declared local capability is absent; callers must report degradation."""


def model_path(local_dir: str | Path) -> Path:
    return Path(local_dir) / "models" / "multilingual-e5-small" / MODEL_REVISION


def download_model(local_dir: str | Path) -> dict:
    """Explicit setup operation. Download only pinned public safetensors and tokenizer files."""
    try:
        from huggingface_hub import snapshot_download
    except ImportError as error:
        raise EmbeddingUnavailable("Install the optional rag dependencies first") from error
    destination = model_path(local_dir)
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    if destination.is_symlink():
        raise ValueError("Model directory cannot be a symlink")
    snapshot_download(
        repo_id=MODEL_ID,
        revision=MODEL_REVISION,
        local_dir=destination,
        token=False,
        allow_patterns=[
            "config.json",
            "modules.json",
            "sentence_bert_config.json",
            "model.safetensors",
            "tokenizer.json",
            "tokenizer_config.json",
            "special_tokens_map.json",
            "sentencepiece.bpe.model",
            "1_Pooling/config.json",
            "README.md",
        ],
        max_workers=2,
    )
    manifest = destination / "incidentdesk-model.json"
    manifest.write_text(
        json.dumps(
            {
                "model_id": MODEL_ID,
                "revision": MODEL_REVISION,
                "dimensions": DIMENSIONS,
                "fingerprint": FINGERPRINT,
            }
        )
    )
    manifest.chmod(0o600)
    return LocalEmbeddings(local_dir).status()


class LocalEmbeddings:
    """CPU inference with fixed prefixes, normalization, revision and no remote code."""

    fingerprint = FINGERPRINT

    def __init__(self, local_dir: str | Path):
        self.path = model_path(local_dir)

    def status(self) -> dict:
        installed = importlib.util.find_spec("sentence_transformers") is not None
        present = (self.path / "model.safetensors").is_file()
        manifest = self.path / "incidentdesk-model.json"
        valid_manifest = False
        if manifest.is_file():
            try:
                valid_manifest = json.loads(manifest.read_text()).get("fingerprint") == FINGERPRINT
            except (OSError, ValueError):
                pass
        ready = installed and present and valid_manifest
        return {
            "state": "ready"
            if ready
            else "missing_dependencies"
            if not installed
            else "not_installed",
            "model_id": MODEL_ID,
            "revision": MODEL_REVISION,
            "dimensions": DIMENSIONS,
            "fingerprint": FINGERPRINT,
            "local_only": True,
            "max_tokens": 512,
            "download_required": not present or not valid_manifest,
        }

    def _model(self):
        if self.status()["state"] != "ready":
            raise EmbeddingUnavailable("Pinned embedding model is not installed locally")
        key = str(self.path.resolve())
        with _LOCK:
            if key not in _MODELS:
                # Bound CPU fan-out; optional packages are imported only for explicit inference.
                import torch
                from sentence_transformers import SentenceTransformer

                torch.set_num_threads(min(4, os.cpu_count() or 1))
                _MODELS[key] = SentenceTransformer(
                    str(self.path),
                    device="cpu",
                    local_files_only=True,
                    trust_remote_code=False,
                    token=False,
                )
            return _MODELS[key]

    def encode(self, texts: list[str], *, kind: str) -> list[list[float]]:
        if kind not in {"query", "passage"} or len(texts) > 2000:
            raise ValueError("Invalid embedding kind or batch size")
        if not texts:
            return []
        try:
            model = self._model()
            with _LOCK:
                values = model.encode(
                    [f"{kind}: {text}" for text in texts],
                    normalize_embeddings=True,
                    batch_size=16,
                    show_progress_bar=False,
                    convert_to_numpy=True,
                ).tolist()
        except Exception as error:
            raise EmbeddingUnavailable("Local embedding inference failed") from error
        for vector in values:
            if len(vector) != DIMENSIONS or any(not math.isfinite(v) for v in vector):
                raise EmbeddingUnavailable("Embedding dimensions or numeric values are invalid")
            norm = math.sqrt(sum(v * v for v in vector))
            if abs(norm - 1) > 0.01:
                raise EmbeddingUnavailable("Embedding vectors must be normalized")
        return values


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--local-dir", type=Path, required=True)
    parser.add_argument("--download", action="store_true", help="explicit network setup")
    args = parser.parse_args()
    result = (
        download_model(args.local_dir)
        if args.download
        else LocalEmbeddings(args.local_dir).status()
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
