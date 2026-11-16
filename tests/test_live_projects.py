"""Real local source and registration security contracts; no model API calls."""

import asyncio
import hashlib
import json
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
from fastapi import FastAPI

from incident_desk.api.projects import create_projects_router
from incident_desk.projects import ProjectConflict, ProjectProfile, ProjectStore, safe_project_path
from incident_desk.settings import Settings
from incident_desk.tools.live_evidence import LiveEvidence, redact


def profile(root, **changes):
    return ProjectProfile(id="actual", name="Actual", root=root, **changes)


def settings(root):
    return Settings(_env_file=None, api_key=None, local_dir=root / "state")


@pytest.mark.asyncio
async def test_current_http_collector_has_an_overall_deadline(tmp_path, monkeypatch):
    from incident_desk.tools import live_evidence

    class SlowBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            for _ in range(100):
                await asyncio.sleep(0.01)
                yield b"x"

    original = httpx.AsyncClient
    transport = httpx.MockTransport(lambda request: httpx.Response(200, stream=SlowBody()))
    monkeypatch.setattr(live_evidence, "HTTP_DEADLINE_SECONDS", 0.03)
    monkeypatch.setattr(
        live_evidence.httpx, "AsyncClient", lambda **kwargs: original(transport=transport, **kwargs)
    )
    collector = LiveEvidence(
        profile(tmp_path, health_url="http://127.0.0.1:12345/health"), settings(tmp_path)
    )
    now = datetime.now(UTC)
    batch = await asyncio.wait_for(collector.get("health", now - timedelta(minutes=1), now), 0.5)
    assert batch["status"] == "unavailable"
    assert batch["error"]["code"] == "TimeoutError"
    assert not batch["items"]


def test_versions_are_private_immutable_and_conflicts_explicit(tmp_path):
    store = ProjectStore(tmp_path / "state")
    first = store.save(profile(tmp_path), expected_version=0)
    second = store.save(first.model_copy(update={"name": "Updated"}), expected_version=1)
    assert first.version == 1 and second.version == 2
    assert store.get("actual", 1).name == "Actual"
    assert store.get("actual").name == "Updated"
    assert (store.directory / "actual/1.json").stat().st_mode & 0o777 == 0o600
    assert store.directory.stat().st_mode & 0o777 == 0o700
    with pytest.raises(ProjectConflict):
        store.save(first, expected_version=1)
    assert store.list_public()[0]["config_digest"] == second.digest


@pytest.mark.parametrize(
    "changes",
    [
        {"health_url": "http://example.com/health"},
        {"health_url": "http://127.0.0.1/health?token=private"},
        {"health_url": "http://user:private@localhost/health"},
        {"log_files": [".env"]},
        {"config_files": ["../outside.conf"]},
        {"start_argv": ["/bin/sh", "-c", "echo no"]},
        {"start_argv": ["./server", "--api-key", "private"]},
    ],
)
def test_registration_rejects_external_urls_secrets_and_escapes(tmp_path, changes):
    with pytest.raises(ValueError):
        profile(tmp_path, **changes)


@pytest.mark.asyncio
async def test_log_timestamps_redaction_empty_and_errors_are_distinct(tmp_path):
    (tmp_path / "runtime.log").write_text(
        "2026-11-12 13:10:00.123 INFO password=private failure\n"
        "2026-11-12T05:11:00Z INFO success\nundated event\n"
        "2026-11-11 13:10:00 INFO outside window\n"
    )
    collector = LiveEvidence(profile(tmp_path, log_files=["runtime.log"]), settings(tmp_path))
    result = await collector.get("logs", "2026-11-12T05:00:00Z", "2026-11-12T06:00:00Z")
    assert result["status"] == "partial" and result["error"] is None
    assert len(result["items"]) == 2
    assert result["items"][0]["observed_at"] == "2026-11-12T05:10:00.123000+00:00"
    assert result["items"][0]["provenance"]["assumed_timezone"] == "Asia/Shanghai"
    assert "private" not in json.dumps(result)
    assert result["coverage"]["undated_lines_excluded"] == 1
    empty = await collector.get("logs", "2025-01-01T00:00:00Z", "2025-01-02T00:00:00Z")
    assert empty["items"] == [] and empty["error"] is None
    missing = LiveEvidence(profile(tmp_path, log_files=["absent.log"]), settings(tmp_path))
    unavailable = await missing.get("logs", "2026-11-12T05:00:00Z", "2026-11-12T06:00:00Z")
    assert unavailable["status"] == "unavailable" and unavailable["error"]


