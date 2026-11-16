"""Synthetic provider contracts via HTTPX MockTransport; never read .env or call an API."""

import asyncio
import json

import httpx
import openai
import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from incident_desk.agent.models import make_model
from incident_desk.settings import Settings


@pytest.fixture
def settings(tmp_path, monkeypatch):
    for variable in ("DEEPSEEK_API_KEY", "LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2"):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "false")
    return Settings(
        _env_file=None,
        api_key="offline-synthetic-key",
        base_url="https://deepseek.invalid",
        local_dir=tmp_path,
    )


def provider_reply(*, content="offline diagnosis", usage=True, tool_call=False):
    message = {"role": "assistant", "content": None if tool_call else content}
    if tool_call:
        message["tool_calls"] = [
            {
                "id": "mock-call-1",
                "type": "function",
                "function": {"name": "probe_evidence", "arguments": '{"source":"logs"}'},
            }
        ]
    body = {
        "id": "mock-completion",
        "object": "chat.completion",
        "created": 1794212000,
        "model": "deepseek-mocked-contract-v1",
        "choices": [
            {"index": 0, "message": message, "finish_reason": "tool_calls" if tool_call else "stop"}
        ],
    }
    if usage:
        body["usage"] = {
            "prompt_tokens": 1000,
            "completion_tokens": 50,
            "total_tokens": 1050,
            "prompt_cache_hit_tokens": 600,
            "prompt_cache_miss_tokens": 400,
        }
    return httpx.Response(200, json=body)


