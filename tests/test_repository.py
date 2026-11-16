"""Business transactions, idempotency, approval binding and durable boundaries."""

import asyncio
import sqlite3
from datetime import datetime, timedelta

import pytest
import pytest_asyncio

from incident_desk.agent.budget import BudgetExceeded, CallLedger
from incident_desk.settings import Settings
from incident_desk.storage import Conflict, NotFound, Repository

REQUEST = {
    "question": "Inspect the elevated checkout error rate",
    "service_id": "checkout-api",
    "incident_id": "fixture-001",
    "start": "2026-11-09T00:00:00Z",
    "end": "2026-11-09T01:00:00Z",
}
SNAPSHOT = {"id": "ev-001", "version": "v1", "content": "pool waits increased"}
PARAMS = {
    "draft": {"title": "Investigate checkout", "description": "Review pool wait evidence"},
    "references": [{"id": "ev-001", "version": "v1", "quote": "pool waits"}],
}


@pytest_asyncio.fixture
async def repo(tmp_path):
    repository = Repository(tmp_path / "app.db")
    await repository.init()
    try:
        yield repository
    finally:
        await repository.close()


async def prepare(repo, *, key="one", approve=False):
    run, _ = await repo.create_run(REQUEST, key)
    await repo.transition(run["id"], "RUNNING")
    await repo.save_evidence(run["id"], SNAPSHOT)
    operation = await repo.freeze_operation(run["id"], "interrupt-one", PARAMS)
    if approve:
        operation = await decide(repo, run["id"], operation)
    return run["id"], operation


async def decide(repo, run_id, operation, decision="approve", **overrides):
    values = {
        "interrupt_id": operation["interrupt_id"],
        "operation_id": operation["id"],
        "draft_version": operation["draft_version"],
        "params_digest": operation["params_digest"],
        "decision": decision,
    }
    return await repo.decide(run_id, **(values | overrides))


async def counts(repo):
    with sqlite3.connect(repo.path) as database:
        return {
            table: database.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("tickets", "tool_receipts", "events", "commands")
        }


@pytest.mark.asyncio
async def test_run_request_idempotency_and_start_command_are_atomic(repo):
    first, created = await repo.create_run(REQUEST, "same")
    retry, created_again = await repo.create_run(dict(reversed(list(REQUEST.items()))), "same")
    assert created and not created_again
    assert first["id"] == retry["id"]
    assert len(await repo.pending_commands()) == 1
    assert len(await repo.events(first["id"])) == 1
    with pytest.raises(Conflict):
        await repo.create_run(REQUEST | {"question": "A different diagnosis question"}, "same")
    assert len(await repo.list_runs()) == 1


@pytest.mark.asyncio
async def test_concurrent_duplicate_submission_has_one_durable_run(repo):
    second = Repository(repo.path)
    await second.init()
    try:
        results = await asyncio.gather(
            repo.create_run(REQUEST, "concurrent"), second.create_run(REQUEST, "concurrent")
        )
        assert sum(created for _run, created in results) == 1
        assert results[0][0]["id"] == results[1][0]["id"]
        assert len(await repo.pending_commands()) == 1
    finally:
        await second.close()


@pytest.mark.asyncio
async def test_snapshot_is_frozen_and_scoped_to_the_run(repo):
    run, _ = await repo.create_run(REQUEST, "a")
    other, _ = await repo.create_run(REQUEST, "b")
    await repo.save_evidence(run["id"], SNAPSHOT)
    await repo.save_evidence(run["id"], SNAPSHOT)
    with pytest.raises(Conflict):
        await repo.save_evidence(run["id"], SNAPSHOT | {"version": "v2"})
    with pytest.raises(NotFound):
        await repo.get_evidence(other["id"], SNAPSHOT["id"])
    assert await repo.list_evidence(run["id"]) == [SNAPSHOT]


