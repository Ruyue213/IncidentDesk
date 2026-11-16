"""Persistent request/command API. Model keys and graph objects are never exposed."""

import asyncio
import json
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import Field

from incident_desk.api.contracts import EventPage, ResumeView, RunPage, RunView
from incident_desk.api.knowledge import create_knowledge_router
from incident_desk.api.model_settings import create_model_settings_router
from incident_desk.api.observations import create_observations_router
from incident_desk.api.projects import create_projects_router
from incident_desk.knowledge.project_store import ProjectKnowledgeStore
from incident_desk.model_config import ModelConfigStore
from incident_desk.projects import ProjectStore
from incident_desk.runtime_paths import RESOURCE_ROOT
from incident_desk.schemas import RunInput, StrictModel
from incident_desk.settings import Settings
from incident_desk.storage import Conflict, NotFound, Repository
from incident_desk.storage.repository import TERMINAL


class ResumeInput(StrictModel):
    interrupt_id: str = Field(min_length=1, max_length=200)
    operation_id: str = Field(min_length=1, max_length=200)
    draft_version: int
    params_digest: str = Field(min_length=1, max_length=200)
    decision: Literal["approve", "reject"]


def create_app(settings: Settings, repository: Repository | None = None) -> FastAPI:
    repo = repository or Repository(settings.app_db)
    models = ModelConfigStore(settings)
    projects = ProjectStore(settings.local_dir)
    knowledge = ProjectKnowledgeStore(settings.local_dir)

    @asynccontextmanager
    async def lifespan(_app):
        await repo.init()
        try:
            yield
        finally:
            await repo.close()

    app = FastAPI(title="IncidentDesk", version="0.3.0b1", lifespan=lifespan)
    app.state.repository = repo
    app.include_router(create_model_settings_router(settings, models))
    app.include_router(create_projects_router(settings, projects))
    app.include_router(create_knowledge_router(settings, projects, knowledge))
    app.include_router(create_observations_router(settings, projects))

    @app.middleware("http")
    async def local_boundary(request: Request, call_next):
        if request.url.hostname not in {"127.0.0.1", "localhost", "::1"}:
            return JSONResponse(
                status_code=403, content={"detail": "Only loopback hosts are allowed"}
            )
        origin = request.headers.get("origin")
        if origin and origin != f"{request.url.scheme}://{request.url.netloc}":
            return JSONResponse(
                status_code=403, content={"detail": "Cross-origin access is not allowed"}
            )
        if request.headers.get("sec-fetch-site") == "cross-site":
            return JSONResponse(
                status_code=403, content={"detail": "Cross-site access is not allowed"}
            )
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            content_type = request.headers.get("content-type", "").split(";")[0]
            if (
                request.headers.get("content-length", "0") != "0"
                and content_type != "application/json"
            ):
                return JSONResponse(
                    status_code=415, content={"detail": "Writes require application/json"}
                )
        return await call_next(request)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_request, error):
        # Pydantic's default `input`/`ctx` may contain a submitted API key.
        return JSONResponse(
            status_code=422,
            content={
                "detail": [
                    {"type": item["type"], "loc": item["loc"], "msg": "Invalid field value"}
                    for item in error.errors()
                ]
            },
        )

    def worker_state():
        path = settings.local_dir / "worker-heartbeat.json"
        try:
            state = json.loads(path.read_text())
            age = max(0, (datetime.now(UTC) - datetime.fromisoformat(state["at"])).total_seconds())
            os.kill(state["pid"], 0)
            return {**state, "status": "ready" if age < 15 else "stale", "age_seconds": age}
        except (OSError, ValueError, KeyError):
            return {"status": "unavailable"}

    @app.get("/api/readiness")
    async def readiness():
        public = models.public()
        try:
            sampler = json.loads((settings.local_dir / "sampler-status.json").read_text())
            age = max(
                0, (datetime.now(UTC) - datetime.fromisoformat(sampler["at"])).total_seconds()
            )
            sampler["age_seconds"] = age
            if age > 60 or worker_state()["status"] != "ready":
                sampler["status"] = "stale"
        except (OSError, ValueError, KeyError):
            sampler = {"status": "unavailable"}
        return {
            "api": "ready",
            "worker": worker_state(),
            "model": {
                "status": "configured"
                if (public.get("profile") or {}).get("has_credential")
                else "unconfigured",
                "active_version": public.get("active_version"),
                "note": "Configuration is not an online connection test",
            },
            "projects": len(projects.list_public()),
            "sampler": sampler,
        }

    @app.exception_handler(NotFound)
    async def not_found(_request, error):
        return JSONResponse(status_code=404, content={"detail": str(error)})

    @app.exception_handler(Conflict)
    async def conflict(_request, error):
        return JSONResponse(status_code=409, content={"detail": str(error)})

    @app.get("/health")
    async def health():
        try:
            await repo.ping()
        except Exception as error:
            raise HTTPException(503, "local application database unavailable") from error
        return JSONResponse(
            content={"status": "ok", "database": "ok", "worker": worker_state()},
            headers={"X-IncidentDesk-PID": str(os.getpid())},
        )

    @app.get("/readyz")
    async def ready():
        worker = worker_state()
        try:
            await repo.ping()
            database = "ready"
        except Exception:
            database = "unavailable"
        available = database == "ready" and worker["status"] == "ready"
        return JSONResponse(
            status_code=200 if available else 503,
            content={
                "ready": available,
                "database": database,
                "worker": worker,
                "scope": (
                    "Application database and worker; not provider connectivity "
                    "or target-service health"
                ),
            },
        )

    @app.post("/api/runs", status_code=202, response_model=RunView)
    async def submit_run(
        body: RunInput,
        idempotency_key: Annotated[str, Header(min_length=1, max_length=200)],
    ):
        if not idempotency_key.strip():
            raise HTTPException(422, "Idempotency-Key must not be blank")
        if body.mode == "live":
            existing = await repo.find_by_key(idempotency_key)
            if existing:
                previous = RunInput.model_validate(existing["request"]).model_dump(mode="json")
                incoming = body.model_dump(mode="json")
                for field in (
                    "project_version",
                    "model_profile_version",
                    "knowledge_generation",
                    "retrieval_mode",
                ):
                    if incoming.get(field) is None:
                        incoming[field] = previous.get(field)
                if previous == incoming:
                    return existing
                raise Conflict("Idempotency-Key is already bound to different parameters")
            try:
                project = projects.get(body.service_id, body.project_version)
                model_version = body.model_profile_version or models.freeze()
                models.settings_for_version(model_version)
                generation = await asyncio.to_thread(
                    knowledge.active_generation, project.id, project.version
                )
                if (
                    body.knowledge_generation is not None
                    and body.knowledge_generation != generation
                ):
                    raise ValueError(
                        "Requested knowledge generation is not current for this project"
                    )
            except (ValueError, KeyError, FileNotFoundError) as error:
                raise HTTPException(
                    422, "Register a valid project and model profile before submitting"
                ) from error
            body = body.model_copy(
                update={
                    "project_version": project.version,
                    "model_profile_version": model_version,
                    "knowledge_generation": generation,
                    "retrieval_mode": body.retrieval_mode or project.retrieval_mode,
                }
            )
            if body.parent_run_id:
                parent = await repo.get_run(body.parent_run_id)
                if (
                    parent["status"] not in TERMINAL
                    or parent["request"]["service_id"] != body.service_id
                    or parent["request"]["mode"] != "live"
                ):
                    raise HTTPException(422, "Follow-up requires a finished run of this project")
        run, _created = await repo.create_run(body.model_dump(mode="json"), idempotency_key)
        return run

    @app.get("/api/incidents")
    async def development_incidents():
        # Explicitly allow only neutral development inputs. No acceptance material,
        # expected answer, label, or caller-supplied file path is exposed.
        items = []
        for path in sorted((RESOURCE_ROOT / "evals/dev").glob("*.json")):
            source = json.loads(path.read_text())
            body = RunInput.model_validate(
                {
                    "incident_id": source["incident_id"],
                    "service_id": source["service_id"],
                    "question": source["question"],
                    "start": source["window"]["start"],
                    "end": source["window"]["end"],
                }
            ).model_dump(mode="json")
            items.append(
                {
                    **{
                        key: body[key]
                        for key in ("incident_id", "service_id", "question", "start", "end")
                    },
                    "window": {"start": body["start"], "end": body["end"]},
                }
            )
        return {"items": items}

    @app.get("/api/runs", response_model=RunPage)
    async def list_runs(limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0)):
        return {"items": await repo.list_runs(limit, offset), "limit": limit, "offset": offset}

    @app.get("/api/runs/{run_id}", response_model=RunView)
    async def get_run(run_id: str):
        return await repo.get_run(run_id)

    @app.get("/api/runs/{run_id}/evidence/{evidence_id}")
    async def get_evidence(run_id: str, evidence_id: str):
        return await repo.get_evidence(run_id, evidence_id)

    @app.post("/api/runs/{run_id}/resume", status_code=202, response_model=ResumeView)
    async def resume(run_id: str, body: ResumeInput):
        operation = await repo.decide(run_id, **body.model_dump())
        return {"accepted": True, "operation": operation}

    @app.post("/api/runs/{run_id}/cancel", status_code=202, response_model=RunView)
    async def cancel(run_id: str):
        return await repo.request_cancel(run_id)

    @app.get("/api/runs/{run_id}/events", response_model=EventPage)
    async def get_events(
        run_id: str, after: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=200)
    ):
        items = await repo.events(run_id, after, limit)
        return {"items": items, "next_cursor": items[-1]["event_id"] if items else after}

    @app.get("/api/runs/{run_id}/events/stream")
    async def stream_events(
        run_id: str,
        request: Request,
        cursor: int = Query(0, ge=0),
        last_event_id: Annotated[str | None, Header()] = None,
    ):
        await repo.get_run(run_id)
        try:
            header_position = int(last_event_id or 0)
        except ValueError as error:
            raise HTTPException(422, "Last-Event-ID must be a nonnegative integer") from error
        if header_position < 0:
            raise HTTPException(422, "Last-Event-ID must be a nonnegative integer")
        position = max(cursor, header_position)

        async def stream():
            nonlocal position
            while not await request.is_disconnected():
                items = await repo.events(run_id, position, 100)
                for item in items:
                    position = item["event_id"]
                    data = json.dumps(item, ensure_ascii=False, separators=(",", ":"))
                    yield f"id: {position}\nevent: {item['kind']}\ndata: {data}\n\n"
                if items:
                    # Requery the durable cursor even after a terminal event; a full batch
                    # must not truncate the backlog, and no in-memory wakeup is required.
                    continue
                run = await repo.get_run(run_id)
                if run["status"] in TERMINAL:
                    # Capture an event committed after the empty read and before this
                    # terminal read before deciding that the stream is complete.
                    if await repo.events(run_id, position, 1):
                        continue
                    return
                yield ": keep-alive\n\n"
                await asyncio.sleep(0.5)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    frontend = RESOURCE_ROOT / "frontend/dist"
    if (frontend / "index.html").is_file():
        app.mount("/", StaticFiles(directory=frontend, html=True), name="workbench")
    return app
