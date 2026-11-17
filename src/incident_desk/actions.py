"""Approved, journaled local effects. Never execute model-authored shell commands."""

import asyncio
import difflib
import hashlib
import json
import os
import signal
import subprocess
import tempfile
import time
from pathlib import Path
from subprocess import Popen

import httpx

from incident_desk.process_identity import (
    finish_cleanup,
    owned_child_identity,
    process_birth,
    process_exists,
    process_has_exited,
    terminate_owned_child,
)
from incident_desk.projects import ProjectStore, safe_project_path
from incident_desk.schemas import ServiceActionRequest
from incident_desk.storage.repository import Conflict
from incident_desk.tools.live_evidence import redact


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def executable_hash(path: Path) -> str:
    if path.stat().st_size > 100 * 1024 * 1024:
        raise ValueError("Registered executable exceeds the hashing limit")
    return sha256(path.read_bytes())


def read_config(path: Path) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as stream:
        if os.fstat(stream.fileno()).st_size > 16000:
            raise ValueError("Configuration exceeds the editable size limit")
        data = stream.read(16001)
    if len(data) > 16000:
        raise ValueError("Configuration exceeds the editable size limit")
    return data


def identity(pid: int) -> str | None:
    """Bind a PID to its start time and command, avoiding signals to reused PIDs."""
    result = subprocess.run(
        ["ps", "-p", str(pid), "-o", "lstart=", "-o", "command="],
        capture_output=True,
        text=True,
        timeout=3,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def atomic_write(path: Path, content: bytes, mode: int = 0o600):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=".incidentdesk-")
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(name).unlink(missing_ok=True)


