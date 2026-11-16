"""Task idempotency freezes knowledge alongside project/model versions."""

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from incident_desk.api import create_app
from incident_desk.knowledge.project_store import ProjectKnowledgeStore
from incident_desk.projects import ProjectStore
from incident_desk.settings import Settings


@pytest.mark.asyncio
async def test_live_retries_keep_generation_and_explicit_changes_conflict(tmp_path):
    root = tmp_path / "service"
    root.mkdir()
    document = root / "runbook.md"
    document.write_text("# Queue saturation\nCompare queue depth and rejection counters.\n")
    settings = Settings(_env_file=None, api_key=None, local_dir=tmp_path / "state")
    project = ProjectStore(settings.local_dir).save(
        {
            "id": "service",
            "name": "Service",
            "root": str(root),
            "knowledge_paths": ["runbook.md"],
            "retrieval_mode": "hybrid",
        }
    )
    knowledge = ProjectKnowledgeStore(settings.local_dir)
    first_generation = knowledge.ingest(project, embed=False)["generation"]
    now = datetime.now(UTC)
    body = {
        "mode": "live",
        "service_id": project.id,
        "question": "Check recent queue saturation using actual evidence",
        "start": (now - timedelta(minutes=30)).isoformat(),
        "end": now.isoformat(),
    }
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://127.0.0.1"
        ) as client:
            first = await client.post("/api/runs", json=body, headers={"Idempotency-Key": "first"})
            assert first.status_code == 202, first.text
            frozen = first.json()["request"]
            assert frozen["knowledge_generation"] == first_generation
            assert frozen["retrieval_mode"] == "hybrid"
            document.write_text(
                "# Queue saturation\nNew capacity guidance and rejection counters.\n"
            )
            next_generation = knowledge.ingest(project, embed=False)["generation"]
            assert first_generation != next_generation
            retry = await client.post("/api/runs", json=body, headers={"Idempotency-Key": "first"})
            assert retry.json()["request"] == frozen
            assert retry.json()["id"] == first.json()["id"]
            for change in (
                {"knowledge_generation": next_generation},
                {"retrieval_mode": "lexical"},
                {"project_version": 99},
                {"model_profile_version": 99},
            ):
                response = await client.post(
                    "/api/runs", json=body | change, headers={"Idempotency-Key": "first"}
                )
                assert response.status_code == 409
            newer = await client.post("/api/runs", json=body, headers={"Idempotency-Key": "new"})
            assert newer.json()["request"]["knowledge_generation"] == next_generation
            # Old generations remain addressable and do not silently pick up new text.
            old = knowledge.search(project.id, first_generation, "queue", mode="lexical")
            assert old["items"] and "New capacity" not in old["items"][0]["content"]


@pytest.mark.asyncio
async def test_empty_knowledge_is_frozen_until_a_new_task(tmp_path):
    settings = Settings(_env_file=None, api_key=None, local_dir=tmp_path / "state")
    project = ProjectStore(settings.local_dir).save(
        {"id": "service", "name": "Service", "root": str(tmp_path)}
    )
    app = create_app(settings)
    now = datetime.now(UTC)
    body = {
        "mode": "live",
        "service_id": project.id,
        "question": "Check actual service availability",
        "start": (now - timedelta(minutes=5)).isoformat(),
        "end": now.isoformat(),
    }
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://127.0.0.1"
        ) as client:
            run = await client.post("/api/runs", json=body, headers={"Idempotency-Key": "empty"})
            assert run.json()["request"]["knowledge_generation"] is None
            unavailable = ProjectKnowledgeStore(settings.local_dir).search(
                project.id, None, "queue"
            )
            assert unavailable["status"] == "unavailable" and not unavailable["items"]
