"""Frozen project data-egress controls and payload-free policy audit records.

These controls reduce selected data exposure. They do not provide enterprise DLP,
encryption, user authentication, or isolation from the same operating-system user.
"""

import hashlib
import json
import re
import sqlite3
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator


def origin(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("A cloud destination must be an HTTPS origin")
    host = parsed.hostname.lower()
    host = f"[{host}]" if ":" in host else host
    port = parsed.port or 443
    return f"https://{host}" + (f":{port}" if port != 443 else "")


class CloudPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    mode: Literal["redacted", "deny"] = "redacted"
    allowed_origins: list[str] = Field(
        default_factory=lambda: ["https://api.deepseek.com"], min_length=1, max_length=10
    )
    allow_source_code: bool = True
    redact_literals: list[str] = Field(default_factory=list, max_length=30)
    policy_version: Literal["cloud-egress-v1"] = "cloud-egress-v1"

    @field_validator("allowed_origins")
    @classmethod
    def destination_origins(cls, values):
        for value in values:
            if (
                urlsplit(value).path not in {"", "/"}
                or urlsplit(value).query
                or urlsplit(value).fragment
            ):
                raise ValueError("Allowlist entries must contain only an HTTPS origin")
        return sorted(set(origin(value) for value in values))

    @field_validator("redact_literals")
    @classmethod
    def bounded_literals(cls, values):
        if any(not 2 <= len(value.strip()) <= 200 or "\x00" in value for value in values):
            raise ValueError("Redaction literals must contain 2–200 nonblank characters")
        return sorted(set(values), key=lambda value: (-len(value), value))

    @property
    def fingerprint(self):
        return hashlib.sha256(self.model_dump_json().encode()).hexdigest()


class CloudPolicyDenied(RuntimeError):
    """A project policy blocked a provider request before any network effect."""


def scrub_text(text: str, policy: CloudPolicy, secrets=()) -> str:
    from incident_desk.tools.live_evidence import redact

    # Replace explicit literals first, including credentials that lack a prefix.
    for value in sorted(
        set(policy.redact_literals) | {v for v in secrets if v}, key=len, reverse=True
    ):
        text = text.replace(value, "[REDACTED]")
    return redact(text)


def _scrub_messages(value, policy, secrets, *, preserve_protocol=False):
    if isinstance(value, str):
        return scrub_text(value, policy, secrets)
    if isinstance(value, list):
        return [
            _scrub_messages(item, policy, secrets, preserve_protocol=preserve_protocol)
            for item in value
        ]
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if preserve_protocol and key in {"role", "id", "tool_call_id", "type", "name"}:
                result[key] = item
            elif preserve_protocol and key == "arguments" and isinstance(item, str):
                # Keep JSON arguments valid when a selected literal contains quotes.
                try:
                    result[key] = json.dumps(
                        _scrub_messages(json.loads(item), policy, secrets), ensure_ascii=False
                    )
                except ValueError:
                    result[key] = scrub_text(item, policy, secrets)
            elif re.fullmatch(r"(?i)(?:password|passwd|api[-_]?key|secret|access[-_]?token)", key):
                result[key] = "[REDACTED]"
            else:
                result[key] = _scrub_messages(
                    item, policy, secrets, preserve_protocol=preserve_protocol
                )
        return result
    return value


def redact_snapshot(item: dict, policy: CloudPolicy) -> dict:
    result = dict(item)
    content = result.get("content")
    if isinstance(content, str):
        result["content"] = scrub_text(content, policy)
        if result["content"] != content:
            result["provenance"] = dict(result.get("provenance") or {}) | {
                "cloud_redaction_policy": policy.fingerprint,
                "captured_content_sha256": hashlib.sha256(result["content"].encode()).hexdigest(),
            }
    return result


class CloudAudit:
    def __init__(self, local_dir: Path):
        local_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = local_dir / "cloud-audit.db"
        if self.path.is_symlink():
            raise ValueError("Cloud audit storage cannot be a symlink")
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS decisions (id INTEGER PRIMARY KEY,run_id TEXT NOT NULL,"
                "at TEXT NOT NULL,decision TEXT NOT NULL,origin TEXT NOT NULL,"
                "policy_sha256 TEXT NOT NULL,"
                "payload_sha256 TEXT,payload_bytes INTEGER,changed INTEGER NOT NULL,reason TEXT)"
            )
        self.path.chmod(0o600)

    @contextmanager
    def connect(self):
        with closing(sqlite3.connect(self.path, timeout=5)) as connection, connection:
            yield connection

    def record(self, run_id, destination, policy, *, payload=None, changed=False, reason=None):
        raw = (
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
            if payload is not None
            else None
        )
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO decisions(run_id,at,decision,origin,policy_sha256,payload_sha256,"
                "payload_bytes,changed,reason) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    datetime.now(UTC).isoformat(),
                    "denied" if reason else "allowed",
                    destination,
                    policy.fingerprint,
                    hashlib.sha256(raw).hexdigest() if raw else None,
                    len(raw) if raw else None,
                    int(changed),
                    reason,
                ),
            )
            connection.execute(
                "DELETE FROM decisions WHERE id < COALESCE("
                "(SELECT id FROM decisions ORDER BY id DESC LIMIT 1 OFFSET 49999),0)"
            )

    def for_run(self, run_id):
        with self.connect() as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                "SELECT * FROM decisions WHERE run_id=? ORDER BY id DESC LIMIT 100", (run_id,)
            ).fetchall()
        return {
            "items": [dict(row) for row in rows],
            "scope": "Policy decisions, not proof of provider delivery; no payload text is stored",
        }


def guard_payload(settings, run_id, payload, *, audit=False):
    """Check the current registration, then scrub the exact serialized messages."""
    destination = origin(settings.base_url)
    policy = CloudPolicy(allowed_origins=[destination])
    request = None
    if settings.app_db.is_file():
        with closing(
            sqlite3.connect(settings.app_db.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
        ) as connection:
            table = connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='runs'"
            ).fetchone()
            row = (
                connection.execute("SELECT request_json FROM runs WHERE id=?", (run_id,)).fetchone()
                if table
                else None
            )
            request = json.loads(row[0]) if row else None
    reason = None
    if request and request.get("mode") == "live":
        from incident_desk.projects import ProjectStore

        current = ProjectStore(settings.local_dir).get(request["service_id"])
        frozen = ProjectStore(settings.local_dir).get(
            request["service_id"], request["project_version"]
        )
        policy = CloudPolicy.model_validate(request.get("cloud_policy") or frozen.cloud_policy)
        if current.version != frozen.version:
            reason = "project_version_changed"
        elif policy.mode == "deny" or current.cloud_policy.mode == "deny":
            reason = "project_cloud_disabled"
        elif destination not in policy.allowed_origins:
            reason = "destination_not_allowed"
    if reason:
        if audit:
            CloudAudit(settings.local_dir).record(run_id, destination, policy, reason=reason)
        raise CloudPolicyDenied("Project cloud-data policy blocked the model request")
    secrets = [settings.api_key.get_secret_value()] if settings.api_key else []
    cleaned = dict(payload)
    cleaned["messages"] = _scrub_messages(
        payload.get("messages", []), policy, secrets, preserve_protocol=True
    )
    if audit:
        CloudAudit(settings.local_dir).record(
            run_id, destination, policy, payload=cleaned, changed=cleaned != payload
        )
    return cleaned
