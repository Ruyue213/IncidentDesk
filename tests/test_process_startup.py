"""Real child exec, cancellation and failed-start cleanup contracts."""

import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from incident_desk import actions
from incident_desk.actions import ActionExecutor, identity
from incident_desk.process_identity import process_birth, process_has_exited, terminate_owned_child
from incident_desk.projects import ProjectStore
from incident_desk.schemas import RunInput
from incident_desk.settings import Settings
from incident_desk.storage import Conflict, Repository


async def setup(tmp_path, argv, health=None):
    config = Settings(_env_file=None, api_key=None, local_dir=tmp_path / "state")
    project = ProjectStore(config.local_dir).save(
        {
            "id": "startup",
            "name": "Synthetic startup",
            "root": str(tmp_path),
            "start_argv": argv,
            "health_url": health,
        }
    )
    repo = Repository(config.app_db)
    await repo.init()
    now = datetime.now(UTC)
    request = RunInput(
        mode="live",
        service_id=project.id,
        project_version=project.version,
        question="Start the registered synthetic service",
        start=now - timedelta(minutes=1),
        end=now,
    )
    return ActionExecutor(config, repo), project, request, repo


def test_kernel_exit_proof_includes_unreaped_zombie():
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        birth = process_birth(child.pid)
        assert birth is not None and not process_has_exited(child.pid, birth)
        child.terminate()
        deadline = time.monotonic() + 5
        while not process_has_exited(child.pid, birth) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert process_has_exited(child.pid, birth)
    finally:
        child.wait(timeout=5)


@pytest.mark.asyncio
async def test_bootstrap_exec_freezes_final_identity_and_rejects_later_argv_change(
    tmp_path, monkeypatch
):
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    target = tmp_path / "final_service.py"
    # Preserve real bind/listen/HTTP without environmental reverse-DNS work.
    target.write_text(
        "from http.server import HTTPServer, BaseHTTPRequestHandler\n"
        "from socketserver import TCPServer\n"
        "class LoopbackHTTPServer(HTTPServer):\n"
        " def server_bind(self):\n"
        "  TCPServer.server_bind(self)\n"
        "  self.server_name='127.0.0.1'; self.server_port=self.server_address[1]\n"
        "class H(BaseHTTPRequestHandler):\n"
        " def do_GET(self):\n"
        "  self.send_response(200); self.end_headers()\n"
        f"LoopbackHTTPServer(('127.0.0.1',{port}),H).serve_forever()\n"
    )
    gate = tmp_path / "exec-gate"
    launcher = tmp_path / "bootstrap.py"
    launcher.write_text(
        "import os,sys,time\nfrom pathlib import Path\n"
        f"while not Path({str(gate)!r}).exists(): time.sleep(.01)\n"
        f"os.execv(sys.executable,[sys.executable,{str(target)!r}])\n"
    )
    executor, project, _, repo = await setup(
        tmp_path, [sys.executable, str(launcher)], f"http://127.0.0.1:{port}/health"
    )
    observed = []
    actual_identity = actions.identity

    def observe(pid):
        value = actual_identity(pid)
        if value:
            observed.append(value)
            if str(launcher) in value:
                gate.touch()
        return value

    monkeypatch.setattr(actions, "identity", observe)
    pid, frozen = None, None
    try:
        pid, health, listener = await executor.start_service(
            project, list(project.start_argv), "test"
        )
        frozen = json.loads(executor.state_path(project).read_text())
        assert frozen["phase"] == "READY" and health["status"] == "HEALTHY" and listener
        assert len(set(observed)) >= 2, "The real exec must change argv without changing PID"
        assert str(target) in frozen["identity"] and executor.managed(project)["pid"] == pid
        executor.state_path(project).write_text(json.dumps(frozen | {"identity": "different argv"}))
        with pytest.raises(Conflict, match="identity changed"):
            executor.managed(project)
        assert process_birth(pid) == tuple(frozen["birth"])
    finally:
        if pid and frozen and process_birth(pid) == tuple(frozen["birth"]):
            os.kill(pid, signal.SIGTERM)
        await repo.close()


@pytest.mark.asyncio
async def test_cancel_before_spawn_returns_recovers_handle_and_never_replays(tmp_path, monkeypatch):
    script = tmp_path / "waiting.py"
    script.write_text("import time\ntime.sleep(60)\n")
    executor, project, request, repo = await setup(tmp_path, [sys.executable, str(script)])
    params = {"action": "restart_service", "reason": "Synthetic cancellation", "references": []}
    run, _ = await repo.create_run(request.model_dump(mode="json"), "cancel-spawn")
    operation = await repo.freeze_operation(
        run["id"],
        "interrupt",
        {"request": params, "preview": executor.preview(request, params)},
        kind="service_action",
    )
    await repo.decide(
        run["id"],
        "interrupt",
        operation["id"],
        operation["draft_version"],
        operation["params_digest"],
        "approve",
    )
    created, release, children = threading.Event(), threading.Event(), []
    actual_popen = subprocess.Popen

    def delayed_spawn(*args, **kwargs):
        child = actual_popen(*args, **kwargs)
        children.append(child)
        created.set()
        assert release.wait(10), "Test did not release the spawn thread"
        return child

    monkeypatch.setattr(actions, "Popen", delayed_spawn)
    task = asyncio.create_task(executor.execute(request, run["id"], params))
    try:
        assert await asyncio.to_thread(created.wait, 10)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()  # Cleanup must survive a second cancellation too.
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert children[0].poll() is not None
        stored = await repo.get_run(run["id"])
        assert stored["action_receipt"]["status"] == "UNKNOWN"
        assert (await executor.execute(request, run["id"], params))["status"] == "UNKNOWN"
        assert len(children) == 1
    finally:
        release.set()
        for child in children:
            if child.poll() is None:
                child.terminate()
            child.wait(timeout=5)
        await repo.close()


