"""Explicit local knowledge indexing and retrieval; no model download or LLM calls."""

import asyncio

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from incident_desk.knowledge.project_store import KnowledgeIndexBusy, ProjectKnowledgeStore
from incident_desk.projects import ProjectNotFound, ProjectStore


class IndexInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    embed: bool = True
    project_version: int | None = Field(default=None, ge=1)


class SearchInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=1000)
    generation: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    mode: str = Field(default="hybrid", pattern=r"^(lexical|dense|hybrid)$")
    top_k: int = Field(default=5, ge=1, le=10)

    @field_validator("query")
    @classmethod
    def meaningful_query(cls, value):
        if not value.strip():
            raise ValueError("Query cannot be whitespace")
        return value


async def _input(request: Request, schema):
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > 16384:
            raise HTTPException(413, "Knowledge request is too large")
    try:
        return schema.model_validate_json(data or b"{}")
    except ValidationError:
        raise HTTPException(422, "Invalid knowledge request fields") from None


def create_knowledge_router(settings, projects=None, knowledge=None):
    projects = projects or ProjectStore(settings.local_dir)
    knowledge = knowledge or ProjectKnowledgeStore(settings.local_dir)
    router = APIRouter(prefix="/api/projects/{project_id}/knowledge", tags=["knowledge"])

    def project(project_id):
        try:
            return projects.get(project_id)
        except ProjectNotFound:
            raise HTTPException(404, "Registered project not found") from None
        except ValueError:
            raise HTTPException(422, "Invalid project identity") from None

    @router.get("")
    async def status(project_id: str):
        current = project(project_id)
        result = await asyncio.to_thread(knowledge.status, project_id)
        result["usable_generation"] = await asyncio.to_thread(
            knowledge.active_generation, project_id, current.version
        )
        result["project_version"] = current.version
        return result

    @router.post("/index")
    async def index(project_id: str, request: Request):
        body = await _input(request, IndexInput)
        current = project(project_id)
        if body.project_version is not None and body.project_version != current.version:
            raise HTTPException(409, "Project changed; reload before indexing")
        try:
            return await asyncio.to_thread(knowledge.ingest, current, embed=body.embed)
        except KnowledgeIndexBusy:
            raise HTTPException(
                409, "Another knowledge index is running; retry when it finishes"
            ) from None
        except (ValueError, OSError):
            raise HTTPException(
                422, "Knowledge indexing failed safely; check registered paths and limits"
            ) from None

    @router.post("/search")
    async def search(project_id: str, request: Request):
        body = await _input(request, SearchInput)
        current = project(project_id)
        generation = body.generation or await asyncio.to_thread(
            knowledge.active_generation, project_id, current.version
        )
        return await asyncio.to_thread(
            knowledge.search, project_id, generation, body.query, mode=body.mode, top_k=body.top_k
        )

    return router
