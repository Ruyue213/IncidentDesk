import importlib.util
import json
import socket
from argparse import Namespace

import pytest
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field

from incident_desk.agent.budget import BudgetExceeded
from incident_desk.agent.diagnostics import DiagnosticSession, InvalidModelOutput
from incident_desk.agent.scripted import ScriptedToolModel
from incident_desk.schemas import Diagnosis, Reference, RunInput, snapshot, validate_references
from incident_desk.settings import ROOT, Settings
from incident_desk.storage.repository import Repository


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("Diagnostic contract tests cannot access the network")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


@pytest.fixture
def settings(tmp_path):
    return Settings(_env_file=None, local_dir=tmp_path)


@pytest.fixture
def request_input():
    return RunInput(
        question="请结合三类证据分析服务异常并提出下一步检查。",
        incident_id="dev-001",
        start="2026-10-01T08:00:00Z",
        end="2026-10-01T09:00:00Z",
    )


def diagnosis_payload():
    return {
        "summary": "缺少足够运行证据，当前不能确定候选原因。",
        "hypotheses": [],
        "supporting_evidence": [],
        "contradicting_evidence": [],
        "missing_information": ["需要同一时间窗的指标、日志和有效变更记录。"],
        "recommended_checks": ["补取缺失来源，再评估候选解释。"],
        "needs_human": True,
        "ticket_draft": None,
    }


class OneShotDiagnosisModel(ScriptedToolModel):
    output: dict = Field(default_factory=diagnosis_payload)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.transcript.append({"messages": [m.model_dump(mode="json") for m in messages]})
        return ChatResult(
            generations=[
                ChatGeneration(
                    message=AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "Diagnosis",
                                "args": self.output,
                                "id": "diagnosis-output-1",
                                "type": "tool_call",
                            }
                        ],
                    )
                )
            ]
        )


def test_missing_evidence_schema_can_express_no_cause_instead_of_inventing_one():
    report = Diagnosis.model_validate(diagnosis_payload())
    assert report.hypotheses == []
    assert report.supporting_evidence == []
    assert report.missing_information
    assert report.needs_human
    validate_references(report.supporting_evidence, {})
    # This demonstrates representability; schema validity is not a semantic judge.


@pytest.mark.parametrize(
    "reference,error",
    [
        ({"id": "invented", "version": "1", "quote": "连接池等待超时"}, "not obtained"),
        ({"id": "observed", "version": "2", "quote": "连接池等待超时"}, "version mismatch"),
        ({"id": "observed", "version": "1", "quote": "数据库已经崩溃"}, "absent from source"),
    ],
)
def test_forged_id_version_and_quote_are_rejected(reference, error):
    snapshots = {"observed": {"id": "observed", "version": "1", "content": "连接池等待超时。"}}
    with pytest.raises(ValueError, match=error):
        validate_references([Reference.model_validate(reference)], snapshots)


def test_reference_cannot_be_borrowed_from_another_run(settings, request_input):
    first = DiagnosticSession(settings, "first", request_input)
    second = DiagnosticSession(settings, "second", request_input)
    first.snapshots["only-first"] = {
        "id": "only-first",
        "version": "v1",
        "content": "observed fact",
    }
    report = diagnosis_payload()
    report["supporting_evidence"] = [
        {"id": "only-first", "version": "v1", "quote": "observed fact"}
    ]
    assert first.validate({"structured_response": report})
    with pytest.raises(ValueError, match="not obtained"):
        second.validate({"structured_response": report})


@pytest.mark.parametrize(
    "start,end",
    [
        ("2026-10-01T07:59:00Z", "2026-10-01T09:00:00Z"),
        ("2026-10-01T08:00:00Z", "2026-10-01T09:01:00Z"),
    ],
)
@pytest.mark.asyncio
async def test_model_query_cannot_expand_submitted_run_window(settings, request_input, start, end):
    session = DiagnosticSession(settings, "window-test", request_input)
    evidence = session.tools()[0]
    with pytest.raises(ValueError, match="inside the submitted run window"):
        await evidence.ainvoke(
            {
                "service_id": "checkout-api",
                "source": "metrics",
                "start": start,
                "end": end,
            }
        )
    assert session.snapshots == {}
    assert session.trace == []


