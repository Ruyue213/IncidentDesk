"""Thirty fresh-process reliability experiments using only a network-blocked fake model."""

import argparse
import asyncio
import json
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

from incident_desk.__main__ import request_for
from incident_desk.settings import ROOT
from incident_desk.storage import Conflict, Repository

SCENARIOS = (
    "interrupt_before_operation",
    "decision_before_resume",
    "ticket_committed_response_lost",
    "graph_complete_before_application",
    "reject",
    "queued_cancel",
    "approved_cancel_first",
    "ticket_first_cancel",
    "two_run_isolation",
    "stale_conflicting_approval",
)

BOOTSTRAP = r"""
import asyncio, json, socket, sys
from pathlib import Path
from incident_desk.settings import Settings
from incident_desk.agent.reliability_model import make_reliability_model
from incident_desk.worker.runtime import Worker
def forbid_network(*args, **kwargs):
    raise RuntimeError("network forbidden in deterministic reliability process")
socket.socket.connect = forbid_network
socket.create_connection = forbid_network
settings = Settings(_env_file=None, api_key=None, local_dir=Path(sys.argv[1]),
                    **json.loads(sys.argv[3]))
asyncio.run(Worker(settings, model_factory=make_reliability_model,
                   crash_at=sys.argv[2] or None).run(once=True))
"""


async def execute_worker(
    directory: Path, crash_at=None, *, expected=0, settings_overrides=None
) -> dict:
    environment = dict(os.environ)
    for name in ("DEEPSEEK_API_KEY", "OPENAI_API_KEY", "LANGSMITH_API_KEY"):
        environment.pop(name, None)
    environment["LANGSMITH_TRACING"] = "false"
    environment["LANGCHAIN_TRACING_V2"] = "false"
    started = time.monotonic()
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        BOOTSTRAP,
        str(directory),
        crash_at or "",
        json.dumps(settings_overrides or {}),
        cwd=ROOT,
        env=environment,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=30)
    except TimeoutError:
        process.kill()
        await process.communicate()
        raise
    record = {
        "crash_at": crash_at,
        "exit_code": process.returncode,
        "expected_exit_code": expected,
        "elapsed_seconds": time.monotonic() - started,
        "stdout": stdout.decode(errors="replace"),
        "stderr": stderr.decode(errors="replace"),
    }
    if process.returncode != expected:
        raise RuntimeError(
            f"worker exit {process.returncode}, expected {expected}: {record['stderr']}"
        )
    return record


async def decide(repo, run_id, decision="approve", **overrides):
    operation = (await repo.get_run(run_id))["operation"]
    if operation is None:
        raise AssertionError("worker did not expose its persisted interrupt")
    values = {
        "interrupt_id": operation["interrupt_id"],
        "operation_id": operation["id"],
        "draft_version": operation["draft_version"],
        "params_digest": operation["params_digest"],
        "decision": decision,
    }
    return await repo.decide(run_id, **(values | overrides))


def database_counts(path: Path, run_ids: list[str]) -> dict:
    with sqlite3.connect(path) as db:
        tickets = db.execute("SELECT COUNT(*) FROM tickets").fetchone()[0]
        receipts = db.execute("SELECT COUNT(*) FROM tool_receipts").fetchone()[0]
        effects = db.execute("SELECT COUNT(*) FROM events WHERE kind='ticket.created'").fetchone()[
            0
        ]
        unauthorized = db.execute(
            "SELECT COUNT(*) FROM tickets t LEFT JOIN operations o ON t.operation_id=o.id "
            "LEFT JOIN tool_receipts r ON r.ticket_id=t.id "
            "WHERE o.id IS NULL OR COALESCE(o.decision,'')!='approve' OR r.ticket_id IS NULL "
            "OR r.params_digest!=o.params_digest OR t.run_id!=o.run_id"
        ).fetchone()[0]
        duplicates = db.execute(
            "SELECT COALESCE(SUM(n-1),0) FROM "
            "(SELECT COUNT(*) n FROM tickets GROUP BY run_id HAVING COUNT(*)>1)"
        ).fetchone()[0]
        per_run = {
            run_id: db.execute("SELECT COUNT(*) FROM tickets WHERE run_id=?", (run_id,)).fetchone()[
                0
            ]
            for run_id in run_ids
        }
    return {
        "tickets": tickets,
        "receipts": receipts,
        "effect_events": effects,
        "unapproved_effects": unauthorized,
        "duplicate_tickets": duplicates,
        "tickets_per_run": per_run,
    }


