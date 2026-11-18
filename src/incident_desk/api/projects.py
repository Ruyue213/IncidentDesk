"""Local project registration and real read-only connection probes."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from incident_desk.cloud_policy import CloudPolicy
from incident_desk.knowledge.retrieval_config import RetrievalConfig
from incident_desk.projects import (
    BusinessProbeProfile,
    ProjectConflict,
    ProjectNotFound,
    ProjectProfile,
    ProjectStore,
)
from incident_desk.settings import Settings
from incident_desk.tools.live_evidence import LiveEvidence


class ProjectInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str | None = None
    name: str = Field(min_length=1, max_length=100)
    root: str
    log_files: list[str] = Field(default_factory=list, max_length=20)
    health_url: str | None = None
    metrics_url: str | None = None
    stats_url: str | None = None
    start_argv: list[str] = Field(default_factory=list, max_length=100)
    config_files: list[str] = Field(default_factory=list, max_length=20)
    knowledge_paths: list[str] = Field(default_factory=list, max_length=30)
    retrieval_mode: Literal["lexical", "dense", "hybrid"] = "hybrid"
    retrieval_config: RetrievalConfig = Field(default_factory=RetrievalConfig)
    cloud_policy: CloudPolicy = Field(default_factory=CloudPolicy)
    history_enabled: bool = True
    business_probe: BusinessProbeProfile | None = None
    timezone: str = "Asia/Shanghai"
    expected_version: int | None = Field(default=None, ge=0)


def create_projects_router(settings: Settings, store: ProjectStore | None = None) -> APIRouter:
    store = store or ProjectStore(settings.local_dir)
    router = APIRouter(prefix="/api/projects", tags=["projects"])

    def find(project_id: str) -> ProjectProfile:
        try:
            return store.get(project_id)
        except ProjectNotFound as error:
            raise HTTPException(404, str(error)) from error
        except ValueError as error:
            raise HTTPException(422, "invalid project registration") from error

    @router.get("")
    async def list_projects():
        return {"items": await asyncio.to_thread(store.list_public)}

    @router.get("/{project_id}")
    async def get_project(project_id: str):
        return store.public(await asyncio.to_thread(find, project_id))

    @router.put("/{project_id}")
    async def put_project(project_id: str, request: Request):
        try:
            chunks, length = [], 0
            async for chunk in request.stream():
                length += len(chunk)
                if length > 65536:
                    raise ValueError("registration is too large")
                chunks.append(chunk)
            body = ProjectInput.model_validate(json.loads(b"".join(chunks)))
        except (ValueError, ValidationError) as error:
            raise HTTPException(422, "invalid project registration") from error
        if body.id is not None and body.id != project_id:
            raise HTTPException(422, "project ID must match the URL")
        try:
            # Clients predating these settings must not reset an existing policy.
            values = body.model_dump(exclude={"id", "expected_version"}) | {"id": project_id}
            expected = body.expected_version
            try:
                prior = await asyncio.to_thread(store.get, project_id)
            except ProjectNotFound:
                prior = None
            if prior is not None:
                for key in ("retrieval_config", "cloud_policy"):
                    if key not in body.model_fields_set:
                        values[key] = getattr(prior, key).model_dump()
            profile = await asyncio.to_thread(
                store.save,
                values,
                expected if expected is not None else prior.version if prior else 0,
            )
        except ProjectConflict as error:
            raise HTTPException(409, str(error)) from error
        except (ValueError, ValidationError, OSError) as error:
            # Pydantic errors may embed the submitted credential: return no raw error input.
            raise HTTPException(
                422, "Invalid collection, retrieval, or cloud-data settings"
            ) from error
        return store.public(profile)

    @router.post("/{project_id}/probe")
    async def probe_project(project_id: str):
        profile = await asyncio.to_thread(find, project_id)
        collector = LiveEvidence(profile, settings)
        now = datetime.now(UTC)
        items = await asyncio.gather(
            *(
                collector.get(source, now - timedelta(minutes=10), now, 10)
                for source in ("health", "metrics", "stats", "process", "logs", "changes")
            )
        )
        return {"project_id": profile.id, "project_version": profile.version, "items": items}

    return router
