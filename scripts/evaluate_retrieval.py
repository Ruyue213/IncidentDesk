"""Reproducible local lexical/dense/hybrid ablation over original synthetic runbooks."""

import argparse
import hashlib
import importlib.metadata
import json
import math
import shutil
import statistics
import subprocess
import tempfile
import time
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

from incident_desk.knowledge.embeddings import LocalEmbeddings
from incident_desk.knowledge.project_store import (
    CHUNK_CHARS,
    CHUNKER_VERSION,
    RRF_K,
    ProjectKnowledgeStore,
    canonical,
    digest,
)
from incident_desk.projects import ProjectProfile

ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "evals" / "retrieval"
FREEZE = DATASET / "freeze-v1.json"
MODES = ("lexical", "dense", "hybrid")
TOP_K = 5


def dataset_manifest() -> dict:
    paths = [*sorted((DATASET / "corpus").glob("*.md"))]
    paths += [DATASET / "development.json", DATASET / "holdout.json"]
    hashes = {
        path.relative_to(DATASET).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in paths
    }
    return {
        "schema": 1,
        "dataset": "synthetic-service-runbooks-v1",
        "files": hashes,
        "sha256": digest(hashes),
    }


def validate_dataset() -> dict:
    current = dataset_manifest()
    if not FREEZE.is_file() or json.loads(FREEZE.read_text()) != current:
        raise ValueError("Dataset is not frozen or differs from freeze-v1.json")
    return current


def metrics(items: list[dict], relevance: dict[str, int], *, valid: bool) -> dict:
    paths = [item["provenance"]["relative_path"] for item in items] if valid else []
    positives = {path for path, grade in relevance.items() if grade > 0}
    first = next((i for i, path in enumerate(paths, 1) if path in positives), None)
    result = {
        f"recall@{k}": len(set(paths[:k]) & positives) / len(positives) for k in (1, 3, TOP_K)
    }
    result["mrr@5"] = 1 / first if first else 0.0
    seen, gain = set(), 0.0
    for rank, path in enumerate(paths[:TOP_K], 1):
        if path not in seen:
            gain += (2 ** relevance.get(path, 0) - 1) / math.log2(rank + 1)
        seen.add(path)
    ideal = sum(
        (2**grade - 1) / math.log2(rank + 1)
        for rank, grade in enumerate(sorted(relevance.values(), reverse=True)[:TOP_K], 1)
    )
    result["ndcg@5"] = gain / ideal if ideal else 0.0
    return result


def aggregate(rows: list[dict]) -> dict:
    measured = [row["latency_ms"] for row in rows]
    ordered = sorted(measured)
    return {
        "queries": len(rows),
        "successful_queries": sum(row["valid_mode"] for row in rows),
        "failed_or_degraded_queries": sum(not row["valid_mode"] for row in rows),
        "metrics": {
            key: round(statistics.mean(row["metrics"][key] for row in rows), 6)
            for key in ("recall@1", "recall@3", "recall@5", "mrr@5", "ndcg@5")
        },
        "latency_ms": {
            "p50": round(statistics.median(measured), 3),
            "p95": round(ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)], 3),
            "max": round(max(measured), 3),
        },
    }