async def experiment(scenario: str, repeat: int) -> dict:
    record = {
        "scenario": scenario,
        "repeat": repeat,
        "model": "incident-desk-scripted-v1",
        "model_api_calls": 0,
        "processes": [],
        "status": "failed",
    }
    with tempfile.TemporaryDirectory(prefix="incident-desk-fault-") as temporary:
        directory = Path(temporary)
        repo = Repository(directory / "app.db")
        await repo.init()
        run_ids = []
        started = time.monotonic()

        async def child(crash_at=None):
            outcome = await execute_worker(directory, crash_at, expected=86 if crash_at else 0)
            record["processes"].append(outcome)

        async def submit(key):
            request = request_for("dev-001")
            request["question"] += " 请创建人工交接工单。"
            run, _ = await repo.create_run(request, key)
            run_ids.append(run["id"])
            return run["id"]

        try:
            run_id = await submit("first")
            expected = [1]
            expected_status = ["SUCCEEDED"]
            if scenario == "interrupt_before_operation":
                await child("after_graph_interrupt_before_operation")
                assert (await repo.get_run(run_id))["operation"] is None
                started = time.monotonic()
                await child()
                await decide(repo, run_id)
                await child()
            elif scenario == "queued_cancel":
                expected, expected_status = [0], ["CANCELLED"]
                await repo.request_cancel(run_id)
                await child()
            else:
                if scenario == "two_run_isolation":
                    other = await submit("second")
                await child()
                before = await repo.get_run(run_id)
                assert before["status"] == "WAITING_APPROVAL"
                assert before["ticket_id"] is None
                if scenario == "reject":
                    expected = [0]
                    await decide(repo, run_id, "reject")
                elif scenario == "stale_conflicting_approval":
                    rejected = 0
                    for changes in (
                        {"interrupt_id": "previous-interrupt"},
                        {"draft_version": 2},
                        {"operation_id": "different-operation"},
                        {"params_digest": "wrong-digest"},
                    ):
                        try:
                            await decide(repo, run_id, **changes)
                        except Conflict:
                            rejected += 1
                    assert rejected == 4
                    await decide(repo, run_id)
                    await decide(repo, run_id)
                    try:
                        await decide(repo, run_id, "reject")
                    except Conflict:
                        rejected += 1
                    assert rejected == 5
                    record["rejected_invalid_decisions"] = rejected
                else:
                    await decide(repo, run_id)
                if scenario == "two_run_isolation":
                    await decide(repo, other, "reject")
                    expected, expected_status = [1, 0], ["SUCCEEDED", "SUCCEEDED"]
                if scenario == "approved_cancel_first":
                    expected, expected_status = [0], ["CANCELLED"]
                    await repo.request_cancel(run_id)
                crash = {
                    "decision_before_resume": "after_decision_before_resume",
                    "ticket_committed_response_lost": "after_ticket_commit",
                    "graph_complete_before_application": "after_graph_complete_before_app",
                    "ticket_first_cancel": "after_ticket_commit",
                }.get(scenario)
                if crash:
                    await child(crash)
                    record["state_at_crash"] = await repo.get_run(run_id)
                    started = time.monotonic()
                if scenario == "ticket_first_cancel":
                    expected_status = ["CANCELLED"]
                    assert (await repo.get_run(run_id))["ticket_id"]
                    await repo.request_cancel(run_id)
                await child()
            record["recovery_seconds"] = time.monotonic() - started
            states = [await repo.get_run(run_id) for run_id in run_ids]
            record["runs"] = states
            counts = database_counts(repo.path, run_ids)
            record["counts"] = counts
            actual_tickets = [counts["tickets_per_run"][run_id] for run_id in run_ids]
            assert actual_tickets == expected, (actual_tickets, expected)
            assert [state["status"] for state in states] == expected_status, states
            assert counts["tickets"] == counts["receipts"] == counts["effect_events"]
            assert counts["duplicate_tickets"] == counts["unapproved_effects"] == 0
            assert all(state["diagnosis"] or state["status"] == "CANCELLED" for state in states)
            assert all(state["active_started_at"] is None for state in states)
            # A further fresh worker must not re-run terminal graphs or append effects.
            before = [(state["usage"]["model_calls"], state["ticket_id"]) for state in states]
            await child()
            after = [await repo.get_run(run_id) for run_id in run_ids]
            assert [
                (state["usage"]["model_calls"], state["ticket_id"]) for state in after
            ] == before
            record["terminal_replay_unchanged"] = True
            record["status"] = "passed"
        except Exception as error:
            record["error_type"] = type(error).__name__
            record["error"] = str(error)
            record["runs"] = [await repo.get_run(run_id) for run_id in run_ids]
            record["counts"] = database_counts(repo.path, run_ids)
        finally:
            await repo.close()
    return record


async def main(args):
    args.output.parent.mkdir(parents=True, exist_ok=True)
    semaphore = asyncio.Semaphore(args.workers)
    records = []

    async def one(scenario, repeat):
        async with semaphore:
            record = await experiment(scenario, repeat)
            records.append(record)
            with args.output.open("a") as stream:
                stream.write(json.dumps(record, ensure_ascii=False) + "\n")
            print(
                json.dumps({"scenario": scenario, "repeat": repeat, "status": record["status"]}),
                flush=True,
            )

    # Each invocation starts a new run log; no selective preservation of best attempts.
    args.output.write_text("")
    await asyncio.gather(
        *(one(scenario, repeat) for repeat in range(1, args.repeats + 1) for scenario in SCENARIOS)
    )
    summary = {
        "experiments": len(records),
        "passed": sum(r["status"] == "passed" for r in records),
        "duplicate_tickets": sum(r["counts"]["duplicate_tickets"] for r in records),
        "unapproved_effects": sum(r["counts"]["unapproved_effects"] for r in records),
        "model_api_calls": 0,
        "raw_results": str(args.output),
        "scope": "deterministic subprocess reliability, not model quality or production SLA",
    }
    args.output.with_suffix(".summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2)
    )
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    return 0 if summary["passed"] == summary["experiments"] else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--output", type=Path, default=ROOT / "docs/evidence/CP09/faults.jsonl")
    options = parser.parse_args()
    if not 1 <= options.repeats <= 10 or not 1 <= options.workers <= 4:
        parser.error("repeats must be 1–10 and workers must be 1–4")
    raise SystemExit(asyncio.run(main(options)))
