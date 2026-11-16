"""Local ASGI contracts; no worker or model runs in the API process."""

import asyncio
import json

import httpx
import pytest
import pytest_asyncio

from incident_desk.api import create_app
from incident_desk.settings import Settings

REQUEST = {
    "question": "Inspect the elevated checkout error rate",
    "service_id": "checkout-api",
    "incident_id": "fixture-001",
    "start": "2026-11-09T00:00:00Z",
    "end": "2026-11-09T01:00:00Z",
}
PARAMS = {
    "draft": {"title": "Investigate checkout", "description": "Review pool wait evidence"},
    "references": [{"id": "ev-001", "version": "v1", "quote": "pool waits"}],
}


@pytest_asyncio.fixture
async def api(tmp_path):
    settings = Settings(_env_file=None, api_key=None, local_dir=tmp_path)
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
        ) as client:
            yield client, app.state.repository


async def submit(client, *, key="same", data=None):
    return await client.post("/api/runs", json=data or REQUEST, headers={"Idempotency-Key": key})


@pytest.mark.asyncio
async def test_submit_is_durable_and_idempotency_is_enforced(api):
    client, repo = api
    first = await submit(client)
    assert first.status_code == 202
    run = first.json()
    assert run["status"] == "QUEUED"
    assert (await repo.get_run(run["id"]))["id"] == run["id"]
    retry = await submit(client)
    assert retry.status_code == 202
    assert retry.json()["id"] == run["id"]
    conflict = await submit(client, data=REQUEST | {"question": "different failure question"})
    assert conflict.status_code == 409
    assert (await client.get("/api/runs?limit=1&offset=0")).json()["items"][0]["id"] == run["id"]
    assert len(await repo.pending_commands()) == 1
    assert "api_key" not in json.dumps(run).lower()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes",
    [
        {"start": "2026-11-09T00:00:00"},
        {"end": "2026-11-08T00:00:00Z"},
        {"service_id": "another-service"},
        {"question": "short"[:3]},
        {"incident_id": "../../secret"},
        {"admin": True},
    ],
)
async def test_invalid_inputs_fail_before_persistence(api, changes):
    client, repo = api
    response = await submit(client, data=REQUEST | changes)
    assert response.status_code == 422
    assert await repo.list_runs() == []


@pytest.mark.asyncio
async def test_missing_idempotency_key_and_unbounded_paging_are_rejected(api):
    client, _repo = api
    assert (await client.post("/api/runs", json=REQUEST)).status_code == 422
    assert (await client.get("/api/runs?limit=10000")).status_code == 422
    assert (await client.get("/api/runs?offset=-1")).status_code == 422
    assert (await client.get("/api/runs/missing")).status_code == 404


@pytest.mark.asyncio
async def test_approval_accepts_commands_without_invoking_a_graph(api):
    client, repo = api
    run_id = (await submit(client)).json()["id"]
    operation = await repo.freeze_operation(run_id, "interrupt-one", PARAMS)
    body = {
        "interrupt_id": operation["interrupt_id"],
        "operation_id": operation["id"],
        "draft_version": operation["draft_version"],
        "params_digest": operation["params_digest"],
        "decision": "approve",
    }
    url = f"/api/runs/{run_id}/resume"
    assert (await client.post(url, json=body | {"interrupt_id": "stale"})).status_code == 409
    accepted = await client.post(url, json=body)
    assert accepted.status_code == 202
    assert (await client.post(url, json=body)).json() == accepted.json()
    assert (await client.post(url, json=body | {"decision": "reject"})).status_code == 409
    assert (await client.post(url, json=body | {"params": PARAMS})).status_code == 422
    current = (await client.get(f"/api/runs/{run_id}")).json()
    assert current["status"] == "WAITING_APPROVAL"
    assert current["ticket_id"] is None
    assert [command["kind"] for command in await repo.pending_commands()].count("RESUME") == 1


