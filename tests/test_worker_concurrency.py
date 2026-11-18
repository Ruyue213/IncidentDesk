"""Bounded real graph execution with controlled async models and no provider calls."""

import asyncio
import json
import os
import signal
import sqlite3
import sys
import threading
from collections import Counter

import httpx
import openai
import pytest
from pydantic import PrivateAttr, ValidationError

from incident_desk.__main__ import request_for
from incident_desk.agent.budget import CallLedger
from incident_desk.agent.reliability_model import ReliabilityModel, make_reliability_model
from incident_desk.runtime_status import read_runtime_status
from incident_desk.settings import ROOT, Settings
from incident_desk.storage import Repository
from incident_desk.worker.runtime import Worker, WorkerLock


async def eventually(read, predicate):
    async with asyncio.timeout(10):
        while True:
            value = await read()
            if predicate(value):
                return value
            await asyncio.sleep(0.02)


class ControlledModel(ReliabilityModel):
    _control = PrivateAttr()
    _first = PrivateAttr(default=True)

    async def _agenerate(self, *args, **kwargs):
        if self._first:
            self._first = False
            state = self._control
            state["entered"].add(self._run_id)
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
            try:
                await state["gates"][self._run_id].wait()
                if self._run_id in state["fail"]:
                    raise RuntimeError("Synthetic isolated model error")
            except asyncio.CancelledError:
                state["cancelled"].add(self._run_id)
                raise
            finally:
                state["active"] -= 1
        return await super()._agenerate(*args, **kwargs)


def controller():
    state = {
        "entered": set(),
        "gates": {},
        "active": 0,
        "peak": 0,
        "fail": set(),
        "cancelled": set(),
        "closed": Counter(),
    }

    class Client:
        def __init__(self, run_id):
            self.run_id = run_id

        async def close(self):
            state["closed"][self.run_id] += 1

    def factory(configuration, run_id):
        model = ControlledModel()
        model._ledger = CallLedger(configuration.app_db, configuration)
        model._run_id, model._control = run_id, state
        object.__setattr__(model, "root_async_client", Client(run_id))
        return model

    return state, factory


async def queued(repo, state, identity, incident="dev-001", *, ready=False, ticket=False):
    request = request_for(incident)
    if ticket:
        request["question"] += " 请创建人工交接工单。"
    run, _ = await repo.create_run(request, identity)
    state["gates"][run["id"]] = asyncio.Event()
    if ready:
        state["gates"][run["id"]].set()
    return run["id"]


def test_worker_capacity_is_validated_and_environment_configurable(monkeypatch):
    monkeypatch.setenv("INCIDENT_DESK_WORKER_CONCURRENCY", "4")
    assert Settings(_env_file=None).worker_concurrency == 4
    for value in (0, 9):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, worker_concurrency=value)


@pytest.mark.asyncio
async def test_slow_models_overlap_capacity_is_bounded_and_checkpoints_do_not_mix(tmp_path):
    config = Settings(_env_file=None, api_key=None, local_dir=tmp_path)
    repo = Repository(config.app_db)
    await repo.init()
    state, factory = controller()
    ids = [await queued(repo, state, str(index), f"dev-{index + 1:03}") for index in range(3)]
    worker = Worker(config, model_factory=factory)
    task = asyncio.create_task(worker.run(once=True))
    try:
        await eventually(lambda: repo.workload_counts(), lambda x: x["claimed_count"] == 2)
        await eventually(lambda: asyncio.sleep(0, result=state["entered"]), lambda x: len(x) == 2)
        assert ids[2] not in state["entered"]
        status = await eventually(
            lambda: asyncio.to_thread(read_runtime_status, config),
            lambda x: x.get("active_count") == 2 and x.get("queued_count") == 1,
        )
        assert status["capacity"] == 2 and set(status["active_run_ids"]) == set(ids[:2])
        state["gates"][ids[0]].set()
        state["gates"][ids[2]].set()
        await eventually(lambda: repo.get_run(ids[2]), lambda x: x["status"] == "SUCCEEDED")
        assert (await repo.get_run(ids[1]))["status"] == "RUNNING"
        state["gates"][ids[1]].set()
        await asyncio.wait_for(task, 10)
        assert state["peak"] == 2 and state["closed"] == Counter({run_id: 1 for run_id in ids})
        references = []
        for run_id in ids:
            result = await repo.get_run(run_id)
            evidence = await repo.list_evidence(run_id)
            references.append({item["id"] for item in evidence})
            assert result["status"] == "SUCCEEDED"
            assert all(
                item["id"] in references[-1] for item in result["diagnosis"]["supporting_evidence"]
            )
        assert not (references[0] & references[1] or references[0] & references[2])
        with sqlite3.connect(config.graph_db) as db:
            assert {
                row[0] for row in db.execute("SELECT DISTINCT thread_id FROM checkpoints")
            } == set(ids)
        assert (await repo.workload_counts())["claimed_count"] == 0
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await repo.close()


