"""Version-pinned Deep Agents assembly with explicit capability boundaries.

The profile limits model-visible tools; ToolAllowlistMiddleware independently
checks execution. Files live in graph state, never in the host filesystem.
"""

from collections.abc import Callable, Sequence
from typing import Any

from deepagents import (
    GeneralPurposeSubagentProfile,
    HarnessProfile,
    create_deep_agent,
    register_harness_profile,
)
from deepagents.backends import StateBackend
from deepagents.middleware.filesystem import FilesystemMiddleware
from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ToolCallRequest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool

VIRTUAL_TOOLS = frozenset({"ls", "read_file", "write_file"})
BUSINESS_TOOLS = frozenset(
    {
        "search_knowledge",
        "get_service_evidence",
        "create_ticket",
        "read_project_file",
        "search_project",
        "propose_service_action",
        "inspect_project",
        "get_metric_history",
        "run_business_probe",
    }
)
DISALLOWED_TOOLS = frozenset(
    {
        "task",
        "execute",
        "edit_file",
        "delete",
        "glob",
        "grep",
        "write_todos",
        "launch_subagent",
        "check_subagent",
        "cancel_subagent",
    }
)


def execution_step_limit(model_calls: int, tool_calls: int) -> int:
    """Allow middleware graph nodes without replacing the durable call budgets.

    A single model/tool round includes several before/after/approval nodes. A
    fixed graph limit can therefore stop well before the configured call budget.
    The ledger, RuntimeGuard and active-time deadline remain the actual limits.
    """
    return max(128, 16 * (model_calls + 2) + 4 * tool_calls)


class ToolAllowlistMiddleware(AgentMiddleware):
    """Refuse unapproved capabilities even if a model invents a tool call."""

    def __init__(self, allowed: frozenset[str]):
        self.allowed = allowed

    def _refusal(self, request: ToolCallRequest) -> ToolMessage | None:
        if request.tool_call["name"] in self.allowed:
            return None
        return ToolMessage(
            content="TOOL_NOT_ALLOWED: requested capability is not available in this run",
            tool_call_id=request.tool_call["id"],
            name=request.tool_call["name"],
            status="error",
        )

    def wrap_tool_call(self, request: ToolCallRequest, handler: Callable) -> Any:
        refusal = self._refusal(request)
        return refusal if refusal is not None else handler(request)

    async def awrap_tool_call(self, request: ToolCallRequest, handler: Callable) -> Any:
        refusal = self._refusal(request)
        return refusal if refusal is not None else await handler(request)


def build_harness(
    model: BaseChatModel,
    tools: Sequence[BaseTool],
    *,
    response_format: Any = None,
    middleware: Sequence[AgentMiddleware] = (),
    checkpointer: Any = None,
    interrupt_on: dict[str, Any] | None = None,
    context_schema: type | None = None,
    system_prompt: str = "Use supplied evidence; distinguish observations from hypotheses.",
    virtual_files: bool = True,
) -> Any:
    """Build the single-agent runtime used offline and with ChatDeepSeek.

    The pinned SDK resolves profiles from provider and model name. Fail closed
    when that identity is absent rather than quietly inheriting default powers.
    Schema output should be an explicit ToolStrategy(handle_errors=False),
    supplied by the caller; structured output tools are handled by LangChain.
    """
    names = [tool.name for tool in tools]
    if len(names) != len(set(names)) or not set(names) <= BUSINESS_TOOLS:
        raise ValueError("Business tools must be unique and explicitly permitted")
    params = model._get_ls_params()
    provider = params.get("ls_provider")
    identifier = getattr(model, "model_name", None) or getattr(model, "model", None)
    if not isinstance(provider, str) or not provider or not isinstance(identifier, str):
        raise ValueError("Cannot resolve model identity for the restricted HarnessProfile")
    register_harness_profile(
        f"{provider}:{identifier}",
        HarnessProfile(
            general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False),
            excluded_tools=DISALLOWED_TOOLS | (frozenset() if virtual_files else VIRTUAL_TOOLS),
            excluded_middleware=frozenset({"SummarizationMiddleware"}),
        ),
    )
    backend = StateBackend()
    filesystem = FilesystemMiddleware(
        backend=backend,
        tools=["ls", "read_file", "write_file"] if virtual_files else ["read_file"],
        tool_token_limit_before_evict=None,
        human_message_token_limit_before_evict=None,
    )
    return create_deep_agent(
        model=model,
        tools=tools,
        system_prompt=system_prompt,
        backend=backend,
        subagents=[],
        middleware=[
            filesystem,
            ToolAllowlistMiddleware(
                (VIRTUAL_TOOLS if virtual_files else frozenset()) | frozenset(names)
            ),
            *middleware,
        ],
        response_format=response_format,
        checkpointer=checkpointer,
        interrupt_on=interrupt_on,
        context_schema=context_schema,
        name="incident-desk",
    ).with_config({"recursion_limit": 50})
