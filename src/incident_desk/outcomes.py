"""Operator-supplied outcome annotations, separate from task/model success.

Nothing here derives a real-world assessment from a run state. Private reasons and
quotes stay in app.db; aggregate export uses a fixed, text-free whitelist.
"""

import asyncio
import hashlib
import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from incident_desk.schemas import Reference, validate_references

DIAGNOSIS = ("confirmed", "rejected", "inconclusive")
RECOVERY = ("verified", "failed", "not_attempted")
TERMINAL = frozenset({"SUCCEEDED", "FAILED", "CANCELLED"})


class OutcomeConflict(RuntimeError):
    """A stale annotation cannot overwrite a newer operator decision."""


class OutcomeVerificationMismatch(ValueError):
    """An explicit finite business check contradicts its selected recovery label."""


class OutcomeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    diagnosis_assessment: Literal["confirmed", "rejected", "inconclusive"]
    diagnosis_reason: str = Field(min_length=3, max_length=2000)
    recovery_assessment: Literal["verified", "failed", "not_attempted"]
    recovery_reason: str = Field(min_length=3, max_length=2000)
    verification_references: list[Reference] = Field(default_factory=list, max_length=20)
    expected_revision: int = Field(default=0, ge=0, strict=True)

    @field_validator("diagnosis_reason", "recovery_reason")
    @classmethod
    def meaningful_reason(cls, value: str) -> str:
        if len(value.strip()) < 3:
            raise ValueError("An assessment requires an explicit nonblank reason")
        return value.strip()

    @model_validator(mode="after")
    def evidence_for_claim(self):
        if (
            self.diagnosis_assessment != "inconclusive"
            or self.recovery_assessment != "not_attempted"
        ) and not self.verification_references:
            raise ValueError("Confirmed/rejected diagnoses and attempted recovery need references")
        return self


def validate_outcome(payload: OutcomeInput, run: dict, evidence: list[dict]) -> None:
    if run["status"] not in TERMINAL:
        raise OutcomeConflict("Finish or cancel the task before recording its outcome")
    if payload.diagnosis_assessment != "inconclusive" and not run.get("diagnosis"):
        raise OutcomeConflict("No final diagnosis exists to confirm or reject")
    validate_references(payload.verification_references, {item["id"]: item for item in evidence})
    _verification_sources(payload, {item["id"]: item for item in evidence})


def _verification_sources(payload: OutcomeInput, evidence: dict) -> None:
    if payload.recovery_assessment != "not_attempted" and not any(
        evidence[reference.id].get("source")
        in {"business_probe", "health", "metrics", "stats", "logs", "process", "changes", "history"}
        for reference in payload.verification_references
    ):
        raise ValueError("Recovery annotations require an observed runtime source, not a runbook")
    checks = [
        evidence[reference.id]
        for reference in payload.verification_references
        if evidence[reference.id].get("source") == "business_probe"
        and evidence[reference.id].get("provenance", {}).get("purpose")
        == "operator_outcome_verification"
    ]
    if checks and payload.recovery_assessment != "not_attempted":
        latest = max(
            checks, key=lambda item: item.get("collected_at") or item.get("observed_at", "")
        )
        expected = payload.recovery_assessment == "verified"
        if latest.get("provenance", {}).get("business_ok") is not expected:
            raise OutcomeVerificationMismatch(
                "Recovery assessment contradicts the latest referenced business verification; "
                "run a fresh check or revise the assessment"
            )


