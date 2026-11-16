"""Project knowledge contracts using deterministic test vectors, never cloud inference."""

import json
import math
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from incident_desk.agent.diagnostics import DiagnosticSession
from incident_desk.api.knowledge import create_knowledge_router
from incident_desk.knowledge.embeddings import EmbeddingUnavailable, LocalEmbeddings
from incident_desk.knowledge.project_store import (
    MAX_DOCUMENT_BYTES,
    KnowledgeIndexBusy,
    ProjectKnowledgeStore,
)
from incident_desk.projects import ProjectProfile, ProjectStore
from incident_desk.schemas import RunInput
from incident_desk.settings import Settings
from scripts.evaluate_retrieval import metrics, validate_dataset


class TestEmbeddings:
    """Transparent test double; semantic quality is measured by the separate real benchmark."""

    __test__ = False
    fingerprint = "test-only:384:normalized:v1"

    def __init__(self, *, available=True, broken=False):
        self.available, self.broken, self.calls = available, broken, []

    def status(self):
        return {"state": "ready" if self.available else "not_installed"}

    def encode(self, texts, *, kind):
        self.calls.append((kind, list(texts)))
        if self.broken:
            raise EmbeddingUnavailable("test model failure")
        values = []
        for text in texts:
            vector = [0.0] * 384
            vector[0] = 1.0
            vector[1] = float("queue" in text)
            vector[2] = float("database" in text)
            scale = math.sqrt(sum(v * v for v in vector))
            values.append([v / scale for v in vector])
        return values


def profile(root, *, identity="application", **changes):
    return ProjectProfile(
        id=identity, name=identity, root=root, knowledge_paths=["docs"], **changes
    )


def documents(root):
    docs = root / "docs"
    docs.mkdir(parents=True)
    (docs / "queue.md").write_text("# Queue pressure\nqueue saturation needs evidence.\n")
    (docs / "database.md").write_text("# Database delay\ndatabase acquisition wait matters.\n")
    return docs


