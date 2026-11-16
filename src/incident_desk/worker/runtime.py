"""The API persists commands; only this worker invokes or resumes a graph."""

import asyncio
import contextlib
import fcntl
import json
import os
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Command

from incident_desk.agent.budget import BudgetExceeded
from incident_desk.agent.diagnostics import DiagnosticSession, InvalidModelOutput
from incident_desk.observability import logger
from incident_desk.schemas import RunInput, ServiceActionRequest, TicketRequest
from incident_desk.settings import Settings
from incident_desk.storage.repository import Repository

TERMINAL = {"SUCCEEDED", "FAILED", "CANCELLED"}


class RunCancelled(RuntimeError):
    pass


class WorkerLock:
    def __init__(self, directory: Path):
        self.path = directory / "worker.lock"
        self.file = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("a+")
        try:
            fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.file.close()
            raise RuntimeError(
                "Another IncidentDesk worker already owns the local process lock"
            ) from None
        self.file.seek(0)
        self.file.truncate()
        self.file.write(str(os.getpid()))
        self.file.flush()
        return self

    def __exit__(self, *args):
        if self.file:
            fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
            self.file.close()


class RuntimeGuard(AgentMiddleware):
    def __init__(self, repository: Repository, run_id: str, settings: Settings):
        self.repository = repository
        self.run_id = run_id
        self.settings = settings

    async def boundary(self):
        run = await self.repository.get_run(self.run_id)
        if run["cancel_requested"] or run["status"] in TERMINAL:
            raise RunCancelled("run cancelled or already terminal")
        active = run["active_seconds"]
        if run.get("active_started_at"):
            active += max(
                0,
                (
                    datetime.now(UTC) - datetime.fromisoformat(run["active_started_at"])
                ).total_seconds(),
            )
        if active >= self.settings.active_seconds:
            raise BudgetExceeded("active execution time limit reached")
        if run["request"].get("mode") == "live":
            from incident_desk.projects import ProjectStore

            project = ProjectStore(self.settings.local_dir).get(run["request"]["service_id"])
            if project.version != run["request"].get("project_version"):
                from incident_desk.storage.repository import Conflict

                raise Conflict("Project permissions changed; start a new diagnosis")

    async def abefore_model(self, state, runtime):
        await self.boundary()
        run = await self.repository.get_run(self.run_id)
        if run["tool_calls"] >= self.settings.tool_call_limit:
            raise BudgetExceeded("tool call count limit reached before next model request")

    async def aafter_model(self, state, runtime):
        await self.boundary()
        # Includes virtual files, structured output, invalid names and attempted calls.
        latest = next((m for m in reversed(state["messages"]) if isinstance(m, AIMessage)), None)
        for _ in getattr(latest, "tool_calls", []):
            await self.repository.consume_tool_call(self.run_id, self.settings.tool_call_limit)

    async def awrap_tool_call(self, request, handler):
        await self.boundary()
        run = await self.repository.get_run(self.run_id)
        live = run["request"].get("mode") == "live"
        call = request.tool_call
        if live:
            await self.repository.record_event(
                self.run_id,
                "tool.started",
                {"tool": call["name"]},
                f"tool-start:{self.run_id}:{call['id']}",
            )
        try:
            result = await handler(request)
        except Exception:
            if live:
                await self.repository.record_event(
                    self.run_id,
                    "tool.finished",
                    {"tool": call["name"], "status": "error"},
                    f"tool-end:{self.run_id}:{call['id']}",
                )
            raise
        if live:
            await self.repository.record_event(
                self.run_id,
                "tool.finished",
                {"tool": call["name"], "status": getattr(result, "status", "success")},
                f"tool-end:{self.run_id}:{call['id']}",
            )
        return result


