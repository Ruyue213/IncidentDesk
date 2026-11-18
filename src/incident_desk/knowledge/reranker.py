"""Explicit local CrossEncoder setup and killable, offline, bounded CPU inference."""

import argparse
import contextlib
import fcntl
import hashlib
import importlib.util
import json
import math
import os
import resource
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from incident_desk.knowledge.retrieval_config import (
    BATCH_SIZE,
    MAX_PAIR_TOKENS,
    MAX_QUERY_TOKENS,
    RERANKER_FINGERPRINT,
    RERANKER_MODEL_ID,
    RERANKER_REVISION,
)

TIMEOUT_SECONDS = 60
MAX_PAIRS = 100
MAX_PASSAGE_CHARS = 4000
MAX_INPUT_BYTES = 2 * 1024 * 1024
MAX_OUTPUT_BYTES = 65536
REQUIRED_FILES = ("config.json", "model.safetensors", "tokenizer.json", "tokenizer_config.json")


class RerankerUnavailable(RuntimeError):
    def __init__(self, code: str, message: str, metadata: dict | None = None):
        super().__init__(message)
        self.code, self.metadata = code, metadata or {}


def model_path(local_dir: str | Path) -> Path:
    return Path(local_dir) / "models" / "mmarco-mMiniLMv2-L12-H384-v1" / RERANKER_REVISION


def download_model(local_dir: str | Path) -> dict:
    """Explicit network setup. Never called by status(), score(), indexing or search."""
    try:
        from huggingface_hub import snapshot_download
    except ImportError as error:
        raise RerankerUnavailable("missing_dependencies", "Install the rag extra first") from error
    destination = model_path(local_dir)
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    if any(path.is_symlink() for path in (destination, *destination.parents)):
        raise ValueError("Reranker model path cannot contain symlinks")
    snapshot_download(
        repo_id=RERANKER_MODEL_ID,
        revision=RERANKER_REVISION,
        local_dir=destination,
        token=False,
        max_workers=2,
        allow_patterns=[
            *REQUIRED_FILES,
            "special_tokens_map.json",
            "sentencepiece.bpe.model",
            "README.md",
            "LICENSE",
            "NOTICE",
        ],
    )
    files = {}
    for filename in REQUIRED_FILES:
        path = destination / filename
        if path.is_symlink() or not path.is_file():
            raise ValueError("Required pinned reranker file is missing")
        hasher = hashlib.sha256()
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                hasher.update(chunk)
        files[filename] = {"size": path.stat().st_size, "sha256": hasher.hexdigest()}
    manifest = destination / "incidentdesk-reranker.json"
    manifest.write_text(
        json.dumps(
            {
                "fingerprint": RERANKER_FINGERPRINT,
                "model_id": RERANKER_MODEL_ID,
                "revision": RERANKER_REVISION,
                "files": files,
            },
            indent=2,
        )
    )
    manifest.chmod(0o600)
    return LocalCrossEncoder(local_dir).status()


def _validate_input(query, passages):
    if not isinstance(query, str) or not 1 <= len(query.strip()) <= 1000:
        raise ValueError("Reranker query must contain 1..1000 characters")
    if not isinstance(passages, list) or not 1 <= len(passages) <= MAX_PAIRS:
        raise ValueError("Reranker requires 1..100 passages")
    if any(
        not isinstance(text, str) or not 1 <= len(text) <= MAX_PASSAGE_CHARS for text in passages
    ):
        raise ValueError("Reranker passages must contain 1..4000 characters")


def _validated_scores(result, count):
    values = result.get("scores") if isinstance(result, dict) else None
    if (
        not isinstance(values, list)
        or len(values) != count
        or any(type(value) not in (int, float) or not math.isfinite(value) for value in values)
    ):
        raise RerankerUnavailable("invalid_scores", "Reranker returned invalid scores")
    return [float(value) for value in values]