@pytest.mark.asyncio
async def test_freeze_is_stable_and_rejects_any_second_proposal(repo):
    run_id, operation = await prepare(repo)
    assert (await repo.get_run(run_id))["status"] == "WAITING_APPROVAL"
    assert (await counts(repo))["tickets"] == 0
    assert await repo.freeze_operation(run_id, "interrupt-one", PARAMS) == operation
    with pytest.raises(Conflict):
        await repo.freeze_operation(run_id, "interrupt-two", PARAMS)
    changed = {**PARAMS, "draft": {**PARAMS["draft"], "title": "Different title"}}
    with pytest.raises(Conflict):
        await repo.freeze_operation(run_id, "interrupt-one", changed)
    assert len(operation["params_digest"]) == 64


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "override",
    [
        {"interrupt_id": "stale-interrupt"},
        {"operation_id": "wrong-operation"},
        {"draft_version": 2},
        {"params_digest": "wrong-digest"},
    ],
)
async def test_stale_or_mismatched_approval_never_writes(repo, override):
    run_id, operation = await prepare(repo)
    with pytest.raises(Conflict):
        await decide(repo, run_id, operation, **override)
    assert (await repo.get_run(run_id))["operation"]["decision"] is None
    assert len(await repo.pending_commands(run_id)) == 1
    assert (await counts(repo))["tickets"] == 0


@pytest.mark.asyncio
async def test_duplicate_decision_has_one_resume_command_and_conflict_is_rejected(repo):
    run_id, operation = await prepare(repo)
    approved = await decide(repo, run_id, operation)
    assert await decide(repo, run_id, operation) == approved
    commands = await repo.pending_commands(run_id)
    assert [item["kind"] for item in commands].count("RESUME") == 1
    with pytest.raises(Conflict):
        await decide(repo, run_id, operation, "reject")
    assert len([e for e in await repo.events(run_id) if e["kind"] == "operation.decided"]) == 1


@pytest.mark.asyncio
async def test_unapproved_and_rejected_ticket_writes_are_denied(repo):
    run_id, operation = await prepare(repo)
    with pytest.raises(Conflict):
        await repo.create_ticket(run_id, PARAMS)
    rejected = await decide(repo, run_id, operation, "reject")
    assert rejected["status"] == "REJECTED"
    with pytest.raises(Conflict):
        await repo.create_ticket(run_id, PARAMS)
    await repo.transition(
        run_id, "SUCCEEDED", diagnosis={"summary": "diagnosis kept; user rejected"}
    )
    assert (await repo.get_run(run_id))["ticket_id"] is None


@pytest.mark.asyncio
async def test_success_response_loss_replays_same_ticket_even_after_cancel(repo):
    run_id, _ = await prepare(repo, approve=True)

    def lose_response(point):
        if point == "ticket.after_commit":
            raise ConnectionError("simulated lost tool response")

    repo.fault_hook = lose_response
    with pytest.raises(ConnectionError):
        await repo.create_ticket(run_id, PARAMS)
    repo.fault_hook = None
    await repo.request_cancel(run_id)
    restarted = Repository(repo.path)
    await restarted.init()
    try:
        receipt = await restarted.create_ticket(run_id, PARAMS)
        assert receipt == await restarted.create_ticket(run_id, PARAMS)
        assert (await restarted.get_run(run_id))["ticket_id"] == receipt["ticket_id"]
        values = await counts(restarted)
        assert values["tickets"] == values["tool_receipts"] == 1
        assert (
            len([e for e in await restarted.events(run_id) if e["kind"] == "ticket.created"]) == 1
        )
        changed = {**PARAMS, "draft": {**PARAMS["draft"], "title": "Conflicting retry"}}
        with pytest.raises(Conflict):
            await restarted.create_ticket(run_id, changed)
    finally:
        await restarted.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("point", ["ticket.after_insert", "ticket.before_commit"])
