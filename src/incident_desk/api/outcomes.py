"""Explicit operator feedback; importing this router never labels or runs a task."""

from contextlib import asynccontextmanager

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import ValidationError

from incident_desk.outcomes import (
    OutcomeConflict,
    OutcomeInput,
    OutcomeStore,
    OutcomeVerificationMismatch,
    validate_outcome,
)
from incident_desk.storage.repository import NotFound, Repository


def create_outcomes_router(settings, repository=None, outcomes=None):
    owns_repository = repository is None
    repository = repository or Repository(settings.app_db)
    outcomes = outcomes or OutcomeStore(settings.app_db)

    @asynccontextmanager
    async def lifespan(_app):
        if owns_repository:
            await repository.init()
        try:
            yield
        finally:
            if owns_repository:
                await repository.close()

    router = APIRouter(tags=["operator outcomes"], lifespan=lifespan)

    async def run_record(run_id):
        try:
            return await repository.get_run(run_id)
        except NotFound:
            raise HTTPException(404, "Task not found") from None

    @router.get("/api/runs/{run_id}/outcome")
    async def latest(run_id: str):
        await run_record(run_id)
        return {"outcome": await outcomes.latest(run_id), "source": "operator_supplied"}

    @router.post("/api/runs/{run_id}/outcome")
    async def record(run_id: str, request: Request):
        data = bytearray()
        async for part in request.stream():
            data.extend(part)
            if len(data) > 65536:
                raise HTTPException(413, "Outcome annotation exceeds its body limit")
        try:
            payload = OutcomeInput.model_validate_json(data)
        except ValidationError:
            raise HTTPException(
                422, "Invalid assessment, reasons, references or revision"
            ) from None
        run = await run_record(run_id)
        try:
            validate_outcome(payload, run, await repository.list_evidence(run_id))
            return {
                "outcome": await outcomes.record(run_id, payload),
                "source": "operator_supplied",
            }
        except OutcomeConflict as error:
            raise HTTPException(409, str(error)) from None
        except OutcomeVerificationMismatch as error:
            raise HTTPException(422, str(error)) from None
        except ValueError:
            raise HTTPException(
                422, "Verification references must match this task's snapshots"
            ) from None

    @router.get("/api/outcomes/aggregate")
    async def aggregate(mode: str = Query("live", pattern=r"^(all|live|fixture)$")):
        return await outcomes.aggregate(mode)

    return router
