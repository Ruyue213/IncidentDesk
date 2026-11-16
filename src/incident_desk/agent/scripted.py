"""Deterministic model for offline contracts and fault experiments, not diagnosis quality."""

from collections.abc import Sequence
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import Field, PrivateAttr


class ScriptedToolModel(BaseChatModel):
    """Emit planned tool calls, then report actual ToolMessages from the next request."""

    model_name: str = "offline-script-v1"
    plans: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)
    transcript: list[dict[str, Any]] = Field(default_factory=list, exclude=True)
    tool_schemas: list[list[dict[str, Any]]] = Field(default_factory=list, exclude=True)
    _bound_tools: list[dict[str, Any]] = PrivateAttr(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "incidentdesk-scripted"

    def _get_ls_params(self, stop=None, **kwargs):
        return {"ls_provider": "incidentdesk", "ls_model_name": self.model_name}

    def bind_tools(self, tools: Sequence[Any], *, tool_choice=None, **kwargs):
        schemas = [convert_to_openai_tool(tool) for tool in tools]
        self._bound_tools = schemas
        self.tool_schemas.append(schemas)
        return self

    def _generate(self, messages: list[BaseMessage], stop=None, run_manager=None, **kwargs):
        human_index = max(
            i for i, message in enumerate(messages) if isinstance(message, HumanMessage)
        )
        question = str(messages[human_index].content)
        recent = messages[human_index + 1 :]
        completed_steps = sum(isinstance(m, AIMessage) and bool(m.tool_calls) for m in recent)
        plan = self.plans.get(question, [])
        self.transcript.append(
            {
                "messages": [message.model_dump(mode="json") for message in messages],
                "tool_schemas": self._bound_tools,
            }
        )
        if completed_steps < len(plan):
            call = plan[completed_steps]
            response = AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": call["name"],
                        "args": call["args"],
                        "id": f"script-call-{len(self.transcript)}",
                        "type": "tool_call",
                    }
                ],
            )
        else:
            results = [m for m in recent if isinstance(m, ToolMessage)]
            if plan and len(results) != len(plan):
                raise AssertionError(
                    "Final answer requires actual ToolMessages for every planned call"
                )
            response = AIMessage(
                content="Observed tool results: "
                + "\n".join(f"[{m.name}:{m.status}] {m.content}" for m in results)
            )
        return ChatResult(generations=[ChatGeneration(message=response)])
