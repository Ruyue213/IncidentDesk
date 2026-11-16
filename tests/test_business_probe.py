"""HTTP liveness alone never establishes a successful registered business check."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from incident_desk.projects import ProjectProfile
from incident_desk.settings import Settings
from incident_desk.tools.business_probe import BusinessProbe, validate_probe


@pytest.mark.asyncio
async def test_real_http_200_requires_registered_json_assertion_and_snapshots_are_logged(tmp_path):
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append((self.path, body))
            data = b'{"distance":0}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    config = Settings(_env_file=None, api_key=None, local_dir=tmp_path / "state")
    project = ProjectProfile(
        id="actual",
        name="Actual",
        root=tmp_path,
        health_url=f"http://127.0.0.1:{server.server_port}/healthz",
        business_probe={
            "method": "POST",
            "path": "/query",
            "body": {"type": "dist", "a": 1, "b": 1},
            "expected_json": {"distance": 0},
        },
    )
    try:
        result = await BusinessProbe(config).run(project, attempts=3)
        assert result["status"] == "complete" and result["business_ok"]
        assert len(result["items"]) == 3 and len(calls) == 3
        assert all(
            path == "/query" and body == {"type": "dist", "a": 1, "b": 1} for path, body in calls
        )
        records = (config.local_dir / "business-probes.jsonl").read_text().splitlines()
        assert len(records) == 3 and json.loads(records[0])["result"]["business_ok"]
        mismatch = project.model_copy(
            update={
                "business_probe": {
                    "method": "POST",
                    "path": "/query",
                    "body": {"type": "dist", "a": 1, "b": 1},
                    "expected_json": {"distance": 1},
                }
            }
        )
        failed = await BusinessProbe().run(mismatch)
        evidence = json.loads(failed["items"][0]["content"])
        assert failed["status"] == "complete" and not failed["business_ok"]
        assert evidence["http_status"] == 200 and not evidence["assertions"]["expected_json"]
        assert evidence["outcome"] == "failed"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize(
    "changes",
    [
        {"path": "/edge"},
        {"path": "//elsewhere/query"},
        {"path": "/query?x=1"},
        {"path": "/%71uery"},
        {"path": "/admin/status", "method": "GET"},
        {"body": {"type": "cf", "a": True, "b": 2}},
        {"body": {"type": "dist", "a": 1, "b": 1, "write": True}},
        {"body": {"type": "sd", "a": 1, "b": 2}},
        {"expected_json": {}},
        {"max_latency_ms": float("nan")},
        {"path": 1},
    ],
)
def test_probe_validation_rejects_writes_dynamic_parameters_and_weak_contracts(changes):
    probe = {
        "method": "POST",
        "path": "/query",
        "body": {"type": "dist", "a": 1, "b": 1},
        "expected_json": {"distance": 0},
    } | changes
    project = SimpleNamespace(health_url="http://127.0.0.1:19000/healthz", business_probe=probe)
    with pytest.raises((ValueError, TypeError)):
        validate_probe(project)


@pytest.mark.asyncio
async def test_unavailable_probe_and_attempt_bounds_are_honest(tmp_path):
    project = ProjectProfile(
        id="actual",
        name="Actual",
        root=tmp_path,
        health_url="http://127.0.0.1:1/healthz",
        business_probe={
            "method": "POST",
            "path": "/query",
            "body": {"type": "dist", "a": 1, "b": 1},
            "expected_json": {"distance": 0},
        },
    )
    result = await BusinessProbe().run(project)
    assert result["status"] == "unavailable" and not result["business_ok"]
    assert json.loads(result["items"][0]["content"])["outcome"] == "unavailable"
    for attempts in (0, 4, True):
        with pytest.raises(ValueError):
            await BusinessProbe().run(project, attempts)


@pytest.mark.asyncio
async def test_secret_invalid_json_and_large_body_are_bounded_without_false_business_success(
    tmp_path,
):
    bodies = iter(
        [
            b'{"distance":0,"password":"private-value"}',
            b'{"distance":NaN}',
            json.dumps({"distance": 0, "padding": "x" * 20000}).encode(),
            json.dumps({"distance": 0, "padding": "x" * 100000}).encode(),
        ]
    )

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            data = next(bodies)
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    project = ProjectProfile(
        id="actual",
        name="Actual",
        root=tmp_path,
        health_url=f"http://127.0.0.1:{server.server_port}/healthz",
        business_probe={
            "method": "POST",
            "path": "/query",
            "body": {"type": "dist", "a": 1, "b": 1},
            "expected_json": {"distance": 0},
        },
    )
    try:
        results = [await BusinessProbe().run(project) for _ in range(4)]
        assert [result["business_ok"] for result in results] == [False, False, True, False]
        assert "private-value" not in json.dumps(results)
        assert all(len(result["items"][0]["content"]) <= 12000 for result in results)
        assert results[2]["truncated"] and results[3]["truncated"]
        assert not json.loads(results[3]["items"][0]["content"])["assertions"]["complete_response"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
