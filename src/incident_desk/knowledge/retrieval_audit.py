"""Private bounded retrieval traces: query hashes and source IDs, never source text."""

import hashlib
import json
import math
import re
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

MAX_PER_RUN = 100
MAX_RECORDS = 50000
MAX_TRACE_BYTES = 128 * 1024
MAX_TOTAL_TRACE_BYTES = 64 * 1024 * 1024
MAX_DATABASE_BYTES = 128 * 1024 * 1024
RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


def _run_id(value):
    if not isinstance(value, str) or not RUN_ID.fullmatch(value):
        raise ValueError("Invalid retrieval audit run ID")


def _candidate(row):
    if not isinstance(row, dict):
        return {}
    result = {
        key: row[key]
        for key in ("id", "doc_id", "path", "line_start", "line_end", "rank")
        if key in row
    }
    result["scores"] = {
        str(key): value
        for key, value in row.get("scores", {}).items()
        if type(value) in (int, float) and math.isfinite(value)
    }
    return result


def _numeric_fields(value, names):
    return {
        key: child
        for key, child in value.items()
        if key in names and type(child) in (int, float, bool) and math.isfinite(child)
    }


def _trace(batch):
    retrieval = batch.get("retrieval") or {}
    reranker = retrieval.get("reranker") or {}
    # No free-form exception text, query, passage, or arbitrary batch keys are copied.
    model = reranker.get("model") or {}
    return {
        "status": batch.get("status"),
        "error_code": (batch.get("error") or {}).get("code"),
        "generation": (batch.get("coverage") or {}).get("index_generation"),
        "requested_mode": retrieval.get("requested_mode"),
        "effective_mode": retrieval.get("effective_mode"),
        "config": {
            key: value
            for key, value in (retrieval.get("config") or {}).items()
            if key
            in {
                "candidate_n",
                "rerank_top_n",
                "top_k",
                "reranker",
                "policy_version",
                "reranker_fingerprint",
            }
        },
        "phase_counts": _numeric_fields(
            retrieval.get("phase_counts", {}),
            {
                "scored_chunks",
                "lexical_matches",
                "dense_scored",
                "fusion_candidates",
                "rerank_candidates",
                "returned",
            },
        ),
        "stage_timings_ms": _numeric_fields(
            retrieval.get("stage_timings_ms", {}),
            {"lexical", "dense", "fusion", "reranker", "selection"},
        ),
        "latency_ms": retrieval.get("latency_ms"),
        "candidate_trace_truncated": retrieval.get("candidate_trace_truncated", False),
        "candidates": {
            stage: [_candidate(row) for row in rows[:200]]
            for stage, rows in (retrieval.get("candidates") or {}).items()
            if stage in {"lexical", "dense", "fused", "before_rerank", "after_rerank"}
        },
        "reranker": {
            **{
                key: reranker[key]
                for key in (
                    "requested",
                    "effective",
                    "status",
                    "pair_count",
                    "latency_ms",
                    "model_load_ms",
                    "inference_ms",
                    "peak_rss_mib",
                    "max_pair_tokens",
                    "max_query_tokens",
                    "batch_size",
                    "fallback_order",
                )
                if key in reranker
            },
            "error_code": (reranker.get("error") or {}).get("code"),
            "truncation": _numeric_fields(
                reranker.get("truncation", {}),
                {"query_truncated", "passages_truncated", "pairs_truncated"},
            ),
            "model": {
                key: model[key]
                for key in ("model_id", "revision", "fingerprint", "state")
                if key in model
            },
        },
        "returned_ids": [row.get("id") for row in batch.get("items", [])[:10]],
    }


