"""Full Deep Agents wire integration without provider traffic or real project data."""

import json
from datetime import UTC, datetime, timedelta

import httpx
import openai
import pytest
from langchain_core.messages import AIMessage, HumanMessage

from incident_desk.agent.diagnostics import DiagnosticSession
from incident_desk.agent.models import UnsupportedThinkingToolChoice, make_model
from incident_desk.projects import ProjectProfile, ProjectStore
from incident_desk.schemas import RunInput
from incident_desk.settings import Settings


@pytest.mark.asyncio
async def test_full_live_harness_thinking_uses_auto_and_keeps_schema_and_reasoning(
    tmp_path, monkeypatch, caplog
):
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "false")
    project = tmp_path / "synthetic-project"
    project.mkdir()
    (project / "README.md").write_text("Synthetic service instructions for an offline contract.")
    settings = Settings(
        _env_file=None,
        api_key="synthetic-key",
        local_dir=tmp_path / "state",
        reasoning="low",
        max_output_tokens=4096,
        max_request_bytes=262144,
    )
    profile = ProjectStore(settings.local_dir).save(
        ProjectProfile(
            id="wire-live",
            name="Synthetic wire project",
            root=project,
        )
    )
    end = datetime.now(UTC)
    request = RunInput(
        mode="live",
        service_id=profile.id,
        project_version=profile.version,
        question="只读检查注册项目说明，并提供有引用的诊断。",
        start=end - timedelta(hours=1),
        end=end,
    )
    captured = []

    def reply(incoming):
        payload = json.loads(incoming.content)
        captured.append(payload)
        assert payload["tool_choice"] == "auto"
        assert payload["thinking"] == {"type": "enabled"}
        assert payload["reasoning_effort"] == "low"
        if len(captured) == 1:
            name, arguments = "read_project_file", {"path": "README.md"}
        else:
            assert payload["messages"][-2]["reasoning_content"] == "synthetic tool reasoning"
            tool_result = json.loads(payload["messages"][-1]["content"])
            evidence = tool_result["items"][0]
            name, arguments = (
                "Diagnosis",
                {
                    "summary": "已读取合成项目说明，尚无运行指标足以判断服务状态。",
                    "hypotheses": [],
                    "supporting_evidence": [
                        {
                            "id": evidence["id"],
                            "version": evidence["version"],
                            "quote": "Synthetic service instructions",
                        }
                    ],
                    "contradicting_evidence": [],
                    "missing_information": ["缺少运行指标"],
                    "recommended_checks": ["核查注册健康端点"],
                    "needs_human": True,
                    "ticket_draft": None,
                },
            )
        return httpx.Response(
            200,
            json={
                "id": "synthetic-wire",
                "object": "chat.completion",
                "created": 1,
                "model": "returned-flash-route",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "reasoning_content": "synthetic tool reasoning",
                            "tool_calls": [
                                {
                                    "id": f"wire-{len(captured)}",
                                    "type": "function",
                                    "function": {"name": name, "arguments": json.dumps(arguments)},
                                }
                            ],
                        },
                    }
                ],
                "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
            },
        )

    model = make_model(settings, "harness-contract")
    await model.root_async_client.close()
    model.root_client.close()
    client = openai.AsyncOpenAI(
        api_key="synthetic-key",
        base_url="https://api.deepseek.com",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(reply)),
    )
    model.root_async_client = client
    model.async_client = client.chat.completions
    session = DiagnosticSession(settings, "harness-contract", request)

    async def no_action(params):
        raise AssertionError("No action should be proposed in the read-only wire contract")

    try:
        with caplog.at_level("INFO", logger="incident_desk"):
            result = await session.graph(model=model, action_writer=no_action).ainvoke(
                session.input()
            )
        report = session.validate(result)
    finally:
        await client.close()
    assert len(captured) == 2
    tools = {entry["function"]["name"]: entry["function"] for entry in captured[0]["tools"]}
    assert {
        "inspect_project",
        "read_project_file",
        "search_project",
        "get_service_evidence",
        "propose_service_action",
        "Diagnosis",
    } <= tools.keys()
    assert not {"ls", "read_file", "write_file"} & tools.keys()
    evidence_schema = tools["get_service_evidence"]["parameters"]
    for field in ("start", "end"):
        variants = evidence_schema["properties"][field]["anyOf"]
        assert {"format": "date-time", "type": "string"} in variants
        assert {"type": "null"} in variants
        assert field not in evidence_schema.get("required", [])
    assert "anyOf" in tools["propose_service_action"]["parameters"]["properties"]["content"]
    assert report.supporting_evidence[0].quote == "Synthetic service instructions"
    assert all(row["status"] == "SUCCEEDED" for row in model._ledger.rows())
    ai_messages = [message for message in result["messages"] if isinstance(message, AIMessage)]
    assert ai_messages
    assert all(
        message.response_metadata["incidentdesk_wire_adaptations"] == ["thinking_required_to_auto"]
        for message in ai_messages
    )
    assert sum(record.getMessage() == "model.tool_choice_adapted" for record in caplog.records) == 2


@pytest.mark.asyncio
async def test_thinking_named_forcing_is_rejected_before_reservation_or_network(tmp_path):
    settings = Settings(
        _env_file=None, api_key="synthetic-key", local_dir=tmp_path, reasoning="high"
    )
    model = make_model(settings, "named-forcing")
    try:
        with pytest.raises(UnsupportedThinkingToolChoice, match="does not support forcing"):
            await model.ainvoke(
                [HumanMessage(content="inspect")],
                tool_choice={
                    "type": "function",
                    "function": {"name": "read_file"},
                },
            )
        assert model._ledger.rows() == []
    finally:
        await model.root_async_client.close()
        model.root_client.close()


@pytest.mark.asyncio
async def test_non_thinking_required_tool_contract_is_preserved(tmp_path):
    settings = Settings(
        _env_file=None, api_key="synthetic-key", local_dir=tmp_path, reasoning="off"
    )
    model = make_model(settings, "non-thinking-required")
    try:
        payload = model._get_request_payload(
            [HumanMessage(content="inspect")], tool_choice="required"
        )
        assert payload["tool_choice"] == "required"
    finally:
        await model.root_async_client.close()
        model.root_client.close()
