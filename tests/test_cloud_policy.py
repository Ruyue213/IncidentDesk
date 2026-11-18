"""Inspect actual provider wire bodies; policy denial must precede paid requests."""

import asyncio
import json
from datetime import UTC, datetime, timedelta

import httpx
import openai
import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from incident_desk.agent.diagnostics import DiagnosticSession
from incident_desk.agent.models import make_model
from incident_desk.api.app import create_app
from incident_desk.cloud_policy import CloudAudit, CloudPolicy, CloudPolicyDenied
from incident_desk.projects import ProjectStore
from incident_desk.schemas import Reference, RunInput, validate_references
from incident_desk.settings import Settings
from incident_desk.storage import Repository


async def context(tmp_path, policy=None):
    settings = Settings(
        _env_file=None, api_key="synthetic-plain-credential", local_dir=tmp_path / "state"
    )
    project = ProjectStore(settings.local_dir).save(
        {
            "id": "guarded",
            "name": "Guarded",
            "root": str(tmp_path),
            "cloud_policy": policy or {},
        }
    )
    now = datetime.now(UTC)
    request = RunInput(
        mode="live",
        service_id=project.id,
        project_version=project.version,
        cloud_policy=project.cloud_policy,
        question="Investigate this synthetic service",
        start=now - timedelta(minutes=1),
        end=now,
    )
    repo = Repository(settings.app_db)
    await repo.init()
    run, _ = await repo.create_run(request.model_dump(mode="json"), "guard-test")
    return settings, project, request, repo, run


async def transport(model, captured):
    await model.root_async_client.close()
    model.root_client.close()

    def respond(request):
        captured.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "synthetic",
                "object": "chat.completion",
                "created": 1,
                "model": "mock",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "checked"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            },
        )

    client = openai.AsyncOpenAI(
        api_key="mock",
        base_url="https://api.deepseek.com",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
    )
    model.root_async_client = client
    model.async_client = client.chat.completions
    return client


@pytest.mark.asyncio
async def test_actual_wire_redacts_question_tool_arguments_reasoning_and_result(tmp_path):
    settings, _, _, repo, run = await context(tmp_path, {"redact_literals": ["internal-customer"]})
    model = await asyncio.to_thread(make_model, settings, run["id"])
    captured = []
    client = await transport(model, captured)
    try:
        await model.ainvoke(
            [
                HumanMessage(content="internal-customer synthetic-plain-credential"),
                AIMessage(
                    content="",
                    additional_kwargs={"reasoning_content": "internal-customer"},
                    tool_calls=[
                        {"id": "call-1", "name": "read", "args": {"customer": "internal-customer"}}
                    ],
                ),
                ToolMessage(
                    content="password=unsafe-test-value internal-customer", tool_call_id="call-1"
                ),
            ]
        )
        raw = json.dumps(captured)
        assert len(captured) == 1
        for needle in ("internal-customer", "synthetic-plain-credential", "unsafe-test-value"):
            assert needle not in raw
        assert captured[0]["messages"][1]["tool_calls"][0]["id"] == "call-1"
        args = json.loads(captured[0]["messages"][1]["tool_calls"][0]["function"]["arguments"])
        assert args == {"customer": "[REDACTED]"}
        audit = CloudAudit(settings.local_dir).for_run(run["id"])["items"]
        assert len(audit) == 1 and audit[0]["decision"] == "allowed" and audit[0]["changed"]
        assert "internal-customer" not in (
            settings.local_dir / "cloud-audit.db"
        ).read_bytes().decode(errors="ignore")
        assert model._ledger.rows()[0]["status"] == "SUCCEEDED"
    finally:
        await client.close()
        await repo.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reason", ["project_cloud_disabled", "destination_not_allowed", "project_version_changed"]
)
async def test_cloud_denial_precedes_network_and_budget_reservation(tmp_path, reason):
    policy = (
        {"mode": "deny"}
        if reason == "project_cloud_disabled"
        else (
            {"allowed_origins": ["https://other.invalid"]}
            if reason == "destination_not_allowed"
            else {}
        )
    )
    settings, project, _, repo, run = await context(tmp_path, policy)
    if reason == "project_version_changed":
        ProjectStore(settings.local_dir).save(
            project.model_dump(mode="json") | {"name": "Changed"}, project.version
        )
    model = make_model(settings, run["id"])
    captured = []
    client = await transport(model, captured)
    try:
        with pytest.raises(CloudPolicyDenied):
            await model.ainvoke("Investigate without leaking")
        assert not captured and not model._ledger.rows()
        audit = CloudAudit(settings.local_dir).for_run(run["id"])["items"]
        assert audit[0]["reason"] == reason and audit[0]["payload_sha256"] is None
    finally:
        await client.close()
        await repo.close()