def _compact(trace, batch):
    """Deduplicate sources; trim trace tails explicitly, never block retrieval on volume."""
    sources, stages = {}, {}
    trace["audit_schema"] = 2
    original_counts = {}
    for stage, rows in trace["candidates"].items():
        original_counts[stage] = len(rows)
        stages[stage] = []
        for row in rows:
            identity = row.get("id")
            if not identity:
                continue
            sources.setdefault(
                identity,
                {
                    key: row[key]
                    for key in ("doc_id", "path", "line_start", "line_end")
                    if key in row
                },
            )
            stages[stage].append({key: row[key] for key in ("id", "rank", "scores") if key in row})
    for item in batch.get("items", [])[:10]:
        provenance = item.get("provenance") or {}
        sources.setdefault(
            item.get("id"),
            {
                "path": provenance.get("relative_path"),
                **{
                    key: provenance[key]
                    for key in ("doc_id", "line_start", "line_end")
                    if key in provenance
                },
            },
        )
    truncated_paths = 0
    for source in sources.values():
        path = source.get("path")
        if isinstance(path, str) and len(path.encode("utf-8")) > 4096:
            source["path"] = path.encode("utf-8")[:4096].decode("utf-8", errors="ignore")
            source["path_truncated"] = True
            truncated_paths += 1
    trace["candidates"], trace["candidate_sources"] = stages, sources
    trace["audit_truncation"] = {
        "truncated": bool(trace["candidate_trace_truncated"]),
        "omitted_rows": {stage: 0 for stage in stages},
        "paths_truncated": truncated_paths,
        "max_record_bytes": MAX_TRACE_BYTES,
    }

    def encode():
        return json.dumps(trace, ensure_ascii=False, allow_nan=False, separators=(",", ":"))

    encoded = encode()
    for stage in ("lexical", "dense", "fused", "before_rerank", "after_rerank"):
        rows = stages.get(stage, [])
        while rows and len(encoded.encode()) > MAX_TRACE_BYTES:
            del rows[-max(1, len(rows) // 4) :]
            referenced = {row["id"] for values in stages.values() for row in values}
            referenced.update(trace["returned_ids"])
            trace["candidate_sources"] = {
                key: value for key, value in sources.items() if key in referenced
            }
            trace["audit_truncation"]["truncated"] = True
            trace["audit_truncation"]["omitted_rows"][stage] = original_counts[stage] - len(rows)
            encoded = encode()
    return trace, encoded


class RetrievalAudit:
    def __init__(self, local_dir):
        directory = Path(local_dir)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = directory / "retrieval-audit.db"
        if self.path.is_symlink():
            raise ValueError("Retrieval audit cannot be a symlink")
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS retrieval_traces (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
                    query_sha256 TEXT NOT NULL, created_at TEXT NOT NULL, trace_json TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS retrieval_run ON retrieval_traces(run_id,id);
            """)
            if "trace_bytes" not in {
                row[1] for row in db.execute("PRAGMA table_info(retrieval_traces)")
            }:
                db.execute(
                    "ALTER TABLE retrieval_traces ADD COLUMN trace_bytes INTEGER NOT NULL DEFAULT 0"
                )
                db.execute(
                    "UPDATE retrieval_traces SET trace_bytes=length(CAST(trace_json AS BLOB))"
                )
            db.executescript("""
                CREATE TABLE IF NOT EXISTS retrieval_usage (
                    id INTEGER PRIMARY KEY CHECK(id=1), records INTEGER NOT NULL,
                    trace_bytes INTEGER NOT NULL);
                INSERT OR IGNORE INTO retrieval_usage
                    SELECT 1,count(*),COALESCE(sum(trace_bytes),0) FROM retrieval_traces;
                CREATE TRIGGER IF NOT EXISTS retrieval_insert AFTER INSERT ON retrieval_traces
                    BEGIN UPDATE retrieval_usage SET records=records+1,
                        trace_bytes=trace_bytes+new.trace_bytes WHERE id=1; END;
                CREATE TRIGGER IF NOT EXISTS retrieval_delete AFTER DELETE ON retrieval_traces
                    BEGIN UPDATE retrieval_usage SET records=records-1,
                        trace_bytes=trace_bytes-old.trace_bytes WHERE id=1; END;
            """)
            db.commit()
        self.path.chmod(0o600)

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=0.2)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA journal_size_limit=1048576")
        db.execute("PRAGMA wal_autocheckpoint=256")
        page_size = db.execute("PRAGMA page_size").fetchone()[0]
        db.execute(f"PRAGMA max_page_count={MAX_DATABASE_BYTES // page_size}")
        try:
            yield db
        finally:
            db.close()

    def record(self, run_id, query, batch):
        _run_id(run_id)
        if not isinstance(query, str) or len(query) > 4000:
            raise ValueError("Invalid retrieval audit query")
        query_hash = hashlib.sha256(query.encode()).hexdigest()
        stamp = datetime.now(UTC).isoformat()
        trace, encoded = _compact(_trace(batch), batch)
        size = len(encoded.encode())
        if size > min(MAX_TRACE_BYTES, MAX_TOTAL_TRACE_BYTES):
            return {"status": "unavailable", "error_code": "audit_record_too_large"}
        try:
            with self._connect() as db, db:
                db.execute("BEGIN IMMEDIATE")
                db.execute(
                    "DELETE FROM retrieval_traces WHERE run_id=? AND id NOT IN "
                    "(SELECT id FROM retrieval_traces WHERE run_id=? ORDER BY id DESC LIMIT ?)",
                    (run_id, run_id, MAX_PER_RUN - 1),
                )
                self._prune(db, reserve_bytes=size)
                cursor = db.execute(
                    "INSERT INTO retrieval_traces"
                    "(run_id,query_sha256,created_at,trace_json,trace_bytes)"
                    " VALUES (?,?,?,?,?)",
                    (run_id, query_hash, stamp, encoded, size),
                )
                identity = cursor.lastrowid
        except (OSError, sqlite3.Error):
            return {"status": "unavailable", "error_code": "audit_storage_unavailable"}
        return {
            "id": identity,
            "run_id": run_id,
            "query_sha256": query_hash,
            "created_at": stamp,
            **trace,
        }

    def _prune(self, db, *, reserve_bytes):
        records, size = db.execute(
            "SELECT records,trace_bytes FROM retrieval_usage WHERE id=1"
        ).fetchone()
        excess = max(0, records + 1 - MAX_RECORDS)
        if excess:
            db.execute(
                "DELETE FROM retrieval_traces WHERE id IN "
                "(SELECT id FROM retrieval_traces ORDER BY id LIMIT ?)",
                (excess,),
            )
        while True:
            size = db.execute("SELECT trace_bytes FROM retrieval_usage WHERE id=1").fetchone()[0]
            if size + reserve_bytes <= MAX_TOTAL_TRACE_BYTES:
                break
            oldest = db.execute(
                "SELECT id,trace_bytes FROM retrieval_traces ORDER BY id LIMIT 128"
            ).fetchall()
            if not oldest:
                break
            delete, freed = [], 0
            for row in oldest:
                delete.append((row["id"],))
                freed += row["trace_bytes"]
                if size - freed + reserve_bytes <= MAX_TOTAL_TRACE_BYTES:
                    break
            db.executemany("DELETE FROM retrieval_traces WHERE id=?", delete)

    def for_run(self, run_id):
        """Latest one hundred entries in execution order; contains no query or source text."""
        _run_id(run_id)
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM retrieval_traces WHERE run_id=? ORDER BY id DESC LIMIT ?",
                (run_id, MAX_PER_RUN),
            ).fetchall()
        return [
            {
                "id": row["id"],
                "run_id": row["run_id"],
                "query_sha256": row["query_sha256"],
                "created_at": row["created_at"],
                **json.loads(row["trace_json"]),
            }
            for row in reversed(rows)
        ]