@pytest.mark.asyncio
async def test_fixed_managed_log_is_collected(tmp_path):
    config = settings(tmp_path)
    managed = config.local_dir / "managed/actual"
    managed.mkdir(parents=True)
    (managed / "service.log").write_text("2026-11-12 13:10:00 INFO actual managed event\n")
    result = await LiveEvidence(profile(tmp_path), config).get(
        "logs", "2026-11-12T05:00:00Z", "2026-11-12T06:00:00Z"
    )
    assert result["items"][0]["provenance"]["collector"] == "managed_log"


@pytest.mark.asyncio
async def test_source_tools_deny_secrets_and_symlink_escape(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "main.cpp").write_text("int main() { return 0; } // password=private\n")
    (root / ".env").write_text("API_KEY=not-readable")
    outside = tmp_path / "outside.py"
    outside.write_text("private")
    (root / "escape.py").symlink_to(outside)
    collector = LiveEvidence(profile(root), settings(tmp_path))
    result = await collector.read_file("main.cpp")
    assert "private" not in result["items"][0]["content"]
    for path in (".env", "escape.py", "../outside.py"):
        with pytest.raises(ValueError):
            await collector.read_file(path)
    matches = await collector.search("main")
    assert "main.cpp" in matches["items"][0]["content"]
    assert "outside.py" not in matches["items"][0]["content"]
    with pytest.raises(ValueError):
        safe_project_path(profile(root), "escape.py")


@pytest.mark.asyncio
async def test_config_file_hash_matches_original_bytes_and_editability_is_explicit(tmp_path):
    config = tmp_path / "server.conf"
    raw = b"port=8080\nqueue=32\n"
    config.write_bytes(raw)
    collector = LiveEvidence(profile(tmp_path, config_files=["server.conf"]), settings(tmp_path))
    result = await collector.read_file("server.conf")
    origin = result["items"][0]["provenance"]
    assert result["file_sha256"] == hashlib.sha256(raw).hexdigest()
    assert result["editable"] and origin["registered_config_file"]
    assert origin["content_format"] == "full_file_plain_text"
    assert result["items"][0]["content"] == raw.decode()
    config.write_text("password=private\n")
    result = await collector.read_file("server.conf")
    assert not result["editable"] and "private" not in result["items"][0]["content"]
    config.write_bytes(b"x" * (256 * 1024 + 1))
    result = await collector.read_file("server.conf")
    assert result["file_sha256"] is None and not result["editable"]


@pytest.mark.asyncio
async def test_real_http_is_current_bounded_and_redirects_are_not_followed(tmp_path):
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            calls.append(self.path)
            if self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", "/forbidden")
                body = b"redirect declined"
            else:
                self.send_response(200)
                body = b"password=private\n" + b"metric 1\n" * 10000
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        collector = LiveEvidence(
            profile(tmp_path, health_url=base + "/redirect", metrics_url=base + "/metrics"),
            settings(tmp_path),
        )
        now = datetime.now(UTC)
        metrics = await collector.get("metrics", now - timedelta(days=1), now)
        assert metrics["coverage"]["historical"] is False
        assert metrics["status"] == "partial" and metrics["truncated"]
        assert len(metrics["items"][0]["content"]) <= 12000
        assert "private" not in json.dumps(metrics)
        health = await collector.get("health", now - timedelta(minutes=1), now)
        assert health["status"] == "unavailable"
        assert health["error"]["status"] == 302 and "/forbidden" not in calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.asyncio
