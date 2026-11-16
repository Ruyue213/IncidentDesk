"""Static deployment must preserve the API namespace and its public contracts."""

import json
import os
from datetime import UTC, datetime

import httpx
import pytest

from incident_desk.api import app as api_module
from incident_desk.settings import Settings


@pytest.mark.asyncio
async def test_application_readiness_requires_database_and_live_worker(tmp_path, monkeypatch):
    settings = Settings(_env_file=None, local_dir=tmp_path / "runtime")
    app = api_module.create_app(settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
        ) as client:
            unavailable = await client.get("/readyz")
            assert unavailable.status_code == 503 and not unavailable.json()["ready"]
            (settings.local_dir / "worker-heartbeat.json").write_text(
                json.dumps(
                    {"pid": os.getpid(), "at": datetime.now(UTC).isoformat(), "mode": "offline"}
                )
            )
            ready = await client.get("/readyz")
            assert ready.status_code == 200 and ready.json()["ready"]
            assert "not provider connectivity" in ready.json()["scope"]

            async def failed_database():
                raise OSError("synthetic database failure")

            monkeypatch.setattr(app.state.repository, "ping", failed_database)
            failed = await client.get("/readyz")
            assert failed.status_code == 503 and failed.json()["database"] == "unavailable"


@pytest.mark.asyncio
async def test_static_workbench_does_not_swallow_api_or_expose_parent_files(tmp_path, monkeypatch):
    frontend = tmp_path / "frontend/dist"
    frontend.mkdir(parents=True)
    (frontend / "index.html").write_text("<title>IncidentDesk local workbench</title>")
    (tmp_path / ".env").write_text("not-a-real-secret")
    monkeypatch.setattr(api_module, "RESOURCE_ROOT", tmp_path)
    settings = Settings(_env_file=None, local_dir=tmp_path / "runtime")
    app = api_module.create_app(settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
        ) as client:
            home = await client.get("/")
            assert home.status_code == 200 and "workbench" in home.text
            assert (await client.get("/api/not-a-route")).status_code == 404
            assert (await client.get("/.env")).status_code == 404
            assert (await client.get("/%2e%2e/.env")).status_code == 404
            schema = (await client.get("/openapi.json")).json()
            assert "RunView" in schema["components"]["schemas"]
            assert "Diagnosis" in schema["components"]["schemas"]
            assert "TicketRequest" in schema["components"]["schemas"]
            assert "api_key" not in str(schema).lower()