@pytest.mark.asyncio
async def test_starting_record_blocks_duplicate_and_failure_cleans_only_own_child(
    tmp_path, monkeypatch
):
    script = tmp_path / "waiting.py"
    script.write_text("import time\ntime.sleep(60)\n")
    executor, project, _, repo = await setup(tmp_path, [sys.executable, str(script)])
    other = await asyncio.to_thread(subprocess.Popen, [sys.executable, str(script)])
    children, actual_popen = [], subprocess.Popen

    def spawn(*args, **kwargs):
        child = actual_popen(*args, **kwargs)
        children.append(child)
        return child

    async def fail_health(_project):
        pending = json.loads(executor.state_path(project).read_text())
        assert pending["phase"] == "STARTING" and pending["operation_id"] == "pending"
        with pytest.raises(Conflict, match="startup is unresolved"):
            executor.managed(project)
        raise RuntimeError("Synthetic startup verification failure")

    monkeypatch.setattr(actions, "Popen", spawn)
    monkeypatch.setattr(executor, "health", fail_health)
    try:
        with pytest.raises(RuntimeError, match="Synthetic startup"):
            await executor.start_service(project, list(project.start_argv), "pending")
        assert len(children) == 1 and children[0].poll() is not None
        assert other.poll() is None
        assert executor.managed(project) is None
        wrong_birth = ("different", other.pid, 0)
        assert terminate_owned_child(other, wrong_birth, identity) == "identity_unverified"
        assert other.poll() is None
    finally:
        other.terminate()
        other.wait(timeout=5)
        await repo.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["exited", "unknown", "changed_before_signal", "reused_pid"])
async def test_restart_requires_exit_proof_after_one_authorized_signal(
    tmp_path, monkeypatch, outcome
):
    executor, project, request, repo = await setup(tmp_path, [sys.executable, "-V"])
    pid, birth, full_identity = 2147483000, ("synthetic", 2147483000, 1), "original argv"
    recorded = {"pid": pid, "birth": list(birth), "identity": full_identity, "phase": "READY"}
    path = executor.state_path(project)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(recorded))
    signals, observations, launches = [], [], []
    monkeypatch.setattr(actions, "identity", lambda _: full_identity if not signals else None)
    monkeypatch.setattr(actions, "process_birth", lambda _: birth if not signals else None)
    monkeypatch.setattr(actions.os, "kill", lambda target, sig: signals.append((target, sig)))

    def exit_proof(target, expected):
        assert signals == [(pid, signal.SIGTERM)] and (target, expected) == (pid, birth)
        observations.append(True)
        return outcome == "exited" and len(observations) >= 2

    async def launch(*args):
        assert outcome == "exited" and len(observations) >= 2
        launches.append(True)
        return 2147483001, {"status": "NOT_CONFIGURED"}, False

    monkeypatch.setattr(actions, "process_has_exited", exit_proof)
    monkeypatch.setattr(executor, "start_service", launch)
    # Advance only this module's deadline clock, leaving the event loop clock intact.
    ticks = iter([0, 11]) if outcome == "unknown" else iter(range(100))
    monkeypatch.setattr(actions, "time", SimpleNamespace(monotonic=lambda: next(ticks)))
    params = {"action": "restart_service", "reason": "Synthetic shutdown proof", "references": []}
    run, _ = await repo.create_run(request.model_dump(mode="json"), "stop-proof")
    operation = await repo.freeze_operation(
        run["id"],
        "stop-proof",
        {"request": params, "preview": executor.preview(request, params)},
        kind="service_action",
    )
    await repo.decide(
        run["id"],
        "stop-proof",
        operation["id"],
        operation["draft_version"],
        operation["params_digest"],
        "approve",
    )
    if outcome in {"changed_before_signal", "reused_pid"}:

        async def change_after_preview(_project):
            # The frozen preview already matched. Change only the last effect boundary.
            if outcome == "changed_before_signal":
                monkeypatch.setattr(actions, "identity", lambda _: "different argv")
            else:
                monkeypatch.setattr(actions, "process_birth", lambda _: (*birth[:-1], 2))
            return {"status": "NOT_CONFIGURED"}

        monkeypatch.setattr(executor, "health", change_after_preview)
    try:
        result = await executor.execute(request, run["id"], params)
        assert (
            result["status"]
            == {
                "exited": "SUCCEEDED",
                "unknown": "UNKNOWN",
                "changed_before_signal": "FAILED",
                "reused_pid": "FAILED",
            }[outcome]
        )
        assert signals == (
            [] if outcome in {"changed_before_signal", "reused_pid"} else [(pid, signal.SIGTERM)]
        )
        assert len(launches) == int(outcome == "exited")
        if outcome != "exited":
            assert json.loads(path.read_text()) == recorded
            assert (await executor.execute(request, run["id"], params))["status"] == result[
                "status"
            ]
            assert not launches
    finally:
        await repo.close()