def version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def code_identity() -> dict:
    git = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    paths = [
        ROOT / "src/incident_desk/knowledge/project_store.py",
        ROOT / "src/incident_desk/knowledge/embeddings.py",
        ROOT / "src/incident_desk/knowledge/store.py",
        Path(__file__),
        ROOT / "uv.lock",
    ]
    return {
        "git_commit": git.stdout.strip() if git.returncode == 0 else None,
        "file_sha256": {
            path.relative_to(ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in paths
        },
    }


def evaluate(args) -> dict:
    freeze = validate_dataset()
    specification = json.loads((DATASET / f"{args.split}.json").read_text())
    model = LocalEmbeddings(args.local_dir)
    state = model.status()
    if args.require_dense and state["state"] != "ready":
        raise ValueError("Dense comparison requires the explicitly installed pinned local model")
    warmup_started = time.perf_counter()
    warmup_error = None
    if state["state"] == "ready":
        try:
            model.encode(["local retrieval model warmup"], kind="query")
        except RuntimeError:
            warmup_error = "local_embedding_inference_failed"
            if args.require_dense:
                raise ValueError("Pinned model could not perform genuine local inference") from None
    warmup_ms = round((time.perf_counter() - warmup_started) * 1000, 3)
    with tempfile.TemporaryDirectory(prefix="incidentdesk-retrieval-") as directory:
        temporary = Path(directory)
        project_root = temporary / "project"
        shutil.copytree(DATASET / "corpus", project_root / "docs")
        project = ProjectProfile(
            id="retrieval-benchmark-v1",
            name="Synthetic retrieval benchmark",
            root=project_root,
            knowledge_paths=["docs"],
        )
        store = ProjectKnowledgeStore(temporary / "state", embeddings=model)
        index = store.ingest(project)
        if args.require_dense and not index["embedding"]["indexed"]:
            raise ValueError("Dense indexing failed; no valid three-mode ablation was produced")
        results = []
        # Every mode sees exactly the same frozen generation and chunk-result budget.
        for query in specification["queries"]:
            for mode in MODES:
                started = time.perf_counter()
                error = None
                try:
                    batch = store.search(
                        project.id, index["generation"], query["query"], mode=mode, top_k=TOP_K
                    )
                except (ValueError, OSError, RuntimeError) as failure:
                    # Record a sanitized failure without dropping the query from the denominator.
                    error = type(failure).__name__
                    batch = {"status": "unavailable", "items": [], "retrieval": {}}
                elapsed = round((time.perf_counter() - started) * 1000, 3)
                valid = (
                    batch["status"] == "complete"
                    and batch["retrieval"].get("effective_mode") == mode
                )
                results.append(
                    {
                        "query_id": query["id"],
                        "kind": query["kind"],
                        "query": query["query"],
                        "mode": mode,
                        "valid_mode": valid,
                        "status": batch["status"],
                        "effective_mode": batch["retrieval"].get("effective_mode"),
                        "latency_ms": elapsed,
                        "error": error or batch.get("error"),
                        "metrics": metrics(batch["items"], query["relevance"], valid=valid),
                        "relevance": query["relevance"],
                        "hits": [
                            {
                                "id": item["id"],
                                "path": item["provenance"]["relative_path"],
                                "line_start": item["provenance"]["line_start"],
                                "line_end": item["provenance"]["line_end"],
                            }
                            for item in batch["items"]
                        ],
                        "ranking": batch["retrieval"].get("hits", []),
                    }
                )
    by_mode, by_kind = {}, defaultdict(dict)
    for mode in MODES:
        selected = [row for row in results if row["mode"] == mode]
        by_mode[mode] = aggregate(selected)
        for kind in sorted({row["kind"] for row in selected}):
            by_kind[kind][mode] = aggregate([row for row in selected if row["kind"] == kind])
    return {
        "schema": 1,
        "evaluated_at": datetime.now(UTC).isoformat(),
        "dataset": freeze,
        "split": args.split,
        "split_sha256": freeze["files"][f"{args.split}.json"],
        "code": code_identity(),
        "environment": {
            name: version(name)
            for name in (
                "sentence-transformers",
                "torch",
                "transformers",
                "huggingface-hub",
                "jieba",
            )
        },
        "protocol": {
            "chunker": CHUNKER_VERSION,
            "chunk_chars": CHUNK_CHARS,
            "lexical": "generation-scoped-bm25-jieba-v1",
            "dense": "exact normalized cosine",
            "rrf_k": RRF_K,
            "fusion_candidates_per_channel": 50,
            "top_k_chunk_slots": TOP_K,
            "document_diversity_first_pass": 2,
            "failed_or_degraded_queries_count_as_zero": True,
            "public_synthetic_only": True,
            "cloud_model_calls": 0,
        },
        "model": state,
        "model_warmup_ms": warmup_ms,
        "model_warmup_error": warmup_error,
        "index": index,
        "summary": by_mode,
        "by_query_kind": dict(by_kind),
        "failures": [row for row in results if not row["valid_mode"]],
        "results": results,
        "limitations": [
            "Small synthetic corpus; not a production query distribution or diagnosis benchmark",
            "Chinese paraphrases intentionally test cross-language English-document retrieval",
            "Public holdout supports regression checks but cannot guarantee future model isolation",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--local-dir", type=Path, default=ROOT / ".local")
    parser.add_argument("--split", choices=("development", "holdout"), default="development")
    parser.add_argument("--require-dense", action="store_true")
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--freeze", action="store_true", help="freeze v1 inputs without running queries"
    )
    args = parser.parse_args()
    try:
        if args.freeze:
            manifest = dataset_manifest()
            if FREEZE.exists() and json.loads(FREEZE.read_text()) != manifest:
                raise ValueError("Refusing to overwrite a different frozen v1 dataset")
            FREEZE.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
            print(canonical({"dataset_sha256": manifest["sha256"], "frozen": True}))
            return 0
        report = evaluate(args)
    except ValueError as error:
        print(canonical({"status": "failed", "error": str(error)}))
        return 2
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        args.output.chmod(0o600)
    print(
        json.dumps(
            {
                "split": args.split,
                "summary": report["summary"],
                "failures": len(report["failures"]),
                "dataset_sha256": report["dataset"]["sha256"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 2 if args.require_dense and report["failures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
