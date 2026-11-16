"""Pure reference-feedback bounds; no models, project reads or report rewriting."""

import copy
import json

from incident_desk.agent.report_repair import feedback_for_report
from incident_desk.schemas import Diagnosis


def report(references):
    return {
        "structured_response": {
            "supporting_evidence": references,
            "contradicting_evidence": [],
        }
    }


def ref(identifier="health", version="v1", quote="healthy"):
    return {"id": identifier, "version": version, "quote": quote}


def source(content="healthy", version="v1"):
    return {"content": content, "version": version, "provenance": {"http_status": 200}}


def errors(feedback):
    return [json.loads(line) for line in feedback.splitlines() if line.startswith("{")]


def test_feedback_aggregates_unknown_id_wrong_version_and_quote_without_changing_report():
    result = report(
        [
            ref("unobtained-private-id", quote="invented"),
            ref(version="v0", quote="healthy http_status=200"),
            ref("metrics", quote="errors=1...requests=2"),
        ]
    )
    snapshots = {"health": source(), "metrics": source("errors=1\nqueue=5\nrequests=2")}
    original = copy.deepcopy((result, snapshots))
    feedback = feedback_for_report(result, snapshots)
    details = errors(feedback)
    assert len(details) == 3
    assert any("来源ID不属于" in item["错误"] for item in details)
    assert any("version" in item["错误"] for item in details)
    assert sum("quote" in item["错误"] for item in details) == 2
    assert "unobtained-private-id" not in feedback
    assert "healthy http_status=200" not in feedback
    assert "连续原文" in feedback and "metadata" in feedback and "省略号" in feedback
    assert (result, snapshots) == original


def test_each_source_sample_is_at_most_200_chars_and_never_contains_metadata():
    snapshots = {"health": source("A" * 200 + "HIDDEN_AFTER_SAMPLE")}
    snapshots["health"]["provenance"]["secret_extra"] = "DO_NOT_COPY_METADATA"
    feedback = feedback_for_report(report([ref(quote="not in content")]), snapshots)
    assert errors(feedback)[0]["不可信content样例"] == "A" * 200
    assert "HIDDEN_AFTER_SAMPLE" not in feedback
    assert "DO_NOT_COPY_METADATA" not in feedback
    assert "service operation" not in feedback
    assert "不要提议、重复或执行服务操作" in feedback


def test_error_count_and_total_text_are_bounded_for_many_malformed_references():
    refs = [ref(f"id-{index}", version="wrong", quote="missing") for index in range(100)]
    snapshots = {f"id-{index}": source("\0" * 200) for index in range(100)}
    feedback = feedback_for_report(report(refs), snapshots)
    assert len(errors(feedback)) <= 8
    assert len(feedback) <= 6000


def test_empty_missing_or_invalid_report_always_produces_actionable_feedback():
    for result in ({}, {"structured_response": None}, report([]), report([{"quote": "bad"}])):
        feedback = feedback_for_report(result, {})
        assert errors(feedback)
        assert "Diagnosis" in feedback
        assert "不能" in feedback or "不得" in feedback


def test_pydantic_report_and_nonstring_content_follow_canonical_reference_validator():
    diagnosis = Diagnosis(
        summary="合成数据用于验证引用反馈。",
        hypotheses=[],
        supporting_evidence=[ref(quote='"status": "healthy"')],
        contradicting_evidence=[],
        missing_information=[],
        recommended_checks=["检查指标"],
        needs_human=False,
    )
    snapshots = {"health": source({"status": "healthy"})}
    feedback = feedback_for_report({"structured_response": diagnosis}, snapshots)
    assert "quote不在" not in feedback
    assert "未定位到引用匹配错误" in feedback


def test_feedback_contains_only_referenced_obtained_sources():
    snapshots = {"health": source("known source"), "another": source("UNREFERENCED_SOURCE")}
    feedback = feedback_for_report(report([ref(quote="invalid")]), snapshots)
    assert "known source" in feedback
    assert "UNREFERENCED_SOURCE" not in feedback