class LocalCrossEncoder:
    fingerprint = RERANKER_FINGERPRINT

    def __init__(self, local_dir: str | Path, *, timeout_seconds: float = TIMEOUT_SECONDS):
        if not 0 < timeout_seconds <= 120:
            raise ValueError("Reranker timeout must be in (0,120] seconds")
        self.local_dir, self.path = Path(local_dir), model_path(local_dir)
        self.timeout_seconds = float(timeout_seconds)

    def status(self) -> dict:
        installed = all(
            importlib.util.find_spec(name) is not None for name in ("torch", "transformers")
        )
        complete = False
        try:
            manifest = json.loads((self.path / "incidentdesk-reranker.json").read_text())
            complete = manifest.get("fingerprint") == self.fingerprint and all(
                not (self.path / name).is_symlink()
                and (self.path / name).is_file()
                and (self.path / name).stat().st_size == manifest["files"][name]["size"]
                for name in REQUIRED_FILES
            )
        except (OSError, ValueError, KeyError, TypeError):
            pass
        return {
            "state": "ready"
            if installed and complete
            else "missing_dependencies"
            if not installed
            else "not_installed",
            "model_id": RERANKER_MODEL_ID,
            "revision": RERANKER_REVISION,
            "fingerprint": self.fingerprint,
            "license": "Apache-2.0",
            "local_only": True,
            "download_required": not complete,
            "device": "cpu",
            "dtype": "float32",
            "score_type": "raw_logit",
            "max_pair_tokens": MAX_PAIR_TOKENS,
            "max_query_tokens": MAX_QUERY_TOKENS,
            "batch_size": BATCH_SIZE,
            "max_pairs": MAX_PAIRS,
            "timeout_seconds": self.timeout_seconds,
            "installation_integrity": "setup SHA256 manifest and required-file sizes",
            "isolation": "new_process_per_call_model_load_included",
        }

    def _command(self):
        return [
            sys.executable,
            "-m",
            "incident_desk.knowledge.reranker",
            "--worker",
            "--local-dir",
            str(self.local_dir.resolve()),
            "--timeout-seconds",
            str(self.timeout_seconds),
            "--parent-pid",
            str(os.getpid()),
        ]

    def score(self, query: str, passages: list[str], *, cancel_event=None) -> dict:
        _validate_input(query, passages)
        if cancel_event is not None and cancel_event.is_set():
            raise RerankerUnavailable(
                "reranker_cancelled", "Local rerank was cancelled", {"termination": "not_started"}
            )
        state = self.status()
        if state["state"] != "ready":
            raise RerankerUnavailable(state["state"], "Pinned reranker is not installed locally")
        payload = json.dumps({"query": query, "passages": passages}, ensure_ascii=False).encode()
        if len(payload) > MAX_INPUT_BYTES:
            raise ValueError("Reranker input exceeds the byte budget")
        self.local_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(
            self.local_dir / "reranker-inference.lock",
            os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        started = time.perf_counter()
        with os.fdopen(descriptor, "r+b") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RerankerUnavailable(
                    "reranker_busy", "Another local rerank is running"
                ) from None
            try:
                return self._score_process(payload, len(passages), started, cancel_event)
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _score_process(self, payload, count, started, cancel_event=None):
        # Do not inherit provider credentials, proxies, .env discovery or user Python hooks.
        env = {
            key: os.environ[key]
            for key in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR")
            if key in os.environ
        }
        env.update(
            PYTHONPATH=str(Path(__file__).resolve().parents[2]),
            HF_HUB_OFFLINE="1",
            TRANSFORMERS_OFFLINE="1",
            HF_DATASETS_OFFLINE="1",
            TOKENIZERS_PARALLELISM="false",
            HF_HUB_DISABLE_PROGRESS_BARS="1",
            OMP_NUM_THREADS="2",
            MKL_NUM_THREADS="2",
        )
        with tempfile.TemporaryFile() as output:
            process = subprocess.Popen(
                self._command(),
                env=env,
                stdin=subprocess.PIPE,
                stdout=output,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            try:
                deadline, pending_input = time.monotonic() + self.timeout_seconds, payload
                while True:
                    cancelled = cancel_event is not None and cancel_event.is_set()
                    remaining = deadline - time.monotonic()
                    if cancelled or remaining <= 0:
                        termination = _kill_and_reap(process)
                        raise RerankerUnavailable(
                            "reranker_cancelled" if cancelled else "reranker_timeout",
                            "Local rerank was cancelled"
                            if cancelled
                            else "Local rerank exceeded its deadline",
                            {
                                "timeout_seconds": self.timeout_seconds,
                                "termination": termination,
                                "latency_ms": round((time.perf_counter() - started) * 1000, 3),
                            },
                        )
                    try:
                        process.communicate(input=pending_input, timeout=min(0.1, remaining))
                        break
                    except subprocess.TimeoutExpired:
                        # communicate retains its input offset across retries. Sending
                        # input twice would be an error and could duplicate pair data.
                        pending_input = None
            except BaseException:
                if process.poll() is None:
                    _kill_and_reap(process)
                raise
            if process.returncode:
                raise RerankerUnavailable("worker_failed", "Local reranker process failed")
            if output.tell() > MAX_OUTPUT_BYTES:
                raise RerankerUnavailable(
                    "invalid_worker_output", "Local rerank output is too large"
                )
            output.seek(0)
            try:
                result = json.loads(output.read(MAX_OUTPUT_BYTES + 1))
            except (ValueError, UnicodeError):
                raise RerankerUnavailable(
                    "invalid_worker_output", "Invalid local rerank output"
                ) from None
        result["scores"] = _validated_scores(result, count)
        result["latency_ms"] = round((time.perf_counter() - started) * 1000, 3)
        return result


def _kill_and_reap(process):
    was_running = process.poll() is None
    if was_running:
        process.kill()
    try:
        process.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        raise RerankerUnavailable(
            "worker_reap_unconfirmed",
            "Local worker exit could not be confirmed",
            {"termination": "unconfirmed"},
        ) from None
    return "killed_and_reaped" if was_running else "exited_and_reaped"


def _prepare_pairs(tokenizer, query, passages):
    """Use the pinned tokenizer's pair template; supports Transformers 5 tokenizers."""
    backend = tokenizer.backend_tokenizer
    backend.no_truncation()
    backend.no_padding()
    encoded_query = backend.encode(query, add_special_tokens=False)
    query_tokens = len(encoded_query.ids)
    encoded_query.truncate(MAX_QUERY_TOKENS, direction="right")
    available = (
        MAX_PAIR_TOKENS - len(encoded_query.ids) - tokenizer.num_special_tokens_to_add(pair=True)
    )
    if available <= 0:
        raise ValueError("Tokenizer pair special tokens exceed the fixed token budget")
    features, truncated = [], 0
    for text in passages:
        encoded_passage = backend.encode(text, add_special_tokens=False)
        truncated += int(len(encoded_passage.ids) > available)
        encoded_passage.truncate(available, direction="right")
        pair = backend.post_process(encoded_query, encoded_passage, add_special_tokens=True)
        if len(pair.ids) > MAX_PAIR_TOKENS:
            raise ValueError("Tokenizer exceeded the fixed pair budget")
        features.append({"input_ids": pair.ids, "attention_mask": pair.attention_mask})
    return features, {
        "query_truncated": query_tokens > MAX_QUERY_TOKENS,
        "passages_truncated": truncated,
        "pairs_truncated": len(passages) if query_tokens > MAX_QUERY_TOKENS else truncated,
    }


def _watch_parent(expected_parent):
    # A hard parent crash cannot set cancel_event. This worker owns no external
    # effect, so it exits itself if the process which spawned it disappears.
    while True:
        if os.getppid() != expected_parent:
            os._exit(125)
        time.sleep(0.1)


def _worker(local_dir, timeout_seconds, parent_pid=None):
    # A hard child-side ceiling also handles a parent process dying mid-request.
    signal.setitimer(signal.ITIMER_REAL, min(122, timeout_seconds + 2))
    threading.Thread(target=_watch_parent, args=(parent_pid or os.getppid(),), daemon=True).start()
    payload = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    if len(payload) > MAX_INPUT_BYTES:
        raise ValueError("Worker input is too large")
    body = json.loads(payload)
    query, passages = body["query"], body["passages"]
    _validate_input(query, passages)
    model = LocalCrossEncoder(local_dir)
    if model.status()["state"] != "ready":
        raise ValueError("Pinned local model is unavailable")
    started = time.perf_counter()
    with contextlib.redirect_stdout(sys.stderr):
        import torch
        from transformers import AutoModelForSequenceClassification, AutoTokenizer

        torch.set_num_threads(min(2, os.cpu_count() or 1))
        torch.set_num_interop_threads(1)
        tokenizer = AutoTokenizer.from_pretrained(
            str(model.path), local_files_only=True, trust_remote_code=False, token=False
        )
        network = (
            AutoModelForSequenceClassification.from_pretrained(
                str(model.path),
                local_files_only=True,
                trust_remote_code=False,
                token=False,
                use_safetensors=True,
                dtype=torch.float32,
            )
            .to("cpu")
            .eval()
        )
        loaded = time.perf_counter()
        features, truncation = _prepare_pairs(tokenizer, query, passages)
        scores = []
        with torch.inference_mode():
            for offset in range(0, len(features), BATCH_SIZE):
                tensors = tokenizer.pad(features[offset : offset + BATCH_SIZE], return_tensors="pt")
                values = network(**tensors).logits.reshape(-1).float().tolist()
                if len(values) != len(features[offset : offset + BATCH_SIZE]):
                    raise ValueError("Reranker head has unexpected dimensions")
                scores.extend(values)
    result = {
        "scores": scores,
        "pair_count": len(passages),
        "model_load_ms": round((loaded - started) * 1000, 3),
        "inference_ms": round((time.perf_counter() - loaded) * 1000, 3),
        "truncation": truncation,
        "max_pair_tokens": MAX_PAIR_TOKENS,
        "max_query_tokens": MAX_QUERY_TOKENS,
        "batch_size": BATCH_SIZE,
        "score_type": "raw_logit",
        "device": "cpu",
        "dtype": "float32",
        "isolation": "new_process_per_call_model_load_included",
        "peak_rss_mib": round(
            resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            / (1024 * 1024 if sys.platform == "darwin" else 1024),
            3,
        ),
    }
    _validated_scores(result, len(passages))
    print(json.dumps(result, allow_nan=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--local-dir", type=Path, required=True)
    parser.add_argument("--download", action="store_true")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--timeout-seconds", type=float, default=TIMEOUT_SECONDS)
    parser.add_argument("--parent-pid", type=int, default=None, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        _worker(args.local_dir, args.timeout_seconds, args.parent_pid)
    else:
        print(
            json.dumps(
                download_model(args.local_dir)
                if args.download
                else LocalCrossEncoder(args.local_dir).status(),
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
