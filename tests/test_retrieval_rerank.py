"""Frozen candidate policy, local isolation and private audit contracts; no downloads."""

import hashlib
import json
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from incident_desk.knowledge import reranker, retrieval_audit
from incident_desk.knowledge.project_store import ProjectKnowledgeStore
from incident_desk.knowledge.reranker import LocalCrossEncoder, RerankerUnavailable
from incident_desk.knowledge.retrieval_audit import RetrievalAudit
from incident_desk.knowledge.retrieval_config import RERANKER_FINGERPRINT, RetrievalConfig
from incident_desk.projects import ProjectProfile


class FakeEmbeddings:
    fingerprint = "test-only-embedding-v1"

    def status(self):
        return {"state": "ready"}

    def encode(self, texts, *, kind):
        return [[1.0] + [0.0] * 383 for _ in texts]


class FakeReranker:
    """Explicit test double: reverse candidates, without implying semantic quality."""

    fingerprint = RERANKER_FINGERPRINT

    def __init__(self, *, error=None, invalid=False):
        self.calls, self.error, self.invalid = [], error, invalid

    def status(self):
        return {"state": "ready", "model_id": "test-only-reranker", "fingerprint": self.fingerprint}

    def score(self, query, passages):
        self.calls.append((query, passages))
        if self.error:
            raise RerankerUnavailable(self.error, "Synthetic inference failure")
        return {
            "scores": [float("nan")] * len(passages)
            if self.invalid
            else list(map(float, range(len(passages)))),
            "pair_count": len(passages),
            "truncation": {"pairs_truncated": 0},
        }


def corpus(tmp_path, model=None):
    root = tmp_path / "project"
    docs = root / "docs"
    docs.mkdir(parents=True)
    for index in range(12):
        (docs / f"guide-{index}.md").write_text(f"# Guide {index}\nqueue evidence rule {index}.\n")
    project = ProjectProfile(id="bench", name="Bench", root=root, knowledge_paths=["docs"])
    store = ProjectKnowledgeStore(
        tmp_path / "state", embeddings=FakeEmbeddings(), reranker=model or FakeReranker()
    )
    return store, project, store.ingest(project)["generation"]


@pytest.mark.parametrize(
    "fields",
    [
        {"candidate_n": 4},
        {"candidate_n": 101},
        {"candidate_n": True},
        {"top_k": 0},
        {"top_k": 11},
        {"rerank_top_n": 101},
        {"top_k": 6, "rerank_top_n": 5},
        {"candidate_n": 5, "rerank_top_n": 11},
        {"reranker": "unknown"},
    ],
)
def test_retrieval_budget_rejects_invalid_or_incoherent_fields(fields):
    with pytest.raises(ValidationError):
        RetrievalConfig(**fields)


def test_retrieval_policy_is_frozen_and_includes_model_identity():
    config = RetrievalConfig()
    assert config.model_dump()["reranker_fingerprint"] == RERANKER_FINGERPRINT
    assert config.policy_version and config.reranker == "off"
    with pytest.raises(ValidationError):
        config.top_k = 1


def test_same_candidates_rerank_before_diversity_and_scores_do_not_mutate_citations(tmp_path):
    model = FakeReranker()
    store, project, generation = corpus(tmp_path, model)
    base = RetrievalConfig(candidate_n=5, rerank_top_n=6, top_k=3)
    before = store.search(project.id, generation, "queue", retrieval_config=base)
    after = store.search(
        project.id,
        generation,
        "queue",
        retrieval_config=base.model_copy(update={"reranker": "cross_encoder"}),
    )
    trace = after["retrieval"]
    candidates = trace["candidates"]
    assert len(candidates["lexical"]) == len(candidates["dense"]) == 5
    assert len(candidates["fused"]) <= 10
    assert candidates["before_rerank"] == before["retrieval"]["candidates"]["before_rerank"]
    assert [row["id"] for row in candidates["after_rerank"]] == [
        row["id"] for row in reversed(candidates["before_rerank"])
    ]
    assert len(after["items"]) == 3 and len(model.calls) == 1
    assert trace["reranker"]["effective"] == "cross_encoder"
    before_items = {row["id"]: row for row in before["items"]}
    for item in after["items"]:
        immutable = store.search(project.id, generation, "queue", top_k=10)["items"]
        assert item == next(row for row in immutable if row["id"] == item["id"])
        if item["id"] in before_items:
            assert item == before_items[item["id"]]
    assert "cross_encoder" not in json.dumps(after["items"])
    with pytest.raises(ValueError, match="conflicts"):
        store.search(project.id, generation, "queue", top_k=2, retrieval_config=base)