@pytest.mark.asyncio
async def test_baseline_reads_all_three_sources_then_same_kb_and_one_generation(
    settings, request_input
):
    session = DiagnosticSession(settings, "baseline-test", request_input)
    collected = []
    searches = []

    class EvidenceProbe:
        def get(self, incident_id, service_id, start, end, source, limit):
            collected.append((incident_id, service_id, start, end, source, limit))
            return [
                {
                    "evidence_id": f"probe-{source}",
                    "source": source,
                    "version": "v1",
                    "observed_at": "2026-10-01T08:05:00Z",
                    "content": f"source={source}",
                }
            ]

    class KnowledgeProbe:
        def search(self, query, service_id, top_k):
            searches.append((query, service_id, top_k))
            return [
                {
                    "chunk_id": "kb-probe@1:x",
                    "doc_id": "kb-probe",
                    "doc_version": "1",
                    "content": "模拟排障规范",
                    "source": "probe.md",
                    "bm25": -1.0,
                }
            ]

    session.evidence = EvidenceProbe()
    session.knowledge = KnowledgeProbe()
    output = diagnosis_payload()
    output["supporting_evidence"] = [
        {"id": "probe-metrics", "version": "v1", "quote": "source=metrics"}
    ]
    model = OneShotDiagnosisModel(output=output)
    report, metadata = await session.fixed_baseline(model=model)
    assert [call[4] for call in collected] == ["metrics", "logs", "changes"]
    assert all(
        call[:4]
        == (
            request_input.incident_id,
            request_input.service_id,
            request_input.start,
            request_input.end,
        )
        and call[5] == 10
        for call in collected
    )
    assert searches == [(request_input.question, request_input.service_id, 5)]
    assert metadata["tool_calls"] == 5
    assert len(model.transcript) == 1
    actual_input = json.loads(model.transcript[0]["messages"][-1]["content"])
    assert {row["source"] for row in actual_input["observations"]} == {"metrics", "logs", "changes"}
    assert actual_input["knowledge"][0]["id"] == "kb-probe@1:x"
    assert report.supporting_evidence[0].id in session.snapshots
    assert {t["function"]["name"] for t in model.tool_schemas[0]} == {"Diagnosis"}


class InvalidJsonModel(ScriptedToolModel):
    call_count: int = 0

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.call_count += 1
        return ChatResult(
            generations=[
                ChatGeneration(
                    message=AIMessage(
                        content="",
                        invalid_tool_calls=[
                            {
                                "id": "invalid-json",
                                "name": "Diagnosis",
                                "args": '{"summary":',
                                "error": "simulated truncated JSON",
                                "type": "invalid_tool_call",
                            }
                        ],
                    )
                )
            ]
        )


@pytest.mark.parametrize("method", ["agent", "fixed_baseline"])
@pytest.mark.asyncio
async def test_invalid_json_is_terminal_without_hidden_format_repair(
    settings, request_input, method
):
    session = DiagnosticSession(settings, f"invalid-{method}", request_input)
    session.knowledge.ingest_dir(ROOT / "data/knowledge")
    model = InvalidJsonModel()
    with pytest.raises(InvalidModelOutput):
        if method == "agent":
            await session.graph(model=model).ainvoke(session.input())
        else:
            await session.fixed_baseline(model=model)
    assert model.call_count == 1
    assert session.model_calls == 1


@pytest.mark.parametrize("tool_limit", [1, 3, 4])
@pytest.mark.asyncio
async def test_baseline_insufficient_tool_budget_stops_before_model_request(
    settings,
    request_input,
    tool_limit,
):
    session = DiagnosticSession(
        settings.model_copy(update={"tool_call_limit": tool_limit}),
        "short-budget",
        request_input,
    )
    session.knowledge.ingest_dir(ROOT / "data/knowledge")
    model = InvalidJsonModel()
    with pytest.raises(BudgetExceeded):
        await session.fixed_baseline(model=model)
    assert model.call_count == 0
    assert session.model_calls == 0
    assert len(session.trace) <= tool_limit


