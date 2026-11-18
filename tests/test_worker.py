"""Real process boundaries plus targeted deterministic budget/cancellation checks."""

import asyncio
import json
import runpy
import sqlite3
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import pytest_asyncio
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from incident_desk.__main__ import request_for
from incident_desk.agent.budget import CallLedger
from incident_desk.agent.reliability_model import make_reliability_model
from incident_desk.settings import ROOT, Settings
from incident_desk.storage import Repository
from incident_desk.worker.runtime import Worker, WorkerLock

HELPERS = runpy.run_path(str(ROOT / "scripts/fault_experiments.py"))
execute_worker = HELPERS["execute_worker"]
decide = HELPERS["decide"]


class AsyncProbeModel(BaseChatModel):
    model_name: str = "offline-probe-v1"

    @property
    def _llm_type(self):
        return "offline-probe"

    def _get_ls_params(self, **kwargs):
        return {"ls_provider": "incident-desk-scripted", "ls_model_name": self.model_name}

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, *args, **kwargs):
        raise AssertionError("synchronous invocation not expected")


@pytest_asyncio.fixture
async def context(tmp_path, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "false")
    settings = Settings(_env_file=None, api_key=None, local_dir=tmp_path)
    repo = Repository(settings.app_db)
    await repo.init()
    request = request_for("dev-001")
    request["question"] += " 请创建人工交接工单。"
    run, _ = await repo.create_run(request, "worker-test")
    try:
        yield settings, repo, run["id"]
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_second_worker_process_is_rejected_by_lifetime_lock(tmp_path):
    with WorkerLock(tmp_path):
        result = await execute_worker(tmp_path, expected=1)
    assert "Another IncidentDesk worker" in result["stderr"]
    assert not (tmp_path / "graph.db").exists()
    assert (await execute_worker(tmp_path))["exit_code"] == 0


@pytest.mark.asyncio
async def test_waiting_for_human_does_not_rerun_or_self_approve(context):
    settings, repo, run_id = context
    await execute_worker(settings.local_dir)
    first = await repo.get_run(run_id)
    assert first["status"] == "WAITING_APPROVAL"
    await execute_worker(settings.local_dir)
    second = await repo.get_run(run_id)
    assert second["operation"] == first["operation"]
    assert second["usage"] == first["usage"]
    assert second["ticket_id"] is None


@pytest.mark.asyncio
async def test_old_resume_command_cannot_authorize_current_pending_interrupt(context):
    settings, repo, run_id = context
    await execute_worker(settings.local_dir)
    first = await repo.get_run(run_id)
    with sqlite3.connect(settings.app_db) as db:
        db.execute(
            "INSERT INTO commands VALUES (?,?,?,?,?,?,?)",
            (
                "obsolete-command",
                run_id,
                "RESUME",
                json.dumps(
                    {
                        "interrupt_id": "previous-interrupt",
                        "operation_id": "previous-operation",
                        "decision": "approve",
                    }
                ),
                "obsolete-resume",
                "PENDING",
                datetime.now(UTC).isoformat(),
            ),
        )
    await execute_worker(settings.local_dir)
    current = await repo.get_run(run_id)
    assert current["status"] == "WAITING_APPROVAL"
    assert current["operation"]["decision"] is None
    assert current["ticket_id"] is None
    assert current["usage"] == first["usage"]


@pytest.mark.asyncio
async def test_mismatched_saved_interrupt_is_not_resumed_with_old_approval(context):
    settings, repo, run_id = context
    await execute_worker(settings.local_dir)
    await decide(repo, run_id)
    with sqlite3.connect(settings.app_db) as db:
        db.execute("UPDATE operations SET interrupt_id='obsolete' WHERE run_id=?", (run_id,))
    await execute_worker(settings.local_dir)
    current = await repo.get_run(run_id)
    assert current["status"] == "FAILED"
    assert current["error"]["type"] == "Conflict"
    assert current["ticket_id"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "settings_override,expected_calls",
    [({"model_call_limit": 1}, 1), ({"tool_call_limit": 1}, 1)],
)
async def test_worker_count_limits_stop_and_remain_stopped_after_restart(
    context, settings_override, expected_calls
):
    settings, repo, run_id = context
    await execute_worker(settings.local_dir, settings_overrides=settings_override)
    first = await repo.get_run(run_id)
    assert first["status"] == "FAILED"
    assert first["error"]["type"] == "BudgetExceeded"
    assert first["usage"]["model_calls"] == expected_calls
    assert first["tool_calls"] == 1
    assert first["ticket_id"] is None
    await execute_worker(settings.local_dir, settings_overrides=settings_override)
    assert (await repo.get_run(run_id))["usage"] == first["usage"]