@pytest.mark.parametrize("failure", ["reranker_timeout", "not_installed", "invalid_scores"])
def test_reranker_failure_explicitly_preserves_base_order(tmp_path, failure):
    model = FakeReranker(
        error=None if failure == "invalid_scores" else failure, invalid=failure == "invalid_scores"
    )
    store, project, generation = corpus(tmp_path, model)
    base = RetrievalConfig()
    original = store.search(project.id, generation, "queue", retrieval_config=base)
    failed = store.search(
        project.id,
        generation,
        "queue",
        retrieval_config=base.model_copy(update={"reranker": "cross_encoder"}),
    )
    assert failed["status"] == "degraded" and failed["items"] == original["items"]
    assert failed["retrieval"]["reranker"]["effective"] == "off"
    assert failed["retrieval"]["reranker"]["error"]["code"] == failure


@pytest.mark.parametrize(
    "update,code",
    [
        (
            {"reranker_fingerprint": "historical-other-model", "reranker": "cross_encoder"},
            "reranker_fingerprint_mismatch",
        ),
        ({"policy_version": "historical-other-policy"}, "unsupported_retrieval_policy"),
    ],
)
def test_historical_config_does_not_silently_load_new_policy_or_model(tmp_path, update, code):
    model = FakeReranker()
    store, project, generation = corpus(tmp_path, model)
    result = store.search(
        project.id, generation, "queue", retrieval_config=RetrievalConfig(**update)
    )
    assert result["status"] == "degraded" and result["error"]["code"] == code
    assert not model.calls


def test_local_uninstalled_reranker_never_starts_process_or_download(tmp_path, monkeypatch):
    model = LocalCrossEncoder(tmp_path)
    monkeypatch.setattr(
        model, "_score_process", lambda *_: pytest.fail("Must fail before spawning")
    )
    with pytest.raises(RerankerUnavailable) as error:
        model.score("question", ["passage"])
    assert error.value.code in {"not_installed", "missing_dependencies"}
    assert not (tmp_path / "models").exists()


def test_real_timeout_kills_and_reaps_dedicated_worker(tmp_path, monkeypatch):
    model = LocalCrossEncoder(tmp_path, timeout_seconds=0.08)
    monkeypatch.setattr(model, "status", lambda: {"state": "ready"})
    monkeypatch.setattr(
        model, "_command", lambda: [sys.executable, "-c", "import time;time.sleep(30)"]
    )
    children, actual = [], subprocess.Popen

    def spawn(*args, **kwargs):
        child = actual(*args, **kwargs)
        children.append(child)
        assert "DEEPSEEK_API_KEY" not in kwargs["env"]
        assert kwargs["env"]["HF_HUB_OFFLINE"] == "1"
        return child

    monkeypatch.setenv("DEEPSEEK_API_KEY", "synthetic-never-inherited")
    monkeypatch.setattr(reranker.subprocess, "Popen", spawn)
    started = time.perf_counter()
    with pytest.raises(RerankerUnavailable) as error:
        model.score("question", ["passage"])
    assert error.value.code == "reranker_timeout"
    assert error.value.metadata["termination"] == "killed_and_reaped"
    assert time.perf_counter() - started < 3
    assert len(children) == 1 and children[0].poll() == -signal.SIGKILL


def test_cancellation_before_launch_never_starts_inference(tmp_path, monkeypatch):
    model = LocalCrossEncoder(tmp_path)
    cancelled = threading.Event()
    cancelled.set()
    monkeypatch.setattr(model, "status", lambda: pytest.fail("Cancellation precedes model setup"))
    with pytest.raises(RerankerUnavailable) as error:
        model.score("question", ["passage"], cancel_event=cancelled)
    assert error.value.code == "reranker_cancelled"
    assert error.value.metadata["termination"] == "not_started"


