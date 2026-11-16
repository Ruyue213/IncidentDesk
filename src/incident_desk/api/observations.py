"""Version-scoped local observations and explicit finite read-only business checks."""

from datetime import datetime

from fastapi import APIRouter, HTTPException, Query

from incident_desk.observations import ObservationStore
from incident_desk.projects import ProjectStore
from incident_desk.tools.business_probe import BusinessProbe


def create_observations_router(settings, project_store=None, observation_store=None):
    projects = project_store or ProjectStore(settings.local_dir)
    observations = observation_store or ObservationStore(settings.local_dir)
    probe = BusinessProbe(settings)
    router = APIRouter(prefix="/api/projects", tags=["observations"])

    def selected(project_id, version):
        try:
            return projects.get(project_id, version)
        except ValueError:
            raise HTTPException(404, "Registered project/version not found") from None

    @router.get("/{project_id}/observations")
    async def history(
        project_id: str,
        start: datetime,
        end: datetime,
        version: int | None = Query(None, ge=1),
        limit: int = Query(200, ge=1, le=200),
    ):
        project = selected(project_id, version)
        try:
            return await observations.query(project.id, project.version, start, end, limit)
        except ValueError:
            raise HTTPException(
                422, "History requires timezone-aware ordered bounds of at most seven days"
            ) from None

    @router.post("/{project_id}/sample")
    async def sample(project_id: str):
        return await observations.sample(selected(project_id, None))

    @router.post("/{project_id}/probe-business")
    async def business(project_id: str, attempts: int = Query(1, ge=1, le=3)):
        try:
            return await probe.run(selected(project_id, None), attempts)
        except ValueError:
            raise HTTPException(
                422, "Business probe is not configured as a supported fixed read-only request"
            ) from None

    return router
