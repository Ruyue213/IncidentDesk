"""Single-host business repository; every effect is serialized by a short write transaction."""

import hashlib
import json
import math
import uuid
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import (
    Boolean,
    Column,
    Float,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    delete,
    event,
    func,
    insert,
    or_,
    select,
    update,
)
from sqlalchemy.ext.asyncio import create_async_engine

from incident_desk.agent.budget import BudgetExceeded
from incident_desk.schemas import (
    RunInput,
    ServiceActionRequest,
    TicketRequest,
    snapshot,
    validate_references,
)

TERMINAL = frozenset({"SUCCEEDED", "FAILED", "CANCELLED"})
STATUSES = TERMINAL | {"QUEUED", "RUNNING", "WAITING_APPROVAL"}
metadata = MetaData()
runs = Table(
    "runs",
    metadata,
    Column("id", String, primary_key=True),
    Column("idempotency_key", String, nullable=False, unique=True),
    Column("request_json", Text, nullable=False),
    Column("status", String, nullable=False),
    Column("diagnosis_json", Text),
    Column("error_json", Text),
    Column("cancel_requested", Boolean, nullable=False, default=False),
    Column("active_seconds", Float, nullable=False, default=0),
    Column("active_started_at", String),
    Column("tool_calls", Integer, nullable=False, default=0),
    Column("report_repairs", Integer, nullable=False, default=0, server_default="0"),
    Column("revision", Integer, nullable=False, default=0),
    Column("created_at", String, nullable=False),
    Column("updated_at", String, nullable=False),
)
run_claims = Table(
    "worker_run_claims",
    metadata,
    Column("run_id", String, ForeignKey("runs.id"), primary_key=True),
    Column("worker_id", String, nullable=False),
    Column("claimed_at", String, nullable=False),
)
operations = Table(
    "operations",
    metadata,
    Column("id", String, primary_key=True),
    Column("run_id", String, ForeignKey("runs.id"), nullable=False, unique=True),
    Column("interrupt_id", String, nullable=False),
    Column("draft_version", Integer, nullable=False),
    Column("params_digest", String, nullable=False),
    Column("params_json", Text, nullable=False),
    Column("decision", String),
    Column("status", String, nullable=False),
    Column("created_at", String, nullable=False),
    Column("decided_at", String),
    Column("kind", String, nullable=False, default="create_ticket", server_default="create_ticket"),
)
commands = Table(
    "commands",
    metadata,
    Column("id", String, primary_key=True),
    Column("run_id", String, ForeignKey("runs.id"), nullable=False),
    Column("kind", String, nullable=False),
    Column("payload_json", Text, nullable=False),
    Column("dedupe_key", String, nullable=False, unique=True),
    Column("status", String, nullable=False),
    Column("created_at", String, nullable=False),
)
tickets = Table(
    "tickets",
    metadata,
    Column("id", String, primary_key=True),
    Column("run_id", String, ForeignKey("runs.id"), nullable=False, unique=True),
    Column("operation_id", String, ForeignKey("operations.id"), nullable=False, unique=True),
    Column("params_json", Text, nullable=False),
    Column("created_at", String, nullable=False),
)
receipts = Table(
    "tool_receipts",
    metadata,
    Column("operation_id", String, ForeignKey("operations.id"), primary_key=True),
    Column("run_id", String, ForeignKey("runs.id"), nullable=False, unique=True),
    Column("ticket_id", String, ForeignKey("tickets.id"), nullable=False, unique=True),
    Column("params_digest", String, nullable=False),
    Column("created_at", String, nullable=False),
)
action_receipts = Table(
    "action_receipts",
    metadata,
    Column("operation_id", String, ForeignKey("operations.id"), primary_key=True),
    Column("run_id", String, ForeignKey("runs.id"), nullable=False, unique=True),
    Column("status", String, nullable=False),
    Column("result_json", Text, nullable=False),
    Column("created_at", String, nullable=False),
    Column("updated_at", String, nullable=False),
)
evidence = Table(
    "evidence_snapshots",
    metadata,
    Column("run_id", String, ForeignKey("runs.id"), primary_key=True),
    Column("evidence_id", String, primary_key=True),
    Column("snapshot_json", Text, nullable=False),
)
event_rows = Table(
    "events",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("run_id", String, ForeignKey("runs.id"), nullable=False, index=True),
    Column("event_key", String, nullable=False, unique=True),
    Column("kind", String, nullable=False),
    Column("payload_json", Text, nullable=False),
    Column("created_at", String, nullable=False),
    UniqueConstraint("run_id", "event_key"),
)


