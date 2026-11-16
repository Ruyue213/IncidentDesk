"""Real project files/transactions, deterministic model, no paid API requests."""

import asyncio
import json
import os
import signal
import socket
import sys
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from incident_desk.actions import ActionExecutor, sha256
from incident_desk.agent.diagnostics import DiagnosticSession
from incident_desk.api import create_app
from incident_desk.model_config import ModelConfigStore
from incident_desk.projects import ProjectStore
from incident_desk.schemas import RunInput
from incident_desk.settings import Settings
from incident_desk.storage import Repository
from incident_desk.worker.runtime import Worker


def context(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "server.conf").write_text("queue=8\n")
    settings = Settings(_env_file=None, api_key=None, local_dir=tmp_path / "state")
    profile = ProjectStore(settings.local_dir).save(
        {"id": "real", "name": "Real project", "root": str(root), "config_files": ["server.conf"]}
    )
    end = datetime.now(UTC)
    request = RunInput(
        mode="live",
        service_id=profile.id,
        project_version=profile.version,
        question="Check the service configuration and explain the result",
        start=end - timedelta(minutes=30),
        end=end,
    )
    return settings, profile, request


async def prepare(settings, request, params):
    repo = Repository(settings.app_db)
    await repo.init()
    run, _ = await repo.create_run(request.model_dump(mode="json"), "action")
    executor = ActionExecutor(settings, repo)
    operation = await repo.freeze_operation(
        run["id"],
        "actual-interrupt",
        {"request": params, "preview": executor.preview(request, params)},
        kind="service_action",
    )
    return repo, run, executor, operation


async def approve(repo, run, operation):
    await repo.decide(
        run["id"],
        operation["interrupt_id"],
        operation["id"],
        operation["draft_version"],
        operation["params_digest"],
        "approve",
    )


@pytest.mark.asyncio
async def test_approved_config_write_backup_and_replay_are_one_effect(tmp_path):
    settings, profile, request = context(tmp_path)
    params = {
        "action": "update_config",
        "path": "server.conf",
        "content": "queue=64\n",
        "expected_sha256": sha256(b"queue=8\n"),
        "reason": "Increase the bounded queue",
        "references": [],
    }
    repo, run, executor, operation = await prepare(settings, request, params)
    try:
        with pytest.raises(Exception, match="not been approved"):
            await executor.execute(request, run["id"], params)
        await approve(repo, run, operation)
        result = await executor.execute(request, run["id"], params)
        assert result["status"] == "SUCCEEDED"
        assert not result["result"]["recovery_verified"]
        assert (profile.root / "server.conf").read_text() == "queue=64\n"
        assert (
            settings.local_dir / "backups" / f"{operation['id']}.bak"
        ).read_bytes() == b"queue=8\n"
        assert (await executor.execute(request, run["id"], params))["status"] == "SUCCEEDED"
        events = await repo.events(run["id"])
        assert sum(event["kind"] == "action.finished" for event in events) == 1
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_stale_file_fails_without_overwriting_and_crash_claim_never_replays(tmp_path):
    settings, profile, request = context(tmp_path)
    params = {
        "action": "update_config",
        "path": "server.conf",
        "content": "queue=64\n",
        "expected_sha256": sha256(b"queue=8\n"),
        "reason": "Increase the bounded queue",
        "references": [],
    }
    repo, run, executor, operation = await prepare(settings, request, params)
    try:
        await approve(repo, run, operation)
        # Simulate a crash after durable claim, before the external write.
        await repo.begin_service_action(run["id"], params)
        result = await executor.execute(request, run["id"], params)
        assert result["status"] == "UNKNOWN"
        assert (profile.root / "server.conf").read_text() == "queue=8\n"
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_changed_target_and_project_permissions_fail_closed(tmp_path):
    settings, profile, request = context(tmp_path)
    params = {
        "action": "update_config",
        "path": "server.conf",
        "content": "queue=64\n",
        "expected_sha256": sha256(b"queue=8\n"),
        "reason": "Increase the bounded queue",
        "references": [],
    }
    repo, run, executor, operation = await prepare(settings, request, params)
    try:
        await approve(repo, run, operation)
        (profile.root / "server.conf").write_text("queue=32\n")
        result = await executor.execute(request, run["id"], params)
        assert result["status"] == "FAILED"
        assert (profile.root / "server.conf").read_text() == "queue=32\n"
        changed = profile.model_dump(mode="json") | {"config_files": []}
        ProjectStore(settings.local_dir).save(changed, expected_version=profile.version)
        with pytest.raises(Exception, match="permissions changed"):
            executor.preview(request, params)
    finally:
        await repo.close()


