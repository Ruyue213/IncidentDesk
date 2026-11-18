"""Approved runs cannot both restart a project based on one stale frozen preview."""

import asyncio
import socket
import subprocess
import sys
from datetime import UTC, datetime, timedelta

import pytest

from incident_desk import actions
from incident_desk.actions import ActionExecutor, identity, sha256
from incident_desk.process_identity import process_birth
from incident_desk.project_actions import project_action_lock
from incident_desk.projects import ProjectStore
from incident_desk.schemas import RunInput
from incident_desk.settings import Settings
from incident_desk.storage import Repository


async def approved(repo, executor, request, key, params):
    run, _ = await repo.create_run(request.model_dump(mode="json"), key)
    operation = await repo.freeze_operation(
        run["id"],
        key,
        {"request": params, "preview": executor.preview(request, params)},
        kind="service_action",
    )
    await repo.decide(
        run["id"],
        key,
        operation["id"],
        operation["draft_version"],
        operation["params_digest"],
        "approve",
    )
    return run["id"]


@pytest.mark.asyncio
async def test_two_approved_runs_revalidate_under_project_lock_and_spawn_only_once(
    tmp_path, monkeypatch
):
    root = tmp_path / "project"
    root.mkdir()
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    program = root / "service.py"
    program.write_text(
        "from http.server import HTTPServer,BaseHTTPRequestHandler\n"
        "from socketserver import TCPServer\n"
        "class LocalHTTPServer(HTTPServer):\n"
        " def server_bind(self):\n"
        "  TCPServer.server_bind(self)\n"
        "  self.server_name='127.0.0.1';self.server_port=self.server_address[1]\n"
        "class H(BaseHTTPRequestHandler):\n"
        " def do_GET(self): self.send_response(200);self.end_headers()\n"
        f"LocalHTTPServer(('127.0.0.1',{port}),H).serve_forever()\n"
    )
    config = Settings(_env_file=None, api_key=None, local_dir=tmp_path / "state")
    project = ProjectStore(config.local_dir).save(
        {
            "id": "service",
            "name": "Synthetic",
            "root": str(root),
            "start_argv": [sys.executable, str(program)],
            "health_url": f"http://127.0.0.1:{port}/health",
        }
    )
    repo = Repository(config.app_db)
    await repo.init()
    one, two = ActionExecutor(config, repo), ActionExecutor(config, repo)
    now = datetime.now(UTC)
    request = RunInput(
        mode="live",
        service_id=project.id,
        project_version=project.version,
        question="Start the registered synthetic service",
        start=now - timedelta(minutes=1),
        end=now,
    )
    params = {"action": "restart_service", "reason": "Synthetic approved restart", "references": []}
    first = await approved(repo, one, request, "one", params)
    second = await approved(repo, two, request, "two", params)
    actual_popen, children = subprocess.Popen, []

    def spawn(*args, **kwargs):
        child = actual_popen(*args, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(actions, "Popen", spawn)
    try:
        # The constructors and freeze happened before either intervention; both
        # approved the empty initial process state. The second must now fail closed.
        result = await asyncio.gather(
            one.execute(request, first, params), two.execute(request, second, params)
        )
        assert sorted(item["status"] for item in result) == ["FAILED", "SUCCEEDED"]
        assert len(children) == 1 and children[0].poll() is None
        frozen = one.managed(project)
        assert frozen["pid"] == children[0].pid
        assert frozen["identity"] == identity(children[0].pid)
        assert process_birth(children[0].pid) == tuple(frozen["birth"])
        assert sum(item["result"].get("recovery_verified", False) for item in result) == 1
    finally:
        for child in children:
            if child.poll() is None:
                child.terminate()
            await asyncio.to_thread(child.wait, timeout=5)
        await repo.close()


@pytest.mark.asyncio
async def test_project_lock_wait_is_cancellable_and_other_projects_progress(tmp_path):
    first_acquired, third_acquired = asyncio.Event(), asyncio.Event()

    async def waiting():
        async with project_action_lock(tmp_path, "one"):
            first_acquired.set()

    async def different_project():
        async with project_action_lock(tmp_path, "two"):
            third_acquired.set()

    async with project_action_lock(tmp_path, "one"):
        task = asyncio.create_task(waiting())
        await asyncio.wait_for(different_project(), 1)
        assert third_acquired.is_set() and not first_acquired.is_set()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    async with project_action_lock(tmp_path, "one"):
        first_acquired.set()
    assert first_acquired.is_set()


@pytest.mark.asyncio
async def test_cancellation_during_failure_receipt_retains_project_lock_until_commit(
    tmp_path, monkeypatch
):
    root = tmp_path / "project"
    root.mkdir()
    target = root / "server.conf"
    target.write_text("queue=8\n")
    config = Settings(_env_file=None, api_key=None, local_dir=tmp_path / "state")
    project = ProjectStore(config.local_dir).save(
        {"id": "one", "name": "Synthetic", "root": str(root), "config_files": ["server.conf"]}
    )
    repo = Repository(config.app_db)
    await repo.init()
    executor = ActionExecutor(config, repo)
    now = datetime.now(UTC)
    request = RunInput(
        mode="live",
        service_id=project.id,
        project_version=project.version,
        question="Update registered synthetic config",
        start=now - timedelta(minutes=1),
        end=now,
    )
    params = {
        "action": "update_config",
        "path": "server.conf",
        "content": "queue=32\n",
        "expected_sha256": sha256(b"queue=8\n"),
        "reason": "Synthetic approved change",
        "references": [],
    }
    run_id = await approved(repo, executor, request, "one", params)
    target.write_text("queue=16\n")
    writing, release_write, entered = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original_finish = repo.finish_service_action

    async def delayed_receipt(*args, **kwargs):
        writing.set()
        await release_write.wait()
        return await original_finish(*args, **kwargs)

    async def contend():
        async with project_action_lock(config.local_dir, project.id):
            entered.set()

    monkeypatch.setattr(repo, "finish_service_action", delayed_receipt)
    task = asyncio.create_task(executor.execute(request, run_id, params))
    contender = None
    try:
        await asyncio.wait_for(writing.wait(), 5)
        task.cancel()
        contender = asyncio.create_task(contend())
        await asyncio.sleep(0.1)
        assert not task.done() and not entered.is_set()
        release_write.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(contender, 5)
        assert entered.is_set()
        assert (await repo.get_run(run_id))["action_receipt"]["status"] == "FAILED"
        assert target.read_text() == "queue=16\n"
    finally:
        release_write.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, *([contender] if contender else []), return_exceptions=True)
        await repo.close()
