"""Bounded local history. Scrapes are observations, never reconstructed past data."""

import asyncio
import hashlib
import json
import math
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
from prometheus_client.parser import text_string_to_metric_families

from incident_desk.projects import local_url
from incident_desk.tools.live_evidence import redact, utc

MAX_RESPONSE_BYTES = 65536
MAX_CONTENT = 12000
MAX_SERIES = 256
SCRAPE_TIMEOUT_SECONDS = 3


def canonical(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def metric_unit(name):
    if name == "engine_cache_hit_rate":
        return "permille"
    stem = name.removesuffix("_bucket").removesuffix("_sum").removesuffix("_total")
    if name.endswith("_count"):
        return "count"
    for suffix, unit in (
        ("_seconds", "seconds"),
        ("_milliseconds", "milliseconds"),
        ("_microseconds", "microseconds"),
        ("_us", "microseconds"),
        ("_bytes", "bytes"),
    ):
        if stem.endswith(suffix):
            return unit
    if name.endswith("_total"):
        return "count"
    return "unspecified"


def parse_metrics(raw):
    rows, skipped = [], 0
    for family in text_string_to_metric_families(raw):
        for sample in family.samples:
            if len(rows) >= MAX_SERIES:
                return rows, True, skipped
            if not math.isfinite(sample.value):
                skipped += 1
                continue
            rows.append(
                {
                    "name": sample.name[:200],
                    "type": family.type,
                    "labels": {str(k)[:100]: str(v)[:500] for k, v in sample.labels.items()},
                    "value": sample.value,
                    "unit": metric_unit(sample.name),
                }
            )
    return rows, False, skipped


def process_epoch(metric, starts):
    """Join only start-time labels present on this series within the same scrape/source."""
    matched = [
        (canonical(start["labels"]), start["value"])
        for start in starts
        if all(metric["labels"].get(key) == value for key, value in start["labels"].items())
    ]
    return tuple(sorted(matched)) if matched else None


def restart_count(epochs):
    known = [epoch for epoch in epochs if epoch is not None]
    return sum(before != after for before, after in zip(known, known[1:], strict=False))


class ObservationStore:
    def __init__(
        self,
        local_dir,
        *,
        retention_seconds=7 * 86400,
        max_storage_bytes=128 * 1024 * 1024,
        max_rows=50000,
        sampling_interval_seconds=10,
    ):
        self.directory = Path(local_dir)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.directory / "observations.db"
        if self.path.is_symlink():
            raise ValueError("Observation database must not be a symbolic link")
        self.retention_seconds = max(60, int(retention_seconds))
        self.max_storage_bytes = max(1024 * 1024, int(max_storage_bytes))
        self.max_rows = max(2, min(50000, int(max_rows)))
        self.interval = max(1, float(sampling_interval_seconds))
        with self._connect() as db:
            db.execute("PRAGMA auto_vacuum=INCREMENTAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS observations (
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL,
                    project_version INTEGER NOT NULL, observed_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL, payload_bytes INTEGER NOT NULL);
                CREATE INDEX IF NOT EXISTS observation_window
                    ON observations(project_id, project_version, observed_at);
                CREATE TABLE IF NOT EXISTS observation_catalog (
                    project_id TEXT NOT NULL, project_version INTEGER NOT NULL,
                    first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
                    pruned_through TEXT, PRIMARY KEY(project_id, project_version));
            """)
        self.path.chmod(0o600)

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=10000")
        db.execute("PRAGMA journal_size_limit=1048576")
        page_size = db.execute("PRAGMA page_size").fetchone()[0]
        db.execute(f"PRAGMA max_page_count={self.max_storage_bytes // page_size}")
        try:
            yield db
        finally:
            db.close()

    async def _scrape(self, source, url):
        result = {
            "source": source,
            "status": "unavailable",
            "raw": "",
            "metrics": [],
            "http_status": None,
            "truncated": False,
            "error": None,
        }
        if not url:
            result["error"] = "not_configured"
            return result
        try:
            url = local_url(str(url))
            chunks, size = [], 0
            async with httpx.AsyncClient(
                timeout=3, trust_env=False, follow_redirects=False
            ) as client:
                async with (
                    asyncio.timeout(SCRAPE_TIMEOUT_SECONDS),
                    client.stream("GET", url) as response,
                ):
                    result["http_status"] = response.status_code
                    async for chunk in response.aiter_bytes():
                        room = MAX_RESPONSE_BYTES - size
                        chunks.append(chunk[:room])
                        size += min(room, len(chunk))
                        if len(chunk) > room:
                            result["truncated"] = True
                            break
                    result["raw"] = redact(b"".join(chunks).decode("utf-8", errors="replace"))
                    result["status"] = "complete" if response.is_success else "unavailable"
                    if not response.is_success:
                        result["error"] = "http_status"
            if result["truncated"]:
                result.update(status="partial", error="response_truncated")
            elif source == "metrics" and result["status"] == "complete":
                parsed, clipped, skipped = parse_metrics(result["raw"])
                result.update(metrics=parsed, truncated=clipped, non_finite_samples_skipped=skipped)
                if clipped or skipped:
                    result["status"] = "partial"
        except (OSError, ValueError, httpx.HTTPError) as error:
            result.update(status="unavailable", error=type(error).__name__)
        result["collected_at"] = datetime.now(UTC).isoformat()
        return result

    async def sample(self, project):
        if not getattr(project, "history_enabled", True):
            return {"project_id": project.id, "status": "skipped", "reason": "history_disabled"}
        urls = {
            source: getattr(project, f"{source}_url", None)
            for source in ("health", "metrics", "stats")
        }
        if not any(urls.values()):
            return {"project_id": project.id, "status": "skipped", "reason": "no_sources"}
        results = await asyncio.gather(*(self._scrape(source, url) for source, url in urls.items()))
        sample = {
            "project_id": project.id,
            "project_version": project.version,
            "observed_at": datetime.now(UTC).isoformat(),
            "sources": results,
            "sampling_interval_seconds": self.interval,
        }
        identity = await asyncio.to_thread(self._save, sample)
        statuses = [row["status"] for row in results if urls[row["source"]]]
        return {
            **sample,
            "id": identity,
            "status": "complete"
            if all(status == "complete" for status in statuses)
            else "unavailable"
            if all(status == "unavailable" for status in statuses)
            else "partial",
        }

    async def sample_projects(self, projects):
        gate = asyncio.Semaphore(2)

        async def collect(project):
            async with gate:
                try:
                    return await self.sample(project)
                except (OSError, sqlite3.Error, ValueError) as error:
                    return {
                        "project_id": project.id,
                        "status": "unavailable",
                        "error": type(error).__name__,
                    }

        return await asyncio.gather(*(collect(project) for project in projects))

    def _save(self, sample):
        payload = canonical(sample)
        identity = uuid.uuid4().hex
        stamp = sample["observed_at"]
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._prune(db, utc(stamp))
            db.execute(
                "INSERT INTO observations VALUES (?,?,?,?,?,?)",
                (
                    identity,
                    sample["project_id"],
                    sample["project_version"],
                    stamp,
                    payload,
                    len(payload.encode()),
                ),
            )
            db.execute(
                """INSERT INTO observation_catalog VALUES (?,?,?,?,NULL)
                ON CONFLICT(project_id,project_version) DO UPDATE SET
                    first_seen_at=min(first_seen_at,excluded.first_seen_at),
                    last_seen_at=max(last_seen_at,excluded.last_seen_at)
            """,
                (sample["project_id"], sample["project_version"], stamp, stamp),
            )
            self._prune(db, utc(stamp))
            db.commit()
            db.execute("PRAGMA incremental_vacuum(256)")
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return identity

    def _prune(self, db, at):
        cutoff = (at - timedelta(seconds=self.retention_seconds)).isoformat()
        old = db.execute(
            "SELECT id,project_id,project_version,observed_at "
            "FROM observations WHERE observed_at<?",
            (cutoff,),
        ).fetchall()
        self._delete(db, old)
        count, size = db.execute(
            "SELECT count(*),COALESCE(sum(payload_bytes),0) FROM observations"
        ).fetchone()
        # Leave room for SQLite indexes and a bounded new sample before the physical page cap.
        limit = self.max_storage_bytes // 3
        old = []
        if count > self.max_rows or (count > 1 and size > limit):
            for row in db.execute(
                "SELECT id,project_id,project_version,observed_at,payload_bytes "
                "FROM observations ORDER BY observed_at,id"
            ):
                if count <= self.max_rows and (size <= limit or count <= 1):
                    break
                old.append(row)
                count -= 1
                size -= row["payload_bytes"]
        self._delete(db, old)

    def _delete(self, db, rows):
        for row in rows:
            db.execute(
                """UPDATE observation_catalog SET pruned_through=
                CASE WHEN pruned_through IS NULL OR pruned_through<? THEN ? ELSE pruned_through END
                WHERE project_id=? AND project_version=?""",
                (
                    row["observed_at"],
                    row["observed_at"],
                    row["project_id"],
                    row["project_version"],
                ),
            )
            db.execute("DELETE FROM observations WHERE id=?", (row["id"],))

    async def query(self, project_id, project_version, start, end, limit=200):
        start, end = utc(start), utc(end)
        if (
            start >= end
            or end - start > timedelta(days=7)
            or type(limit) is not int
            or not 1 <= limit <= 200
        ):
            raise ValueError("History requires an ordered window up to seven days and limit 1..200")
        return await asyncio.to_thread(self._query, project_id, project_version, start, end, limit)

    def _query(self, project_id, project_version, start, end, limit):
        with self._connect() as db:
            db.execute("BEGIN")
            catalog = db.execute(
                "SELECT * FROM observation_catalog WHERE project_id=? AND project_version=?",
                (project_id, project_version),
            ).fetchone()
            meta = db.execute(
                """SELECT id,observed_at FROM observations WHERE project_id=? AND project_version=?
                AND observed_at>=? AND observed_at<? ORDER BY observed_at,id LIMIT 50001""",
                (project_id, project_version, start.isoformat(), end.isoformat()),
            ).fetchall()
            if len(meta) <= limit:
                chosen = meta
            elif limit == 1:
                chosen = meta[-1:]
            else:
                indices = {round(index * (len(meta) - 1) / (limit - 1)) for index in range(limit)}
                chosen = [meta[index] for index in sorted(indices)]
            rows, read_bytes = [], 0
            # Read endpoints first so a payload cap cannot silently discard the newest point.
            ordered = chosen[:1] + chosen[-1:] + chosen[1:-1] if len(chosen) > 1 else chosen
            for row in ordered:
                record = db.execute(
                    "SELECT payload_json FROM observations WHERE id=?", (row["id"],)
                ).fetchone()
                read_bytes += len(record[0].encode())
                if read_bytes > 2 * 1024 * 1024:
                    break
                rows.append(json.loads(record[0]))
            db.rollback()
        rows.sort(key=lambda row: row["observed_at"])
        gaps = []
        timeline = [utc(row["observed_at"]) for row in meta]
        for before, after in zip(timeline, timeline[1:], strict=False):
            if (after - before).total_seconds() > self.interval * 2.5:
                gaps.append({"start": before.isoformat(), "end": after.isoformat()})
        leading_gap = not timeline or (timeline[0] - start).total_seconds() > self.interval * 2.5
        trailing_gap = not timeline or (end - timeline[-1]).total_seconds() > self.interval * 2.5
        coverage = {
            "kind": "retained_historical_samples",
            "historical": True,
            "requested_start": start.isoformat(),
            "requested_end": end.isoformat(),
            "sampling_interval_seconds": self.interval,
            "samples_in_window": len(meta),
            "samples_returned": len(rows),
            "sampled_down": len(rows) < len(meta),
            "first_recorded_at": catalog["first_seen_at"] if catalog else None,
            "last_recorded_at": catalog["last_seen_at"] if catalog else None,
            "pruned_through": catalog["pruned_through"] if catalog else None,
            "earliest_in_window": timeline[0].isoformat() if timeline else None,
            "latest_in_window": timeline[-1].isoformat() if timeline else None,
            "gap_count": len(gaps),
            "gaps": gaps[:20],
            "leading_gap": leading_gap,
            "trailing_gap": trailing_gap,
            "complete_window": bool(
                len(meta) >= 2 and not gaps and not leading_gap and not trailing_gap
            ),
            "note": (
                "No samples exist before collection started or after pruning. "
                "Counter rates use observed endpoints; gaps and resets limit interpretation."
            ),
        }
        summaries, histograms, failures = self._summaries(rows)
        if coverage["sampled_down"]:
            coverage["complete_window"] = False
            for metric in summaries:
                metric.update(
                    rate_per_second=None,
                    observed_delta=None,
                    note="sampled_down_reset_and_extrema_coverage_unknown",
                )
            for histogram in histograms:
                histogram.update(
                    interval_observation_count=None,
                    p50_upper_bound=None,
                    p95_upper_bound=None,
                    buckets=[],
                    note="sampled_down_reset_coverage_unknown",
                )
        if failures:
            coverage["complete_window"] = False
        content = {
            "coverage": coverage,
            "metric_summaries": summaries[:32],
            "histograms": histograms[:4],
            "source_failures": failures[:20],
        }
        omitted = max(0, len(summaries) - 32) + max(0, len(histograms) - 4)
        while len(canonical(content)) > MAX_CONTENT and (
            content["metric_summaries"] or content["histograms"]
        ):
            if content["metric_summaries"]:
                content["metric_summaries"].pop()
            else:
                content["histograms"].pop()
            omitted += 1
        coverage["omitted_metric_summaries"] = omitted
        raw = canonical(content)
        digest = hashlib.sha256(raw.encode()).hexdigest()
        collected = datetime.now(UTC).isoformat()
        identity = (
            f"history-{project_id}-{hashlib.sha256((digest + collected).encode()).hexdigest()[:24]}"
        )
        item = {
            "id": identity,
            "evidence_id": identity,
            "source": "metric_history",
            "version": f"sha256:{digest}",
            "content": raw,
            "observed_at": rows[-1]["observed_at"] if rows else None,
            "collected_at": collected,
            "provenance": {
                "collector": "local_observation_store",
                "project_id": project_id,
                "project_version": project_version,
            },
            "truncated": bool(omitted),
        }
        return {
            "source": "metric_history",
            "project_id": project_id,
            "project_version": project_version,
            "status": "unavailable"
            if not rows
            else "complete"
            if coverage["complete_window"]
            else "partial",
            "items": [item],
            "coverage": coverage,
            "truncated": bool(omitted or coverage["sampled_down"]),
            "error": {"code": "no_retained_samples"} if not rows else None,
        }

    def _summaries(self, rows):
        series, buckets, failures = {}, {}, []
        for row in rows:
            for source in row["sources"]:
                if source["status"] != "complete":
                    if source.get("error") != "not_configured":
                        failures.append(
                            {
                                "observed_at": row["observed_at"],
                                "source": source["source"],
                                "status": source["status"],
                                "error": source.get("error"),
                            }
                        )
                    continue
                metrics = source.get("metrics", [])
                starts = [
                    metric for metric in metrics if metric["name"] == "process_start_time_seconds"
                ]
                for metric in metrics:
                    if metric["name"].endswith("_created"):
                        continue
                    metric = {**metric, "_process_epoch": process_epoch(metric, starts)}
                    key = (source["source"], metric["name"], canonical(metric["labels"]))
                    series.setdefault(key, []).append((row["observed_at"], metric))
                    if metric["name"].endswith("_bucket") and "le" in metric["labels"]:
                        labels = {
                            key: value for key, value in metric["labels"].items() if key != "le"
                        }
                        group = (
                            source["source"],
                            metric["name"].removesuffix("_bucket"),
                            canonical(labels),
                        )
                        buckets.setdefault(group, {}).setdefault(metric["labels"]["le"], []).append(
                            (row["observed_at"], metric["value"], metric["_process_epoch"])
                        )
        summary = []
        for (source_name, name, _labels), points in sorted(series.items()):
            if name.endswith("_bucket"):
                continue
            first_at, first = points[0]
            last_at, last = points[-1]
            elapsed = (utc(last_at) - utc(first_at)).total_seconds()
            counter = last["type"] == "counter" or name.endswith("_count")
            consistent_type = len({point[1]["type"] for point in points}) == 1
            restarts = restart_count(point[1]["_process_epoch"] for point in points)
            resets = (
                sum(
                    after[1]["value"] < before[1]["value"]
                    for before, after in zip(points, points[1:], strict=False)
                )
                if counter
                else 0
            )
            enough = (
                len(points) >= 2 and elapsed > 0 and consistent_type and not (counter and restarts)
            )
            delta = last["value"] - first["value"] if enough and not resets else None
            summary.append(
                {
                    "name": name,
                    "source": source_name,
                    "labels": last["labels"],
                    "type": last["type"],
                    "unit": last["unit"],
                    "sample_count": len(points),
                    "first_at": first_at,
                    "last_at": last_at,
                    "first": first["value"],
                    "last": last["value"],
                    "min": min(point[1]["value"] for point in points),
                    "max": max(point[1]["value"] for point in points),
                    "observed_delta": delta,
                    "counter_resets": resets,
                    "process_restarts": restarts,
                    "rate_per_second": delta / elapsed if counter and delta is not None else None,
                    "note": "process_restart_detected"
                    if counter and restarts
                    else "metric_type_changed"
                    if not consistent_type
                    else "counter_reset_detected"
                    if resets
                    else "insufficient_samples"
                    if not enough
                    else "observed_samples_only",
                }
            )
        histograms = []
        for (source_name, name, labels), bounds in sorted(buckets.items()):
            deltas, reset, enough, windows = [], False, True, set()
            restarts = 0
            for bound, points in bounds.items():
                restarts = max(restarts, restart_count(point[2] for point in points))
                if len(points) < 2 or points[0][0] == points[-1][0]:
                    enough = False
                    continue
                if any(
                    after[1] < before[1] for before, after in zip(points, points[1:], strict=False)
                ):
                    reset = True
                try:
                    boundary = float(bound)
                except ValueError:
                    enough = False
                    continue
                if math.isnan(boundary) or boundary == -math.inf:
                    enough = False
                    continue
                windows.add((points[0][0], points[-1][0]))
                deltas.append((boundary, points[-1][1] - points[0][1], bound))
            deltas.sort()
            count = next((value for boundary, value, _ in deltas if boundary == math.inf), None)
            monotone = all(
                after[1] >= before[1] for before, after in zip(deltas, deltas[1:], strict=False)
            )
            valid = (
                enough
                and not restarts
                and len(windows) == 1
                and not reset
                and monotone
                and count is not None
                and all(value >= 0 for _, value, _ in deltas)
            )
            quantiles = {}
            for quantile, minimum in ((0.5, 20), (0.95, 100)):
                quantiles[quantile] = (
                    next((bound for _, value, bound in deltas if value >= count * quantile), None)
                    if valid and count >= minimum
                    else None
                )
            compact = []
            previous = None
            for boundary, value, bound in deltas:
                if value != previous or math.isinf(boundary):
                    compact.append({"le": bound, "count_delta": value})
                previous = value
            histograms.append(
                {
                    "name": name,
                    "source": source_name,
                    "labels": json.loads(labels),
                    "unit": metric_unit(name),
                    "interval_observation_count": count if valid else None,
                    "process_restarts": restarts,
                    "p50_upper_bound": quantiles[0.5],
                    "p95_upper_bound": quantiles[0.95],
                    "buckets": compact if valid else [],
                    "note": "process_restart_detected"
                    if restarts
                    else "counter_reset_detected"
                    if reset
                    else "insufficient_samples"
                    if not valid
                    else "bucket_upper_bounds_not_exact_quantiles",
                }
            )
        return summary, histograms, failures