@pytest.mark.parametrize("cancel_stage", ["popen_return", "in_flight"])
def test_running_worker_cancellation_kills_and_reaps_before_deadline(
    tmp_path, monkeypatch, cancel_stage
):
    model = LocalCrossEncoder(tmp_path, timeout_seconds=60)
    cancelled, children, actual = threading.Event(), [], subprocess.Popen
    monkeypatch.setattr(model, "status", lambda: {"state": "ready"})
    monkeypatch.setattr(
        model, "_command", lambda: [sys.executable, "-c", "import time;time.sleep(30)"]
    )

    def spawn(*args, **kwargs):
        child = actual(*args, **kwargs)
        children.append(child)
        if cancel_stage == "popen_return":
            cancelled.set()  # Cancellation races with a successful Popen return.
        else:
            # Exercise repeated communicate(timeout) calls before cancellation,
            # including its retained stdin offset rather than resending input.
            threading.Timer(0.25, cancelled.set).start()
        return child

    monkeypatch.setattr(reranker.subprocess, "Popen", spawn)
    started = time.perf_counter()
    with pytest.raises(RerankerUnavailable) as error:
        model.score("question", ["passage"], cancel_event=cancelled)
    assert error.value.code == "reranker_cancelled"
    assert error.value.metadata["termination"] == "killed_and_reaped"
    assert time.perf_counter() - started < 3
    assert len(children) == 1 and children[0].poll() == -signal.SIGKILL


def test_search_passes_cancel_event_and_keeps_cancellation_degradation_explicit(tmp_path):
    cancelled = threading.Event()

    class CancelableReranker(FakeReranker):
        def score(self, query, passages, *, cancel_event=None):
            assert cancel_event is cancelled
            raise RerankerUnavailable("reranker_cancelled", "Synthetic operator cancellation")

    store, project, generation = corpus(tmp_path, CancelableReranker())
    result = store.search(
        project.id,
        generation,
        "queue",
        cancel_event=cancelled,
        retrieval_config=RetrievalConfig(reranker="cross_encoder"),
    )
    assert result["status"] == "degraded"
    assert result["retrieval"]["reranker"]["error"]["code"] == "reranker_cancelled"
    assert result["retrieval"]["reranker"]["effective"] == "off"


def test_pair_preparation_enforces_separate_query_and_combined_token_budgets():
    class Encoding:
        def __init__(self, ids):
            self.ids = ids
            self.attention_mask = [1] * len(ids)

        def truncate(self, limit, *, direction):
            assert direction == "right"
            self.ids = self.ids[:limit]

    class Backend:
        def no_truncation(self):
            pass

        def no_padding(self):
            pass

        def encode(self, text, *, add_special_tokens):
            assert not add_special_tokens
            return Encoding(list(range(len(text))))

        def post_process(self, first, second, *, add_special_tokens):
            assert add_special_tokens
            return Encoding([0, *first.ids, 2, 2, *second.ids, 2])

    tokenizer = SimpleNamespace(
        backend_tokenizer=Backend(), num_special_tokens_to_add=lambda **_: 4
    )
    features, trace = reranker._prepare_pairs(tokenizer, "q" * 1000, ["p" * 4000, "p"])
    assert [len(row["input_ids"]) for row in features] == [512, 133]
    assert features[0]["input_ids"][1:129] == list(range(128))
    assert trace == {"query_truncated": True, "passages_truncated": 1, "pairs_truncated": 2}


def test_worker_non_finite_output_is_rejected(tmp_path, monkeypatch):
    model = LocalCrossEncoder(tmp_path)
    monkeypatch.setattr(model, "status", lambda: {"state": "ready"})
    monkeypatch.setattr(
        model,
        "_command",
        lambda: [
            sys.executable,
            "-c",
            "import sys;sys.stdin.buffer.read();sys.stdout.write('{\"scores\":[NaN]}')",
        ],
    )
    with pytest.raises(RerankerUnavailable) as error:
        model.score("question", ["passage"])
    assert error.value.code == "invalid_scores"


