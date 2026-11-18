"""Explicit outcome records must not manufacture quality from workflow success."""

import asyncio
import json
import sqlite3

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from incident_desk.api.outcomes import create_outcomes_router
from incident_desk.outcomes import OutcomeConflict, OutcomeInput, OutcomeStore
from incident_desk.settings import Settings
from incident_desk.storage.repository import Repository

INPUT = {
    "service_id": "checkout-api",
    "incident_id": "dev-001",
    "question": "Synthetic outcome module test, no real service",
    "start": "2026-11-17T00:00:00Z",
    "end": "2026-11-17T01:00:00Z",
}
EVIDENCE = {
    "id": "verification-1",
    "version": "v1",
    "source": "business_probe",
    "content": "Synthetic business assertion passed after the operator's intervention.",
}
LABEL = {
    "diagnosis_assessment": "confirmed",
    "diagnosis_reason": "Private reason /secret/project",
    "recovery_assessment": "verified",
    "recovery_reason": "Private check user@example.invalid",
    "verification_references": [
        {"id": "verification-1", "version": "v1", "quote": "Synthetic business assertion passed"}
    ],
    "expected_revision": 0,
}


@pytest_asyncio.fixture
async def setup(tmp_path, monkeypatch):
    settings = Settings(_env_file=None, api_key=None, local_dir=tmp_path)
    repo = Repository(settings.app_db)
    await repo.init()
    store = OutcomeStore(settings.app_db)
    app = FastAPI()
    app.include_router(create_outcomes_router(settings, repo, store))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
    ) as client:
        yield client, repo, store, settings
    await repo.close()


async def finished(repo, key="task", status="SUCCEEDED", *, report=True):
    run, _ = await repo.create_run(INPUT, key)
    await repo.save_evidence(run["id"], EVIDENCE)
    await repo.transition(
        run["id"], status, diagnosis={"summary": "Synthetic hypothesis"} if report else None
    )
    return run["id"]


@pytest.mark.asyncio
async def test_success_never_automatically_confirms_diagnosis_or_recovery(setup):
    client, repo, _store, _settings = setup
    run = await finished(repo)
    assert (await client.get(f"/api/runs/{run}/outcome")).json()["outcome"] is None
    aggregate = (await client.get("/api/outcomes/aggregate?mode=all")).json()
    assert aggregate["task_succeeded_runs"] == 1
    assert aggregate["annotated_runs"] == 0
    assert aggregate["diagnosis_assessments"] == {"confirmed": 0, "rejected": 0, "inconclusive": 0}
    assert aggregate["confirmation_share_among_annotated"] is None
    assert aggregate["verified_share_among_attempted"] is None
    assert aggregate["unannotated_terminal_runs"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("business_ok,assessment", [(False, "verified"), (True, "failed")])
async def test_explicit_business_verification_cannot_be_labeled_with_opposite_result(
    setup, business_ok, assessment
):
    client, repo, _store, _settings = setup
    run = await finished(repo)
    observed = EVIDENCE | {
        "id": "outcome-check",
        "collected_at": "2026-11-18T00:00:00Z",
        "provenance": {"purpose": "operator_outcome_verification", "business_ok": business_ok},
    }
    await repo.save_evidence(run, observed)
    label = LABEL | {
        "recovery_assessment": assessment,
        "verification_references": [{"id": observed["id"], "version": "v1", "quote": "Synthetic"}],
    }
    response = await client.post(f"/api/runs/{run}/outcome", json=label)
    assert response.status_code == 422
    assert "contradicts the latest referenced business verification" in response.json()["detail"]


@pytest.mark.asyncio
async def test_latest_referenced_verification_wins_and_ordinary_manual_evidence_remains_allowed(
    setup,
):
    client, repo, _store, _settings = setup
    run = await finished(repo)
    for index, ok in enumerate((False, True), 1):
        await repo.save_evidence(
            run,
            EVIDENCE
            | {
                "id": f"check-{index}",
                "collected_at": f"2026-11-18T00:00:0{index}Z",
                "provenance": {"purpose": "operator_outcome_verification", "business_ok": ok},
            },
        )
    refs = [{"id": f"check-{index}", "version": "v1", "quote": "Synthetic"} for index in (2, 1)]
    response = await client.post(
        f"/api/runs/{run}/outcome", json=LABEL | {"verification_references": refs}
    )
    assert response.status_code == 200
    manual = LABEL | {"expected_revision": 1, "recovery_assessment": "failed"}
    assert (await client.post(f"/api/runs/{run}/outcome", json=manual)).status_code == 200


@pytest.mark.asyncio
async def test_operator_annotation_is_durable_idempotent_and_private(setup):
    client, repo, store, settings = setup
    run = await finished(repo)
    first = await client.post(f"/api/runs/{run}/outcome", json=LABEL)
    assert first.status_code == 200
    outcome = first.json()["outcome"]
    assert outcome["source"] == "operator_supplied" and not outcome["independent_verification"]
    retry = (await client.post(f"/api/runs/{run}/outcome", json=LABEL)).json()["outcome"]
    assert retry["id"] == outcome["id"] and retry["revision"] == 1
    assert (await OutcomeStore(settings.app_db).latest(run))["id"] == outcome["id"]
    assert (await repo.get_run(run))["status"] == "SUCCEEDED"
    assert (await store.aggregate("all"))["recovery_assessments"]["verified"] == 1
    assert (await store.aggregate("live"))["annotated_runs"] == 0
    with sqlite3.connect(settings.app_db) as db:
        assert db.execute("SELECT COUNT(*) FROM outcomes").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_revision_conflict_and_history_prevent_lost_annotation_overwrite(setup):
    client, repo, _store, settings = setup
    run = await finished(repo)
    await client.post(f"/api/runs/{run}/outcome", json=LABEL)
    changed = LABEL | {"diagnosis_assessment": "rejected", "expected_revision": 1}
    reply = await client.post(f"/api/runs/{run}/outcome", json=changed)
    assert reply.json()["outcome"]["revision"] == 2
    assert (await client.post(f"/api/runs/{run}/outcome", json=LABEL)).status_code == 409
    assert (await client.get(f"/api/runs/{run}/outcome")).json()["outcome"][
        "diagnosis_assessment"
    ] == "rejected"
    with sqlite3.connect(settings.app_db) as db:
        assert db.execute("SELECT COUNT(*) FROM outcomes").fetchone()[0] == 2


@pytest.mark.asyncio
async def test_concurrent_different_labels_have_one_winner(setup):
    _client, repo, store, _settings = setup
    run = await finished(repo)
    choices = [
        OutcomeInput(**LABEL),
        OutcomeInput(**(LABEL | {"diagnosis_assessment": "rejected"})),
    ]
    results = await asyncio.gather(
        *(store.record(run, value) for value in choices), return_exceptions=True
    )
    assert sum(isinstance(result, OutcomeConflict) for result in results) == 1
    assert sum(isinstance(result, dict) for result in results) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"diagnosis_assessment": "automatic_success"},
        {"recovery_assessment": "SUCCEEDED"},
        {"diagnosis_reason": "   "},
        {"verification_references": []},
        {"expected_revision": True},
        {"source": "independent_human"},
    ],
)
async def test_invalid_or_implicit_assessment_is_rejected(setup, change):
    client, repo, _store, _settings = setup
    run = await finished(repo)
    assert (await client.post(f"/api/runs/{run}/outcome", json=LABEL | change)).status_code == 422


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"id": "cross-run-source"},
        {"version": "changed"},
        {"quote": "invented claim"},
    ],
)
async def test_cross_run_fabricated_and_version_changed_references_rejected(setup, change):
    client, repo, _store, _settings = setup
    run = await finished(repo)
    reference = LABEL["verification_references"][0] | change
    reply = await client.post(
        f"/api/runs/{run}/outcome", json=LABEL | {"verification_references": [reference]}
    )
    assert reply.status_code == 422