@pytest.mark.asyncio
async def test_repeated_queries_different_bm25_freeze_same_chunk_with_real_repository(
    settings, request_input
):
    repo = Repository(settings.app_db)
    await repo.init()
    try:
        run, _ = await repo.create_run(request_input.model_dump(mode="json"), "retrieval-repeat")
        session = DiagnosticSession(
            settings,
            run["id"],
            request_input,
            save_snapshot=lambda item: repo.save_evidence(run["id"], item),
        )
        session.knowledge.ingest_dir(ROOT / "data/knowledge")
        raw_first = session.knowledge.search("连接池", "checkout-api")
        raw_second = session.knowledge.search("连接池等待", "checkout-api")
        common = {row["chunk_id"] for row in raw_first} & {row["chunk_id"] for row in raw_second}
        assert common
        assert any(
            first["bm25"] != second["bm25"]
            for first in raw_first
            for second in raw_second
            if first["chunk_id"] == second["chunk_id"]
        )
        search = session.tools()[1]
        await search.ainvoke({"query": "连接池", "service_id": "checkout-api"})
        await search.ainvoke({"query": "连接池等待", "service_id": "checkout-api"})
        for chunk_id in common:
            saved = await repo.get_evidence(run["id"], chunk_id)
            assert "bm25" not in saved
            assert saved == session.snapshots[chunk_id]
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_failed_persistence_does_not_authorize_in_memory_reference(settings, request_input):
    async def fail(item):
        raise RuntimeError("simulated persistence failure")

    session = DiagnosticSession(settings, "failed-save", request_input, save_snapshot=fail)
    with pytest.raises(RuntimeError, match="persistence failure"):
        await session.remember([{"evidence_id": "x", "version": "v1", "content": "fact"}])
    assert session.snapshots == {}


def test_snapshot_content_not_query_score_defines_identity():
    item = {"chunk_id": "chunk-1", "doc_version": "1", "content": "same content"}
    assert snapshot({**item, "bm25": -1.0}) == snapshot({**item, "bm25": -2.0})


@pytest.fixture
def evaluation_module():
    spec = importlib.util.spec_from_file_location(
        "incident_desk_evaluation", ROOT / "scripts/evaluate.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_acceptance_inputs_are_not_opened_without_freeze_metadata(
    evaluation_module,
    settings,
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(evaluation_module, "verify_dataset", lambda: "dataset-hash")
    args = Namespace(
        split="acceptance",
        repeats=2,
        batch_budget_cny=5,
        freeze_metadata=None,
        output=tmp_path / "not-created.jsonl",
    )
    with pytest.raises(ValueError, match="requires --freeze-metadata"):
        await evaluation_module.evaluate(args, settings)
    assert not args.output.exists()


def test_acceptance_freeze_checks_git_code_dataset_prompt_and_configuration(
    evaluation_module,
    settings,
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(evaluation_module, "git_commit", lambda: "a" * 40)
    monkeypatch.setattr(evaluation_module, "verify_dataset", lambda: "dataset-hash")
    monkeypatch.setattr(evaluation_module, "code_hashes", lambda: {"src/example.py": "source-hash"})
    metadata = evaluation_module.freeze_metadata(settings)
    path = tmp_path / "freeze.json"
    path.write_text(json.dumps(metadata))
    assert evaluation_module.verify_freeze(path, settings)["git_commit"] == "a" * 40
    for field in (
        "git_commit",
        "dataset_manifest_sha256",
        "source_hashes",
        "system_prompt_sha256",
        "configuration_sha256",
    ):
        path.write_text(json.dumps({**metadata, field: "tampered"}))
        with pytest.raises(ValueError, match=field):
            evaluation_module.verify_freeze(path, settings)


def test_dataset_guard_detects_unfrozen_extra_knowledge_file(
    evaluation_module, tmp_path, monkeypatch
):
    (tmp_path / "data/knowledge").mkdir(parents=True)
    (tmp_path / "evals").mkdir()
    source = tmp_path / "data/knowledge/kb-001.md"
    source.write_text("frozen knowledge")
    manifest = {"files": {"data/knowledge/kb-001.md": evaluation_module.digest(source)}}
    (tmp_path / "evals/dataset-manifest.json").write_text(json.dumps(manifest))
    monkeypatch.setattr(evaluation_module, "ROOT", tmp_path)
    evaluation_module.verify_dataset()
    (tmp_path / "data/knowledge/unfrozen.md").write_text("extra content")
    with pytest.raises(ValueError, match="file inventory"):
        evaluation_module.verify_dataset()


@pytest.mark.asyncio
async def test_evaluation_releases_sync_and_async_clients(evaluation_module):
    closed = []

    class AsyncClient:
        async def close(self):
            closed.append("async")

    class SyncClient:
        def close(self):
            closed.append("sync")

    model = Namespace(root_async_client=AsyncClient(), root_client=SyncClient())
    assert await evaluation_module.close_model(model) == []
    assert closed == ["async", "sync"]