@pytest.mark.asyncio
async def test_evidence_endpoint_is_run_scoped_and_cancel_is_idempotent(api):
    client, repo = api
    run_id = (await submit(client)).json()["id"]
    other_id = (await submit(client, key="other")).json()["id"]
    await repo.save_evidence(run_id, {"id": "ev-001", "version": "v1", "content": "pool waits"})
    assert (await client.get(f"/api/runs/{run_id}/evidence/ev-001")).status_code == 200
    assert (await client.get(f"/api/runs/{other_id}/evidence/ev-001")).status_code == 404
    for _ in range(2):
        response = await client.post(f"/api/runs/{run_id}/cancel")
        assert response.status_code == 202
        assert response.json()["cancel_requested"] is True
    assert len([e for e in await repo.events(run_id) if e["kind"] == "run.cancel_requested"]) == 1


@pytest.mark.asyncio
async def test_health_has_no_paid_model_dependency_and_event_pages_are_bounded(api):
    client, _repo = api
    assert (await client.get("/health")).json() == {
        "status": "ok",
        "database": "ok",
        "worker": {"status": "unavailable"},
    }
    run_id = (await submit(client)).json()["id"]
    page = (await client.get(f"/api/runs/{run_id}/events?after=0&limit=1")).json()
    assert len(page["items"]) == 1
    cursor = page["next_cursor"]
    assert (await client.get(f"/api/runs/{run_id}/events?after={cursor}")).json()["items"] == []
    assert (await client.get(f"/api/runs/{run_id}/events?limit=10000")).status_code == 422


@pytest.mark.asyncio
async def test_sse_replays_terminal_backlog_and_honors_last_event_id(api):
    client, repo = api
    run_id = (await submit(client)).json()["id"]
    first = (await repo.events(run_id))[0]["event_id"]
    for number in range(103):
        await repo.record_event(run_id, "probe", {"number": number})
    await repo.transition(run_id, "SUCCEEDED", diagnosis={"summary": "done"})
    response = await client.get(
        f"/api/runs/{run_id}/events/stream", headers={"Last-Event-ID": str(first)}
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    ids = [int(line[4:]) for line in response.text.splitlines() if line.startswith("id: ")]
    expected = [event["event_id"] for event in await repo.events(run_id, first, 200)]
    assert ids == expected
    assert len(ids) == 104


@pytest.mark.asyncio
async def test_sse_does_not_lose_event_inserted_between_backlog_and_tail(api):
    client, repo = api
    run_id = (await submit(client)).json()["id"]
    first_read = asyncio.Event()
    continue_read = asyncio.Event()
    original = repo.events
    observed = False

    async def intercept(run, after=0, limit=100):
        nonlocal observed
        items = await original(run, after, limit)
        if not observed and after > 0 and not items:
            observed = True
            first_read.set()
            await continue_read.wait()
        return items

    repo.events = intercept
    request = asyncio.create_task(client.get(f"/api/runs/{run_id}/events/stream"))
    await asyncio.wait_for(first_read.wait(), timeout=2)
    inserted = await repo.record_event(run_id, "tail.inserted", {"observable": True})
    await repo.transition(run_id, "SUCCEEDED")
    continue_read.set()
    response = await asyncio.wait_for(request, timeout=2)
    assert f"id: {inserted}\n" in response.text
    assert "event: tail.inserted" in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("cursor", ["-1", "invalid"])
async def test_sse_rejects_invalid_reconnect_cursor(api, cursor):
    client, _repo = api
    run_id = (await submit(client)).json()["id"]
    response = await client.get(
        f"/api/runs/{run_id}/events/stream", headers={"Last-Event-ID": cursor}
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_incident_catalog_exposes_only_neutral_development_inputs(api):
    client, _repo = api
    response = await client.get("/api/incidents")
    assert response.status_code == 200
    items = response.json()["items"]
    assert len(items) == 8
    assert all(item["incident_id"].startswith("dev-") for item in items)
    allowed = {"incident_id", "service_id", "question", "start", "end", "window"}
    assert all(set(item) == allowed for item in items)