@pytest.mark.asyncio
async def test_one_failure_and_one_cancellation_release_slots_without_stopping_other_runs(tmp_path):
    config = Settings(_env_file=None, api_key=None, local_dir=tmp_path)
    repo = Repository(config.app_db)
    await repo.init()
    state, factory = controller()
    failed = await queued(repo, state, "failed")
    cancelled = await queued(repo, state, "cancelled")
    waiting = await queued(repo, state, "next", ready=True)
    state["fail"].add(failed)
    worker = Worker(config, model_factory=factory)
    task = asyncio.create_task(worker.run(once=True))
    try:
        await eventually(lambda: asyncio.sleep(0, result=state["entered"]), lambda x: len(x) == 2)
        state["gates"][failed].set()
        await eventually(lambda: repo.get_run(waiting), lambda x: x["status"] == "SUCCEEDED")
        await repo.request_cancel(cancelled)
        await asyncio.wait_for(task, 10)
        assert (await repo.get_run(failed))["status"] == "FAILED"
        assert (await repo.get_run(cancelled))["status"] == "CANCELLED"
        assert cancelled in state["cancelled"]
        assert state["peak"] <= 2
        assert (await repo.workload_counts())["claimed_count"] == 0
        assert worker.sessions == worker.models_by_run == {} and worker.models == []
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await repo.close()


@pytest.mark.asyncio
async def test_waiting_for_approval_does_not_hold_capacity_and_shutdown_preserves_recovery(
    tmp_path,
):
    config = Settings(_env_file=None, api_key=None, local_dir=tmp_path, worker_concurrency=1)
    repo = Repository(config.app_db)
    await repo.init()
    state, factory = controller()
    human = await queued(repo, state, "human", ready=True, ticket=True)
    slow = await queued(repo, state, "slow")
    worker = Worker(config, model_factory=factory)
    task = asyncio.create_task(worker.run())
    try:
        await eventually(lambda: repo.get_run(human), lambda x: x["status"] == "WAITING_APPROVAL")
        await eventually(lambda: asyncio.sleep(0, result=state["entered"]), lambda x: slow in x)
        before = await repo.get_run(human)
        worker.request_stop()
        await asyncio.wait_for(task, 10)
        assert slow in state["cancelled"]
        assert (await repo.get_run(slow))["status"] == "RUNNING"
        assert (await repo.workload_counts())["claimed_count"] == 0
        assert (await repo.get_run(human))["operation"] == before["operation"]
        state["gates"][slow].set()
        await Worker(config, model_factory=factory).run(once=True)
        assert (await repo.get_run(slow))["status"] == "SUCCEEDED"
        assert (await repo.get_run(human))["operation"]["decision"] is None
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await repo.close()