async def test_ticket_receipt_and_event_rollback_as_one_transaction(repo, point):
    run_id, _ = await prepare(repo, approve=True)
    before = await counts(repo)

    def abort(actual):
        if actual == point:
            raise RuntimeError("injected transaction fault")

    repo.fault_hook = abort
    with pytest.raises(RuntimeError, match="injected"):
        await repo.create_ticket(run_id, PARAMS)
    repo.fault_hook = None
    assert await counts(repo) == before
    assert (await repo.get_run(run_id))["operation"]["status"] == "APPROVED"
    await repo.create_ticket(run_id, PARAMS)
    values = await counts(repo)
    assert values["tickets"] == values["tool_receipts"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("ticket_first", [True, False])
async def test_cancel_and_ticket_are_arbitrated_by_committed_write_order(repo, ticket_first):
    run_id, _ = await prepare(repo, approve=True)
    if ticket_first:
        receipt = await repo.create_ticket(run_id, PARAMS)
        await repo.request_cancel(run_id)
        assert (await repo.get_run(run_id))["ticket_id"] == receipt["ticket_id"]
        assert await repo.create_ticket(run_id, PARAMS) == receipt
    else:
        await repo.request_cancel(run_id)
        with pytest.raises(Conflict, match="cancelled"):
            await repo.create_ticket(run_id, PARAMS)
        assert (await counts(repo))["tickets"] == 0


@pytest.mark.asyncio
async def test_ticket_cannot_reference_unobtained_evidence(repo):
    run, _ = await repo.create_run(REQUEST, "no-evidence")
    operation = await repo.freeze_operation(run["id"], "interrupt", PARAMS)
    await decide(repo, run["id"], operation)
    with pytest.raises(ValueError, match="not obtained"):
        await repo.create_ticket(run["id"], PARAMS)
    assert (await counts(repo))["tickets"] == 0


@pytest.mark.asyncio
async def test_cancel_is_idempotent_and_terminal_results_ignore_late_updates(repo):
    run, _ = await repo.create_run(REQUEST, "terminal")
    await repo.request_cancel(run["id"])
    await repo.request_cancel(run["id"])
    final = await repo.transition(run["id"], "CANCELLED", error={"code": "user_cancelled"})
    late = await repo.transition(run["id"], "SUCCEEDED", diagnosis={"summary": "late result"})
    assert late == final
    assert late["diagnosis"] is None
    assert await repo.pending_runs() == []
    assert (
        len([e for e in await repo.events(run["id"]) if e["kind"] == "run.cancel_requested"]) == 1
    )


@pytest.mark.asyncio
async def test_cursor_reads_are_bounded_ordered_and_have_no_side_effects(repo):
    run_id, operation = await prepare(repo)
    await decide(repo, run_id, operation)
    first = await repo.events(run_id, limit=2)
    rest = await repo.events(run_id, after=first[-1]["event_id"])
    together = first + rest
    assert len({e["event_id"] for e in together}) == len(together)
    assert [e["event_id"] for e in together] == sorted(e["event_id"] for e in together)
    before = await counts(repo)
    assert await repo.events(run_id) == together
    assert await counts(repo) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("point", ["freeze.before_commit", "decision.before_commit"])
async def test_approval_state_command_and_event_are_atomic(repo, point):
    run, _ = await repo.create_run(REQUEST, point)
    run_id = run["id"]
    operation = None
    if point == "decision.before_commit":
        operation = await repo.freeze_operation(run_id, "interrupt", PARAMS)
    before = await counts(repo)

    def abort(actual):
        if actual == point:
            raise RuntimeError("approval transaction fault")

    repo.fault_hook = abort
    with pytest.raises(RuntimeError, match="transaction fault"):
        if operation:
            await decide(repo, run_id, operation)
        else:
            await repo.freeze_operation(run_id, "interrupt", PARAMS)
    repo.fault_hook = None
    assert await counts(repo) == before
    current = await repo.get_run(run_id)
    if operation:
        assert current["operation"]["decision"] is None
    else:
        assert current["operation"] is None
        assert current["status"] == "QUEUED"


@pytest.mark.asyncio
async def test_tool_and_active_budgets_persist_and_waiting_time_is_excluded(repo):
    run_id, _ = await prepare(repo)
    assert await repo.consume_tool_call(run_id, max_calls=1) == 1
    with pytest.raises(BudgetExceeded, match="tool call"):
        await repo.consume_tool_call(run_id, max_calls=1)
    started = await repo.begin_active(run_id)
    with pytest.raises(Conflict, match="settled"):
        await repo.begin_active(run_id)
    stop = datetime.fromisoformat(started) + timedelta(seconds=5)
    assert await repo.finish_active(run_id, stop.isoformat()) == 5
    # No span is active while a person is considering the frozen proposal.
    assert await repo.finish_active(run_id, (stop + timedelta(hours=2)).isoformat()) == 5
    restarted = Repository(repo.path)
    await restarted.init()
    try:
        current = await restarted.get_run(run_id)
        assert current["active_seconds"] == 5
        assert current["tool_calls"] == 1
        with pytest.raises(BudgetExceeded):
            await restarted.add_active_seconds(run_id, 176)
        assert (await restarted.get_run(run_id))["active_seconds"] == 181
    finally:
        await restarted.close()


@pytest.mark.asyncio
async def test_concurrent_ticket_attempts_create_one_effect_and_one_receipt(repo):
    run_id, _ = await prepare(repo, approve=True)
    second = Repository(repo.path)
    await second.init()
    try:
        left, right = await asyncio.gather(
            repo.create_ticket(run_id, PARAMS), second.create_ticket(run_id, PARAMS)
        )
        assert left == right
        values = await counts(repo)
        assert values["tickets"] == values["tool_receipts"] == 1
        assert len([e for e in await repo.events(run_id) if e["kind"] == "ticket.created"]) == 1
    finally:
        await second.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "point", ["run.before_commit", "transition.before_commit", "cancel.before_commit"]
)
async def test_run_state_intent_and_corresponding_events_are_atomic(repo, point):
    run = None
    if point != "run.before_commit":
        run, _ = await repo.create_run(REQUEST, point)
    before = await counts(repo)

    def abort(actual):
        if actual == point:
            raise RuntimeError("state transaction fault")

    repo.fault_hook = abort
    with pytest.raises(RuntimeError, match="transaction fault"):
        if point == "run.before_commit":
            await repo.create_run(REQUEST, point)
        elif point == "transition.before_commit":
            await repo.transition(run["id"], "RUNNING")
        else:
            await repo.request_cancel(run["id"])
    repo.fault_hook = None
    assert await counts(repo) == before
    if run:
        current = await repo.get_run(run["id"])
        assert current["status"] == "QUEUED"
        assert current["cancel_requested"] is False
    else:
        assert await repo.list_runs() == []


