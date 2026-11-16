"""Observed history must remain honest across gaps, restart, resets and retention."""

import json
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
from fastapi import FastAPI

from incident_desk.api.observations import create_observations_router
from incident_desk.observations import ObservationStore, metric_unit, parse_metrics
from incident_desk.projects import ProjectProfile, ProjectStore
from incident_desk.settings import Settings


def save_point(store, at, value, *, version=1, raw="", extra=""):
    metrics, _, _ = parse_metrics(f"# TYPE work_total counter\nwork_total {value}\n" + extra)
    store._save(
        {
            "project_id": "actual",
            "project_version": version,
            "observed_at": at.isoformat(),
            "sources": [
                {"source": "metrics", "status": "complete", "raw": raw, "metrics": metrics}
            ],
            "sampling_interval_seconds": 10,
        }
    )


def contents(result):
    return json.loads(result["items"][0]["content"])


@pytest.mark.asyncio
async def test_history_survives_restart_is_version_scoped_and_never_invents_past(tmp_path):
    at = datetime.now(UTC)
    store = ObservationStore(tmp_path)
    save_point(store, at, 10)
    save_point(store, at + timedelta(seconds=10), 30)
    save_point(store, at + timedelta(seconds=11), 999, version=2)
    reopened = ObservationStore(tmp_path)
    result = await reopened.query(
        "actual", 1, at - timedelta(seconds=60), at + timedelta(seconds=11)
    )
    data = contents(result)
    assert result["status"] == "partial" and data["coverage"]["leading_gap"]
    assert data["coverage"]["first_recorded_at"] == at.isoformat()
    metric = data["metric_summaries"][0]
    assert metric["first"] == 10 and metric["last"] == 30 and metric["rate_per_second"] == 2
    empty = await reopened.query("actual", 1, at - timedelta(hours=1), at - timedelta(seconds=1))
    assert empty["status"] == "unavailable" and empty["coverage"]["samples_in_window"] == 0
    assert contents(empty)["metric_summaries"] == []


@pytest.mark.asyncio
async def test_reset_gaps_downsampling_and_insufficient_samples_are_explicit(tmp_path):
    at = datetime.now(UTC)
    store = ObservationStore(tmp_path)
    for seconds, value in ((0, 10), (10, 40), (60, 1), (70, 20)):
        save_point(store, at + timedelta(seconds=seconds), value)
    result = await store.query("actual", 1, at, at + timedelta(seconds=71))
    metric = contents(result)["metric_summaries"][0]
    assert result["coverage"]["gap_count"] == 1
    assert metric["counter_resets"] == 1 and metric["rate_per_second"] is None
    downsampled = await store.query("actual", 1, at, at + timedelta(seconds=71), limit=2)
    assert (
        downsampled["coverage"]["sampled_down"] and not downsampled["coverage"]["complete_window"]
    )
    assert contents(downsampled)["metric_summaries"][0]["observed_delta"] is None
    single = await store.query("actual", 1, at, at + timedelta(seconds=1))
    assert contents(single)["metric_summaries"][0]["note"] == "insufficient_samples"


@pytest.mark.asyncio
async def test_positive_counter_and_histogram_growth_across_known_restart_has_no_rate(tmp_path):
    at = datetime.now(UTC)
    store = ObservationStore(tmp_path)
    for seconds, start, value in ((0, 1000, 100), (10, 2000, 200)):
        extra = (
            f'process_start_time_seconds{{instance="a"}} {start}\n'
            f'process_start_time_seconds{{instance="b"}} 900\n'
            "# TYPE requests_total counter\n"
            f'requests_total{{instance="a"}} {value}\n'
            f'requests_total{{instance="b"}} {value}\n'
            "# TYPE duration_seconds histogram\n"
            f'duration_seconds_bucket{{instance="a",le="1"}} {value}\n'
            f'duration_seconds_bucket{{instance="a",le="+Inf"}} {value}\n'
        )
        save_point(store, at + timedelta(seconds=seconds), value, extra=extra)
    data = contents(await store.query("actual", 1, at, at + timedelta(seconds=11)))
    by_instance = {
        row["labels"]["instance"]: row
        for row in data["metric_summaries"]
        if row["name"] == "requests_total"
    }
    assert by_instance["a"]["process_restarts"] == 1
    assert (
        by_instance["a"]["observed_delta"] is None and by_instance["a"]["rate_per_second"] is None
    )
    assert by_instance["a"]["counter_resets"] == 0
    assert by_instance["b"]["process_restarts"] == 0 and by_instance["b"]["rate_per_second"] == 10
    assert data["histograms"][0]["process_restarts"] == 1
    assert data["histograms"][0]["interval_observation_count"] is None
    assert data["histograms"][0]["p95_upper_bound"] is None