class OutcomeStore:
    """Append-only revisions in the application DB, included in its normal backup."""

    def __init__(self, app_db: str | Path):
        self.path = Path(app_db)

    @contextmanager
    def _connect(self):
        if self.path.is_symlink():
            raise ValueError("Outcome database cannot be a symbolic link")
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=10000")
        self.path.chmod(0o600)
        try:
            db.execute("""
                CREATE TABLE IF NOT EXISTS outcomes (
                    id TEXT NOT NULL UNIQUE, run_id TEXT NOT NULL REFERENCES runs(id),
                    revision INTEGER NOT NULL, payload_json TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL, created_at TEXT NOT NULL,
                    PRIMARY KEY(run_id, revision)
                )
            """)
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _public(row) -> dict | None:
        if row is None:
            return None
        return {
            "id": row["id"],
            "run_id": row["run_id"],
            "revision": row["revision"],
            "created_at": row["created_at"],
            "source": "operator_supplied",
            "independent_verification": False,
            **json.loads(row["payload_json"]),
        }

    def _record(self, run_id: str, payload: OutcomeInput) -> dict:
        values = payload.model_dump(mode="json", exclude={"expected_revision"})
        encoded = json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        checksum = hashlib.sha256(encoded.encode()).hexdigest()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            run = db.execute(
                "SELECT status,diagnosis_json FROM runs WHERE id=?", (run_id,)
            ).fetchone()
            if run is None or run["status"] not in TERMINAL:
                raise OutcomeConflict("Outcome requires an existing terminal task")
            if payload.diagnosis_assessment != "inconclusive" and not run["diagnosis_json"]:
                raise OutcomeConflict("No final diagnosis exists to confirm or reject")
            evidence = {
                item["id"]: item
                for row in db.execute(
                    "SELECT snapshot_json FROM evidence_snapshots WHERE run_id=?", (run_id,)
                )
                if (item := json.loads(row["snapshot_json"]))
            }
            validate_references(payload.verification_references, evidence)
            _verification_sources(payload, evidence)
            current = db.execute(
                "SELECT * FROM outcomes WHERE run_id=? ORDER BY revision DESC LIMIT 1", (run_id,)
            ).fetchone()
            revision = current["revision"] if current else 0
            if payload.expected_revision != revision:
                # The most recent identical request may have committed before its response was lost.
                if (
                    current
                    and payload.expected_revision == revision - 1
                    and current["payload_sha256"] == checksum
                ):
                    return self._public(current)
                raise OutcomeConflict("Outcome revision changed; reload before submitting")
            if current and current["payload_sha256"] == checksum:
                return self._public(current)
            record_id = str(uuid.uuid4())
            db.execute(
                "INSERT INTO outcomes VALUES (?,?,?,?,?,?)",
                (record_id, run_id, revision + 1, encoded, checksum, datetime.now(UTC).isoformat()),
            )
            return self._public(
                db.execute("SELECT * FROM outcomes WHERE id=?", (record_id,)).fetchone()
            )

    async def record(self, run_id: str, payload: OutcomeInput) -> dict:
        return await asyncio.to_thread(self._record, run_id, payload)

    def _latest(self, run_id: str) -> dict | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM outcomes WHERE run_id=? ORDER BY revision DESC LIMIT 1", (run_id,)
            ).fetchone()
        return self._public(row)

    async def latest(self, run_id: str) -> dict | None:
        return await asyncio.to_thread(self._latest, run_id)

    def _aggregate(self, mode: str) -> dict:
        if mode not in {"all", "live", "fixture"}:
            raise ValueError("Outcome scope must be all, live or fixture")
        with self._connect() as db:
            rows = db.execute(
                """
                SELECT r.status AS task_status,
                    COALESCE(json_extract(r.request_json,'$.mode'),'fixture') AS mode,
                    r.diagnosis_json IS NOT NULL AS report_present,
                    o.revision IS NOT NULL AS annotated,
                    json_extract(o.payload_json,'$.diagnosis_assessment') AS diagnosis,
                    json_extract(o.payload_json,'$.recovery_assessment') AS recovery
                FROM runs r LEFT JOIN outcomes o ON o.run_id=r.id AND o.revision=(
                    SELECT MAX(x.revision) FROM outcomes x WHERE x.run_id=r.id)
                WHERE (?='all' OR COALESCE(json_extract(r.request_json,'$.mode'),'fixture')=?)
            """,
                (mode, mode),
            ).fetchall()
        selected = [row for row in rows if row["annotated"]]
        finished = sum(row["task_status"] in TERMINAL for row in rows)
        diagnosis = {
            label: sum(row["diagnosis"] == label for row in selected) for label in DIAGNOSIS
        }
        recovery = {label: sum(row["recovery"] == label for row in selected) for label in RECOVERY}
        attempted = recovery["verified"] + recovery["failed"]
        return {
            "schema": "incidentdesk-outcome-aggregate-v1",
            "scope": mode,
            "generated_at": datetime.now(UTC).isoformat(),
            "aggregation_only": True,
            "assessment_source": "operator_supplied; not independently verified",
            "total_runs": len(rows),
            "terminal_runs": finished,
            "task_succeeded_runs": sum(row["task_status"] == "SUCCEEDED" for row in rows),
            "runs_with_report": sum(row["report_present"] for row in rows),
            "annotated_runs": len(selected),
            "unannotated_terminal_runs": finished - len(selected),
            "annotation_coverage": len(selected) / finished if finished else None,
            "diagnosis_assessments": diagnosis,
            "recovery_assessments": recovery,
            "confirmation_share_among_annotated": diagnosis["confirmed"] / len(selected)
            if selected
            else None,
            "attempted_recovery_annotations": attempted,
            "verified_share_among_attempted": recovery["verified"] / attempted
            if attempted
            else None,
            "mode_counts": {
                key: sum(row["mode"] == key for row in rows) for key in ("live", "fixture")
            },
            "limitations": [
                "Task SUCCEEDED does not establish a correct diagnosis or recovered service.",
                "Local submissions are not authenticated independent human review.",
                "Unannotated tasks and not-attempted recoveries remain visible in denominators.",
                "Counts may still be sensitive; no automatic upload or cross-user claim is made.",
                "Runs and follow-ups are not independent incidents or distinct users.",
            ],
        }

    async def aggregate(self, mode: str = "live") -> dict:
        return await asyncio.to_thread(self._aggregate, mode)
