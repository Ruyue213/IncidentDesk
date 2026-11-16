"""CP02 contract probe: real DeepSeek, bounded calls, simulated evidence only."""

import asyncio
import json
import sys
import uuid
from datetime import UTC, datetime
from typing import Literal

from langchain.agents.structured_output import ToolStrategy
from langchain_core.messages import ToolMessage
from langchain_core.tools import ToolException, tool
from pydantic import BaseModel, ConfigDict, Field

from incident_desk.agent.budget import CallLedger
from incident_desk.agent.harness import build_harness
from incident_desk.agent.models import make_model
from incident_desk.settings import ROOT, load_settings


class ContractResult(BaseModel):
    """Report the exact observed fixture value, or explicitly report missing evidence."""

    model_config = ConfigDict(extra="forbid")
    service_id: Literal["checkout-api"]
    state: Literal["observed", "unavailable"]
    error_rate: float | None
    evidence_ids: list[str]
    summary: str = Field(max_length=500)


def make_evidence_tool(failure: bool, executed: list):
    @tool
    def get_service_evidence(
        service_id: Literal["checkout-api"], source: Literal["metrics"]
    ) -> dict:
        """Read the current service metrics fixture. It may be unavailable."""
        executed.append({"service_id": service_id, "source": source})
        if failure:
            raise ToolException("fixture_unavailable: metrics are unavailable")
        return {
            "evidence_id": "cp02-metrics-001",
            "error_rate": 0.073,
            "source": "metrics",
            "service_id": service_id,
        }

    get_service_evidence.handle_tool_error = True
    return get_service_evidence


async def main() -> int:
    settings = load_settings()
    # Validate configuration before constructing requests or writing any credentials.
    session = str(uuid.uuid4())
    plain = make_model(settings, f"cp02-{session}-plain", scope="CP02")
    output = ROOT / "docs/evidence/CP02"
    output.mkdir(parents=True, exist_ok=True)
    report = {
        "session": session,
        "started_at": datetime.now(UTC).isoformat(),
        "configuration": {
            "model": settings.model,
            "base_url": settings.base_url,
            "thinking": "disabled",
            "max_retries": 0,
            "max_tokens": settings.max_output_tokens,
            "request_timeout_seconds": settings.request_timeout_seconds,
        },
        "data": "simulated CP02 evidence; not a diagnosis-quality evaluation",
        "cases": [],
    }
    try:
        reply = await plain.ainvoke("只回复：IncidentDesk 在线合同验证")
        assert reply.content and not reply.tool_calls
        report["cases"].append(
            {"name": "plain_reply", "status": "passed", "content": reply.content}
        )
        for failure in (False, True):
            executed = []

            get_service_evidence = make_evidence_tool(failure, executed)
            model = make_model(settings, f"cp02-{session}-tool-{failure}", scope="CP02")
            graph = build_harness(
                model,
                [get_service_evidence],
                response_format=ToolStrategy(ContractResult, handle_errors=False),
                system_prompt=(
                    "你是工具合同测试助手。必须首先调用get_service_evidence获取checkout-api的metrics。"
                    "只根据工具返回的数据生成ContractResult；数据不可用则state=unavailable、"
                    "error_rate=null、evidence_ids=[]。禁止猜测，不使用文件工具。"
                ),
            )
            async with asyncio.timeout(settings.active_seconds):
                result = await graph.ainvoke(
                    {"messages": [{"role": "user", "content": "读取当前指标并提交结构化结果。"}]},
                    {"recursion_limit": 24},
                )
            parsed = result["structured_response"]
            assert executed, "business tool was not executed"
            tool_messages = [m for m in result["messages"] if isinstance(m, ToolMessage)]
            if failure:
                assert parsed.state == "unavailable" and parsed.error_rate is None
                assert not parsed.evidence_ids
                assert any(m.status == "error" for m in tool_messages)
            else:
                assert parsed.state == "observed" and parsed.error_rate == 0.073
                assert parsed.evidence_ids == ["cp02-metrics-001"]
            report["cases"].append(
                {
                    "name": "tool_failure" if failure else "tool_roundtrip_and_schema",
                    "status": "passed",
                    "executions": executed,
                    "structured_result": parsed.model_dump(),
                    "messages": [
                        {
                            "type": m.type,
                            "content": m.content,
                            "tool_calls": getattr(m, "tool_calls", []),
                            "tool_call_id": getattr(m, "tool_call_id", None),
                        }
                        for m in result["messages"]
                    ],
                }
            )
        report["status"] = "passed"
    except Exception as error:
        # Exception messages from HTTP clients may contain sensitive request data.
        report["status"] = "failed"
        report["error_type"] = type(error).__name__
    finally:
        rows = [
            r for r in CallLedger(settings.app_db, settings).rows("CP02") if session in r["run_id"]
        ]
        report["calls"] = rows
        report["known_cost_cny_peak_estimate"] = sum(r["known_cost_cny"] or 0 for r in rows)
        report["unreconciled_reservations_cny"] = sum(
            r["reserved_cny"] for r in rows if r["known_cost_cny"] is None
        )
        path = output / f"{session}.json"
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(
            json.dumps(
                {
                    "status": report["status"],
                    "report": str(path),
                    "calls": len(rows),
                    "known_cost_cny_peak_estimate": report["known_cost_cny_peak_estimate"],
                    "unreconciled_reservations_cny": report["unreconciled_reservations_cny"],
                    "error_type": report.get("error_type"),
                }
            )
        )
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
