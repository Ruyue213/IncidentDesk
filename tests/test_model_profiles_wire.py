"""Offline wire contracts for profile reasoning and immutable price accounting."""

import json

import httpx
import openai
import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from incident_desk.agent.budget import CallLedger
from incident_desk.agent.models import make_model
from incident_desk.settings import Settings


@pytest.mark.asyncio
@pytest.mark.parametrize("effort", ["off", "low", "high", "max"])
async def test_reasoning_effort_and_tool_reasoning_survive_entire_wire_round_trip(
    tmp_path, monkeypatch, effort
):
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "false")
    captured = []

    def reply(request):
        payload = json.loads(request.content)
        captured.append(payload)
        first = len(captured) == 1
        message = {
            "role": "assistant",
            "content": "" if first else "confirmed",
            "reasoning_content": "synthetic private reasoning" if first else "checked",
        }
        if first:
            message["tool_calls"] = [
                {
                    "id": "probe-1",
                    "type": "function",
                    "function": {"name": "probe", "arguments": '{"source":"logs"}'},
                }
            ]
        return httpx.Response(
            200,
            json={
                "id": "offline-test",
                "object": "chat.completion",
                "created": 1,
                "model": "returned-model-alias",
                "choices": [
                    {
                        "index": 0,
                        "message": message,
                        "finish_reason": "tool_calls" if first else "stop",
                    }
                ],
                "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
            },
        )

    settings = Settings(
        _env_file=None,
        api_key="synthetic-key",
        local_dir=tmp_path,
        reasoning=effort,
        model_profile_version=7,
        max_output_tokens=4096,
    )
    model = make_model(settings, "thinking-wire")
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
    try:
        bound = model.bind_tools(
            [
                {
                    "name": "probe",
                    "description": "Synthetic read-only probe",
                    "parameters": {"type": "object", "properties": {"source": {"type": "string"}}},
                }
            ]
        )
        question = HumanMessage(content="Inspect logs")
        requested = await bound.ainvoke([question])
        final = await bound.ainvoke(
            [question, requested, ToolMessage(content="synthetic log", tool_call_id="probe-1")]
        )
    finally:
        await client.close()
    assert final.content == "confirmed"
    assert isinstance(requested, AIMessage)
    assert requested.additional_kwargs["reasoning_content"] == "synthetic private reasoning"
    assert captured[1]["messages"][1]["reasoning_content"] == "synthetic private reasoning"
    assert captured[1]["messages"][2]["tool_call_id"] == "probe-1"
    assert captured[0]["thinking"]["type"] == ("disabled" if effort == "off" else "enabled")
    if effort == "off":
        assert "reasoning_effort" not in captured[0]
    else:
        assert captured[0]["reasoning_effort"] == effort
    assert all(row["model_profile_version"] == 7 for row in model._ledger.rows())
    assert all(row["returned_model"] == "returned-model-alias" for row in model._ledger.rows())


def test_reconciliation_uses_reserved_price_snapshot_even_after_profile_change(tmp_path):
    settings = Settings(
        _env_file=None,
        local_dir=tmp_path,
        input_per_million_cny=10,
        output_per_million_cny=20,
        cache_read_per_million_cny=1,
        model_profile_version=3,
    )
    original = CallLedger(settings.app_db, settings)
    identifier = original.reserve("run", "runtime", "priced-model", {"text": "hello"})
    # A restarted reconciler must honor the original call's price revision.
    changed = CallLedger(
        settings.app_db,
        settings.model_copy(
            update={
                "input_per_million_cny": 999,
                "output_per_million_cny": 999,
            }
        ),
    )
    changed.finish(
        identifier,
        status="SUCCEEDED",
        duration=0.1,
        usage={
            "input_tokens": 1000,
            "output_tokens": 50,
            "input_token_details": {"cache_read": 600},
        },
    )
    row = changed.rows()[0]
    assert row["known_cost_cny"] == pytest.approx(0.0056)
    assert row["model_profile_version"] == 3


def test_profile_larger_bounded_input_and_project_budget_remain_enforced(tmp_path):
    settings = Settings(_env_file=None, local_dir=tmp_path, max_request_bytes=262144)
    ledger = CallLedger(settings.app_db, settings)
    ledger.reserve("run", "runtime", "model", {"text": "a" * 100000})
    assert ledger.rows()[0]["input_bytes"] > 100000
    assert ledger.rows()[0]["reserved_cny"] > 0.2


@pytest.mark.asyncio
async def test_http_clients_refuse_redirects_that_could_forward_a_credential(tmp_path):
    settings = Settings(_env_file=None, api_key="synthetic-key", local_dir=tmp_path)
    model = make_model(settings, "redirect-policy")
    assert model.http_client.follow_redirects is False
    assert model.http_async_client.follow_redirects is False
    model.root_client.close()
    await model.root_async_client.close()
