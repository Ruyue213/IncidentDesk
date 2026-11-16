"""Durable pre-call reservations. Unreconciled calls retain their entire reservation."""

import hashlib
import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from incident_desk.settings import Settings


class BudgetExceeded(RuntimeError):
    """The next call cannot be covered by the remaining durable budget."""


class CallLedger:
    def __init__(self, path: Path, settings: Settings):
        self.path = path
        self.settings = settings
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS model_calls (
                    id TEXT PRIMARY KEY, run_id TEXT NOT NULL, scope TEXT NOT NULL,
                    started_at TEXT NOT NULL, finished_at TEXT, status TEXT NOT NULL,
                    requested_model TEXT NOT NULL, returned_model TEXT,
                    request_sha256 TEXT NOT NULL, input_bytes INTEGER NOT NULL,
                    reserved_cny REAL NOT NULL, known_cost_cny REAL,
                    input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER,
                    duration_seconds REAL, error_type TEXT,
                    model_profile_version INTEGER, pricing_json TEXT
                );
                CREATE INDEX IF NOT EXISTS model_calls_run ON model_calls(run_id);
            """)
            db.execute("BEGIN IMMEDIATE")
            columns = {row[1] for row in db.execute("PRAGMA table_info(model_calls)")}
            for name, declaration in (
                ("model_profile_version", "INTEGER"),
                ("pricing_json", "TEXT"),
            ):
                if name not in columns:
                    db.execute(f"ALTER TABLE model_calls ADD COLUMN {name} {declaration}")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=10000")
        try:
            with db:
                yield db
        finally:
            db.close()

    def reserve(self, run_id: str, scope: str, model: str, request: dict) -> str:
        payload = json.dumps(request, ensure_ascii=False, default=str).encode()
        if len(payload) > self.settings.max_request_bytes:
            limit = self.settings.max_request_bytes
            label = "64KB" if limit == 64000 else f"{limit} bytes"
            raise BudgetExceeded(f"model input exceeds the {label} bounded request limit")
        # Conservative peak pricing frozen with the run's model profile.
        # Byte count + protocol allowance bounds tokens for this text-only application.
        prices = {
            "input": self.settings.input_per_million_cny,
            "output": self.settings.output_per_million_cny,
            "cache_read": self.settings.cache_read_per_million_cny,
        }
        reserved = (
            (len(payload) + 4096) * prices["input"]
            + self.settings.max_output_tokens * prices["output"]
        ) / 1e6
        call_id = str(uuid.uuid4())
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            all_cost = db.execute(
                "SELECT COALESCE(SUM(COALESCE(known_cost_cny,reserved_cny)),0) FROM model_calls"
            ).fetchone()[0]
            count, run_cost = db.execute(
                "SELECT COUNT(*), COALESCE(SUM(COALESCE(known_cost_cny,reserved_cny)),0) "
                "FROM model_calls WHERE run_id=?",
                (run_id,),
            ).fetchone()
            scope_cost = db.execute(
                "SELECT COALESCE(SUM(COALESCE(known_cost_cny,reserved_cny)),0) "
                "FROM model_calls WHERE scope=?",
                (scope,),
            ).fetchone()[0]
            if count >= self.settings.model_call_limit:
                raise BudgetExceeded("model call count limit reached")
            if run_cost + reserved > self.settings.run_budget_cny:
                raise BudgetExceeded("run cost limit reached")
            if all_cost + reserved > self.settings.project_budget_cny:
                raise BudgetExceeded("project cost limit reached")
            if scope in {"CP02", "model-connection-test"} and (
                scope_cost + reserved > self.settings.smoke_budget_cny
            ):
                raise BudgetExceeded("first smoke cost limit reached")
            db.execute(
                "INSERT INTO model_calls "
                "(id,run_id,scope,started_at,status,requested_model,request_sha256,"
                "input_bytes,reserved_cny,model_profile_version,pricing_json) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    call_id,
                    run_id,
                    scope,
                    datetime.now(UTC).isoformat(),
                    "IN_FLIGHT",
                    model,
                    hashlib.sha256(payload).hexdigest(),
                    len(payload),
                    reserved,
                    self.settings.model_profile_version,
                    json.dumps(prices),
                ),
            )
        return call_id

    def finish(
        self,
        call_id: str,
        *,
        status: str,
        duration: float,
        usage: dict | None = None,
        returned_model: str | None = None,
        error_type: str | None = None,
    ) -> None:
        usage = usage or {}
        inp = usage.get("input_tokens")
        out = usage.get("output_tokens")
        cache = (usage.get("input_token_details") or {}).get("cache_read")
        # Missing cache usage is conservatively priced as uncached input.
        cost = None
        valid_tokens = all(type(value) is int and value >= 0 for value in (inp, out))
        valid_cache = cache is None or (type(cache) is int and 0 <= cache <= (inp or 0))
        with self.connect() as db:
            row = db.execute(
                "SELECT pricing_json FROM model_calls WHERE id=?", (call_id,)
            ).fetchone()
        prices = (
            json.loads(row["pricing_json"])
            if row and row["pricing_json"]
            else {"input": 2, "output": 8, "cache_read": 0.04}
        )
        if valid_tokens and valid_cache:
            hits = min(max(cache or 0, 0), inp)
            cost = (
                (inp - hits) * prices["input"]
                + hits * prices["cache_read"]
                + out * prices["output"]
            ) / 1e6
        else:
            # Untrusted/partial metadata must not become negative or misleading totals.
            inp = out = cache = None
        with self.connect() as db:
            db.execute(
                "UPDATE model_calls SET status=?,finished_at=?,duration_seconds=?,"
                "input_tokens=?,output_tokens=?,cache_read_tokens=?,known_cost_cny=?,"
                "returned_model=?,error_type=? WHERE id=? AND status='IN_FLIGHT'",
                (
                    status,
                    datetime.now(UTC).isoformat(),
                    duration,
                    inp,
                    out,
                    cache,
                    cost,
                    returned_model,
                    error_type,
                    call_id,
                ),
            )

    def rows(self, scope: str | None = None) -> list[dict]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM model_calls"
                + (" WHERE scope=?" if scope else "")
                + " ORDER BY started_at",
                (scope,) if scope else (),
            ).fetchall()
        return [dict(row) for row in rows]
