"""Actual worker/SDK/accounting report repair, using synthetic HTTP responses only."""

import json
import socket
from datetime import UTC, datetime, timedelta

import httpx
import openai
import pytest

from incident_desk.agent.budget import CallLedger
from incident_desk.agent.models import MeteredDeepSeek, make_model
from incident_desk.projects import ProjectProfile, ProjectStore
from incident_desk.schemas import RunInput
from incident_desk.settings import Settings
from incident_desk.storage import Repository
from incident_desk.worker.runtime import Worker


@pytest.mark.asyncio
async def test_worker_report_repair_uses_three_metered_requests_with_explicit_feedback(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "false")

    def forbid_network(*args, **kwargs):
        raise AssertionError("Report repair contracts must never connect to a real provider")

    monkeypatch.setattr(socket.socket, "connect", forbid_network)
    monkeypatch.setattr(socket, "create_connection", forbid_network)
    project = tmp_path / "synthetic-project"
    project.mkdir()
    (project / "README.md").write_text("Synthetic service README\nqueue=8\n")
    settings = Settings(
        _env_file=None,
        api_key="synthetic-provider-key",
        local_dir=tmp_path / "state",
        base_url="https://deepseek.invalid",
        reasoning="low",
        max_output_tokens=4096,
        max_request_bytes=262144,
    )
    profile = ProjectStore(settings.local_dir).save(
        ProjectProfile(
            id="metered-repair",
            name="Synthetic metered repair",
            root=project,
        )
    )
    end = datetime.now(UTC)
    request = RunInput(
        mode="live",
        service_id=profile.id,
        project_version=profile.version,
        question="只读检查README并给出有原文引用的诊断，不进行服务操作。",
        start=end - timedelta(minutes=30),
        end=end,
    )
    captured = []
    unused_clients = []
    factory_runs = []
    source = None

    def respond(incoming):
        nonlocal source
        payload = json.loads(incoming.content)
        captured.append(payload)
        number = len(captured)
        assert 1 <= number <= 3, "No hidden provider retries or extra repair requests are allowed"
        assert payload["thinking"] == {"type": "enabled"}
        assert payload["reasoning_effort"] == "low"
        assert payload["tool_choice"] == "auto"
        assistant_reasoning = [
            message.get("reasoning_content")
            for message in payload["messages"]
            if message["role"] == "assistant"
        ]
        assert assistant_reasoning == [f"synthetic reasoning {index}" for index in range(1, number)]
        if number == 1:
            name, arguments = "read_project_file", {"path": "README.md"}
        else:
            if number == 2:
                tool_result = json.loads(payload["messages"][-1]["content"])
                source = tool_result["items"][0]
                assert "queue=8" in source["content"]
            else:
                feedback = [
                    message
                    for message in payload["messages"]
                    if message["role"] == "user" and message.get("name") == "incidentdesk_validator"
                ]
                assert len(feedback) == 1
                assert "连续原文" in feedback[0]["content"]
                assert "不可信content样例" in feedback[0]["content"]
                assert "queue=8" in feedback[0]["content"]
                assert len(feedback[0]["content"]) <= 6000
            name, arguments = (
                "Diagnosis",
                {
                    "summary": "已读取项目说明，队列配置为8，尚缺运行指标。",
                    "hypotheses": [],
                    "supporting_evidence": [
                        {
                            "id": source["id"],
                            "version": source["version"],
                            "quote": "queue=8 (http_status=200)" if number == 2 else "queue=8",
                        }
                    ],
                    "contradicting_evidence": [],
                    "missing_information": ["缺少运行指标"],
                    "recommended_checks": ["只读核对服务当前指标"],
                    "needs_human": True,
                    "ticket_draft": None,
                },
            )
        return httpx.Response(
            200,
            json={
                "id": f"synthetic-completion-{number}",
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
                            "reasoning_content": f"synthetic reasoning {number}",
                            "tool_calls": [
                                {
                                    "id": f"synthetic-tool-{number}",
                                    "type": "function",
                                    "function": {"name": name, "arguments": json.dumps(arguments)},
                                }
                            ],
                        },
                    }
                ],
                "usage": {
                    "prompt_tokens": 100 * number,
                    "completion_tokens": 10 * number,
                    "total_tokens": 110 * number,
                    "prompt_cache_hit_tokens": 20 * number,
                    "prompt_cache_miss_tokens": 80 * number,
                },
            },
        )

    def factory(run_settings, run_id):
        factory_runs.append(run_id)
        model = make_model(run_settings, run_id)
        assert isinstance(model, MeteredDeepSeek)
        unused_clients.append(model.root_async_client)
        model.root_client.close()
        client = openai.AsyncOpenAI(
            api_key="synthetic-provider-key",
            base_url="https://deepseek.invalid",
            max_retries=0,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
        )
        model.root_async_client = client
        model.async_client = client.chat.completions
        return model

    repository = Repository(settings.app_db)
    await repository.init()
    try:
        run, _created = await repository.create_run(
            request.model_dump(mode="json"), "metered-repair"
        )
        await Worker(settings, model_factory=factory).run(once=True)
        result = await repository.get_run(run["id"])
        assert result["status"] == "SUCCEEDED", result["error"]
        assert result["report_repairs"] == 1
        assert result["diagnosis"]["supporting_evidence"][0]["quote"] == "queue=8"
        assert result["active_started_at"] is None
        assert result["active_seconds"] > 0
        assert result["operation"] is None and result["action_receipt"] is None
        assert factory_runs == [run["id"]]
        assert len(captured) == 3
        calls = CallLedger(settings.app_db, settings).rows()
        assert len(calls) == 3
        for number, call in enumerate(calls, start=1):
            assert call["run_id"] == run["id"]
            assert call["status"] == "SUCCEEDED"
            assert call["input_tokens"] == 100 * number
            assert call["output_tokens"] == 10 * number
            assert call["cache_read_tokens"] == 20 * number
            assert call["known_cost_cny"] == pytest.approx(0.0002408 * number)
            assert call["returned_model"] == "returned-flash-route"
        assert result["usage"]["model_calls"] == 3
        assert result["usage"]["known_cost_cny"] == pytest.approx(0.0014448)
        assert result["usage"]["unreconciled_reservations_cny"] == 0
        events = await repository.events(run["id"])
        failures = [event for event in events if event["kind"] == "report.validation_failed"]
        assert len(failures) == 1
        assert failures[0]["payload"]["attempt"] == 1
    finally:
        for client in unused_clients:
            await client.close()
        await repository.close()
