import sqlite3
from pathlib import Path

import pytest
from pydantic import ValidationError

from incident_desk.knowledge.store import KnowledgeStore, match_query, tokenize

ROOT = Path(__file__).resolve().parents[1]


def document(
    directory,
    *,
    doc_id="kb-test",
    version="1",
    content="连接池等待超时需要核对连接数上限。",
    service="checkout-api",
    filename="note.md",
):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / filename
    path.write_text(
        f"---\ndoc_id: {doc_id}\ndoc_version: {version}\nservice_id: {service}\n---\n"
        f"# 排障规范\n\n{content}\n",
        encoding="utf-8",
    )
    return path


def test_chinese_actual_retrieval_and_source_resolution(tmp_path):
    store = KnowledgeStore(tmp_path / "knowledge.db")
    report = store.ingest_dir(ROOT / "data/knowledge")
    assert report["active_documents"] == 8
    result = store.search("连接池等待超时", "checkout-api")
    assert result
    assert "kb-002" in {row["doc_id"] for row in result}
    assert len(result) <= 5
    assert all(row["service_id"] == "checkout-api" for row in result)
    assert all(row["doc_version"] == "1" and row["content"] for row in result)
    for row in result:
        original = store.get_chunk(row["chunk_id"])
        assert original["content"] == row["content"]
        assert original["source"].startswith("kb-")


def test_same_tokenizer_contract_and_literal_fts_operators():
    assert "连接池" in tokenize("连接池等待超时")
    assert tokenize("ＡＰＩ checkout") == tokenize("api CHECKOUT")
    expression = match_query('连接池 " OR * NEAR(超时, 2) --')
    assert '"or"' in expression
    assert '"near"' in expression
    assert "*" not in expression and "(" not in expression


def test_ingestion_is_idempotent_across_new_store_instances(tmp_path):
    data = tmp_path / "knowledge"
    document(data)
    database = tmp_path / "knowledge.db"
    first = KnowledgeStore(database)
    initial = first.ingest_dir(data)
    hits = first.search("连接池", "checkout-api")
    repeated = KnowledgeStore(database).ingest_dir(data)
    assert initial["inserted_documents"] == 1
    assert repeated["inserted_documents"] == repeated["inserted_chunks"] == 0
    assert repeated["unchanged_documents"] == 1
    assert initial["total_chunks"] == repeated["total_chunks"]
    assert hits == first.search("连接池", "checkout-api")


def test_versions_keep_old_chunks_without_searching_stale_content(tmp_path):
    directory = tmp_path / "knowledge"
    document(directory, content="olduniquex 连接池旧版规范。")
    store = KnowledgeStore(tmp_path / "knowledge.db")
    store.ingest_dir(directory)
    old = store.search("olduniquex", "checkout-api")[0]
    document(directory, version="2", content="newuniquey 连接池新版规范。")
    second = store.ingest_dir(directory)
    assert second["total_versions"] == 2
    assert store.search("olduniquex", "checkout-api") == []
    latest = store.search("newuniquey", "checkout-api")[0]
    assert latest["doc_version"] == "2"
    assert latest["chunk_id"] != old["chunk_id"]
    assert store.get_chunk(old["chunk_id"])["content"] == old["content"]
    # Re-ingesting the old, unchanged material cannot reactivate it.
    document(directory, content="olduniquex 连接池旧版规范。")
    store.ingest_dir(directory)
    assert store.search("olduniquex", "checkout-api") == []
    assert store.search("newuniquey", "checkout-api")


def test_changed_content_requires_new_version_and_rolls_back_whole_batch(tmp_path):
    directory = tmp_path / "knowledge"
    document(directory, filename="z.md", content="originaluniquex")
    store = KnowledgeStore(tmp_path / "knowledge.db")
    store.ingest_dir(directory)
    document(directory, filename="a.md", doc_id="kb-added", content="unexpectedinsertx")
    document(directory, filename="z.md", content="changedwithoutversionx")
    with pytest.raises(ValueError, match="without a new doc_version"):
        store.ingest_dir(directory)
    assert store.search("unexpectedinsertx", "checkout-api") == []
    assert store.search("originaluniquex", "checkout-api")