@pytest.mark.asyncio
async def test_unknown_model_usage_is_explicit_in_the_public_summary(repo):
    run, _ = await repo.create_run(REQUEST, "usage")
    settings = Settings(_env_file=None, api_key=None, local_dir=repo.path.parent)
    ledger = CallLedger(repo.path, settings)
    call_id = ledger.reserve(run["id"], "runtime", "offline-model", {"content": "test"})
    ledger.finish(call_id, status="FAILED", duration=0.01, error_type="TimeoutError")
    usage = (await repo.get_run(run["id"]))["usage"]
    assert usage["model_calls"] == usage["unknown_usage_calls"] == 1
    assert usage["known_cost_cny"] == 0
    assert usage["unreconciled_reservations_cny"] > 0


@pytest.mark.asyncio
async def test_inflight_active_span_survives_restart_and_settles_only_once(repo):
    run, _ = await repo.create_run(REQUEST, "active-restart")
    started = await repo.begin_active(run["id"])
    restarted = Repository(repo.path)
    await restarted.init()
    try:
        assert (await restarted.get_run(run["id"]))["active_started_at"] == started
        ended = datetime.fromisoformat(started) + timedelta(seconds=3)
        assert await restarted.finish_active(run["id"], ended.isoformat()) == 3
        assert await restarted.finish_active(run["id"], ended.isoformat()) == 3
        assert (await restarted.get_run(run["id"]))["active_started_at"] is None
    finally:
        await restarted.close()
