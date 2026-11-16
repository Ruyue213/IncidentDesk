"""Two real sibling processes must never be confused with a stopped registered target."""

import asyncio
import json
import shutil
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from incident_desk.actions import identity
from incident_desk.projects import ProjectProfile
from incident_desk.settings import Settings
from incident_desk.tools.live_evidence import LiveEvidence


@pytest.mark.asyncio
async def test_target_identity_and_listener_exclude_other_same_cwd_instance(tmp_path):
    if not shutil.which("lsof") and not await asyncio.to_thread(Path("/proc/self/fd").is_dir):
        pytest.skip("Requires listener ownership inspection")
    script = tmp_path / "listener.py"
    script.write_text("""from http.server import HTTPServer, BaseHTTPRequestHandler
import sys
class Handler(BaseHTTPRequestHandler):
 def do_GET(self):
  self.send_response(200); self.end_headers(); self.wfile.write(b'{}')
 def log_message(self,*args): pass
server=HTTPServer(('127.0.0.1',0),Handler)
open(sys.argv[1],'w').write(str(server.server_port))
server.serve_forever()
""")
    children = []
    config = Settings(_env_file=None, api_key=None, local_dir=tmp_path / "state")
    try:
        for index in range(2):
            ready = tmp_path / f"ready-{index}"
            command = [sys.executable, str(script), str(ready)]
            process = await asyncio.to_thread(
                subprocess.Popen,
                command,
                cwd=tmp_path,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            children.append(process)
            for _ in range(100):
                if ready.exists() and ready.read_text():
                    break
                await asyncio.sleep(0.02)
            assert ready.exists() and ready.read_text()
        target, other = children
        url = f"http://127.0.0.1:{(tmp_path / 'ready-0').read_text()}/healthz"
        project = ProjectProfile(
            id="scoped",
            name="Scoped",
            root=tmp_path,
            start_argv=[sys.executable, str(script), str(tmp_path / "ready-0")],
            health_url=url,
        )
        collector = LiveEvidence(project, config)
        at = datetime.now(UTC)

        async def read():
            response = await collector.get(
                "process", at - timedelta(minutes=1), at + timedelta(minutes=1)
            )
            assert response["items"], response
            return response, json.loads(response["items"][0]["content"])

        _, content = await read()
        assert content["running"] is True
        assert [row["pid"] for row in content["matching_processes"]] == [target.pid]
        assert other.pid in {row["pid"] for row in content["other_observed"]}
        state = config.local_dir / "managed/scoped/process.json"
        state.parent.mkdir(parents=True)
        state.write_text(
            json.dumps(
                {"pid": target.pid, "identity": await asyncio.to_thread(identity, target.pid)}
            )
        )
        target.terminate()
        await asyncio.to_thread(target.wait, timeout=5)
        _, stopped = await read()
        assert stopped["running"] is False and stopped["matching_processes"] == []
        assert other.poll() is None
        assert other.pid in {row["pid"] for row in stopped["other_observed"]}
        state.unlink()
        response, uncertain = await read()
        assert uncertain["running"] is None and response["status"] == "partial"
        async with httpx.AsyncClient() as client:
            other_url = f"http://127.0.0.1:{(tmp_path / 'ready-1').read_text()}/healthz"
            assert (await client.get(other_url)).status_code == 200
    finally:
        for child in children:
            if child.poll() is None:
                child.terminate()
                try:
                    await asyncio.to_thread(child.wait, timeout=3)
                except subprocess.TimeoutExpired:
                    child.kill()
                    await asyncio.to_thread(child.wait, timeout=3)