class Conflict(RuntimeError):
    """The requested operation conflicts with existing durable business state."""


class NotFound(LookupError):
    """A requested run or snapshot does not exist."""


def canonical(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def now() -> str:
    return datetime.now(UTC).isoformat()


def digest(value: dict) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


class Repository:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{self.path}")
        self.fault_hook: Callable[[str], None] | None = None

        @event.listens_for(self.engine.sync_engine, "connect")
        def sqlite_options(dbapi_connection, _connection_record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=10000")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    async def init(self):
        # API and worker can initialize the same old database concurrently.
        # Serialize both the conditional schema checks and their ALTER statements.
        async with self._write() as connection:
            await connection.run_sync(metadata.create_all)
            columns = (await connection.exec_driver_sql("PRAGMA table_info(runs)")).all()
            if "active_started_at" not in {column[1] for column in columns}:
                await connection.exec_driver_sql(
                    "ALTER TABLE runs ADD COLUMN active_started_at TEXT"
                )
            if "report_repairs" not in {column[1] for column in columns}:
                await connection.exec_driver_sql(
                    "ALTER TABLE runs ADD COLUMN report_repairs INTEGER NOT NULL DEFAULT 0"
                )
            columns = (await connection.exec_driver_sql("PRAGMA table_info(operations)")).all()
            if "kind" not in {column[1] for column in columns}:
                await connection.exec_driver_sql(
                    "ALTER TABLE operations ADD COLUMN kind TEXT NOT NULL DEFAULT 'create_ticket'"
                )
            await connection.exec_driver_sql("PRAGMA user_version=4")

    async def close(self):
        await self.engine.dispose()

    async def ping(self) -> bool:
        async with self.engine.connect() as connection:
            await connection.exec_driver_sql("SELECT 1")
        return True

    @asynccontextmanager
    async def _write(self):
        async with self.engine.connect() as connection:
            await connection.exec_driver_sql("BEGIN IMMEDIATE")
            try:
                yield connection
                await connection.commit()
            except BaseException:
                await connection.rollback()
                raise

    @asynccontextmanager
    async def _read(self):
        # Python's SQLite legacy mode does not begin a transaction for SELECT.
        # Explicit BEGIN keeps run, operation, receipt, and usage in one snapshot.
        async with self.engine.connect() as connection:
            await connection.exec_driver_sql("BEGIN")
            try:
                yield connection
            finally:
                await connection.rollback()

    def _fault(self, point):
        if self.fault_hook:
            self.fault_hook(point)

    async def _row(self, connection, run_id):
        row = (await connection.execute(select(runs).where(runs.c.id == run_id))).mappings().first()
        if row is None:
            raise NotFound("run not found")
        return dict(row)

    async def _operation(self, connection, run_id):
        row = (
            (await connection.execute(select(operations).where(operations.c.run_id == run_id)))
            .mappings()
            .first()
        )
        if row is None:
            return None
        result = dict(row)
        result["operation_id"] = result["id"]
        result["params"] = json.loads(result.pop("params_json"))
        return result

    async def _event(self, connection, run_id, kind, payload, event_key):
        await connection.execute(
            insert(event_rows).values(
                run_id=run_id,
                kind=kind,
                payload_json=canonical(payload),
                event_key=event_key,
                created_at=now(),
            )
        )

    async def _usage(self, connection, run_id):
        found = await connection.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='model_calls'"
        )
        if not found.first():
            return {
                "model_calls": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_read_tokens": 0,
                "known_cost_cny": 0,
                "unreconciled_reservations_cny": 0,
                "unknown_usage_calls": 0,
            }
        row = (
            (
                await connection.exec_driver_sql(
                    "SELECT COUNT(*) AS model_calls, "
                    "COALESCE(SUM(CASE WHEN known_cost_cny IS NOT NULL "
                    "THEN input_tokens ELSE 0 END),0) AS input_tokens, "
                    "COALESCE(SUM(CASE WHEN known_cost_cny IS NOT NULL "
                    "THEN output_tokens ELSE 0 END),0) AS output_tokens, "
                    "COALESCE(SUM(CASE WHEN known_cost_cny IS NOT NULL "
                    "THEN cache_read_tokens ELSE 0 END),0) AS cache_read_tokens, "
                    "COALESCE(SUM(CASE WHEN known_cost_cny IS NULL "
                    "THEN 1 ELSE 0 END),0) AS unknown_usage_calls, "
                    "COALESCE(SUM(known_cost_cny),0) AS known_cost_cny, "
                    "COALESCE(SUM(CASE WHEN known_cost_cny IS NULL "
                    "THEN reserved_cny ELSE 0 END),0) "
                    "AS unreconciled_reservations_cny FROM model_calls WHERE run_id=?",
                    (run_id,),
                )
            )
            .mappings()
            .one()
        )
        return dict(row)

    async def _public(self, connection, row):
        result = dict(row)
        result["request"] = json.loads(result.pop("request_json"))
        result["diagnosis"] = json.loads(result.pop("diagnosis_json") or "null")
        result["error"] = json.loads(result.pop("error_json") or "null")
        result.pop("idempotency_key")
        result["operation"] = await self._operation(connection, row["id"])
        result["ticket_id"] = (
            await connection.execute(select(tickets.c.id).where(tickets.c.run_id == row["id"]))
        ).scalar_one_or_none()
        result["usage"] = await self._usage(connection, row["id"])
        receipt = (
            (
                await connection.execute(
                    select(action_receipts).where(action_receipts.c.run_id == row["id"])
                )
            )
            .mappings()
            .first()
        )
        result["action_receipt"] = (
            None
            if receipt is None
            else {**dict(receipt), "result": json.loads(receipt["result_json"])}
        )
        if result["action_receipt"]:
            result["action_receipt"].pop("result_json", None)
        return result

    async def create_run(self, request: dict, key: str):
        if not isinstance(key, str) or not key.strip() or len(key) > 200:
            raise ValueError("Idempotency-Key must have 1–200 nonempty characters")
        request = RunInput.model_validate(request).model_dump(mode="json")
        encoded = canonical(request)
        async with self._write() as connection:
            existing = (
                (await connection.execute(select(runs).where(runs.c.idempotency_key == key)))
                .mappings()
                .first()
            )
            if existing:
                if existing["request_json"] != encoded:
                    raise Conflict("Idempotency-Key was already used with different parameters")
                return await self._public(connection, existing), False
            run_id = str(uuid.uuid4())
            stamp = now()
            await connection.execute(
                insert(runs).values(
                    id=run_id,
                    idempotency_key=key,
                    request_json=encoded,
                    status="QUEUED",
                    cancel_requested=False,
                    active_seconds=0,
                    tool_calls=0,
                    revision=0,
                    created_at=stamp,
                    updated_at=stamp,
                )
            )
            await connection.execute(
                insert(commands).values(
                    id=str(uuid.uuid4()),
                    run_id=run_id,
                    kind="START",
                    payload_json=encoded,
                    dedupe_key=f"start:{run_id}",
                    status="PENDING",
                    created_at=stamp,
                )
            )
            await self._event(
                connection, run_id, "run.queued", {"status": "QUEUED"}, f"state:{run_id}:0"
            )
            self._fault("run.before_commit")
            return await self._public(connection, await self._row(connection, run_id)), True

    async def get_run(self, run_id):
        async with self._read() as connection:
            return await self._public(connection, await self._row(connection, run_id))

    async def find_by_key(self, key):
        async with self._read() as connection:
            row = (
                (await connection.execute(select(runs).where(runs.c.idempotency_key == key)))
                .mappings()
                .first()
            )
            return await self._public(connection, row) if row else None

    async def list_runs(self, limit=50, offset=0):
        if not 1 <= limit <= 100 or offset < 0:
            raise ValueError("invalid pagination")
        async with self._read() as connection:
            rows = (
                (
                    await connection.execute(
                        select(runs).order_by(runs.c.created_at.desc()).limit(limit).offset(offset)
                    )
                )
                .mappings()
                .all()
            )
            return [await self._public(connection, row) for row in rows]

    async def pending_runs(self):
        async with self._read() as connection:
            rows = (
                (
                    await connection.execute(
                        select(runs)
                        .where(runs.c.status.not_in(TERMINAL))
                        .order_by(runs.c.created_at)
                    )
                )
                .mappings()
                .all()
            )
            return [await self._public(connection, row) for row in rows]

    async def reset_worker_claims(self):
        """Only call after acquiring the lifetime WorkerLock: prior owner is then gone."""
        async with self._write() as connection:
            await connection.execute(delete(run_claims))

    async def runnable_runs(self, limit=8):
        """Waiting for a person occupies no execution slot and cannot starve queued work."""
        async with self._read() as connection:
            rows = await connection.execute(
                select(runs.c.id)
                .outerjoin(operations, operations.c.run_id == runs.c.id)
                .outerjoin(run_claims, run_claims.c.run_id == runs.c.id)
                .where(runs.c.status.not_in(TERMINAL), run_claims.c.run_id.is_(None))
                .where(
                    or_(
                        runs.c.status != "WAITING_APPROVAL",
                        runs.c.cancel_requested,
                        operations.c.decision.is_not(None),
                    )
                )
                .order_by(runs.c.created_at, runs.c.id)
                .limit(limit)
            )
            return list(rows.scalars())

    async def claim_run(self, run_id, worker_id):
        """Atomic per-run claim; the single-worker lifetime lock remains mandatory."""
        async with self._write() as connection:
            row = await self._row(connection, run_id)
            if row["status"] in TERMINAL:
                return False
            existing = (
                await connection.execute(
                    select(run_claims.c.worker_id).where(run_claims.c.run_id == run_id)
                )
            ).scalar_one_or_none()
            if existing is not None:
                return False
            if row["status"] == "WAITING_APPROVAL" and not row["cancel_requested"]:
                operation = await self._operation(connection, run_id)
                if not operation or operation["decision"] is None:
                    return False
            await connection.execute(
                insert(run_claims).values(run_id=run_id, worker_id=worker_id, claimed_at=now())
            )
            if row["status"] == "QUEUED" and not row["cancel_requested"]:
                revision = row["revision"] + 1
                await connection.execute(
                    update(runs)
                    .where(runs.c.id == run_id)
                    .values(status="RUNNING", updated_at=now(), revision=revision)
                )
                await self._event(
                    connection,
                    run_id,
                    "run.state_changed",
                    {"status": "RUNNING"},
                    f"state:{run_id}:{revision}",
                )
            return True

    async def release_run_claim(self, run_id, worker_id):
        async with self._write() as connection:
            await connection.execute(
                delete(run_claims).where(
                    run_claims.c.run_id == run_id, run_claims.c.worker_id == worker_id
                )
            )

    async def release_worker_claims(self, worker_id):
        """After all local tasks drain, release even a claim cancelled before task startup."""
        async with self._write() as connection:
            await connection.execute(delete(run_claims).where(run_claims.c.worker_id == worker_id))

    async def workload_counts(self):
        async with self._read() as connection:
            counts = dict(
                (
                    await connection.execute(
                        select(runs.c.status, func.count()).group_by(runs.c.status)
                    )
                ).all()
            )
            claimed = (
                await connection.execute(select(func.count()).select_from(run_claims))
            ).scalar_one()
            return {
                "queued_count": counts.get("QUEUED", 0),
                "claimed_count": claimed,
                "waiting_approval_count": counts.get("WAITING_APPROVAL", 0),
                "running_count": counts.get("RUNNING", 0),
            }

    async def cancelled_run_ids(self, run_ids):
        if not run_ids:
            return set()
        async with self._read() as connection:
            rows = await connection.execute(
                select(runs.c.id).where(
                    runs.c.id.in_(run_ids),
                    runs.c.cancel_requested,
                )
            )
            return set(rows.scalars())

    async def transition(self, run_id, status, diagnosis=None, error=None):
        if status not in STATUSES:
            raise ValueError("invalid run status")
        async with self._write() as connection:
            row = await self._row(connection, run_id)
            if row["status"] in TERMINAL:
                return await self._public(connection, row)
            values = {"status": status, "updated_at": now(), "revision": row["revision"] + 1}
            if diagnosis is not None:
                values["diagnosis_json"] = canonical(diagnosis)
            if error is not None:
                values["error_json"] = (
                    canonical(error) if isinstance(error, dict) else json.dumps(error)
                )
            if status == row["status"] and diagnosis is None and error is None:
                return await self._public(connection, row)
            await connection.execute(update(runs).where(runs.c.id == run_id).values(**values))
            await self._event(
                connection,
                run_id,
                "run.state_changed",
                {"status": status},
                f"state:{run_id}:{values['revision']}",
            )
            self._fault("transition.before_commit")
            return await self._public(connection, await self._row(connection, run_id))

    async def request_cancel(self, run_id):
        async with self._write() as connection:
            row = await self._row(connection, run_id)
            if row["status"] in TERMINAL or row["cancel_requested"]:
                return await self._public(connection, row)
            await connection.execute(
                update(runs)
                .where(runs.c.id == run_id)
                .values(
                    cancel_requested=True,
                    updated_at=now(),
                )
            )
            await self._event(connection, run_id, "run.cancel_requested", {}, f"cancel:{run_id}")
            self._fault("cancel.before_commit")
            return await self._public(connection, await self._row(connection, run_id))

    async def save_evidence(self, run_id, item):
        normalized = snapshot(item)
        if not normalized.get("id") or not normalized.get("version"):
            raise ValueError("evidence requires a stable id and version")
        encoded = canonical(normalized)
        async with self._write() as connection:
            await self._row(connection, run_id)
            existing = (
                await connection.execute(
                    select(evidence.c.snapshot_json).where(
                        evidence.c.run_id == run_id,
                        evidence.c.evidence_id == normalized["id"],
                    )
                )
            ).scalar_one_or_none()
            if existing:
                if existing != encoded:
                    raise Conflict(
                        "this run already froze a different snapshot for the evidence ID"
                    )
                return normalized
            await connection.execute(
                insert(evidence).values(
                    run_id=run_id,
                    evidence_id=normalized["id"],
                    snapshot_json=encoded,
                )
            )
        return normalized

    async def get_evidence(self, run_id, evidence_id):
        async with self._read() as connection:
            await self._row(connection, run_id)
            value = (
                await connection.execute(
                    select(evidence.c.snapshot_json).where(
                        evidence.c.run_id == run_id,
                        evidence.c.evidence_id == evidence_id,
                    )
                )
            ).scalar_one_or_none()
            if value is None:
                raise NotFound("evidence snapshot not obtained by this run")
            return json.loads(value)

    async def list_evidence(self, run_id):
        async with self._read() as connection:
            await self._row(connection, run_id)
            values = (
                (
                    await connection.execute(
                        select(evidence.c.snapshot_json).where(
                            evidence.c.run_id == run_id,
                        )
                    )
                )
                .scalars()
                .all()
            )
            return [json.loads(value) for value in values]

    async def freeze_operation(self, run_id, interrupt_id, params, *, kind="create_ticket"):
        if kind == "create_ticket":
            params = TicketRequest.model_validate(params).model_dump(mode="json")
        elif kind == "service_action":
            params = {
                "request": ServiceActionRequest.model_validate(params["request"]).model_dump(
                    mode="json"
                ),
                "preview": params["preview"],
            }
        else:
            raise ValueError("unsupported operation kind")
        params_digest = digest(params)
        async with self._write() as connection:
            row = await self._row(connection, run_id)
            existing = await self._operation(connection, run_id)
            if existing:
                if (
                    existing["interrupt_id"] == interrupt_id
                    and existing["params_digest"] == params_digest
                    and existing["kind"] == kind
                ):
                    return existing
                raise Conflict("run already has a different frozen operation")
            if row["status"] in TERMINAL or row["cancel_requested"]:
                raise Conflict("cannot freeze a cancelled or terminal run")
            operation_id = str(uuid.uuid4())
            await connection.execute(
                insert(operations).values(
                    id=operation_id,
                    run_id=run_id,
                    interrupt_id=interrupt_id,
                    draft_version=1,
                    params_digest=params_digest,
                    params_json=canonical(params),
                    status="PENDING",
                    kind=kind,
                    created_at=now(),
                )
            )
            revision = row["revision"] + 1
            await connection.execute(
                update(runs)
                .where(runs.c.id == run_id)
                .values(
                    status="WAITING_APPROVAL",
                    revision=revision,
                    updated_at=now(),
                )
            )
            await self._event(
                connection,
                run_id,
                "operation.frozen",
                {
                    "operation_id": operation_id,
                    "interrupt_id": interrupt_id,
                    "draft_version": 1,
                    "params_digest": params_digest,
                },
                f"frozen:{operation_id}",
            )
            await self._event(
                connection,
                run_id,
                "run.state_changed",
                {"status": "WAITING_APPROVAL"},
                f"state:{run_id}:{revision}",
            )
            self._fault("freeze.before_commit")
            return await self._operation(connection, run_id)

    async def decide(
        self, run_id, interrupt_id, operation_id, draft_version, params_digest, decision
    ):
        if decision not in {"approve", "reject"}:
            raise ValueError("decision must be approve or reject")
        async with self._write() as connection:
            row = await self._row(connection, run_id)
            operation = await self._operation(connection, run_id)
            if operation is None or any(
                (
                    operation["id"] != operation_id,
                    operation["interrupt_id"] != interrupt_id,
                    operation["draft_version"] != draft_version,
                    operation["params_digest"] != params_digest,
                )
            ):
                raise Conflict("approval does not match the frozen operation and interrupt")
            if operation["decision"]:
                if operation["decision"] == decision:
                    return operation
                raise Conflict("an incompatible decision has already been committed")
            if row["status"] in TERMINAL or row["cancel_requested"]:
                raise Conflict("cannot decide a cancelled or terminal run")
            await connection.execute(
                update(operations)
                .where(operations.c.id == operation_id)
                .values(
                    decision=decision,
                    status="APPROVED" if decision == "approve" else "REJECTED",
                    decided_at=now(),
                )
            )
            payload = {
                "interrupt_id": interrupt_id,
                "operation_id": operation_id,
                "draft_version": draft_version,
                "params_digest": params_digest,
                "decision": decision,
            }
            await connection.execute(
                insert(commands).values(
                    id=str(uuid.uuid4()),
                    run_id=run_id,
                    kind="RESUME",
                    payload_json=canonical(payload),
                    dedupe_key=f"resume:{operation_id}",
                    status="PENDING",
                    created_at=now(),
                )
            )
            await self._event(
                connection, run_id, "operation.decided", payload, f"decision:{operation_id}"
            )
            self._fault("decision.before_commit")
            return await self._operation(connection, run_id)

    async def create_ticket(self, run_id, params):
        proposal = TicketRequest.model_validate(params)
        params = proposal.model_dump(mode="json")
        async with self._write() as connection:
            row = await self._row(connection, run_id)
            operation = await self._operation(connection, run_id)
            if (
                operation is None
                or operation["kind"] != "create_ticket"
                or operation["params_digest"] != digest(params)
            ):
                raise Conflict("ticket parameters do not match a frozen operation")
            existing = (
                (
                    await connection.execute(
                        select(receipts).where(receipts.c.operation_id == operation["id"])
                    )
                )
                .mappings()
                .first()
            )
            if existing:
                return dict(existing)
            if operation["decision"] != "approve":
                raise Conflict("ticket operation has not been approved")
            if row["cancel_requested"] or row["status"] in TERMINAL:
                raise Conflict("ticket write cancelled before commit")
            snapshots = (
                (
                    await connection.execute(
                        select(evidence.c.snapshot_json).where(
                            evidence.c.run_id == run_id,
                        )
                    )
                )
                .scalars()
                .all()
            )
            obtained = {item["id"]: item for item in map(json.loads, snapshots)}
            validate_references(proposal.references, obtained)
            ticket_id = str(uuid.uuid4())
            stamp = now()
            await connection.execute(
                insert(tickets).values(
                    id=ticket_id,
                    run_id=run_id,
                    operation_id=operation["id"],
                    params_json=canonical(params),
                    created_at=stamp,
                )
            )
            self._fault("ticket.after_insert")
            receipt = {
                "operation_id": operation["id"],
                "run_id": run_id,
                "ticket_id": ticket_id,
                "params_digest": operation["params_digest"],
                "created_at": stamp,
            }
            await connection.execute(insert(receipts).values(**receipt))
            await connection.execute(
                update(operations)
                .where(operations.c.id == operation["id"])
                .values(
                    status="COMMITTED",
                )
            )
            await self._event(
                connection, run_id, "ticket.created", receipt, f"ticket:{operation['id']}"
            )
            self._fault("ticket.before_commit")
        self._fault("ticket.after_commit")
        return receipt

    async def begin_service_action(self, run_id, request):
        """Durable claim before an external effect; a recovered claim is never blindly replayed."""
        request = ServiceActionRequest.model_validate(request).model_dump(mode="json")
        async with self._write() as connection:
            run = await self._row(connection, run_id)
            operation = await self._operation(connection, run_id)
            if (
                not operation
                or operation["kind"] != "service_action"
                or operation["params"]["request"] != request
            ):
                raise Conflict("service action does not match the frozen proposal")
            existing = (
                (
                    await connection.execute(
                        select(action_receipts).where(
                            action_receipts.c.operation_id == operation["id"]
                        )
                    )
                )
                .mappings()
                .first()
            )
            if existing:
                return (
                    operation,
                    {**dict(existing), "result": json.loads(existing["result_json"])},
                    False,
                )
            if operation["decision"] != "approve":
                raise Conflict("service action has not been approved")
            if run["cancel_requested"] or run["status"] in TERMINAL:
                raise Conflict("service action was cancelled")
            obtained = {
                i["id"]: i
                for i in map(
                    json.loads,
                    (
                        await connection.execute(
                            select(evidence.c.snapshot_json).where(evidence.c.run_id == run_id)
                        )
                    )
                    .scalars()
                    .all(),
                )
            }
            validate_references(ServiceActionRequest.model_validate(request).references, obtained)
            stamp = now()
            await connection.execute(
                insert(action_receipts).values(
                    operation_id=operation["id"],
                    run_id=run_id,
                    status="STARTED",
                    result_json="{}",
                    created_at=stamp,
                    updated_at=stamp,
                )
            )
            await connection.execute(
                update(operations)
                .where(operations.c.id == operation["id"])
                .values(status="EXECUTING")
            )
            await self._event(
                connection,
                run_id,
                "action.started",
                {"operation_id": operation["id"], "action": request["action"]},
                f"action-start:{operation['id']}",
            )
            return operation, None, True

    async def finish_service_action(self, run_id, status, result):
        if status not in {"SUCCEEDED", "FAILED", "UNKNOWN"}:
            raise ValueError("invalid external effect outcome")
        async with self._write() as connection:
            operation = await self._operation(connection, run_id)
            if not operation or operation["kind"] != "service_action":
                raise Conflict("missing service action")
            receipt = (
                (
                    await connection.execute(
                        select(action_receipts).where(action_receipts.c.run_id == run_id)
                    )
                )
                .mappings()
                .first()
            )
            if receipt is None:
                raise Conflict("unclaimed service action")
            if receipt["status"] != "STARTED":
                return {**dict(receipt), "result": json.loads(receipt["result_json"])}
            stamp = now()
            await connection.execute(
                update(action_receipts)
                .where(action_receipts.c.run_id == run_id)
                .values(status=status, result_json=canonical(result), updated_at=stamp)
            )
            await connection.execute(
                update(operations)
                .where(operations.c.id == operation["id"])
                .values(status="COMMITTED" if status == "SUCCEEDED" else status)
            )
            await self._event(
                connection,
                run_id,
                "action.finished",
                {"operation_id": operation["id"], "status": status, "result": result},
                f"action-finish:{operation['id']}",
            )
            return {
                "operation_id": operation["id"],
                "run_id": run_id,
                "status": status,
                "result": result,
                "updated_at": stamp,
            }

    async def events(self, run_id, after=0, limit=100):
        if after < 0 or not 1 <= limit <= 200:
            raise ValueError("invalid event cursor or limit")
        async with self._read() as connection:
            await self._row(connection, run_id)
            rows = (
                (
                    await connection.execute(
                        select(event_rows)
                        .where(
                            event_rows.c.run_id == run_id,
                            event_rows.c.id > after,
                        )
                        .order_by(event_rows.c.id)
                        .limit(limit)
                    )
                )
                .mappings()
                .all()
            )
            return [
                {**dict(row), "event_id": row["id"], "payload": json.loads(row["payload_json"])}
                for row in rows
            ]

    async def record_event(self, run_id, kind, payload, event_key=None):
        event_key = event_key or f"custom:{uuid.uuid4()}"
        async with self._write() as connection:
            await self._row(connection, run_id)
            old = (
                await connection.execute(
                    select(event_rows.c.id).where(
                        event_rows.c.event_key == event_key,
                    )
                )
            ).scalar_one_or_none()
            if old is not None:
                return old
            await self._event(connection, run_id, kind, payload, event_key)
            return (
                await connection.execute(
                    select(event_rows.c.id).where(
                        event_rows.c.event_key == event_key,
                    )
                )
            ).scalar_one()

    async def pending_commands(self, run_id=None):
        async with self._read() as connection:
            query = select(commands).where(commands.c.status == "PENDING")
            if run_id:
                query = query.where(commands.c.run_id == run_id)
            rows = (
                (await connection.execute(query.order_by(commands.c.created_at))).mappings().all()
            )
            return [{**dict(row), "payload": json.loads(row["payload_json"])} for row in rows]

    async def complete_command(self, command_id):
        async with self._write() as connection:
            await connection.execute(
                update(commands)
                .where(commands.c.id == command_id)
                .values(
                    status="DONE",
                )
            )

    async def consume_tool_call(self, run_id, max_calls=12):
        async with self._write() as connection:
            row = await self._row(connection, run_id)
            if row["tool_calls"] >= max_calls:
                raise BudgetExceeded("tool call limit reached")
            count = row["tool_calls"] + 1
            await connection.execute(
                update(runs).where(runs.c.id == run_id).values(tool_calls=count)
            )
        return count

    async def consume_report_repair(self, run_id: str, error_summary: str, limit: int = 2) -> int:
        """Reserve a bounded validation repair and its feedback event durably together."""
        if not isinstance(error_summary, str):
            raise ValueError("report validation feedback must be text")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("report validation repair limit must be a positive integer")
        async with self._write() as connection:
            row = await self._row(connection, run_id)
            if row["status"] in TERMINAL:
                raise Conflict("cannot repair a report for a terminal run")
            if row["cancel_requested"]:
                raise Conflict("run was cancelled before report repair")
            if row["report_repairs"] >= limit:
                raise BudgetExceeded("report validation repair limit reached")
            count = row["report_repairs"] + 1
            await connection.execute(
                update(runs)
                .where(runs.c.id == run_id)
                .values(report_repairs=count, updated_at=now())
            )
            await self._event(
                connection,
                run_id,
                "report.validation_failed",
                {"attempt": count, "error": error_summary[:1000]},
                f"report-validation:{run_id}:{count}",
            )
            self._fault("report_repair.before_commit")
        return count

    async def add_active_seconds(self, run_id, seconds, max_seconds=180):
        if not math.isfinite(seconds) or seconds < 0:
            raise ValueError("active duration cannot be negative")
        async with self._write() as connection:
            row = await self._row(connection, run_id)
            total = row["active_seconds"] + seconds
            await connection.execute(
                update(runs)
                .where(runs.c.id == run_id)
                .values(
                    active_seconds=total,
                )
            )
        if total > max_seconds:
            raise BudgetExceeded("active execution time limit reached")
        return total

    async def begin_active(self, run_id):
        async with self._write() as connection:
            row = await self._row(connection, run_id)
            if row["active_started_at"]:
                raise Conflict("previous active execution span must be settled first")
            if row["status"] in TERMINAL:
                raise Conflict("cannot begin active execution for a terminal run")
            started_at = now()
            await connection.execute(
                update(runs)
                .where(runs.c.id == run_id)
                .values(
                    active_started_at=started_at,
                )
            )
            return started_at

    async def finish_active(self, run_id, ended_at=None, max_seconds=180):
        end = datetime.fromisoformat(ended_at) if isinstance(ended_at, str) else ended_at
        end = end or datetime.now(UTC)
        if end.tzinfo is None or end.utcoffset() is None:
            raise ValueError("active span end must include timezone")
        async with self._write() as connection:
            row = await self._row(connection, run_id)
            total = row["active_seconds"]
            if row["active_started_at"]:
                start = datetime.fromisoformat(row["active_started_at"])
                total += max(0, (end - start).total_seconds())
                await connection.execute(
                    update(runs)
                    .where(runs.c.id == run_id)
                    .values(
                        active_seconds=total,
                        active_started_at=None,
                    )
                )
        if total > max_seconds:
            raise BudgetExceeded("active execution time limit reached")
        return total