async def test_api_probe_and_validation_do_not_echo_secret(tmp_path):
    app = FastAPI()
    app.include_router(create_projects_router(settings(tmp_path)))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
    ) as client:
        body = {"name": "Actual", "root": str(tmp_path), "expected_version": 0}
        saved = await client.put("/api/projects/actual", json=body)
        assert saved.status_code == 200 and saved.json()["version"] == 1
        assert (await client.put("/api/projects/actual", json=body)).status_code == 409
        assert len((await client.get("/api/projects")).json()["items"]) == 1
        bad = await client.put(
            "/api/projects/other",
            json=body | {"start_argv": ["./server", "--password", "private-api-credential"]},
        )
        assert bad.status_code == 422 and "private-api-credential" not in bad.text
        extra = await client.put("/api/projects/other", json=body | {"secret": "private-extra"})
        assert extra.status_code == 422 and "private-extra" not in extra.text
        probe = (await client.post("/api/projects/actual/probe")).json()
        assert probe["project_version"] == 1 and len(probe["items"]) == 6


def test_redaction_including_process_args_and_private_key():
    text = (
        "--password private authorization: Bearer private\n"
        "DEEPSEEK_API_KEY=private https://user:private@localhost/path\n"
        "-----BEGIN PRIVATE KEY-----\nprivate\n-----END PRIVATE KEY-----"
    )
    assert "private" not in redact(text)


@pytest.mark.asyncio
async def test_overview_reports_actual_binary_without_reading_and_excludes_private_paths(
    tmp_path, monkeypatch
):
    root = tmp_path / "project"
    (root / "bin").mkdir(parents=True)
    binary = root / "bin/server"
    binary.write_bytes(b"\0actual executable content must never be read")
    binary.chmod(0o700)
    (root / "main.cpp").write_text("int main() {}")
    (root / "server.conf").write_text("port=8080\n")
    (root / ".env").write_text("password=private")
    (root / "third_party").mkdir()
    (root / "third_party/vendor.cpp").write_text("dependency")
    (root / "alias.cpp").symlink_to(root / "main.cpp")
    collector = LiveEvidence(
        profile(
            root,
            start_argv=["bin/server"],
            health_url="http://127.0.0.1:8080/healthz",
            log_files=["runtime.log"],
            config_files=["server.conf"],
        ),
        settings(tmp_path),
    )

    def refuse_content_reads(*_args, **_kwargs):
        raise AssertionError("overview must use stat-only inspection")

    monkeypatch.setattr("incident_desk.tools.live_evidence._read_bounded", refuse_content_reads)
    monkeypatch.setattr("incident_desk.tools.live_evidence._read_source", refuse_content_reads)
    result = await collector.overview()
    content = json.loads(result["items"][0]["content"])
    executable = content["start_executable"]
    assert executable["exists"] and executable["is_file"] and executable["executable"]
    assert executable["size"] == binary.stat().st_size
    assert not executable["readable_by_source_tool"]
    assert content["registered_endpoints"]["health"] == "http://127.0.0.1:8080/healthz"
    assert content["registered_config_files"] == ["server.conf"]
    assert content["registered_log_files"] == ["runtime.log"]
    assert content["allowed_text_files"] == ["main.cpp", "server.conf"]
    assert result["status"] == "complete" and result["coverage"]["historical"] is False


@pytest.mark.asyncio
async def test_overview_scan_and_path_limits_do_not_claim_absent_files(tmp_path):
    root = tmp_path / "many-binary"
    root.mkdir()
    for number in range(510):
        (root / f"binary{number}.dat").touch()
    collector = LiveEvidence(profile(root, start_argv=["missing-server"]), settings(tmp_path))
    result = await collector.overview()
    content = json.loads(result["items"][0]["content"])
    assert result["status"] == "partial" and result["truncated"]
    assert result["coverage"]["entries_scanned"] == 500
    assert not content["start_executable"]["exists"]
    assert "not proof" in content["note"]
    source_root = tmp_path / "many-source"
    source_root.mkdir()
    for number in range(205):
        (source_root / f"module{number}.py").touch()
    result = await LiveEvidence(profile(source_root), settings(tmp_path)).overview()
    content = json.loads(result["items"][0]["content"])
    assert len(content["allowed_text_files"]) == 200
    assert result["truncated"] and result["coverage"]["entries_scanned"] <= 500
