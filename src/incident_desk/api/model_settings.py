"""Local model settings API. Keys are write-only; provider errors are sanitized."""

import json
import uuid

import httpx
from fastapi import APIRouter, HTTPException, Request
from langchain_core.messages import HumanMessage, ToolMessage
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from incident_desk.agent.budget import CallLedger
from incident_desk.agent.models import make_model
from incident_desk.model_config import ModelConfigConflict, ModelConfigStore, ModelProfileInput
from incident_desk.settings import Settings


class VersionInput(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    version: int | None = Field(default=None, ge=1)


class ConnectionTestInput(VersionInput):
    allow_paid: bool = False


async def _body(request: Request, schema: type[BaseModel]) -> BaseModel:
    # Parse manually so FastAPI validation responses can never echo submitted keys.
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 16384:
            raise HTTPException(413, "Model settings request is too large")
    try:
        return schema.model_validate_json(bytes(raw) if raw else b"{}")
    except ValidationError as error:
        detail = [
            {"type": item["type"], "loc": item["loc"], "msg": item["msg"]}
            for item in error.errors(include_input=False, include_context=False)
        ]
        raise HTTPException(422, detail=detail) from None


def _connection_error(error: Exception) -> tuple[str, str]:
    name = type(error).__name__
    messages = {
        "AuthenticationError": "Credential rejected by the configured model endpoint.",
        "PermissionDeniedError": "The credential has no permission to use this model.",
        "NotFoundError": "The configured model or endpoint was not found.",
        "RateLimitError": "Model service rate limit or account balance prevented the test.",
        "APIConnectionError": "Could not connect to the configured model service.",
        "ConnectError": "Could not connect to the configured model service.",
        "APITimeoutError": "The model connection test timed out; no hidden retry was made.",
        "TimeoutError": "The model connection test timed out; no hidden retry was made.",
        "BudgetExceeded": "The durable budget prevented further model requests.",
        "ProbeContractError": "The model did not complete the required tool round trip.",
    }
    if name in messages:
        return name, messages[name]
    return (
        "ModelConnectionError",
        "Model service rejected or failed the test; no hidden retry was made.",
    )


class ProbeContractError(ValueError):
    pass


async def test_model_connection(settings: Settings) -> dict:
    """Exactly two metered requests at most; only a local, synthetic echo tool."""
    run_id = f"connection-test-{uuid.uuid4()}"
    bounded = settings.model_copy(
        update={
            "max_output_tokens": min(settings.max_output_tokens, 4096),
            "model_call_limit": 2,
            "request_timeout_seconds": min(settings.request_timeout_seconds, 60),
            "run_budget_cny": min(settings.run_budget_cny, 1),
        }
    )
    nonce = uuid.uuid4().hex
    schema = {
        "name": "incidentdesk_connection_probe",
        "description": "Echo a synthetic nonce to verify the tool protocol; no external actions.",
        "parameters": {
            "type": "object",
            "properties": {"nonce": {"type": "string", "enum": [nonce]}},
            "required": ["nonce"],
            "additionalProperties": False,
        },
    }
    model = None
    result = {
        "version": settings.model_profile_version,
        "ok": False,
        "tool_roundtrip": False,
        "requested_model": settings.model,
        "returned_model": None,
        "message": "Model connection test did not complete.",
    }
    try:
        model = make_model(bounded, run_id, scope="model-connection-test")
        bound = model.bind_tools([schema])
        question = HumanMessage(
            content=(
                f"Call incidentdesk_connection_probe exactly once with nonce {nonce}. "
                "After its result is returned, reply with a short confirmation "
                "and do not call more tools."
            )
        )
        first = await bound.ainvoke([question])
        if (
            len(first.tool_calls) != 1
            or first.tool_calls[0]["name"] != schema["name"]
            or first.tool_calls[0]["args"] != {"nonce": nonce}
        ):
            raise ProbeContractError()
        tool_result = ToolMessage(
            content=json.dumps({"nonce": nonce, "status": "ok"}),
            tool_call_id=first.tool_calls[0]["id"],
        )
        final = await bound.ainvoke([question, first, tool_result])
        if final.tool_calls or not final.content:
            raise ProbeContractError()
        result.update(
            ok=True, tool_roundtrip=True, message="The model completed a metered tool round trip."
        )
    except Exception as error:
        name, message = _connection_error(error)
        result.update(error_type=name, message=message)
    finally:
        if model is not None:
            await model.root_async_client.close()
            model.root_client.close()
    rows = [row for row in CallLedger(settings.app_db, bounded).rows() if row["run_id"] == run_id]
    result.update(
        model_calls=len(rows),
        known_cost_cny=sum(row["known_cost_cny"] or 0 for row in rows),
        unreconciled_reservations_cny=sum(
            row["reserved_cny"] for row in rows if row["known_cost_cny"] is None
        ),
        returned_model=next(
            (row["returned_model"] for row in reversed(rows) if row["returned_model"]), None
        ),
    )
    return result


def create_model_settings_router(
    settings: Settings, store: ModelConfigStore | None = None
) -> APIRouter:
    store = store or ModelConfigStore(settings)
    router = APIRouter(prefix="/api/model-settings", tags=["model-settings"])

    def selected(version: int | None) -> Settings:
        try:
            return store.settings_for_version(version or store.active_version())
        except ValueError:
            raise HTTPException(404, "Unknown model profile version") from None

    @router.get("")
    async def public_settings():
        return store.public()

    @router.put("")
    async def save_settings(request: Request):
        profile = await _body(request, ModelProfileInput)
        try:
            return store.save(profile)
        except ModelConfigConflict:
            raise HTTPException(409, "Model settings changed; reload before saving") from None
        except ValueError as error:
            # Store errors are authored constants; never include provider errors or raw input.
            raise HTTPException(422, str(error)) from None

    @router.post("/models")
    async def list_models(request: Request):
        body = await _body(request, VersionInput)
        profile = selected(body.version)
        if not profile.api_key:
            raise HTTPException(422, "Save an API key before listing models")
        try:
            async with httpx.AsyncClient(
                timeout=10, follow_redirects=False, trust_env=False
            ) as client:
                async with client.stream(
                    "GET",
                    f"{profile.base_url.rstrip('/')}/models",
                    headers={"Authorization": f"Bearer {profile.api_key.get_secret_value()}"},
                ) as response:
                    response.raise_for_status()
                    content = bytearray()
                    async for chunk in response.aiter_bytes():
                        content.extend(chunk)
                        if len(content) > 1048576:
                            raise ValueError("Model list response too large")
                    data = json.loads(content)
            models = [
                {"id": item["id"]}
                for item in data.get("data", [])[:200]
                if isinstance(item, dict)
                and isinstance(item.get("id"), str)
                and 0 < len(item["id"]) <= 150
            ]
            return {
                "version": profile.model_profile_version,
                "models": models,
                "inference_called": False,
            }
        except Exception:
            raise HTTPException(502, "Could not list models from the saved endpoint") from None

    @router.post("/test")
    async def connection_test(request: Request):
        body = await _body(request, ConnectionTestInput)
        if body.allow_paid is not True:
            raise HTTPException(
                422, "This test can incur API charges; explicitly set allow_paid=true"
            )
        profile = selected(body.version)
        if not profile.api_key:
            raise HTTPException(422, "Save an API key before testing the model")
        return await test_model_connection(profile)

    return router