def test_bm25_lower_score_is_better_and_ties_are_stable(tmp_path):
    directory = tmp_path / "knowledge"
    document(
        directory,
        doc_id="kb-strong",
        filename="a.md",
        content="pooltimeouterror pooltimeouterror pooltimeouterror pooltimeouterror",
    )
    document(
        directory,
        doc_id="kb-weak",
        filename="b.md",
        content="pooltimeouterror " + "unrelatedword " * 30,
    )
    store = KnowledgeStore(tmp_path / "knowledge.db")
    store.ingest_dir(directory)
    result = store.search("pooltimeouterror", "checkout-api")
    assert [row["doc_id"] for row in result] == ["kb-strong", "kb-weak"]
    assert result[0]["bm25"] < result[1]["bm25"] < 0
    assert result == store.search("pooltimeouterror", "checkout-api")


def test_service_filter_excludes_other_service_even_when_terms_match(tmp_path):
    directory = tmp_path / "knowledge"
    document(directory, doc_id="kb-current", filename="a.md", content="shareduniquetermx")
    document(
        directory,
        doc_id="kb-other",
        filename="b.md",
        service="private-service",
        content="shareduniquetermx shareduniquetermx",
    )
    store = KnowledgeStore(tmp_path / "knowledge.db")
    store.ingest_dir(directory)
    assert {r["doc_id"] for r in store.search("shareduniquetermx", "checkout-api")} == {
        "kb-current"
    }
    with pytest.raises(ValidationError):
        store.search("shareduniquetermx", "private-service")


@pytest.mark.parametrize(
    "query",
    [
        '"; DROP TABLE documents; --',
        '* OR (NEAR("x", "y"))',
        "tokenized:连接池 NOT 等待",
        "' UNION SELECT * FROM chunks --",
        "😀 ???",
    ],
)
def test_untrusted_query_cannot_inject_sql_or_fts_syntax(tmp_path, query):
    directory = tmp_path / "knowledge"
    document(directory)
    database = tmp_path / "knowledge.db"
    store = KnowledgeStore(database)
    store.ingest_dir(directory)
    assert isinstance(store.search(query, "checkout-api"), list)
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT count(*) FROM documents").fetchone()[0] == 1
    assert store.search("连接池", "checkout-api")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"query": ""},
        {"query": "x" * 1001},
        {"top_k": 0},
        {"top_k": 11},
        {"top_k": "5"},
        {"top_k": True},
    ],
)
def test_search_bounds(tmp_path, kwargs):
    store = KnowledgeStore(tmp_path / "knowledge.db")
    with pytest.raises(ValidationError):
        store.search(**{"query": "连接池", "service_id": "checkout-api", **kwargs})


def test_default_and_max_result_limits(tmp_path):
    directory = tmp_path / "knowledge"
    for index in range(12):
        document(directory, doc_id=f"kb-{index:03}", filename=f"{index}.md", content="连接池超时")
    store = KnowledgeStore(tmp_path / "knowledge.db")
    store.ingest_dir(directory)
    assert len(store.search("连接池", "checkout-api")) == 5
    assert len(store.search("连接池", "checkout-api", top_k=10)) == 10


def test_document_symlink_and_metadata_guard(tmp_path):
    source = tmp_path / "outside"
    outside = document(source)
    directory = tmp_path / "knowledge"
    directory.mkdir()
    (directory / "note.md").symlink_to(outside)
    store = KnowledgeStore(tmp_path / "knowledge.db")
    with pytest.raises(ValueError, match="symlinks cannot escape"):
        store.ingest_dir(directory)
    (directory / "note.md").unlink()
    document(directory, version="bad/version")
    with pytest.raises(ValueError, match="Invalid knowledge identifier"):
        store.ingest_dir(directory)