@pytest.mark.asyncio
async def test_selected_literal_redaction_is_applied_before_citation_validation(tmp_path):
    settings, _, request, repo, run = await context(
        tmp_path, {"redact_literals": ["internal-customer"]}
    )
    session = DiagnosticSession(settings, run["id"], request)
    items = await session.remember(
        [{"id": "example", "version": "1", "content": "internal-customer failed health"}]
    )
    assert items[0]["content"] == "[REDACTED] failed health"
    validate_references(
        [Reference(id="example", version="1", quote="[REDACTED] failed health")], session.snapshots
    )
    await repo.close()


@pytest.mark.asyncio
async def test_source_code_policy_refuses_tools_before_reading_files(tmp_path, monkeypatch):
    settings, _, request, repo, run = await context(tmp_path, {"allow_source_code": False})
    session = DiagnosticSession(settings, run["id"], request)

    async def forbidden(*args):
        pytest.fail("Source policy must refuse before reading")

    monkeypatch.setattr(session.evidence, "read_file", forbidden)
    monkeypatch.setattr(session.evidence, "search", forbidden)
    tools = {tool.name: tool for tool in session.live_tools()}
    for name, args in [
        ("read_project_file", {"path": "app.py"}),
        ("search_project", {"query": "class"}),
    ]:
        result = await tools[name].ainvoke(args)
        assert result["error"]["code"] == "cloud_source_disabled"
    await repo.close()


@pytest.mark.parametrize(
    "value", ["http://example.com", "https://user:pass@example.com", "https://example.com/path"]
)
def test_destination_allowlist_accepts_only_credential_free_https_origins(value):
    with pytest.raises(ValueError):
        CloudPolicy(allowed_origins=[value])


@pytest.mark.asyncio
async def test_project_editor_preserves_policy_and_run_freezes_retrieval_settings(tmp_path):
    settings = Settings(_env_file=None, api_key=None, local_dir=tmp_path / "state")
    app = create_app(settings)
    now = datetime.now(UTC)
    project = {"name": "Synthetic", "root": str(tmp_path)}
    endpoint = "/api/projects/frozen"
    request = {
        "mode": "live",
        "service_id": "frozen",
        "question": "Check the synthetic process",
        "start": (now - timedelta(minutes=1)).isoformat(),
        "end": now.isoformat(),
    }
    config = {"candidate_n": 30, "rerank_top_n": 15, "top_k": 4, "reranker": "cross_encoder"}
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://127.0.0.1"
        ) as client:
            saved = await client.put(
                endpoint,
                json=project | {"cloud_policy": {"mode": "deny"}, "retrieval_config": config},
            )
            assert saved.status_code == 200
            legacy_save = await client.put(endpoint, json=project | {"name": "Legacy edit"})
            assert legacy_save.json()["cloud_policy"]["mode"] == "deny"
            assert legacy_save.json()["retrieval_config"]["top_k"] == 4
            denied = await client.post(
                "/api/runs", json=request, headers={"Idempotency-Key": "blocked"}
            )
            assert denied.status_code == 422 and "disabled" in denied.json()["detail"]
            saved = await client.put(
                endpoint, json=project | {"cloud_policy": {"mode": "redacted"}}
            )
            headers = {"Idempotency-Key": "frozen"}
            run = await client.post("/api/runs", json=request, headers=headers)
            assert run.status_code == 202
            frozen = run.json()["request"]
            assert frozen["retrieval_config"] == saved.json()["retrieval_config"]
            assert frozen["cloud_policy"] == saved.json()["cloud_policy"]
            assert frozen["project_version"] == saved.json()["version"]
            changed = await client.put(
                endpoint, json=project | {"retrieval_config": config | {"top_k": 6}}
            )
            assert changed.status_code == 200
            retry = await client.post("/api/runs", json=request, headers=headers)
            assert retry.json()["id"] == run.json()["id"]
            assert retry.json()["request"] == frozen
