"""Fixed DeepSeek adapter, async-only, with every request durably metered."""

import asyncio
import copy
import time
from typing import Any

import httpx
from langchain_core.messages import AIMessage
from langchain_deepseek import ChatDeepSeek
from pydantic import PrivateAttr

from incident_desk.agent.budget import CallLedger
from incident_desk.cloud_policy import guard_payload
from incident_desk.observability import logger
from incident_desk.settings import Settings


class UnsupportedThinkingToolChoice(ValueError):
    """Named tool forcing cannot be honored in DeepSeek thinking mode."""


def thinking_enabled(payload: dict) -> bool:
    extra = payload.get("extra_body") or {}
    thinking = extra.get("thinking", payload.get("thinking", {}))
    return isinstance(thinking, dict) and thinking.get("type") == "enabled"


class MeteredDeepSeek(ChatDeepSeek):
    _ledger: CallLedger = PrivateAttr()
    _run_id: str = PrivateAttr()
    _scope: str = PrivateAttr()
    _wire_lock: asyncio.Lock = PrivateAttr(default_factory=asyncio.Lock)
    _prepared_payload: dict | None = PrivateAttr(default=None)

    def _get_request_payload(self, input_, *, stop=None, **kwargs):
        if self._prepared_payload is not None:
            return copy.deepcopy(self._prepared_payload)
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        if thinking_enabled(payload):
            choice = payload.get("tool_choice")
            if isinstance(choice, dict) or (
                isinstance(choice, str) and choice not in {"auto", "none", "required"}
            ):
                raise UnsupportedThinkingToolChoice(
                    "DeepSeek thinking mode does not support forcing a named tool; "
                    "use automatic selection and validate the result"
                )
            if choice == "required":
                # ToolStrategy requests 'required' whenever structured output
                # tools are available. DeepSeek rejects forced tool use while
                # thinking. The graph still requires a validated Diagnosis and
                # ends only on structured_response or its existing run limits.
                payload["tool_choice"] = "auto"
        # langchain-deepseek 1.1.1 reads reasoning_content but its inherited
        # OpenAI serializer drops it. Thinking tool rounds must send it back.
        messages = self._convert_input(input_).to_messages()
        for source, target in zip(messages, payload["messages"], strict=True):
            if isinstance(source, AIMessage) and "reasoning_content" in source.additional_kwargs:
                reasoning = source.additional_kwargs["reasoning_content"]
                if reasoning is not None:
                    target["reasoning_content"] = reasoning
        return payload

    def stream(self, *args: Any, **kwargs: Any):
        raise RuntimeError("Model streaming is disabled; use non-streaming ainvoke")

    async def astream(self, *args: Any, **kwargs: Any):
        raise RuntimeError("Model streaming is disabled; use non-streaming ainvoke")
        yield  # pragma: no cover

    def _generate(self, *args: Any, **kwargs: Any):
        raise RuntimeError("IncidentDesk models require async invocation for bounded timeouts")

    def _stream(self, *args: Any, **kwargs: Any):
        raise RuntimeError("Model streaming is disabled; use non-streaming ainvoke")

    async def _astream(self, *args: Any, **kwargs: Any):
        raise RuntimeError("Model streaming is disabled; use non-streaming ainvoke")
        yield  # pragma: no cover - preserve the async iterator protocol

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        async with self._wire_lock:
            try:
                return await self._metered_generate(messages, stop, run_manager, **kwargs)
            finally:
                self._prepared_payload = None

    async def _metered_generate(self, messages, stop=None, run_manager=None, **kwargs):
        payload = self._get_request_payload(messages, stop=stop, **kwargs)
        payload = await asyncio.to_thread(
            guard_payload, self._ledger.settings, self._run_id, payload, audit=True
        )
        # The exact sanitized payload is used both for budget accounting and
        # the provider's later serialization; no raw-message rebuild may escape it.
        self._prepared_payload = payload
        adaptations = (
            ["thinking_required_to_auto"]
            if thinking_enabled(payload) and kwargs.get("tool_choice") == "required"
            else []
        )
        call_id = await asyncio.to_thread(
            self._ledger.reserve,
            self._run_id,
            self._scope,
            self.model_name,
            {
                "payload": payload,
                "wire_adaptations": adaptations,
            },
        )
        if adaptations:
            logger.info(
                "model.tool_choice_adapted",
                extra={"run_id": self._run_id, "stage": "thinking_required_to_auto"},
            )
        start = time.monotonic()
        try:
            async with asyncio.timeout(float(self.request_timeout)):
                result = await super()._agenerate(
                    messages, stop=stop, run_manager=run_manager, **kwargs
                )
            message = result.generations[0].message
            if adaptations:
                message.response_metadata["incidentdesk_wire_adaptations"] = adaptations
            await asyncio.to_thread(
                self._ledger.finish,
                call_id,
                status="SUCCEEDED",
                duration=time.monotonic() - start,
                usage=getattr(message, "usage_metadata", None),
                returned_model=(result.llm_output or {}).get("model_name")
                or message.response_metadata.get("model_name"),
            )
            return result
        except BaseException as error:
            await asyncio.to_thread(
                self._ledger.finish,
                call_id,
                status="FAILED",
                duration=time.monotonic() - start,
                error_type=type(error).__name__,
            )
            raise


def make_model(settings: Settings, run_id: str, *, scope: str = "runtime") -> MeteredDeepSeek:
    if not settings.api_key or not settings.api_key.get_secret_value().strip():
        raise RuntimeError("Missing DEEPSEEK_API_KEY; configure it in the backend .env file")
    body = {"thinking": {"type": "disabled" if settings.reasoning == "off" else "enabled"}}
    if settings.reasoning != "off":
        body["reasoning_effort"] = settings.reasoning
    model = MeteredDeepSeek(
        model=settings.model,
        api_key=settings.api_key,
        api_base=settings.base_url,
        max_tokens=settings.max_output_tokens,
        timeout=settings.request_timeout_seconds,
        max_retries=0,
        streaming=False,
        stream_usage=False,
        extra_body=body,
        http_client=httpx.Client(follow_redirects=False, trust_env=False),
        http_async_client=httpx.AsyncClient(follow_redirects=False, trust_env=False),
    )
    model._ledger = CallLedger(settings.app_db, settings)
    model._run_id = run_id
    model._scope = scope
    return model