def test_project_namespace_and_generation_scoped_ranking(tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    documents(first)
    documents(second)
    store = ProjectKnowledgeStore(tmp_path / "state", embeddings=TestEmbeddings())
    one, two = profile(first, identity="one"), profile(second, identity="two")
    generation = store.ingest(one)["generation"]
    before = store.search("one", generation, "queue", mode="lexical")
    other_generation = store.ingest(two)["generation"]
    after = store.search("one", generation, "queue", mode="lexical")
    assert before["items"] == after["items"]
    assert before["retrieval"]["hits"] == after["retrieval"]["hits"]
    other = store.search("two", other_generation, "queue", mode="lexical")
    assert {x["id"] for x in before["items"]}.isdisjoint(x["id"] for x in other["items"])
    assert store.search("two", generation, "queue")["status"] == "unavailable"
    assert store.active_generation("one", one.version) == generation
    assert store.active_generation("one", one.version + 1) is None
    assert store.search("one", None, "queue")["status"] == "unavailable"


def test_incremental_cache_historical_content_and_immutable_citations(tmp_path):
    docs = documents(tmp_path / "project")
    model = TestEmbeddings()
    store = ProjectKnowledgeStore(tmp_path / "state", embeddings=model)
    project = profile(docs.parent)
    first = store.ingest(project)
    assert first["embedded_chunks"] == 2
    second = store.ingest(project)
    assert first["generation"] == second["generation"] and second["unchanged"]
    assert second["embedding_cache_hits"] == 2 and second["embedded_chunks"] == 0
    old = store.search(project.id, first["generation"], "queue", mode="lexical")
    same = store.search(project.id, first["generation"], "pressure queue", mode="hybrid")
    identity = old["items"][0]["id"]
    assert old["items"][0] == next(item for item in same["items"] if item["id"] == identity)
    assert "scores" not in json.dumps(old["items"])
    (docs / "queue.md").write_text("# Queue pressure\nqueue recovery is now documented.\n")
    changed = store.ingest(project)
    assert changed["generation"] != first["generation"]
    assert changed["embedding_cache_hits"] == 1 and changed["embedded_chunks"] == 1
    assert (
        old["items"]
        == store.search(project.id, first["generation"], "queue", mode="lexical")["items"]
    )
    assert len(store.status(project.id)["generations"]) == 2
    assert store.path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("mode", ["dense", "hybrid"])
def test_missing_and_failed_embeddings_degrade_explicitly(tmp_path, mode):
    root = tmp_path / "project"
    documents(root)
    model = TestEmbeddings(available=False)
    store = ProjectKnowledgeStore(tmp_path / "state", embeddings=model)
    project = profile(root)
    index = store.ingest(project)
    result = store.search(project.id, index["generation"], "queue", mode=mode)
    assert not model.calls and not index["embedding"]["indexed"]
    assert result["status"] == "degraded" and result["items"]
    assert result["retrieval"]["effective_mode"] == "lexical"
    assert result["error"]["code"] == "embedding_unavailable"
    model.available, model.broken = True, True
    failed = store.ingest(project)
    assert failed["embedding"]["state"] == "embedding_inference_failed"


def test_indexing_redacts_before_embedding_and_rejects_unbounded_or_unsafe_sources(tmp_path):
    root = tmp_path / "project"
    docs = documents(root)
    (docs / "queue.md").write_text("# Queue\nqueue password=super-private-value\n")
    (docs / "credentials.txt").write_text("should never be indexed")
    (docs / "ignored.py").write_text("not knowledge")
    model = TestEmbeddings()
    store = ProjectKnowledgeStore(tmp_path / "state", embeddings=model)
    project = profile(root)
    first = store.ingest(project)
    serialized = json.dumps(store.search(project.id, first["generation"], "queue"))
    assert "super-private-value" not in serialized
    assert "super-private-value" not in json.dumps(model.calls)
    assert first["documents"] == 2
    (docs / "oversized.txt").write_bytes(b"x" * (MAX_DOCUMENT_BYTES + 1))
    with pytest.raises(ValueError):
        store.ingest(project)
    assert store.active_generation(project.id) == first["generation"]
    (docs / "oversized.txt").unlink()
    (root / "alias").symlink_to(docs, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        store.ingest(project.model_copy(update={"knowledge_paths": ["alias"]}))
    outside = tmp_path / "outside.md"
    outside.write_text("outside")
    (docs / "escape.md").symlink_to(outside)
    assert store.ingest(project)["generation"] == first["generation"]
    with pytest.raises(ValueError):
        store.ingest(project.model_copy(update={"knowledge_paths": ["../outside.md"]}))


def test_lexical_diversity_can_select_past_fusion_budget_and_failure_does_not_activate(tmp_path):
    root = tmp_path / "project"
    docs = root / "docs"
    docs.mkdir(parents=True)
    (docs / "many.md").write_text("# queue\n" + ("queue " * 60 + "\n") * 55)
    (docs / "other.md").write_text("# Other\nqueue fallback.\n")
    store = ProjectKnowledgeStore(tmp_path / "state", embeddings=TestEmbeddings())
    project = profile(root)
    first = store.ingest(project)
    result = store.search(project.id, first["generation"], "queue", mode="lexical", top_k=5)
    assert len(result["items"]) == 5
    assert any(item["provenance"]["relative_path"] == "docs/other.md" for item in result["items"])
    # A profile update invalidates the active generation for new runs.
    registry = ProjectStore(tmp_path / "state")
    saved = registry.save(project, expected_version=0)
    registry.save(saved.model_copy(update={"name": "Changed"}), expected_version=1)
    with pytest.raises(ValueError, match="registration changed"):
        store.ingest(saved)
    assert store.active_generation(project.id, 2) is None
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT count(*) FROM generations").fetchone()[0] == 1


def test_uninstalled_local_model_never_loads_or_downloads(tmp_path, monkeypatch):
    import socket

    def deny_network(*args, **kwargs):
        pytest.fail("Local inference must not make a network request")

    monkeypatch.setattr(socket.socket, "connect", deny_network)
    model = LocalEmbeddings(tmp_path)
    assert model.status()["state"] != "ready"
    with pytest.raises(EmbeddingUnavailable):
        model.encode(["queue"], kind="query")
    assert not Path(tmp_path / "models").exists()


def test_parallel_index_is_rejected_before_another_source_scan(tmp_path):
    root = tmp_path / "project"
    documents(root)
    entered, release = threading.Event(), threading.Event()

    class SlowEmbeddings(TestEmbeddings):
        def encode(self, texts, *, kind):
            entered.set()
            assert release.wait(timeout=5)
            return super().encode(texts, kind=kind)

    store = ProjectKnowledgeStore(tmp_path / "state", embeddings=SlowEmbeddings())
    second_store = ProjectKnowledgeStore(tmp_path / "state", embeddings=TestEmbeddings())
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(store.ingest, profile(root))
        assert entered.wait(timeout=5)
        try:
            with pytest.raises(KnowledgeIndexBusy):
                second_store.ingest(profile(root))
        finally:
            release.set()
        assert first.result()["embedding"]["indexed"]


def test_frozen_benchmark_metrics_count_duplicate_slots_and_failures_honestly():
    assert validate_dataset()["dataset"] == "synthetic-service-runbooks-v1"
    items = [{"provenance": {"relative_path": path}} for path in ["one", "one", "two"]]
    result = metrics(items, {"one": 2, "two": 2}, valid=True)
    assert result["recall@1"] == 0.5 and result["recall@3"] == 1.0
    assert result["mrr@5"] == 1.0
    assert result["ndcg@5"] == pytest.approx((3 + 3 / 2) / (3 + 3 / math.log2(3)))
    assert set(metrics(items, {"one": 2}, valid=False).values()) == {0.0}


@pytest.mark.asyncio
async def test_live_agent_tool_retains_generation_and_repeated_citation_payload(tmp_path):
    root = tmp_path / "project"
    docs = documents(root)
    settings = Settings(_env_file=None, api_key=None, local_dir=tmp_path / "state")
    project = ProjectStore(settings.local_dir).save(profile(root), expected_version=0)
    store = ProjectKnowledgeStore(settings.local_dir, embeddings=TestEmbeddings())
    first = store.ingest(project, embed=False)["generation"]
    now, obtained = datetime.now(UTC), {}

    async def remember(item):
        if item["id"] in obtained:
            assert item == obtained[item["id"]]
        obtained[item["id"]] = item

    request = RunInput(
        mode="live",
        service_id=project.id,
        project_version=project.version,
        knowledge_generation=first,
        retrieval_mode="lexical",
        question="Check queue pressure",
        start=now - timedelta(minutes=5),
        end=now,
    )
    session = DiagnosticSession(settings, "frozen-test", request, save_snapshot=remember)
    search = next(tool for tool in session.live_tools() if tool.name == "search_knowledge")
    before = await search.ainvoke({"query": "queue"})
    (docs / "queue.md").write_text("# Queue\nNew queue behavior after indexing.\n")
    assert store.ingest(project, embed=False)["generation"] != first
    again = await search.ainvoke({"query": "queue pressure"})
    assert before["items"] == again["items"]
    assert all(item["provenance"]["generation"] == first for item in again["items"])
    assert "New queue behavior" not in json.dumps(again)
    assert obtained


@pytest.mark.asyncio
async def test_explicit_api_index_search_scope_and_private_validation(tmp_path):
    root = tmp_path / "project"
    documents(root)
    settings = Settings(_env_file=None, api_key=None, local_dir=tmp_path / "state")
    projects = ProjectStore(settings.local_dir)
    projects.save(profile(root), expected_version=0)
    knowledge = ProjectKnowledgeStore(settings.local_dir, embeddings=TestEmbeddings())
    app = FastAPI()
    app.include_router(create_knowledge_router(settings, projects, knowledge))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        base = "/api/projects/application/knowledge"
        status = (await client.get(base)).json()
        assert status["usable_generation"] is None
        invalid = await client.post(
            base + "/search", json={"query": "private", "secret": "never-echo"}
        )
        assert invalid.status_code == 422 and "never-echo" not in invalid.text
        assert (await client.post(base + "/search", json={"query": "   "})).status_code == 422
        stale = await client.post(base + "/index", json={"project_version": 2})
        assert stale.status_code == 409
        indexed = await client.post(base + "/index", json={"project_version": 1})
        assert indexed.status_code == 200
        found = await client.post(base + "/search", json={"query": "queue", "mode": "hybrid"})
        assert found.json()["status"] == "complete"
        assert found.json()["coverage"]["index_generation"] == indexed.json()["generation"]
        absent = await client.get("/api/projects/absent/knowledge")
        assert absent.status_code == 404
