"""Deterministic graph-driving model for fault experiments, never a quality baseline."""

import json
import time

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import PrivateAttr

from incident_desk.agent.budget import CallLedger
from incident_desk.settings import Settings


class ReliabilityModel(BaseChatModel):
    model_name: str = "incident-desk-scripted-v1"
    _ledger: CallLedger = PrivateAttr()
    _run_id: str = PrivateAttr()

    @property
    def _llm_type(self):
        return "incident-desk-scripted"

    def _get_ls_params(self, stop=None, **kwargs):
        return {"ls_provider": "incident-desk-scripted", "ls_model_name": self.model_name}

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        return self.bind(tools=[convert_to_openai_tool(tool) for tool in tools], **kwargs)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        start = time.monotonic()
        call_id = self._ledger.reserve(
            self._run_id,
            "reliability-fake",
            self.model_name,
            {
                "messages": [message.model_dump() for message in messages],
                "tools": kwargs.get("tools", []),
            },
        )
        try:
            request = json.loads(
                str(next(m.content for m in messages if isinstance(m, HumanMessage)))
            )
            results = [m for m in messages if isinstance(m, ToolMessage)]
            evidence_messages = [m for m in results if m.name == "get_service_evidence"]
            ticket_messages = [m for m in results if m.name == "create_ticket"]
            evidence = json.loads(evidence_messages[-1].content) if evidence_messages else []
            reference = (
                {
                    "id": evidence[0]["id"],
                    "version": evidence[0]["version"],
                    "quote": evidence[0]["content"][:80],
                }
                if evidence
                else None
            )
            draft = {
                "title": "模拟故障人工交接",
                "description": "离线脚本模型流程验证，请人工核对证据。",
                "priority": "medium",
            }
            if not evidence_messages:
                name = "get_service_evidence"
                args = {
                    "service_id": request["service_id"],
                    "source": "metrics",
                    "start": request["start"],
                    "end": request["end"],
                    "limit": 10,
                }
            elif (
                reference
                and not ticket_messages
                and any(word in request["question"] for word in ("工单", "建单", "ticket"))
            ):
                name = "create_ticket"
                args = {"draft": draft, "references": [reference]}
            else:
                name = "Diagnosis"
                args = {
                    "summary": "离线脚本模型已取得模拟证据；此结果仅验证执行流程，不证明诊断质量。",
                    "hypotheses": ["需要人工依据完整证据判断故障原因"],
                    "supporting_evidence": [reference] if reference else [],
                    "contradicting_evidence": [],
                    "missing_information": ["离线脚本模型未进行真实模型推断"],
                    "recommended_checks": ["核对来源证据并运行真实模型质量评测"],
                    "needs_human": True,
                    "ticket_draft": draft if reference else None,
                }
            response = AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": name,
                        "args": args,
                        "id": f"fake-{len(messages)}-{name}",
                        "type": "tool_call",
                    }
                ],
            )
            return ChatResult(generations=[ChatGeneration(message=response)])
        finally:
            self._ledger.finish(
                call_id,
                status="SUCCEEDED",
                duration=time.monotonic() - start,
                usage={"input_tokens": 0, "output_tokens": 0},
                returned_model=self.model_name,
            )


def make_reliability_model(settings: Settings, run_id: str) -> ReliabilityModel:
    model = ReliabilityModel()
    model._ledger = CallLedger(settings.app_db, settings)
    model._run_id = run_id
    return model
