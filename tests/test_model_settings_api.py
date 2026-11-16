"""Settings routes use synthetic transports only; no .env or paid provider requests."""

import json
from types import SimpleNamespace

import httpx
import openai
import pytest
from fastapi import FastAPI

from incident_desk.api import model_settings as module
from incident_desk.api.model_settings import create_model_settings_router
from incident_desk.model_config import ModelConfigStore
from incident_desk.settings import Settings


@pytest.fixture
def configured(tmp_path, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "false")
    settings = Settings(_env_file=None, api_key="synthetic-private-key", local_dir=tmp_path)
    store = ModelConfigStore(settings)
    app = FastAPI()
    app.include_router(create_model_settings_router(settings, store))
    return settings, store, app


@pytest.mark.asyncio
async def test_config_api_key_is_write_only_in_success_and_validation_error(configured):
    _, store, app = configured
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        public = (await client.get("/api/model-settings")).json()
        assert public["profile"]["has_credential"] is True
        assert "synthetic-private-key" not in json.dumps(public)
        response = await client.put(
            "/api/model-settings",
            json={
                "expected_version": 1,
                "api_key": "a-new-secret",
                "reasoning": "high",
            },
        )
        assert response.status_code == 200
        assert "a-new-secret" not in response.text
        assert response.json()["active_version"] == 2
        response = await client.put(
            "/api/model-settings",
            json={
                "api_key": "sensitive-key-with whitespace",
                "project_budget_cny": 999,
            },
        )
        assert response.status_code == 422
        assert "sensitive-key" not in response.text
        assert "input" not in response.text
        assert store.active_version() == 2
        response = await client.put(
            "/api/model-settings",
            json={
                "api_key": "secret",
                "expected_version": 1,
            },
        )
        assert response.status_code == 409


@pytest.mark.asyncio
async def test_paid_probe_requires_explicit_action_and_saved_credential(configured, monkeypatch):
    _, _, app = configured

    def forbidden(*args, **kwargs):
        raise AssertionError("must not construct a provider client")

    monkeypatch.setattr(module, "make_model", forbidden)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.post("/api/model-settings/test", json={})
        assert response.status_code == 422
        response = await client.put("/api/model-settings", json={"clear_credential": True})
        assert response.status_code == 200
        response = await client.post("/api/model-settings/test", json={"allow_paid": True})
        assert response.status_code == 422
        assert "API key" in response.text


@pytest.mark.asyncio
async def test_model_list_is_bounded_non_inference_and_ignores_secret_response_fields(
    configured, monkeypatch
):
    _, _, app = configured
    captured = []

    def reply(request):
        captured.append(request)
        return httpx.Response(
            200,
            json={
                "data": [{"id": "deepseek-flash", "key": "upstream-secret"}],
            },
        )

    monkeypatch.setattr(
        module,
        "httpx",
        SimpleNamespace(
            AsyncClient=lambda **kwargs: httpx.AsyncClient(
                transport=httpx.MockTransport(reply), **kwargs
            )
        ),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.post("/api/model-settings/models", json={"version": 1})
    assert response.status_code == 200
    assert response.json() == {
        "version": 1,
        "models": [{"id": "deepseek-flash"}],
        "inference_called": False,
    }
    assert len(captured) == 1
    assert captured[0].method == "GET"
    assert captured[0].url.path == "/models"
    assert "upstream-secret" not in response.text


@pytest.mark.asyncio
async def test_paid_probe_completes_two_actual_metered_tool_requests(configured, monkeypatch):
    _, store, app = configured
    original_factory = module.make_model
    originals = []
    captured = []

    def reply(request):
        payload = json.loads(request.content)
        captured.append(payload)
        nonce = payload["tools"][0]["function"]["parameters"]["properties"]["nonce"]["enum"][0]
        first = len(captured) == 1
        message = {"role": "assistant", "content": "" if first else "probe confirmed"}
        if first:
            message.update(
                reasoning_content="synthetic reasoning",
                tool_calls=[
                    {
                        "id": "synthetic-probe",
                        "type": "function",
                        "function": {
                            "name": "incidentdesk_connection_probe",
                            "arguments": json.dumps({"nonce": nonce}),
                        },
                    }
                ],
            )
        return httpx.Response(
            200,
            json={
                "id": "synthetic",
                "object": "chat.completion",
                "created": 1,
                "model": "returned-flash-route",
                "choices": [
                    {
                        "index": 0,
                        "message": message,
                        "finish_reason": "tool_calls" if first else "stop",
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
            },
        )

    def factory(settings, run_id, **kwargs):
        model = original_factory(settings, run_id, **kwargs)
        originals.append(model.root_async_client)
        model.root_client.close()
        client = openai.AsyncOpenAI(
            api_key="synthetic-key",
            base_url="https://api.deepseek.com",
            max_retries=0,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(reply)),
        )
        model.root_async_client = client
        model.async_client = client.chat.completions
        return model

    monkeypatch.setattr(module, "make_model", factory)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/api/model-settings/test",
                json={
                    "version": store.freeze(),
                    "allow_paid": True,
                },
            )
    finally:
        for original in originals:
            await original.close()
    assert response.status_code == 200
    result = response.json()
    assert result["ok"] is result["tool_roundtrip"] is True
    assert result["model_calls"] == 2
    assert result["known_cost_cny"] > 0
    assert result["unreconciled_reservations_cny"] == 0
    assert result["requested_model"] == "deepseek-flash"
    assert result["returned_model"] == "returned-flash-route"
    assert len(captured) == 2
    assert captured[1]["messages"][1]["reasoning_content"] == "synthetic reasoning"
    assert "synthetic-private-key" not in response.text
    assert "synthetic reasoning" not in response.text


@pytest.mark.asyncio
async def test_provider_error_body_is_not_echoed(configured, monkeypatch):
    _, _, app = configured

    def factory(*args, **kwargs):
        raise RuntimeError("upstream echoed synthetic-private-key and private incident")

    monkeypatch.setattr(module, "make_model", factory)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.post("/api/model-settings/test", json={"allow_paid": True})
    assert response.status_code == 200
    assert response.json()["ok"] is False
    assert response.json()["model_calls"] == 0
    assert "synthetic-private-key" not in response.text
    assert "private incident" not in response.text
