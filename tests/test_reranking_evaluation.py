"""The benchmark must score exact spans and retain every planned failure."""

import copy
import json
import shutil

import pytest

from scripts.evaluate_reranking import DATASET, metrics, summarize, validate_dataset


def test_frozen_v2_has_independent_groups_and_chunk_resolvable_spans():
    dataset = validate_dataset()
    assert dataset["counts"] == {"development": 24, "acceptance": 32}
    assert dataset["chunks"] == 84
    assert dataset["validated_query_spans"] == 84
    first, second = dataset["splits"].values()
    assert not ({q["mechanism_group"] for q in first} & {q["mechanism_group"] for q in second})
    assert all({q["language"] for q in split} == {"en", "zh"} for split in (first, second))
    assert all(any(not q["answerable"] for q in split) for split in (first, second))


def test_tampered_query_or_corpus_is_rejected_before_inference(tmp_path):
    shutil.copytree(DATASET, tmp_path / "dataset")
    directory = tmp_path / "dataset"
    path = directory / "acceptance.json"
    modified = json.loads(path.read_text())
    modified["queries"][0]["query"] += " changed"
    path.write_text(json.dumps(modified))
    with pytest.raises(ValueError, match="Frozen"):
        validate_dataset(directory)


def test_extra_unfrozen_corpus_file_is_rejected(tmp_path):
    directory = tmp_path / "dataset"
    shutil.copytree(DATASET, directory)
    (directory / "corpus/unfrozen.md").write_text("An unlabelled extra candidate")
    with pytest.raises(ValueError, match="file list changed"):
        validate_dataset(directory)


def example():
    return {
        "answerable": True,
        "relevant_spans": [
            {"unit_id": "cause", "path": "runbook.md", "quote": "complete cause", "grade": 3},
            {"unit_id": "check", "path": "runbook.md", "quote": "verify recovery", "grade": 2},
        ],
        "gold_chunk_grades": {"one": 3, "two": 2},
        "hard_negative_paths": ["counterexample.md"],
    }


def test_same_document_wrong_span_gets_no_credit():
    query = example()
    wrong = [{"id": "other", "path": "runbook.md", "content": "not the labeled sentence"}]
    assert metrics(query, wrong) == {
        "span_recall": 0,
        "mrr@5": 0,
        "ndcg@5": 0,
        "hard_negative_at5": 0,
    }
    partial = [{"id": "other", "path": "runbook.md", "content": "complete"}]
    assert metrics(query, partial)["span_recall"] == 0


def test_rank_discount_duplicate_suppression_and_strict_failure():
    query = example()
    ranked = [
        {"id": "negative", "path": "counterexample.md", "content": "same symptoms"},
        {"id": "one", "path": "runbook.md", "content": "complete cause"},
        {"id": "two", "path": "runbook.md", "content": "verify recovery"},
    ]
    actual = metrics(query, ranked)
    assert actual["span_recall"] == 1 and actual["mrr@5"] == 0.5
    assert 0 < actual["ndcg@5"] < 1
    assert actual["hard_negative_at5"] == 1
    duplicate = metrics(query, ranked + [ranked[1]])
    assert duplicate["ndcg@5"] == actual["ndcg@5"]
    assert metrics(query, ranked, valid=False)["span_recall"] == 0


def test_all_failures_and_no_answer_stay_in_denominators():
    zero = dict.fromkeys(
        ("candidate_recall@N", "rerank_pool_recall@M", "recall@5", "mrr@5", "ndcg@5"), 0
    )
    base = {
        "round": "first_pass",
        "mode": "cross_encoder",
        "valid": False,
        "answerable": True,
        "strict": zero,
        "final": [],
        "elapsed_ms": 40,
        "parent_peak_rss_mib": 100,
        "reranker": {},
        "returned_quality": {"span_recall": 0, "mrr@5": 0, "ndcg@5": 0, "hard_negative_at5": 0},
    }
    no_answer = copy.deepcopy(base) | {"answerable": False}
    result = summarize([base, no_answer])["first_pass/cross_encoder"]
    assert result["planned_queries"] == 2 and result["degraded_or_failed"] == 2
    assert result["strict_all_queries"]["ndcg@5"] == 0
    assert result["no_answer_correct_empty_retrieval"] == {
        "numerator": 0,
        "denominator": 1,
        "rate": 0,
    }
    assert result["no_answer_returned_context"]["denominator"] == 1