@pytest.mark.asyncio
async def test_atomic_run_claim_and_lifetime_worker_lock_remain_enforced(tmp_path):
    config = Settings(_env_file=None, api_key=None, local_dir=tmp_path)
    repo = Repository(config.app_db)
    await repo.init()
    state, _ = controller()
    run_id = await queued(repo, state, "claim")
    try:
        claimed = await asyncio.gather(repo.claim_run(run_id, "one"), repo.claim_run(run_id, "two"))
        assert sum(claimed) == 1
        await repo.release_run_claim(run_id, "wrong-owner")
        assert (await repo.workload_counts())["claimed_count"] == 1
        with WorkerLock(tmp_path):
            with pytest.raises(RuntimeError, match="Another IncidentDesk worker"):
                await Worker(config).run(once=True)
        assert (await repo.workload_counts())["claimed_count"] == 1
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_shutdown_during_user_cancel_finishes_client_close_and_releases_claim(tmp_path):
    config = Settings(_env_file=None, api_key=None, local_dir=tmp_path)
    repo = Repository(config.app_db)
    await repo.init()
    state, base_factory = controller()
    run_id = await queued(repo, state, "double-cancel")
    closing, finish_close = asyncio.Event(), asyncio.Event()

    class SlowClose:
        async def close(self):
            closing.set()
            await finish_close.wait()
            state["closed"][run_id] += 1

    def factory(settings, identity):
        model = base_factory(settings, identity)
        object.__setattr__(model, "root_async_client", SlowClose())
        return model

    worker = Worker(config, model_factory=factory)
    task = asyncio.create_task(worker.run())
    try:
        await eventually(lambda: asyncio.sleep(0, result=state["entered"]), lambda x: run_id in x)
        await repo.request_cancel(run_id)
        await asyncio.wait_for(closing.wait(), 5)
        worker.request_stop()
        await asyncio.sleep(0.15)
        assert not task.done()
        finish_close.set()
        await asyncio.wait_for(task, 5)
        assert state["closed"][run_id] == 1
        assert (await repo.workload_counts())["claimed_count"] == 0
        assert (await repo.get_run(run_id))["status"] == "CANCELLED"
        assert worker.models_by_run == {} and worker.models == []
    finally:
        finish_close.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await repo.close()


@pytest.mark.asyncio
async def test_real_metered_mock_transport_wait_and_sqlite_contention_do_not_stop_heartbeat(
    tmp_path, monkeypatch
):
    from incident_desk.agent import models
    from incident_desk.observations import ObservationStore

    config = Settings(
        _env_file=None,
        api_key="synthetic-key",
        local_dir=tmp_path,
        base_url="https://deepseek.invalid",
    )
    repo = Repository(config.app_db)
    await repo.init()
    state, _ = controller()
    ids = [await queued(repo, state, f"sdk-{index}") for index in range(2)]
    real_make = models.make_model
    sampler = Counter()
    original_init = ObservationStore.__init__
    original_sample = ObservationStore.sample_projects

    def make_store(self, *args, **kwargs):
        sampler["instances"] += 1
        original_init(self, *args, **kwargs)

    async def sample_once(self, profiles):
        sampler["calls"] += 1
        return await original_sample(self, profiles)

    monkeypatch.setattr(ObservationStore, "sample_projects", sample_once)
    monkeypatch.setattr(ObservationStore, "__init__", make_store)

    def factory(settings, run_id):
        model = real_make(settings, run_id)
        # The factory runs in a thread. Retain the real adapter, metering, guard,
        # retries and timeout; replace only its transport with a local test peer.
        asyncio.run(model.root_async_client.close())
        model.root_client.close()

        async def respond(request):
            assert request.url.host == "deepseek.invalid"
            state["entered"].add(run_id)
            await state["gates"][run_id].wait()
            report = {
                "summary": "Synthetic transport test without diagnosis claims",
                "hypotheses": [],
                "supporting_evidence": [],
                "contradicting_evidence": [],
                "missing_information": ["No runtime evidence collected in this transport test"],
                "recommended_checks": ["Inspect actual runtime evidence"],
                "needs_human": True,
            }
            return httpx.Response(
                200,
                json={
                    "id": "synthetic-completion",
                    "object": "chat.completion",
                    "created": 1794800000,
                    "model": "synthetic-model",
                    "choices": [
                        {
                            "index": 0,
                            "finish_reason": "tool_calls",
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "synthetic-diagnosis",
                                        "type": "function",
                                        "function": {
                                            "name": "Diagnosis",
                                            "arguments": json.dumps(report),
                                        },
                                    }
                                ],
                            },
                        }
                    ],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 30, "total_tokens": 130},
                },
            )

        client = openai.AsyncOpenAI(
            api_key="synthetic-key",
            base_url="https://deepseek.invalid",
            max_retries=0,
            timeout=model.request_timeout,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
        )
        model.root_async_client, model.async_client = client, client.chat.completions
        return model

    monkeypatch.setattr(models, "make_model", factory)
    worker = Worker(config)
    task = asyncio.create_task(worker.run(once=True))
    try:
        await eventually(lambda: asyncio.sleep(0, result=state["entered"]), lambda x: x == set(ids))
        before = await eventually(
            lambda: asyncio.to_thread(read_runtime_status, config),
            lambda x: x.get("active_count") == 2,
        )
        async with repo._write():
            state["gates"][ids[0]].set()
            # The real adapter's finish transaction waits for this held writer.
            # The heartbeat must continue on the loop while that thread waits.
            await asyncio.sleep(0.7)
            current = read_runtime_status(config)
            assert current["at"] != before["at"] and current["active_count"] == 2
        state["gates"][ids[1]].set()
        await asyncio.wait_for(task, 10)
        assert sampler["instances"] == 1 and sampler["calls"] >= 1
        for run_id in ids:
            assert (await repo.get_run(run_id))["status"] == "SUCCEEDED"
        rows = CallLedger(config.app_db, config).rows()
        assert len(rows) == 2 and all(row["status"] == "SUCCEEDED" for row in rows)
    finally:
        for gate in state["gates"].values():
            gate.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await repo.close()