@pytest.mark.asyncio
async def test_existing_eight_call_reservations_are_not_reset_by_worker(context):
    settings, repo, run_id = context
    ledger = CallLedger(settings.app_db, settings)
    for _ in range(8):
        ledger.reserve(run_id, "reliability-fake", "scripted", {"content": "prior request"})
    await execute_worker(settings.local_dir)
    current = await repo.get_run(run_id)
    assert current["status"] == "FAILED"
    assert current["error"]["type"] == "BudgetExceeded"
    assert current["usage"]["model_calls"] == 8
    assert current["usage"]["unknown_usage_calls"] == 8
    assert current["usage"]["unreconciled_reservations_cny"] > 0
    assert current["ticket_id"] is None


@pytest.mark.asyncio
async def test_persisted_active_budget_is_not_reset_by_worker(context):
    settings, repo, run_id = context
    await repo.add_active_seconds(run_id, 180)
    await execute_worker(settings.local_dir)
    current = await repo.get_run(run_id)
    assert current["status"] == "FAILED"
    assert current["error"]["type"] == "BudgetExceeded"
    assert current["active_seconds"] == 180
    assert current["usage"]["model_calls"] == 0


@pytest.mark.asyncio
async def test_human_wait_older_than_180_seconds_is_excluded_on_resume(context):
    settings, repo, run_id = context
    await execute_worker(settings.local_dir)
    old = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    with sqlite3.connect(settings.app_db) as db:
        db.execute("UPDATE operations SET created_at=? WHERE run_id=?", (old, run_id))
        db.execute("UPDATE runs SET updated_at=? WHERE id=?", (old, run_id))
    await decide(repo, run_id)
    await execute_worker(settings.local_dir)
    current = await repo.get_run(run_id)
    assert current["status"] == "SUCCEEDED"
    assert current["active_seconds"] < 180
    assert current["ticket_id"]


@pytest.mark.asyncio
async def test_crashed_active_span_ends_at_checkpoint_before_long_human_wait(context):
    settings, repo, run_id = context
    end = datetime(2025, 1, 1, tzinfo=UTC)
    start = end - timedelta(seconds=3)
    with sqlite3.connect(settings.app_db) as db:
        db.execute("UPDATE runs SET active_started_at=? WHERE id=?", (start.isoformat(), run_id))
    worker = Worker(settings)
    try:
        await worker._settle_span(
            await repo.get_run(run_id),
            SimpleNamespace(created_at=end.isoformat(), interrupts=("approval",), next=("tools",)),
        )
        current = await repo.get_run(run_id)
        assert current["active_seconds"] == 3
        assert current["active_started_at"] is None
    finally:
        await worker.repository.close()


@pytest.mark.asyncio
async def test_execution_timeout_reaches_an_explainable_terminal_state(context):
    settings, repo, run_id = context

    class WaitingModel(AsyncProbeModel):
        async def _agenerate(self, *args, **kwargs):
            await asyncio.Event().wait()

    settings = settings.model_copy(update={"active_seconds": 0.05})
    await Worker(settings, model_factory=lambda *_args: WaitingModel()).run(once=True)
    current = await repo.get_run(run_id)
    assert current["status"] == "FAILED"
    assert current["error"]["type"] in {"TimeoutError", "BudgetExceeded"}
    assert current["active_started_at"] is None
    assert current["ticket_id"] is None