class Worker:
    def __init__(self, settings: Settings, *, model_factory=None, crash_at: str | None = None):
        self.settings = settings
        self.repository = Repository(settings.app_db)
        self.model_factory = model_factory
        self.crash_at = crash_at
        self.sessions = {}
        self.models = []
        self.models_by_run = {}

    async def release(self, run_id):
        self.sessions.pop(run_id, None)
        model = self.models_by_run.pop(run_id, None)
        if model is not None:
            client = getattr(model, "root_async_client", None)
            if client:
                await client.close()
            client = getattr(model, "root_client", None)
            if client:
                client.close()
            self.models = [item for item in self.models if item is not model]

    def crash(self, point: str):
        if self.crash_at == point:
            os._exit(86)  # deterministic subprocess-only fault injection

    async def _session(self, run: dict, saver):
        run_id = run["id"]
        if run_id in self.sessions:
            return self.sessions[run_id]

        async def save(item):
            await self.repository.save_evidence(run_id, item)

        async def ticket(params):
            receipt = await self.repository.create_ticket(run_id, params)
            self.crash("after_ticket_commit")
            return receipt

        request = RunInput.model_validate(run["request"])
        settings = self.settings
        if request.model_profile_version:
            from incident_desk.model_config import ModelConfigStore

            settings = ModelConfigStore(self.settings).settings_for_version(
                request.model_profile_version
            )

        async def action(params):
            from incident_desk.actions import ActionExecutor

            return await ActionExecutor(settings, self.repository).execute(request, run_id, params)

        session = DiagnosticSession(settings, run_id, request, save_snapshot=save)
        session.snapshots = {
            item["id"]: item for item in await self.repository.list_evidence(run_id)
        }
        if request.parent_run_id:
            previous = await self.repository.get_run(request.parent_run_id)
            if (
                previous["request"]["service_id"] != request.service_id
                or previous["request"]["mode"] != request.mode
            ):
                raise ValueError("Follow-up must stay within the original project and mode")
            session.previous = {
                "run_id": previous["id"],
                "question": previous["request"]["question"],
                "diagnosis": previous["diagnosis"],
                "action_receipt": previous["action_receipt"],
            }
        from incident_desk.agent.models import make_model

        model = (self.model_factory or make_model)(settings, run_id)
        self.models.append(model)
        self.models_by_run[run_id] = model
        graph = session.graph(
            model=model,
            checkpointer=saver,
            middleware=[RuntimeGuard(self.repository, run_id, settings)],
            ticket_writer=ticket if request.mode == "fixture" else None,
            action_writer=action if request.mode == "live" else None,
        )
        self.sessions[run_id] = session, graph
        return session, graph

    async def _settle_span(self, run, state):
        if run.get("active_started_at"):
            # A persisted interrupt/completion timestamp excludes subsequent human wait.
            end = (
                state.created_at
                if state.created_at and (state.interrupts or not state.next)
                else None
            )
            session = self.sessions.get(run["id"])
            maximum = (
                session[0].settings.active_seconds if session else self.settings.active_seconds
            )
            await self.repository.finish_active(run["id"], end, max_seconds=maximum)

    async def _freeze_interrupt(self, run_id, state):
        run = await self.repository.get_run(run_id)
        if run.get("report_repairs", 0):
            raise InvalidModelOutput("Report correction cannot create another approval operation")
        if len(state.interrupts) != 1:
            raise RuntimeError("exactly one approval interrupt is supported")
        interrupt = state.interrupts[0]
        requests = interrupt.value.get("action_requests", [])
        if len(requests) != 1 or requests[0]["name"] not in {
            "create_ticket",
            "propose_service_action",
        }:
            raise RuntimeError("unsupported or ambiguous human approval action")
        kind = "create_ticket"
        if requests[0]["name"] == "create_ticket":
            params = TicketRequest.model_validate(requests[0]["args"]).model_dump(mode="json")
        else:
            from incident_desk.actions import ActionExecutor

            kind = "service_action"
            run = await self.repository.get_run(run_id)
            proposal = ServiceActionRequest.model_validate(requests[0]["args"]).model_dump(
                mode="json"
            )
            if run["operation"]:
                params = run["operation"]["params"]
                if params.get("request") != proposal:
                    raise RuntimeError("Saved action differs from the graph interrupt")
            else:
                params = {
                    "request": proposal,
                    "preview": ActionExecutor(self.settings, self.repository).preview(
                        RunInput.model_validate(run["request"]), proposal
                    ),
                }
        self.crash("after_graph_interrupt_before_operation")
        operation = await self.repository.freeze_operation(run_id, interrupt.id, params, kind=kind)
        return interrupt, operation

    async def _verified_report(self, run_id, session, graph, state, config):
        """Explicit bounded feedback, preserving model/tool/time/cost accounting and checkpoints."""
        while True:
            try:
                return session.validate(state.values)
            except ValueError as error:
                if session.request.mode != "live":
                    raise
                from incident_desk.agent.report_repair import feedback_for_report

                feedback = feedback_for_report(state.values, session.snapshots)
                await self.repository.consume_report_repair(
                    run_id, "Diagnosis references failed validation", limit=2
                )
                current = await self.repository.get_run(run_id)
                remaining = session.settings.active_seconds - current["active_seconds"]
                if remaining <= 0:
                    raise BudgetExceeded(
                        "active execution time limit reached during report repair"
                    ) from error
                await self.repository.transition(run_id, "RUNNING")
                await self.repository.begin_active(run_id)
                try:
                    async with asyncio.timeout(remaining):
                        await graph.ainvoke(
                            {
                                "messages": [
                                    HumanMessage(content=feedback, name="incidentdesk_validator")
                                ],
                                "structured_response": None,
                            },
                            config,
                        )
                finally:
                    await self.repository.finish_active(
                        run_id, max_seconds=session.settings.active_seconds
                    )
                state = await graph.aget_state(config)
                if state.interrupts or state.next:
                    raise InvalidModelOutput(
                        "Report correction cannot request a service action"
                    ) from error

    async def process(self, run_id: str, saver) -> bool:
        """Reconcile current app + graph state; never blindly resend the original question."""
        run = await self.repository.get_run(run_id)
        if run["status"] in TERMINAL:
            await self.release(run_id)
            return False
        logger.info("worker.reconciling", extra={"run_id": run_id, "status": run["status"]})
        if run["cancel_requested"]:
            if run.get("active_started_at"):
                checkpoint = await saver.aget_tuple({"configurable": {"thread_id": run_id}})
                end = None
                if checkpoint:
                    interrupted = any(
                        channel == "__interrupt__" for _, channel, _ in checkpoint.pending_writes
                    )
                    completed = (
                        checkpoint.checkpoint["channel_values"].get("structured_response")
                        is not None
                    )
                    if interrupted or completed or run["status"] == "WAITING_APPROVAL":
                        end = checkpoint.checkpoint["ts"]
                try:
                    await self.repository.finish_active(run_id, end)
                except BudgetExceeded:
                    pass  # the duration was committed; cancellation remains the terminal outcome
            await self.repository.transition(run_id, "CANCELLED")
            await self.release(run_id)
            return True
        config = {"configurable": {"thread_id": run_id}, "recursion_limit": 50}
        try:
            session, graph = await self._session(run, saver)
            state = await graph.aget_state(config)
            await self._settle_span(run, state)
            if state.interrupts:
                interrupt, operation = await self._freeze_interrupt(run_id, state)
                decision = (operation.get("decision") or "pending").lower()
                if decision not in {"approve", "approved", "reject", "rejected"}:
                    await self.repository.transition(run_id, "WAITING_APPROVAL")
                    return run["status"] != "WAITING_APPROVAL"
                self.crash("after_decision_before_resume")
                kind = "approve" if decision in {"approve", "approved"} else "reject"
                update = Command(resume={interrupt.id: {"decisions": [{"type": kind}]}})
            elif state.created_at and not state.next:
                self.crash("after_graph_complete_before_app")
                report = await self._verified_report(run_id, session, graph, state, config)
                await self.repository.transition(
                    run_id, "SUCCEEDED", diagnosis=report.model_dump(mode="json")
                )
                return True
            else:
                update = None if state.created_at else session.input()
            await self.repository.transition(run_id, "RUNNING")
            run = await self.repository.get_run(run_id)
            remaining = session.settings.active_seconds - run["active_seconds"]
            if remaining <= 0:
                raise BudgetExceeded("active execution time limit reached")
            await self.repository.begin_active(run_id)
            try:
                async with asyncio.timeout(remaining):
                    await graph.ainvoke(update, config)
            finally:
                await self.repository.finish_active(
                    run_id, max_seconds=session.settings.active_seconds
                )
            state = await graph.aget_state(config)
            if state.interrupts:
                await self._freeze_interrupt(run_id, state)
                await self.repository.transition(run_id, "WAITING_APPROVAL")
            elif not state.next:
                self.crash("after_graph_complete_before_app")
                report = await self._verified_report(run_id, session, graph, state, config)
                await self.repository.transition(
                    run_id, "SUCCEEDED", diagnosis=report.model_dump(mode="json")
                )
            return True
        except RunCancelled:
            await self.repository.transition(run_id, "CANCELLED")
            return True
        except asyncio.CancelledError:
            raise
        except Exception as error:
            # Keep request/credential text out of the public error payload.
            latest = await self.repository.get_run(run_id)
            status = "CANCELLED" if latest["cancel_requested"] else "FAILED"
            logger.error(
                "worker.stopped",
                extra={"run_id": run_id, "status": status, "error_type": type(error).__name__},
            )
            messages = {
                "BudgetExceeded": "Call, tool, cost, or active execution budget reached",
                "TimeoutError": "Execution or model request exceeded its time limit",
                "AuthenticationError": "DeepSeek authentication failed; check backend .env",
                "APIConnectionError": "Cannot reach DeepSeek; no hidden retry was performed",
                "RateLimitError": "DeepSeek rate limit reached; no hidden retry was performed",
                "ValueError": "Invalid report, reference, or tool parameters",
                "Conflict": "Frozen operation, approval, or snapshot no longer matches",
            }
            await self.repository.transition(
                run_id,
                status,
                error={
                    "type": type(error).__name__,
                    "message": messages.get(
                        type(error).__name__, "Execution stopped; inspect local records"
                    ),
                },
            )
            return True

        finally:
            latest = await self.repository.get_run(run_id)
            if latest["status"] in TERMINAL:
                await self.release(run_id)

    async def run(self, *, once=False):
        with WorkerLock(self.settings.local_dir):
            await self.repository.init()

            async def pulse():
                while True:
                    heartbeat = self.settings.local_dir / "worker-heartbeat.json"
                    temporary = heartbeat.with_suffix(".tmp")
                    temporary.write_text(
                        json.dumps(
                            {
                                "pid": os.getpid(),
                                "at": datetime.now(UTC).isoformat(),
                                "mode": "offline" if self.model_factory else "live",
                            }
                        )
                    )
                    temporary.replace(heartbeat)
                    await asyncio.sleep(2)

            heartbeat_task = asyncio.create_task(pulse())

            async def collect_observations():
                from incident_desk.observations import ObservationStore
                from incident_desk.projects import ProjectStore

                store = None
                projects = None
                while True:
                    state = {"at": datetime.now(UTC).isoformat(), "status": "ready"}
                    try:
                        if store is None:
                            store = await asyncio.to_thread(
                                ObservationStore, self.settings.local_dir
                            )
                        if projects is None:
                            projects = await asyncio.to_thread(
                                ProjectStore, self.settings.local_dir
                            )
                        profiles = [projects.get(item["id"]) for item in projects.list_public()]
                        batches = await store.sample_projects(profiles)
                        counts = {
                            key: sum(item.get("status") == key for item in batches)
                            for key in ("complete", "partial", "unavailable", "skipped")
                        }
                        state["counts"] = counts
                        state["projects_sampled"] = counts["complete"] + counts["partial"]
                        state["status"] = (
                            "error"
                            if counts["unavailable"] and not state["projects_sampled"]
                            else "degraded"
                            if counts["partial"] or counts["unavailable"]
                            else "idle"
                            if not state["projects_sampled"]
                            else "ready"
                        )
                        state["projects"] = [
                            {"project_id": item.get("project_id"), "status": item.get("status")}
                            for item in batches
                        ]
                    except Exception as error:
                        state.update(status="error", error_type=type(error).__name__)
                        logger.warning(
                            "observations.sample_failed", extra={"error_type": type(error).__name__}
                        )
                    path = self.settings.local_dir / "sampler-status.json"
                    temporary = path.with_suffix(".tmp")
                    try:
                        temporary.write_text(json.dumps(state))
                        temporary.replace(path)
                    except OSError as error:
                        logger.warning(
                            "observations.status_write_failed",
                            extra={"error_type": type(error).__name__},
                        )
                    await asyncio.sleep(10)

            sampler_task = (
                asyncio.create_task(collect_observations()) if not self.model_factory else None
            )
            try:
                async with aiosqlite.connect(str(self.settings.graph_db)) as connection:
                    await connection.execute("PRAGMA journal_mode=WAL")
                    await connection.execute("PRAGMA busy_timeout=10000")
                    saver = AsyncSqliteSaver(
                        connection,
                        serde=JsonPlusSerializer(
                            allowed_msgpack_modules=[
                                ("incident_desk.schemas", name)
                                for name in (
                                    "Diagnosis",
                                    "Reference",
                                    "TicketDraft",
                                    "TicketRequest",
                                )
                            ]
                        ),
                    )
                    await saver.setup()
                    while True:
                        runs = await self.repository.pending_runs()
                        for run in runs:
                            await self.process(run["id"], saver)
                        if once:
                            return
                        await asyncio.sleep(0.25)
            finally:
                if sampler_task:
                    sampler_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await sampler_task
                heartbeat_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await heartbeat_task
                for model in self.models:
                    client = getattr(model, "root_async_client", None)
                    if client:
                        await client.close()
                    client = getattr(model, "root_client", None)
                    if client:
                        client.close()
                await self.repository.close()
                self.models.clear()
                self.sessions.clear()
                self.models_by_run.clear()
