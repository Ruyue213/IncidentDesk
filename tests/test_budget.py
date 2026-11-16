"""Offline checks of durable accounting, including prior-charge fault fixtures."""

import sqlite3

import pytest

from incident_desk.agent.budget import BudgetExceeded, CallLedger
from incident_desk.settings import Settings


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    return Settings(_env_file=None, api_key=None, local_dir=tmp_path)


def reserve(ledger, run_id="run-a", scope="runtime"):
    return ledger.reserve(run_id, scope, "offline-model", {"messages": ["inspect evidence"]})


def seed_prior_charge(ledger, *, run_id, scope, charge):
    """Represent a previously reconciled charge without making thousands of requests."""
    call_id = reserve(ledger, run_id, scope)
    with sqlite3.connect(ledger.path) as db:
        db.execute(
            "UPDATE model_calls SET status='SUCCEEDED',known_cost_cny=? WHERE id=?",
            (charge, call_id),
        )


def test_default_limits_match_the_project_contract(settings):
    assert settings.model_call_limit == 8
    assert settings.run_budget_cny == 2
    assert settings.project_budget_cny == 300
    assert settings.smoke_budget_cny == 5


def test_eighth_request_is_allowed_ninth_is_rejected_after_restart(settings):
    ledger = CallLedger(settings.app_db, settings)
    for _ in range(8):
        reserve(ledger)

    restarted = CallLedger(settings.app_db, settings)
    with pytest.raises(BudgetExceeded, match="count limit"):
        reserve(restarted)
    assert len(restarted.rows()) == 8
    assert all(row["known_cost_cny"] is None for row in restarted.rows())
    assert all(row["reserved_cny"] > 0 for row in restarted.rows())
    # A distinct run has its own count while sharing the project's durable costs.
    reserve(restarted, "run-b")


@pytest.mark.parametrize("status,error", [("SUCCEEDED", None), ("FAILED", "TimeoutError")])
def test_missing_usage_retains_reservation_across_restart(settings, status, error):
    settings = settings.model_copy(update={"run_budget_cny": 0.03})
    ledger = CallLedger(settings.app_db, settings)
    call_id = reserve(ledger)
    original_reservation = ledger.rows()[0]["reserved_cny"]
    ledger.finish(call_id, status=status, duration=0.01, usage=None, error_type=error)

    restarted = CallLedger(settings.app_db, settings)
    row = restarted.rows()[0]
    assert row["status"] == status
    assert row["known_cost_cny"] is None
    assert row["input_tokens"] is None
    assert row["output_tokens"] is None
    assert row["reserved_cny"] == original_reservation
    with pytest.raises(BudgetExceeded, match="run cost limit"):
        reserve(restarted)
    assert len(restarted.rows()) == 1


def test_cached_input_is_accounted_and_duplicate_finish_does_not_rewrite(settings):
    ledger = CallLedger(settings.app_db, settings)
    call_id = reserve(ledger)
    ledger.finish(
        call_id,
        status="SUCCEEDED",
        duration=0.12,
        usage={
            "input_tokens": 1000,
            "output_tokens": 50,
            "input_token_details": {"cache_read": 600},
        },
        returned_model="provider-returned-model",
    )
    row = ledger.rows()[0]
    assert row["known_cost_cny"] == pytest.approx(0.001224)
    assert row["input_tokens"] == 1000
    assert row["output_tokens"] == 50
    assert row["cache_read_tokens"] == 600
    assert row["returned_model"] == "provider-returned-model"
    assert row["finished_at"]
    ledger.finish(call_id, status="FAILED", duration=9, error_type="LateFailure")
    assert ledger.rows()[0] == row


def test_missing_cache_breakdown_is_conservatively_uncached(settings):
    ledger = CallLedger(settings.app_db, settings)
    call_id = reserve(ledger)
    ledger.finish(
        call_id,
        status="SUCCEEDED",
        duration=0.01,
        usage={"input_tokens": 1000, "output_tokens": 50},
    )
    assert ledger.rows()[0]["known_cost_cny"] == pytest.approx(0.0024)


@pytest.mark.parametrize(
    "usage",
    [
        {},
        {"input_tokens": 1000},
        {"output_tokens": 50},
        {"input_tokens": None, "output_tokens": 50},
    ],
)
def test_partial_usage_remains_unknown(settings, usage):
    ledger = CallLedger(settings.app_db, settings)
    call_id = reserve(ledger)
    ledger.finish(call_id, status="SUCCEEDED", duration=0.01, usage=usage)
    assert ledger.rows()[0]["known_cost_cny"] is None


@pytest.mark.parametrize(
    "prior_run,prior_scope,new_run,new_scope,charge,message",
    [
        ("target", "runtime", "target", "runtime", 1.99, "run cost limit"),
        ("previous", "runtime", "target", "runtime", 299.99, "project cost limit"),
        ("previous", "CP02", "target", "CP02", 4.99, "first smoke cost limit"),
    ],
)
def test_default_money_limits_include_persisted_prior_charges(
    settings, prior_run, prior_scope, new_run, new_scope, charge, message
):
    ledger = CallLedger(settings.app_db, settings)
    seed_prior_charge(ledger, run_id=prior_run, scope=prior_scope, charge=charge)

    restarted = CallLedger(settings.app_db, settings)
    with pytest.raises(BudgetExceeded, match=message):
        reserve(restarted, new_run, new_scope)
    assert len(restarted.rows()) == 1


def test_smoke_scope_is_aggregate_across_runs_but_does_not_consume_unrelated_scope(settings):
    ledger = CallLedger(settings.app_db, settings)
    seed_prior_charge(ledger, run_id="older-smoke", scope="CP02", charge=4.99)
    with pytest.raises(BudgetExceeded, match="first smoke cost limit"):
        reserve(ledger, "new-smoke", "CP02")
    reserve(ledger, "normal-run", "runtime")
    assert len(ledger.rows()) == 2


def test_rejected_oversized_request_does_not_create_a_charge(settings):
    ledger = CallLedger(settings.app_db, settings)
    with pytest.raises(BudgetExceeded, match="64KB"):
        ledger.reserve("run", "runtime", "offline-model", {"content": "界" * 22000})
    assert ledger.rows() == []


@pytest.mark.parametrize(
    "usage",
    [
        {"input_tokens": -1000, "output_tokens": 0},
        {"input_tokens": 1000, "output_tokens": -500},
        {"input_tokens": True, "output_tokens": 0},
    ],
)
def test_invalid_token_counts_never_create_reconciled_credit(settings, usage):
    ledger = CallLedger(settings.app_db, settings)
    call_id = reserve(ledger)
    ledger.finish(call_id, status="SUCCEEDED", duration=0.01, usage=usage)
    assert ledger.rows()[0]["known_cost_cny"] is None
    assert ledger.rows()[0]["input_tokens"] is None
    assert ledger.rows()[0]["output_tokens"] is None


@pytest.mark.parametrize("cache_read", [-1, 1001, True, "600"])
def test_invalid_cache_usage_remains_unreconciled(settings, cache_read):
    ledger = CallLedger(settings.app_db, settings)
    call_id = reserve(ledger)
    ledger.finish(
        call_id,
        status="SUCCEEDED",
        duration=0.01,
        usage={
            "input_tokens": 1000,
            "output_tokens": 50,
            "input_token_details": {"cache_read": cache_read},
        },
    )
    row = ledger.rows()[0]
    assert row["known_cost_cny"] is None
    assert row["reserved_cny"] > 0