class LiveModel(BaseChatModel):
    model_name: str = "deterministic-live-contract"

    @property
    def _llm_type(self):
        return "live-contract"

    def _get_ls_params(self, **kwargs):
        return {"ls_provider": "incident-desk-scripted", "ls_model_name": self.model_name}

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, *args, **kwargs):
        raise AssertionError("async only")

    async def _agenerate(self, messages, **kwargs):
        results = [m for m in messages if isinstance(m, ToolMessage)]
        if not results:
            name, args = "read_project_file", {"path": "server.conf"}
        else:
            item = json.loads(results[0].content)["items"][0]
            name, args = (
                "Diagnosis",
                {
                    "summary": "The actual registered configuration was read.",
                    "hypotheses": ["Queue capacity needs comparison with observed load"],
                    "supporting_evidence": [
                        {"id": item["id"], "version": item["version"], "quote": "queue=8"}
                    ],
                    "contradicting_evidence": [],
                    "missing_information": ["No historical load data"],
                    "recommended_checks": ["Collect live service metrics"],
                    "needs_human": True,
                },
            )
        return ChatResult(
            generations=[
                ChatGeneration(
                    message=AIMessage(
                        content="",
                        tool_calls=[{"name": name, "args": args, "id": f"call-{len(results)}"}],
                    )
                )
            ]
        )


class LiveActionModel(LiveModel):
    async def _agenerate(self, messages, **kwargs):
        results = [message for message in messages if isinstance(message, ToolMessage)]
        if len(results) != 1:
            return await super()._agenerate(messages, **kwargs)
        item = json.loads(results[0].content)["items"][0]
        args = {
            "action": "update_config",
            "path": "server.conf",
            "content": "queue=64\n",
            "expected_sha256": item["provenance"]["file_sha256"],
            "reason": "Increase the queue according to the approved request",
            "references": [{"id": item["id"], "version": item["version"], "quote": "queue=8"}],
        }
        return ChatResult(
            generations=[
                ChatGeneration(
                    message=AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "propose_service_action",
                                "args": args,
                                "id": "config-proposal",
                            }
                        ],
                    )
                )
            ]
        )


class QuoteRepairModel(LiveModel):
    always_invalid: bool = False

    async def _agenerate(self, messages, **kwargs):
        result = await super()._agenerate(messages, **kwargs)
        call = result.generations[0].message.tool_calls[0]
        corrected = any(
            getattr(message, "name", None) == "incidentdesk_validator" for message in messages
        )
        if call["name"] == "Diagnosis" and (self.always_invalid or not corrected):
            call["args"]["supporting_evidence"][0]["quote"] = "queue=8 (invented metadata)"
        return result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "always_invalid,expected_status,count", [(False, "SUCCEEDED", 1), (True, "FAILED", 2)]
)
async def test_live_reference_feedback_repairs_or_stops_with_durable_limit(
    tmp_path, always_invalid, expected_status, count
):
    settings, _profile, request = context(tmp_path)
    repo = Repository(settings.app_db)
    await repo.init()
    try:
        run, _ = await repo.create_run(request.model_dump(mode="json"), "repair")
        await Worker(
            settings, model_factory=lambda *_: QuoteRepairModel(always_invalid=always_invalid)
        ).run(once=True)
        result = await repo.get_run(run["id"])
        assert result["status"] == expected_status, result["error"]
        assert result["report_repairs"] == count
        assert result["active_started_at"] is None
        assert (
            sum(
                event["kind"] == "report.validation_failed"
                for event in await repo.events(run["id"])
            )
            == count
        )
        if always_invalid:
            assert result["diagnosis"] is None
            assert result["error"]["type"] == "BudgetExceeded"
        else:
            assert result["diagnosis"]["supporting_evidence"][0]["quote"] == "queue=8"
        calls = result["tool_calls"]
        await Worker(settings, model_factory=lambda *_: QuoteRepairModel()).run(once=True)
        assert (await repo.get_run(run["id"]))["tool_calls"] == calls
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_crash_after_report_claim_preserves_count_and_recovers_graph(tmp_path, monkeypatch):
    settings, _profile, request = context(tmp_path)
    repo = Repository(settings.app_db)
    await repo.init()
    original = Repository.consume_report_repair

    async def crash_after_claim(self, *args, **kwargs):
        await original(self, *args, **kwargs)
        raise asyncio.CancelledError()

    try:
        run, _ = await repo.create_run(request.model_dump(mode="json"), "repair-crash")
        monkeypatch.setattr(Repository, "consume_report_repair", crash_after_claim)
        with pytest.raises(asyncio.CancelledError):
            await Worker(settings, model_factory=lambda *_: QuoteRepairModel()).run(once=True)
        assert (await repo.get_run(run["id"]))["report_repairs"] == 1
        monkeypatch.setattr(Repository, "consume_report_repair", original)
        await Worker(settings, model_factory=lambda *_: QuoteRepairModel()).run(once=True)
        result = await repo.get_run(run["id"])
        assert result["status"] == "SUCCEEDED", result["error"]
        assert result["report_repairs"] == 2
        assert result["active_started_at"] is None
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_recovered_report_repair_cannot_create_a_new_approval(tmp_path):
    settings, _profile, request = context(tmp_path)
    repo = Repository(settings.app_db)
    await repo.init()
    worker = Worker(settings, model_factory=lambda *_: LiveModel())
    try:
        run, _ = await repo.create_run(request.model_dump(mode="json"), "repair-no-action")
        await repo.consume_report_repair(run["id"], "Invalid reference")
        # Recovery must reject this phase before inspecting or freezing any new interrupt.
        with pytest.raises(Exception, match="cannot create another approval"):
            await worker._freeze_interrupt(run["id"], object())
        assert (await repo.get_run(run["id"]))["operation"] is None
    finally:
        await worker.repository.close()
        await repo.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["approve", "reject"])