async def install_transport(model, handler):
    """Replace only the provider transport, retaining configured retry and timeout values."""
    retries = model.root_async_client.max_retries
    await model.root_async_client.close()
    model.root_client.close()
    client = openai.AsyncOpenAI(
        api_key="offline-synthetic-key",
        base_url="https://deepseek.invalid",
        max_retries=retries,
        timeout=model.request_timeout,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    model.root_async_client = client
    model.async_client = client.chat.completions
    return client


@pytest.mark.parametrize("key", [None, "", "   "])
def test_missing_key_fails_before_creating_a_ledger(settings, key):
    settings = settings.model_copy(update={"api_key": key})
    # model_copy deliberately does not validate; preserve SecretStr for non-None cases.
    if key is not None:
        settings = Settings(
            _env_file=None, api_key=key, base_url=settings.base_url, local_dir=settings.local_dir
        )
    with pytest.raises(RuntimeError, match="Missing DEEPSEEK_API_KEY"):
        make_model(settings, "missing-key")
    assert not settings.app_db.exists()


@pytest.mark.asyncio
async def test_actual_wire_payload_disables_thinking_and_captures_usage(settings):
    captured = []

    def respond(request):
        assert request.url.host == "deepseek.invalid"
        captured.append(json.loads(request.content))
        return provider_reply()

    model = make_model(settings, "wire-contract", scope="CP02")
    assert model.max_retries == model.root_async_client.max_retries == 0
    assert model.streaming is False
    client = await install_transport(model, respond)
    try:
        result = await model.ainvoke([HumanMessage(content="check evidence")])
    finally:
        await client.close()

    assert len(captured) == 1
    assert captured[0]["thinking"] == {"type": "disabled"}
    assert captured[0]["stream"] is False
    assert captured[0]["max_tokens"] == 1500
    assert result.content == "offline diagnosis"
    row = model._ledger.rows()[0]
    assert row["status"] == "SUCCEEDED"
    assert row["returned_model"] == "deepseek-mocked-contract-v1"
    assert row["known_cost_cny"] == pytest.approx(0.001224)
    assert row["cache_read_tokens"] == 600


@pytest.mark.asyncio
async def test_tool_call_and_tool_message_round_trip_through_adapter(settings):
    captured = []

    def respond(request):
        captured.append(json.loads(request.content))
        return provider_reply(tool_call=len(captured) == 1)

    model = make_model(settings, "tool-contract", scope="CP02")
    client = await install_transport(model, respond)
    schema = {
        "name": "probe_evidence",
        "description": "Read bounded synthetic evidence",
        "parameters": {
            "type": "object",
            "properties": {"source": {"type": "string", "enum": ["logs"]}},
            "required": ["source"],
        },
    }
    try:
        bound = model.bind_tools([schema])
        question = HumanMessage(content="Inspect logs")
        requested = await bound.ainvoke([question])
        assert isinstance(requested, AIMessage)
        assert requested.tool_calls[0]["args"] == {"source": "logs"}
        tool_result = ToolMessage(content="synthetic log evidence", tool_call_id="mock-call-1")
        result = await bound.ainvoke([question, requested, tool_result])
    finally:
        await client.close()

    assert result.content == "offline diagnosis"
    assert captured[1]["messages"][-1] == {
        "role": "tool",
        "content": "synthetic log evidence",
        "tool_call_id": "mock-call-1",
    }
    assert len(model._ledger.rows()) == 2
    assert all(row["known_cost_cny"] is not None for row in model._ledger.rows())


@pytest.mark.asyncio
async def test_server_error_has_no_hidden_adapter_retry(settings):
    attempts = []

    def fail(request):
        attempts.append(request)
        return httpx.Response(500, json={"error": {"message": "synthetic upstream failure"}})

    model = make_model(settings, "no-retry")
    client = await install_transport(model, fail)
    try:
        with pytest.raises(openai.APIStatusError) as failure:
            await model.ainvoke("inspect")
    finally:
        await client.close()

    assert len(attempts) == 1
    row = model._ledger.rows()[0]
    assert row["status"] == "FAILED"
    assert row["error_type"] == type(failure.value).__name__
    assert row["known_cost_cny"] is None
    assert row["reserved_cny"] > 0


@pytest.mark.asyncio
async def test_whole_request_timeout_keeps_unreconciled_reservation(settings):
    settings = settings.model_copy(update={"request_timeout_seconds": 0.02})
    attempts = []

    async def never_finishes(request):
        attempts.append(request)
        await asyncio.Event().wait()

    model = make_model(settings, "timeout")
    client = await install_transport(model, never_finishes)
    try:
        with pytest.raises(TimeoutError):
            await model.ainvoke("inspect")
    finally:
        await client.close()

    assert len(attempts) == 1
    row = model._ledger.rows()[0]
    assert row["status"] == "FAILED"
    assert row["error_type"] == "TimeoutError"
    assert row["known_cost_cny"] is None
    assert row["reserved_cny"] > 0


@pytest.mark.asyncio
async def test_success_without_usage_is_unknown_not_free(settings):
    model = make_model(settings, "missing-usage")
    client = await install_transport(model, lambda request: provider_reply(usage=False))
    try:
        result = await model.ainvoke("inspect")
    finally:
        await client.close()

    assert result.content == "offline diagnosis"
    row = model._ledger.rows()[0]
    assert row["status"] == "SUCCEEDED"
    assert row["known_cost_cny"] is None
    assert row["input_tokens"] is None
    assert row["output_tokens"] is None
    assert row["reserved_cny"] > 0


@pytest.mark.asyncio
async def test_streaming_entry_points_are_disabled_before_any_provider_request(settings):
    attempted = []

    def must_not_send(request):
        attempted.append(request)
        raise AssertionError("streaming must be rejected before sending")

    model = make_model(settings, "stream-disabled")
    client = await install_transport(model, must_not_send)
    try:
        with pytest.raises(RuntimeError, match="streaming is disabled"):
            list(model.stream("inspect"))
        with pytest.raises(RuntimeError, match="streaming is disabled"):
            async for _ in model.astream("inspect"):
                pass
        with pytest.raises(RuntimeError, match="streaming is disabled"):
            model._stream([HumanMessage(content="inspect")])
        with pytest.raises(RuntimeError, match="streaming is disabled"):
            async for _ in model._astream([HumanMessage(content="inspect")]):
                pass
    finally:
        await client.close()
    assert attempted == []
    assert model._ledger.rows() == []


@pytest.mark.asyncio
async def test_cancellation_keeps_an_unreconciled_persistent_charge(settings):
    entered_transport = asyncio.Event()

    async def interrupted_request(request):
        entered_transport.set()
        await asyncio.Event().wait()

    model = make_model(settings, "cancelled-request")
    client = await install_transport(model, interrupted_request)
    task = asyncio.create_task(model.ainvoke("inspect"))
    try:
        await asyncio.wait_for(entered_transport.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        await client.close()

    row = model._ledger.rows()[0]
    assert row["status"] == "FAILED"
    assert row["error_type"] == "CancelledError"
    assert row["known_cost_cny"] is None
    assert row["reserved_cny"] > 0