class ActionExecutor:
    def __init__(self, settings, repository):
        self.settings = settings
        self.repository = repository
        self.projects = ProjectStore(settings.local_dir)

    def project(self, request):
        project = self.projects.get(request.service_id, request.project_version)
        current = self.projects.get(request.service_id)
        if current.version != project.version:
            raise Conflict("Project permissions changed; create a new diagnosis before changing it")
        return project

    def state_path(self, project):
        return self.settings.local_dir / "managed" / project.id / "process.json"

    def managed(self, project):
        path = self.state_path(project)
        state = json.loads(path.read_text()) if path.exists() else None
        if not state:
            return None
        if state.get("phase") == "STARTING":
            if process_has_exited(state["pid"], tuple(state["birth"])):
                return None
            birth = process_birth(state["pid"])
            if birth is not None and birth != tuple(state["birth"]):
                return None  # Original child is gone; never adopt or signal its successor.
            try:
                os.kill(state["pid"], 0)
            except ProcessLookupError:
                return None
            raise Conflict("Previous service startup is unresolved; inspect it before a new action")
        current = identity(state["pid"])
        if current is None:
            if process_exists(state["pid"]) is False:
                return None
            raise Conflict("Saved process cannot be verified; refusing a new action")
        if current != state["identity"]:
            raise Conflict("Saved process identity changed; refusing to signal this PID")
        if "birth" in state and process_birth(state["pid"]) != tuple(state["birth"]):
            raise Conflict("Saved process birth changed; refusing to signal this PID")
        return state

    def preview(self, run_request, params):
        proposal = ServiceActionRequest.model_validate(params)
        project = self.project(run_request)
        preview = {"target": project.id, "warnings": [], "project_version": project.version}
        if proposal.action == "restart_service":
            if not project.start_argv:
                raise ValueError("Register a start command before requesting a restart")
            command = list(project.start_argv)
            executable = Path(command[0])
            if not executable.is_absolute():
                executable = Path(project.root) / executable
            executable = executable.resolve(strict=True)
            if not executable.is_file() or not os.access(executable, os.X_OK):
                raise ValueError("Registered executable is unavailable")
            if executable.stat().st_size > 100 * 1024 * 1024:
                raise ValueError("Registered executable exceeds the hashing limit")
            command[0] = str(executable)
            before = self.managed(project)
            preview.update(
                title="重启已托管服务" if before else "启动已注册服务",
                command=command,
                executable_sha256=sha256(executable.read_bytes()),
                before_process=before,
            )
            preview["warnings"] = [
                "仅管理由 IncidentDesk 启动且身份匹配的进程；不会接管或终止其他进程。",
                "重启会短暂中断该服务；启动后单独核查健康端点。",
            ]
        else:
            if proposal.path not in project.config_files:
                raise ValueError("Configuration file is not explicitly registered for editing")
            path = safe_project_path(project, proposal.path)
            before = read_config(path)
            if len(before) > 16000:
                raise ValueError("Configuration exceeds the editable size limit")
            old = before.decode("utf-8")
            if redact(old) != old or redact(proposal.content) != proposal.content:
                raise ValueError(
                    "Sensitive configuration cannot be edited or previewed through the Agent"
                )
            if sha256(before) != proposal.expected_sha256:
                raise Conflict("Configuration changed since collection")
            preview.update(
                title="修改已注册配置",
                path=proposal.path,
                before_sha256=sha256(before),
                after_sha256=sha256(proposal.content.encode()),
                diff="".join(
                    difflib.unified_diff(
                        old.splitlines(keepends=True),
                        proposal.content.splitlines(keepends=True),
                        fromfile=proposal.path,
                        tofile=proposal.path,
                    )
                ),
            )
            preview["warnings"] = [
                "写入前再次比对原文件摘要并保存备份；本操作不会自动重启服务。",
                "写入成功不代表故障已恢复；请另行验证或发起需批准的重启。",
            ]
        return preview

    async def health(self, project):
        if not project.health_url:
            return {"status": "NOT_CONFIGURED"}
        try:
            async with httpx.AsyncClient(
                timeout=3, trust_env=False, follow_redirects=False
            ) as client:
                async with client.stream("GET", str(project.health_url)) as response:
                    return {
                        "status": "HEALTHY" if response.status_code == 200 else "UNHEALTHY",
                        "http_status": response.status_code,
                    }
        except httpx.HTTPError:
            return {"status": "UNREACHABLE"}

    async def start_service(self, project, command, operation_id):
        """Freeze ownership after readiness, allowing only this child's bootstrap exec."""
        from incident_desk.process_probe import owns_health_listener

        directory = self.state_path(project).parent
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        env = {
            key: os.environ[key]
            for key in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR")
            if key in os.environ
        }
        env["TZ"] = project.timezone
        log = (directory / "service.log").open("ab")
        os.fchmod(log.fileno(), 0o600)
        process, spawn_task = None, None
        birth = None
        try:
            # Retain a task as well as the eventual Popen handle. Cancellation
            # before the thread returns must not lose the newly created child.
            spawn_task = asyncio.create_task(
                asyncio.to_thread(
                    Popen,
                    command,
                    cwd=project.root,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=log,
                    start_new_session=True,
                )
            )
            process = await asyncio.shield(spawn_task)
            birth = await asyncio.to_thread(process_birth, process.pid)
            if birth is None:
                raise RuntimeError("Registered service exited at startup")
            initial = await asyncio.to_thread(owned_child_identity, process, birth, identity)
            atomic_write(
                self.state_path(project),
                json.dumps(
                    {
                        "phase": "STARTING",
                        "pid": process.pid,
                        "birth": birth,
                        "identity": initial,
                        "operation_id": operation_id,
                        "project_version": project.version,
                    }
                ).encode(),
            )
            deadline = time.monotonic() + 15
            stable_identity, stable_since = None, time.monotonic()
            while time.monotonic() < deadline:
                current = await asyncio.to_thread(owned_child_identity, process, birth, identity)
                if current is None:
                    raise RuntimeError("Registered child exited or its birth identity changed")
                if current != stable_identity:
                    stable_identity, stable_since = current, time.monotonic()
                after_health = await self.health(project)
                listener_verified = after_health[
                    "status"
                ] == "HEALTHY" and await owns_health_listener(process.pid, project.health_url)
                if (after_health["status"] == "HEALTHY" and listener_verified) or (
                    after_health["status"] == "NOT_CONFIGURED"
                    and time.monotonic() - stable_since >= 0.2
                ):
                    settled = await asyncio.to_thread(
                        owned_child_identity, process, birth, identity
                    )
                    if settled is not None and settled == current:
                        # Listener lookup and complete identity must describe the
                        # same child; exec after this point fails closed as usual.
                        atomic_write(
                            self.state_path(project),
                            json.dumps(
                                {
                                    "phase": "READY",
                                    "pid": process.pid,
                                    "identity": settled,
                                    "birth": birth,
                                    "operation_id": operation_id,
                                    "project_version": project.version,
                                }
                            ).encode(),
                        )
                        return process.pid, after_health, listener_verified
                await asyncio.sleep(0.1)
            raise RuntimeError("Registered service did not establish owned readiness")
        except BaseException:
            if process is None and spawn_task is not None:
                try:
                    process = await finish_cleanup(spawn_task)
                except Exception:
                    pass  # Popen itself failed: no live handle was produced.
            if process is not None:
                if birth is None:
                    birth = await finish_cleanup(
                        asyncio.create_task(asyncio.to_thread(process_birth, process.pid))
                    )
                await finish_cleanup(
                    asyncio.create_task(
                        asyncio.to_thread(terminate_owned_child, process, birth, identity)
                    )
                )
            raise
        finally:
            log.close()

    async def execute(self, run_request, run_id, params):
        operation, receipt, claimed = await self.repository.begin_service_action(run_id, params)
        if not claimed:
            if receipt["status"] == "STARTED":
                return await self.repository.finish_service_action(
                    run_id,
                    "UNKNOWN",
                    {
                        "message": (
                            "Previous executor stopped after claiming this action. "
                            "It was not repeated; "
                            "inspect process/file state before a new approved action."
                        )
                    },
                )
            return {key: value for key, value in receipt.items() if key != "result_json"}
        effect_started = False

        async def boundary():
            latest = await self.repository.get_run(run_id)
            if latest["cancel_requested"]:
                raise Conflict("Action cancelled before effect boundary")
            self.project(run_request)

        try:
            project = self.project(run_request)
            proposal = ServiceActionRequest.model_validate(params)
            preview = operation["params"]["preview"]
            current_preview = await asyncio.to_thread(self.preview, run_request, params)
            # PID, executable and config must match exactly what the person approved.
            if current_preview != preview:
                raise Conflict("Approved target changed; action was not executed")
            latest = await self.repository.get_run(run_id)
            if latest["cancel_requested"]:
                raise Conflict("Action cancelled before effect")
            before_health = await self.health(project)
            await boundary()
            if (
                proposal.action == "restart_service"
                and not preview["before_process"]
                and before_health["status"] == "HEALTHY"
            ):
                raise Conflict("An unowned service already answers at the registered health URL")
            if proposal.action == "update_config":
                path = safe_project_path(project, proposal.path)
                before = read_config(path)
                if sha256(before) != preview["before_sha256"]:
                    raise Conflict("File changed immediately before write")
                backup = self.settings.local_dir / "backups" / f"{operation['id']}.bak"
                atomic_write(backup, before)
                effect_started = True
                atomic_write(path, proposal.content.encode(), path.stat().st_mode & 0o777)
                result = {
                    "action": proposal.action,
                    "path": proposal.path,
                    "before_sha256": sha256(before),
                    "after_sha256": sha256(path.read_bytes()),
                    "backup_id": operation["id"],
                    "health_before": before_health,
                    "health_after": await self.health(project),
                    "verified": "FILE_HASH",
                    "recovery_verified": False,
                }
            else:
                before = preview["before_process"]
                if before:
                    if identity(before["pid"]) != before["identity"] or (
                        "birth" in before and process_birth(before["pid"]) != tuple(before["birth"])
                    ):
                        raise Conflict("Managed process identity changed before termination")
                    effect_started = True
                    os.kill(before["pid"], signal.SIGTERM)
                    deadline = time.monotonic() + 10
                    while True:
                        # Exit metadata can disappear before the kernel reports death.
                        # Once signalled, observe only: incomplete argv/birth is neither
                        # exit proof nor authority to signal again or launch a replacement.
                        if await asyncio.to_thread(
                            process_has_exited,
                            before["pid"],
                            tuple(before["birth"]) if "birth" in before else None,
                        ):
                            break
                        if time.monotonic() >= deadline:
                            raise RuntimeError(
                                "Managed process did not stop gracefully; "
                                "no replacement was started"
                            )
                        await asyncio.sleep(0.1)
                executable = Path(preview["command"][0])
                if (
                    await asyncio.to_thread(executable_hash, executable)
                    != preview["executable_sha256"]
                ):
                    raise Conflict("Registered executable changed before launch")
                if before and (await self.health(project))["status"] == "HEALTHY":
                    raise Conflict(
                        "A remaining process still answers; replacement was not launched"
                    )
                await boundary()
                effect_started = True
                pid, after_health, listener_verified = await self.start_service(
                    project, preview["command"], operation["id"]
                )
                result = {
                    "action": proposal.action,
                    "pid": pid,
                    "health_before": before_health,
                    "health_after": after_health,
                    "listener_verified": listener_verified,
                    "recovery_verified": after_health["status"] == "HEALTHY" and listener_verified,
                }
            return await self.repository.finish_service_action(run_id, "SUCCEEDED", result)
        except asyncio.CancelledError:
            await asyncio.shield(
                self.repository.finish_service_action(
                    run_id,
                    "UNKNOWN" if effect_started else "FAILED",
                    {"message": "Executor interrupted; action will not be automatically repeated"},
                )
            )
            raise
        except Exception as error:
            return await self.repository.finish_service_action(
                run_id,
                "UNKNOWN" if effect_started else "FAILED",
                {
                    "error_type": type(error).__name__,
                    "message": (
                        "Action did not complete verification. "
                        "Inspect actual process/file state; no automatic retry."
                    ),
                },
            )