@pytest.mark.asyncio
async def test_failed_scrapes_remain_historical_observations_and_report_unavailable(tmp_path):
    store = ObservationStore(tmp_path)
    project = ProjectProfile(
        id="actual", name="Actual", root=tmp_path, health_url="http://127.0.0.1:1/healthz"
    )
    at = datetime.now(UTC)
    failed = await store.sample(project)
    assert failed["status"] == "unavailable"
    history = await store.query("actual", 1, at, datetime.now(UTC) + timedelta(seconds=1))
    assert history["coverage"]["samples_in_window"] == 1
    assert contents(history)["source_failures"][0]["source"] == "health"


@pytest.mark.asyncio
async def test_histograms_use_observed_bucket_bounds_units_and_minimum_samples(tmp_path):
    at = datetime.now(UTC)
    store = ObservationStore(tmp_path)
    histogram = (
        "# TYPE engine_query_duration_us histogram\n"
        'engine_query_duration_us_bucket{{le="100"}} {}\n'
        'engine_query_duration_us_bucket{{le="1000"}} {}\n'
        'engine_query_duration_us_bucket{{le="+Inf"}} {}\n'
    )
    save_point(store, at, 0, extra=histogram.format(0, 0, 0))
    save_point(store, at + timedelta(seconds=10), 100, extra=histogram.format(70, 98, 100))
    result = contents(await store.query("actual", 1, at, at + timedelta(seconds=11)))
    observed = result["histograms"][0]
    assert observed["unit"] == "microseconds" and observed["interval_observation_count"] == 100
    assert observed["p50_upper_bound"] == "100" and observed["p95_upper_bound"] == "1000"
    assert metric_unit("engine_cache_hit_rate") == "permille"
    assert metric_unit("network_bytes_total") == "bytes"
    assert parse_metrics("x NaN\ny +Inf\n")[2] == 2


@pytest.mark.asyncio
async def test_retention_row_and_storage_limits_leave_prune_evidence(tmp_path):
    at = datetime.now(UTC)
    store = ObservationStore(
        tmp_path, max_rows=3, max_storage_bytes=1024 * 1024, retention_seconds=60
    )
    for index in range(30):
        save_point(store, at + timedelta(seconds=10 * index), index, raw="x" * 60000)
    result = await store.query("actual", 1, at, at + timedelta(seconds=301))
    assert result["coverage"]["samples_in_window"] <= 3
    assert result["coverage"]["pruned_through"] is not None
    assert store.path.stat().st_size <= store.max_storage_bytes
    assert (
        sum(path.stat().st_size for path in tmp_path.glob("observations.db*"))
        <= store.max_storage_bytes + 1024 * 1024
    )
    assert store.path.stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_real_sampling_is_bounded_redacted_and_api_does_not_invent_history(tmp_path):
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            calls.append(self.path)
            body = (
                b"# TYPE work_total counter\nwork_total 2\n# password=private\n"
                if self.path == "/metrics"
                else b'{"ok":true}'
            )
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    config = Settings(_env_file=None, api_key=None, local_dir=tmp_path / "state")
    store = ProjectStore(config.local_dir)
    base = f"http://127.0.0.1:{server.server_port}"
    project = store.save(
        ProjectProfile(
            id="actual",
            name="Actual",
            root=tmp_path,
            health_url=base + "/health",
            metrics_url=base + "/metrics",
        ),
        expected_version=0,
    )
    observations = ObservationStore(config.local_dir)
    app = FastAPI()
    app.include_router(create_observations_router(config, store, observations))
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://local"
        ) as client:
            at = datetime.now(UTC)
            response = await client.post("/api/projects/actual/sample")
            assert response.status_code == 200 and response.json()["status"] == "complete"
            assert "private" not in json.dumps(response.json())
            history = await client.get(
                "/api/projects/actual/observations",
                params={
                    "start": at.isoformat(),
                    "end": (datetime.now(UTC) + timedelta(seconds=1)).isoformat(),
                },
            )
            assert (
                history.status_code == 200 and history.json()["coverage"]["samples_in_window"] == 1
            )
            assert (
                await client.get(
                    "/api/projects/actual/observations",
                    params={"start": "2026-01-01T00:00:00", "end": "2026-01-02T00:00:00"},
                )
            ).status_code == 422
            assert (await client.post("/api/projects/missing/sample")).status_code == 404
        disabled = project.model_copy(update={"history_enabled": False})
        assert (await observations.sample(disabled))["status"] == "skipped"
        assert (await observations.sample(ProjectProfile(id="empty", name="Empty", root=tmp_path)))[
            "reason"
        ] == "no_sources"
        assert calls == ["/health", "/metrics"] or calls == ["/metrics", "/health"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