def test_audit_saves_hashes_and_candidate_metadata_never_query_or_passage(tmp_path):
    audit = RetrievalAudit(tmp_path)
    query, passage = "private query text", "private candidate passage"
    batch = {
        "status": "degraded",
        "error": {"code": "reranker_timeout", "message": query},
        "items": [{"id": "one", "content": passage}],
        "retrieval": {
            "phase_counts": {"returned": 1, "query": query},
            "reranker": {"truncation": {"query": query}},
            "candidates": {
                "before_rerank": [
                    {
                        "id": "one",
                        "rank": 1,
                        "path": "docs/guide.md",
                        "content": passage,
                        "scores": {"rrf": 0.1},
                    }
                ]
            },
        },
    }
    saved = audit.record("run-one", query, batch)
    assert saved["query_sha256"] == hashlib.sha256(query.encode()).hexdigest()
    assert audit.for_run("run-two") == []
    encoded = json.dumps(audit.for_run("run-one"))
    assert query not in encoded and passage not in encoded and '"content"' not in encoded
    assert audit.path.stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(audit.path) as db:
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_audit_enforces_per_run_and_global_retention(tmp_path, monkeypatch):
    monkeypatch.setattr(retrieval_audit, "MAX_PER_RUN", 3)
    monkeypatch.setattr(retrieval_audit, "MAX_RECORDS", 5)
    audit = RetrievalAudit(tmp_path)
    for run in ["one", "two"]:
        for index in range(5):
            audit.record(run, f"query {index}", {"status": "complete", "items": []})
    assert len(audit.for_run("two")) == 3 and len(audit.for_run("one")) == 2
    with pytest.raises(ValueError):
        audit.for_run("../outside")


def test_audit_deduplicates_sources_and_bounds_long_unicode_paths(tmp_path):
    audit = RetrievalAudit(tmp_path)
    rows = [
        {
            "id": f"candidate-{index}",
            "rank": index + 1,
            "doc_id": f"doc-{index}",
            "path": "文" * 1200 + f"/{index}.md",
            "line_start": 1,
            "line_end": 4,
            "scores": {"rrf": 0.1, "dense": 0.8, "cross_encoder": 1.0},
        }
        for index in range(200)
    ]
    batch = {
        "status": "complete",
        "items": [{"id": row["id"]} for row in rows[:5]],
        "retrieval": {
            "candidates": {
                stage: rows
                for stage in ("lexical", "dense", "fused", "before_rerank", "after_rerank")
            }
        },
    }
    saved = audit.record("long-path", "query", batch)
    assert saved["audit_truncation"]["truncated"]
    assert any(saved["audit_truncation"]["omitted_rows"].values())
    assert all(row["id"] in saved["candidate_sources"] for row in batch["items"])
    for stage in saved["candidates"].values():
        assert all("path" not in row and row["id"] in saved["candidate_sources"] for row in stage)
    with sqlite3.connect(audit.path) as db:
        size = db.execute("SELECT trace_bytes FROM retrieval_traces").fetchone()[0]
        assert size <= retrieval_audit.MAX_TRACE_BYTES


def test_audit_byte_budget_prunes_and_counters_match_actual_storage(tmp_path, monkeypatch):
    monkeypatch.setattr(retrieval_audit, "MAX_TOTAL_TRACE_BYTES", 2500)
    audit = RetrievalAudit(tmp_path)
    for index in range(12):
        audit.record(f"run-{index}", "query", {"status": "complete", "items": []})
    with sqlite3.connect(audit.path) as db:
        usage = db.execute("SELECT records,trace_bytes FROM retrieval_usage").fetchone()
        actual = db.execute(
            "SELECT count(*),sum(length(CAST(trace_json AS BLOB))) FROM retrieval_traces"
        ).fetchone()
    assert usage == actual and usage[1] <= 2500 and usage[0] < 12
    assert audit.for_run("run-11") and not audit.for_run("run-0")


def test_audit_storage_failure_does_not_discard_successful_retrieval(tmp_path, monkeypatch):
    audit = RetrievalAudit(tmp_path)

    def full():
        raise sqlite3.OperationalError("synthetic full audit database")

    monkeypatch.setattr(audit, "_connect", full)
    result = audit.record("run-one", "query", {"status": "complete", "items": [{"id": "answer"}]})
    assert result == {"status": "unavailable", "error_code": "audit_storage_unavailable"}