async def test_actual_live_graph_interrupt_requires_human_and_keeps_frozen_config(
    tmp_path, decision
):
    settings, profile, request = context(tmp_path)
    repo = Repository(settings.app_db)
    await repo.init()
    try:
        run, _ = await repo.create_run(request.model_dump(mode="json"), "live-graph-action")
        await Worker(settings, model_factory=lambda *_: LiveActionModel()).run(once=True)
        waiting = await repo.get_run(run["id"])
        assert waiting["status"] == "WAITING_APPROVAL", waiting["error"]
        operation = waiting["operation"]
        assert operation["kind"] == "service_action"
        assert "+queue=64" in operation["params"]["preview"]["diff"]
        assert (profile.root / "server.conf").read_text() == "queue=8\n"
        await Worker(settings, model_factory=lambda *_: LiveActionModel()).run(once=True)
        assert (await repo.get_run(run["id"]))["action_receipt"] is None
        await repo.decide(
            run["id"],
            operation["interrupt_id"],
            operation["id"],
            operation["draft_version"],
            operation["params_digest"],
            decision,
        )
        await Worker(settings, model_factory=lambda *_: LiveActionModel()).run(once=True)
        finished = await repo.get_run(run["id"])
        assert finished["status"] == "SUCCEEDED", finished["error"]
        assert (profile.root / "server.conf").read_text() == (
            "queue=64\n" if decision == "approve" else "queue=8\n"
        )
        if decision == "approve":
            assert finished["action_receipt"]["status"] == "SUCCEEDED"
        else:
            assert finished["action_receipt"] is None
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_live_worker_uses_real_files_and_preserves_followup_context(tmp_path):
    settings, _profile, request = context(tmp_path)
    repo = Repository(settings.app_db)
    await repo.init()
    try:
        first, _ = await repo.create_run(request.model_dump(mode="json"), "first")
        worker = Worker(settings, model_factory=lambda *_: LiveModel())
        await worker.run(once=True)
        finished = await repo.get_run(first["id"])
        assert finished["status"] == "SUCCEEDED", finished["error"]
        assert "queue=8" in (await repo.list_evidence(first["id"]))[0]["content"]
        second, _ = await repo.create_run(
            request.model_copy(update={"parent_run_id": first["id"]}).model_dump(mode="json"),
            "second",
        )
        await Worker(settings, model_factory=lambda *_: LiveModel()).run(once=True)
        assert (await repo.get_run(second["id"]))["status"] == "SUCCEEDED"
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_local_security_profile_freezing_and_retry_after_settings_change(tmp_path):
    settings, profile, request = context(tmp_path)
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://127.0.0.1"
        ) as client:
            assert (
                await client.get("/api/projects", headers={"Host": "evil.example"})
            ).status_code == 403
            assert (
                await client.put(
                    "/api/model-settings", headers={"Origin": "https://evil.example"}, json={}
                )
            ).status_code == 403
            body = request.model_dump(mode="json")
            body["project_version"] = None
            first = await client.post("/api/runs", json=body, headers={"Idempotency-Key": "live"})
            assert first.status_code == 202, first.text
            ModelConfigStore(settings).save({"model": "deepseek-flash", "reasoning": "low"})
            retry = await client.post("/api/runs", json=body, headers={"Idempotency-Key": "live"})
            assert retry.json()["id"] == first.json()["id"]
            assert (
                retry.json()["request"]["model_profile_version"]
                == first.json()["request"]["model_profile_version"]
            )
            invalid = await client.put("/api/model-settings", json={"api_key": "secret with space"})
            assert "secret with space" not in invalid.text