@pytest.mark.asyncio
async def test_sigterm_drains_real_worker_process_and_keeps_checkpoint_recoverable(tmp_path):
    config = Settings(_env_file=None, api_key=None, local_dir=tmp_path)
    repo = Repository(config.app_db)
    await repo.init()
    run, _ = await repo.create_run(request_for("dev-001"), "sigterm")
    marker = tmp_path / "model-entered"
    source = ROOT / "src"
    code = """import asyncio,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from incident_desk.agent.budget import CallLedger
from incident_desk.agent.reliability_model import ReliabilityModel
from incident_desk.settings import Settings
from incident_desk.worker.runtime import Worker
class Waiting(ReliabilityModel):
 async def _agenerate(self,*args,**kwargs):
  Path(sys.argv[3]).write_text('entered')
  await asyncio.Event().wait()
def factory(settings,run_id):
 model=Waiting();model._ledger=CallLedger(settings.app_db,settings);model._run_id=run_id
 return model
asyncio.run(Worker(Settings(_env_file=None,api_key=None,local_dir=Path(sys.argv[2])),
 model_factory=factory).run(handle_signals=True))
"""
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("DEEPSEEK_", "INCIDENT_DESK_", "LANGCHAIN_", "LANGSMITH_"))
    }
    environment.update(LANGSMITH_TRACING="false", LANGCHAIN_TRACING_V2="false")
    child = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        code,
        str(source),
        str(tmp_path),
        str(marker),
        env=environment,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        await eventually(lambda: asyncio.sleep(0, result=marker.exists()), bool)
        child.send_signal(signal.SIGTERM)
        output, error = await asyncio.wait_for(child.communicate(), 10)
        assert child.returncode == 0, (output, error)
        stored = await repo.get_run(run["id"])
        assert stored["status"] == "RUNNING" and stored["active_started_at"] is None
        assert (await repo.workload_counts())["claimed_count"] == 0
        await Worker(config, model_factory=make_reliability_model).run(once=True)
        assert (await repo.get_run(run["id"]))["status"] == "SUCCEEDED"
    finally:
        if child.returncode is None:
            child.kill()
            await child.wait()
        await repo.close()


@pytest.mark.asyncio
async def test_late_heartbeat_write_finishes_before_final_stopping_snapshot(tmp_path, monkeypatch):
    from incident_desk.worker import runtime

    started, release = threading.Event(), threading.Event()
    calls = []
    original_write = runtime.write_runtime_status

    def slow_first_write(directory, value):
        calls.append(value["shutting_down"])
        if len(calls) == 1:
            started.set()
            assert release.wait(5)
        original_write(directory, value)

    monkeypatch.setattr(runtime, "write_runtime_status", slow_first_write)
    config = Settings(_env_file=None, api_key=None, local_dir=tmp_path)
    worker = Worker(config, model_factory=lambda *_: None)
    task = asyncio.create_task(worker.run())
    try:
        assert await asyncio.to_thread(started.wait, 5)
        worker.request_stop()
        await asyncio.sleep(0.1)
        assert not task.done()
        release.set()
        await asyncio.wait_for(task, 5)
        status = read_runtime_status(config)
        assert status["status"] == "stopping" and status["active_count"] == 0
        assert calls[0] is False and calls[-1] is True
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