@pytest.mark.asyncio
async def test_unfinished_and_missing_report_cannot_be_confirmed(setup):
    client, repo, _store, _settings = setup
    queued, _ = await repo.create_run(INPUT, "queued")
    assert (await client.post(f"/api/runs/{queued['id']}/outcome", json=LABEL)).status_code == 409
    failed = await finished(repo, "failed", "FAILED", report=False)
    assert (await client.post(f"/api/runs/{failed}/outcome", json=LABEL)).status_code == 409
    missing = LABEL | {
        "diagnosis_assessment": "inconclusive",
        "diagnosis_reason": "No report was available",
    }
    assert (await client.post(f"/api/runs/{failed}/outcome", json=missing)).status_code == 200


@pytest.mark.asyncio
async def test_runbook_quote_alone_cannot_verify_recovery(setup):
    client, repo, _store, _settings = setup
    run = await finished(repo)
    await repo.save_evidence(run, {**EVIDENCE, "id": "runbook", "source": "knowledge"})
    reference = LABEL["verification_references"][0] | {"id": "runbook"}
    assert (
        await client.post(
            f"/api/runs/{run}/outcome", json=LABEL | {"verification_references": [reference]}
        )
    ).status_code == 422


@pytest.mark.asyncio
async def test_aggregate_export_excludes_every_private_identifier_and_text(setup):
    client, repo, _store, _settings = setup
    run = await finished(repo)
    await client.post(f"/api/runs/{run}/outcome", json=LABEL)
    exported = (await client.get("/api/outcomes/aggregate?mode=all")).json()
    rendered = json.dumps(exported)
    for forbidden in (
        run,
        "verification-1",
        "Private reason",
        "/secret/project",
        "user@example.invalid",
        "Synthetic business assertion passed",
    ):
        assert forbidden not in rendered
    assert exported["annotation_coverage"] == 1
    assert exported["confirmation_share_among_annotated"] == 1
    assert exported["aggregation_only"]


@pytest.mark.asyncio
async def test_missing_task_and_body_limit_fail_without_any_label(setup):
    client, _repo, _store, _settings = setup
    assert (await client.get("/api/runs/missing/outcome")).status_code == 404
    assert (await client.post("/api/runs/missing/outcome", json=LABEL)).status_code == 404
    assert (await client.post("/api/runs/missing/outcome", content=b"x" * 65537)).status_code == 413