@pytest.mark.asyncio
async def test_managed_service_start_restart_health_and_no_duplicate_effect(tmp_path):
    settings, profile, request = context(tmp_path)
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    program = profile.root / "service.py"
    # The fixture needs numeric loopback HTTP, not reverse-DNS server naming.
    program.write_text(
        "from http.server import HTTPServer, BaseHTTPRequestHandler\n"
        "from socketserver import TCPServer\n"
        "class LoopbackHTTPServer(HTTPServer):\n"
        " def server_bind(self):\n"
        "  TCPServer.server_bind(self)\n"
        "  self.server_name='127.0.0.1'; self.server_port=self.server_address[1]\n"
        "class H(BaseHTTPRequestHandler):\n"
        " def do_GET(self):\n"
        "  self.send_response(200); self.end_headers(); self.wfile.write(b'healthy')\n"
        f"LoopbackHTTPServer(('127.0.0.1', {port}), H).serve_forever()\n"
    )
    profile = ProjectStore(settings.local_dir).save(
        profile.model_dump(mode="json")
        | {
            "start_argv": [sys.executable, str(program)],
            "health_url": f"http://127.0.0.1:{port}/healthz",
        }
    )
    request = request.model_copy(update={"project_version": profile.version})
    repo = Repository(settings.app_db)
    await repo.init()
    executor = ActionExecutor(settings, repo)
    params = {
        "action": "restart_service",
        "reason": "Start the registered test service",
        "references": [],
    }
    previous_pid = None
    try:
        for index in range(2):
            run, _ = await repo.create_run(request.model_dump(mode="json"), f"restart-{index}")
            preview = executor.preview(request, params)
            operation = await repo.freeze_operation(
                run["id"],
                f"interrupt-{index}",
                {"request": params, "preview": preview},
                kind="service_action",
            )
            await approve(repo, run, operation)
            result = await executor.execute(request, run["id"], params)
            assert result["status"] == "SUCCEEDED", result
            assert result["result"]["recovery_verified"]
            session = DiagnosticSession(settings, run["id"], request)
            tool = {item.name: item for item in session.tools()}["get_service_evidence"]
            future = {
                "start": (request.end + timedelta(hours=1)).isoformat(),
                "end": (request.end + timedelta(hours=2)).isoformat(),
            }
            current = await tool.ainvoke({"service_id": profile.id, "source": "health", **future})
            assert current["items"][0]["provenance"]["http_status"] == 200
            assert not current["coverage"]["time_filter_applied"]
            refused = await tool.ainvoke({"service_id": profile.id, "source": "logs", **future})
            assert refused["status"] == "refused"
            assert refused["error"]["code"] == "time_window"
            assert result["result"]["pid"] != previous_pid
            previous_pid = result["result"]["pid"]
            assert (await executor.execute(request, run["id"], params))["result"][
                "pid"
            ] == previous_pid
    finally:
        state = executor.managed(profile)
        if state:
            os.kill(state["pid"], signal.SIGTERM)
            await asyncio.sleep(0.2)
        await repo.close()


class LongInvestigationModel(LiveModel):
    async def _agenerate(self, messages, **kwargs):
        results = [message for message in messages if isinstance(message, ToolMessage)]
        if len(results) >= 10:
            return await super()._agenerate(messages, **kwargs)
        return ChatResult(
            generations=[
                ChatGeneration(
                    message=AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "read_project_file",
                                "args": {"path": "server.conf"},
                                "id": f"long-{len(results)}",
                            }
                        ],
                    )
                )
            ]
        )


@pytest.mark.asyncio
async def test_middleware_steps_do_not_prematurely_exhaust_configured_call_budget(tmp_path):
    settings, profile, request = context(tmp_path)
    settings.model_call_limit = 16
    settings.tool_call_limit = 40
    repo = Repository(settings.app_db)
    await repo.init()
    try:
        run, _ = await repo.create_run(request.model_dump(mode="json"), "long-investigation")
        worker = Worker(settings, model_factory=lambda *_: LongInvestigationModel())
        await worker.run(once=True)
        finished = await repo.get_run(run["id"])
        assert finished["status"] == "SUCCEEDED", finished["error"]
        assert finished["tool_calls"] == 11
        assert finished["diagnosis"]["supporting_evidence"][0]["quote"] == "queue=8"
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_long_investigation_still_respects_model_budget(tmp_path):
    settings, profile, request = context(tmp_path)
    settings.model_call_limit = 3
    settings.tool_call_limit = 40
    repo = Repository(settings.app_db)
    await repo.init()
    try:
        run, _ = await repo.create_run(request.model_dump(mode="json"), "bounded-investigation")
        await Worker(settings, model_factory=lambda *_: LongInvestigationModel()).run(once=True)
        finished = await repo.get_run(run["id"])
        assert finished["status"] == "FAILED"
        assert finished["error"]["type"] == "BudgetExceeded"
        assert finished["tool_calls"] == 3
    finally:
        await repo.close()