@pytest.mark.asyncio
async def test_cancel_during_model_wait_rejects_late_model_result(context):
    settings, repo, run_id = context
    entered = asyncio.Event()
    release = asyncio.Event()

    class BlockingModel(AsyncProbeModel):
        async def _agenerate(self, *args, **kwargs):
            entered.set()
            await release.wait()
            return ChatResult(
                generations=[ChatGeneration(message=AIMessage(content="late result"))]
            )

    task = asyncio.create_task(
        Worker(settings, model_factory=lambda *_args: BlockingModel()).run(once=True)
    )
    await asyncio.wait_for(entered.wait(), timeout=3)
    await repo.request_cancel(run_id)
    release.set()
    await asyncio.wait_for(task, timeout=3)
    current = await repo.get_run(run_id)
    assert current["status"] == "CANCELLED"
    assert current["diagnosis"] is None
    assert current["ticket_id"] is None


@pytest.mark.asyncio
async def test_graph_completion_cannot_overwrite_newer_cancellation(context):
    settings, repo, run_id = context
    # No ticket request, so the graph can complete without human approval.
    with sqlite3.connect(settings.app_db) as db:
        request = request_for("dev-001")
        db.execute("UPDATE runs SET request_json=? WHERE id=?", (json.dumps(request), run_id))
    await execute_worker(settings.local_dir, "after_graph_complete_before_app", expected=86)
    await repo.request_cancel(run_id)
    await execute_worker(settings.local_dir)
    final = await repo.get_run(run_id)
    assert final["status"] == "CANCELLED"
    assert final["diagnosis"] is None
    await execute_worker(settings.local_dir)
    assert await repo.get_run(run_id) == final


@pytest.mark.asyncio
async def test_terminal_runs_release_clients_and_session_before_worker_shutdown(context):
    settings, repo, first_id = context
    with sqlite3.connect(settings.app_db) as db:
        db.execute(
            "UPDATE runs SET request_json=? WHERE id=?",
            (json.dumps(request_for("dev-001")), first_id),
        )
    second, _created = await repo.create_run(request_for("dev-002"), "second-readonly")
    closed_async = []
    closed_sync = []

    class AsyncClient:
        def __init__(self, run_id):
            self.run_id = run_id

        async def close(self):
            closed_async.append(self.run_id)

    class SyncClient:
        def __init__(self, run_id):
            self.run_id = run_id

        def close(self):
            closed_sync.append(self.run_id)

    def factory(configuration, run_id):
        model = make_reliability_model(configuration, run_id)
        object.__setattr__(model, "root_async_client", AsyncClient(run_id))
        object.__setattr__(model, "root_client", SyncClient(run_id))
        return model

    worker = Worker(settings, model_factory=factory)
    original_release = worker.release
    released = []

    async def release(run_id):
        await original_release(run_id)
        assert run_id not in worker.sessions
        assert run_id not in worker.models_by_run
        assert closed_async.count(run_id) == closed_sync.count(run_id) == 1
        released.append(run_id)

    worker.release = release
    await worker.run(once=True)
    assert sorted(released) == sorted([first_id, second["id"]])
    assert worker.sessions == worker.models_by_run == {}
    assert worker.models == []


@pytest.mark.asyncio
async def test_cancel_after_long_human_wait_settles_span_at_persisted_checkpoint(context):
    settings, repo, run_id = context
    await execute_worker(settings.local_dir)
    before = await repo.get_run(run_id)
    end = datetime.now(UTC) - timedelta(hours=1)
    start = end - timedelta(seconds=2)
    serializer = JsonPlusSerializer()
    # Freeze the checkpoint clock rather than sleeping: the saved interruption is
    # one hour old, while its actual active execution span lasted two seconds.
    with sqlite3.connect(settings.graph_db) as db:
        checkpoint_id, kind, blob = db.execute(
            "SELECT checkpoint_id,type,checkpoint FROM checkpoints WHERE thread_id=? "
            "ORDER BY checkpoint_id DESC LIMIT 1",
            (run_id,),
        ).fetchone()
        checkpoint = serializer.loads_typed((kind, blob))
        checkpoint["ts"] = end.isoformat()
        kind, blob = serializer.dumps_typed(checkpoint)
        db.execute(
            "UPDATE checkpoints SET type=?,checkpoint=? WHERE thread_id=? AND checkpoint_id=?",
            (kind, blob, run_id, checkpoint_id),
        )
    with sqlite3.connect(settings.app_db) as db:
        db.execute("UPDATE runs SET active_started_at=? WHERE id=?", (start.isoformat(), run_id))
    await repo.request_cancel(run_id)
    await execute_worker(settings.local_dir)
    after = await repo.get_run(run_id)
    assert after["status"] == "CANCELLED"
    assert after["active_started_at"] is None
    assert after["active_seconds"] == pytest.approx(before["active_seconds"] + 2)
    assert after["usage"] == before["usage"]
    assert after["ticket_id"] is None


