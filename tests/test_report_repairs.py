"""Validation correction limits survive workers, rollback, cancellation and migrations."""

import asyncio
import json
import sqlite3

import pytest

from incident_desk.agent.budget import BudgetExceeded
from incident_desk.storage import Conflict, Repository
from incident_desk.worker.runtime import WorkerLock

REQUEST = {
    "question": "Inspect the actual service evidence",
    "service_id": "checkout-api",
    "incident_id": "fixture-001",
    "start": "2026-11-09T00:00:00Z",
    "end": "2026-11-09T01:00:00Z",
}


@pytest.mark.asyncio
async def test_repair_limit_is_atomic_across_workers_and_persists_on_restart(tmp_path):
    left = Repository(tmp_path / "app.db")
    right = Repository(left.path)
    await left.init()
    await right.init()
    try:
        run, _ = await left.create_run(REQUEST, "repairs")
        assert run["report_repairs"] == 0
        attempts = await asyncio.gather(
            left.consume_report_repair(run["id"], "quote mismatch " + "x" * 1200),
            right.consume_report_repair(run["id"], "schema mismatch"),
        )
        assert sorted(attempts) == [1, 2]
        await right.close()
        right = Repository(left.path)
        await right.init()
        assert (await right.get_run(run["id"]))["report_repairs"] == 2
        with pytest.raises(BudgetExceeded, match="report validation repair limit"):
            await right.consume_report_repair(run["id"], "not reserved")
        failures = [
            e for e in await right.events(run["id"]) if e["kind"] == "report.validation_failed"
        ]
        assert [event["payload"]["attempt"] for event in failures] == [1, 2]
        assert all(len(event["payload"]["error"]) <= 1000 for event in failures)
    finally:
        await left.close()
        await right.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", ["cancel_requested", "SUCCEEDED", "FAILED", "CANCELLED"])
async def test_repair_rejects_cancelled_and_terminal_runs_without_mutation(tmp_path, stop):
    repo = Repository(tmp_path / "app.db")
    await repo.init()
    try:
        run, _ = await repo.create_run(REQUEST, "stopped")
        if stop == "cancel_requested":
            await repo.request_cancel(run["id"])
        else:
            await repo.transition(run["id"], stop)
        before = await repo.events(run["id"])
        with pytest.raises(Conflict):
            await repo.consume_report_repair(run["id"], "late validation result")
        assert (await repo.get_run(run["id"]))["report_repairs"] == 0
        assert await repo.events(run["id"]) == before
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_repair_count_and_feedback_event_rollback_as_one_transaction(tmp_path):
    repo = Repository(tmp_path / "app.db")
    await repo.init()
    try:
        run, _ = await repo.create_run(REQUEST, "rollback")

        def fail(point):
            if point == "report_repair.before_commit":
                raise RuntimeError("transaction interrupted")

        repo.fault_hook = fail
        with pytest.raises(RuntimeError, match="transaction interrupted"):
            await repo.consume_report_repair(run["id"], "not committed")
        assert (await repo.get_run(run["id"]))["report_repairs"] == 0
        assert not [
            e for e in await repo.events(run["id"]) if e["kind"] == "report.validation_failed"
        ]
        repo.fault_hook = None
        assert await repo.consume_report_repair(run["id"], "committed", limit=1) == 1
        with pytest.raises(BudgetExceeded):
            await repo.consume_report_repair(run["id"], "too many", limit=1)
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_legacy_runs_migrate_to_zero_and_reinitialization_preserves_repairs(tmp_path):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as db:
        db.execute("""CREATE TABLE runs (
            id VARCHAR PRIMARY KEY, idempotency_key VARCHAR NOT NULL UNIQUE,
            request_json TEXT NOT NULL, status VARCHAR NOT NULL,
            diagnosis_json TEXT, error_json TEXT, cancel_requested BOOLEAN NOT NULL,
            active_seconds FLOAT NOT NULL, tool_calls INTEGER NOT NULL,
            revision INTEGER NOT NULL, created_at VARCHAR NOT NULL, updated_at VARCHAR NOT NULL
        )""")
        db.execute(
            """INSERT INTO runs
            (id,idempotency_key,request_json,status,cancel_requested,active_seconds,
             tool_calls,revision,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                "legacy",
                "legacy-key",
                json.dumps(REQUEST),
                "RUNNING",
                False,
                0,
                0,
                0,
                "2026-11-09T00:00:00Z",
                "2026-11-09T00:00:00Z",
            ),
        )
    repo = Repository(path)
    try:
        await repo.init()
        assert (await repo.get_run("legacy"))["report_repairs"] == 0
        with sqlite3.connect(path) as db:
            column = next(
                row for row in db.execute("PRAGMA table_info(runs)") if row[1] == "report_repairs"
            )
            assert column[2] == "INTEGER" and column[3] == 1 and str(column[4]) == "0"
            assert db.execute("PRAGMA user_version").fetchone()[0] == 4
            columns = {row[1] for row in db.execute("PRAGMA table_info(worker_run_claims)")}
            assert columns == {"run_id", "worker_id", "claimed_at"}
        assert await repo.consume_report_repair("legacy", "reference mismatch") == 1
        assert await repo.claim_run("legacy", "previous-worker")
        await repo.init()
        assert (await repo.get_run("legacy"))["report_repairs"] == 1
        assert (await repo.workload_counts())["claimed_count"] == 1
        # API/schema initialization must not steal a live claim. Only the next
        # exclusive worker owner can clear claims left by an earlier process.
        with WorkerLock(tmp_path):
            await repo.reset_worker_claims()
        assert (await repo.workload_counts())["claimed_count"] == 0
    finally:
        await repo.close()