@pytest.mark.asyncio
async def test_cancel_without_checkpoint_settles_unknown_overbudget_span_conservatively(context):
    settings, repo, run_id = context
    start = datetime.now(UTC) - timedelta(seconds=200)
    with sqlite3.connect(settings.app_db) as db:
        db.execute("UPDATE runs SET active_started_at=? WHERE id=?", (start.isoformat(), run_id))
    await repo.request_cancel(run_id)
    await execute_worker(settings.local_dir)
    current = await repo.get_run(run_id)
    assert current["status"] == "CANCELLED"
    assert current["active_started_at"] is None
    assert current["active_seconds"] >= 200
    assert current["usage"]["model_calls"] == 0


@pytest.mark.asyncio
async def test_null_structured_response_does_not_mark_checkpoint_completed_on_cancel(context):
    settings, repo, run_id = context
    start = datetime.now(UTC) - timedelta(seconds=200)
    checkpoint = empty_checkpoint()
    checkpoint["ts"] = (start + timedelta(seconds=3)).isoformat()
    checkpoint["channel_values"] = {"structured_response": None}
    async with AsyncSqliteSaver.from_conn_string(str(settings.graph_db)) as saver:
        await saver.aput(
            {"configurable": {"thread_id": run_id, "checkpoint_ns": ""}},
            checkpoint,
            {"source": "loop", "step": 0, "parents": {}},
            {},
        )
    await repo.transition(run_id, "RUNNING")
    with sqlite3.connect(settings.app_db) as db:
        db.execute("UPDATE runs SET active_started_at=? WHERE id=?", (start.isoformat(), run_id))
    await repo.request_cancel(run_id)
    await execute_worker(settings.local_dir)
    current = await repo.get_run(run_id)
    assert current["status"] == "CANCELLED"
    assert current["active_started_at"] is None
    assert current["active_seconds"] >= 200
    assert current["usage"]["model_calls"] == 0


@pytest.mark.asyncio
async def test_experiment_oracle_counts_a_null_approval_as_unauthorized(context):
    settings, _repo, run_id = context
    stamp = datetime.now(UTC).isoformat()
    # Corrupt fixture bypasses the repository to verify that the experiment's
    # SQL observer would catch a write without an approval, including SQL NULL.
    with sqlite3.connect(settings.app_db) as db:
        db.execute(
            "INSERT INTO operations "
            "(id,run_id,interrupt_id,draft_version,params_digest,params_json,status,created_at) "
            "VALUES ('unapproved-operation',?,'interrupt',1,'digest','{}','PENDING',?)",
            (run_id, stamp),
        )
        db.execute(
            "INSERT INTO tickets (id,run_id,operation_id,params_json,created_at) "
            "VALUES ('illicit-ticket',?,'unapproved-operation','{}',?)",
            (run_id, stamp),
        )
        db.execute(
            "INSERT INTO tool_receipts (operation_id,run_id,ticket_id,params_digest,created_at) "
            "VALUES ('unapproved-operation',?,'illicit-ticket','digest',?)",
            (run_id, stamp),
        )
    counts = HELPERS["database_counts"](settings.app_db, [run_id])
    assert counts["unapproved_effects"] == 1
