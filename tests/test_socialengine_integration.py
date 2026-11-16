"""Opt-in integration against the user's real C++ binary in isolated temporary state.

Set SOCIALENGINE_ROOT to a built checkout. No formal project registration or model call occurs.
"""

import asyncio
import json
import os
import shutil
import socket
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from incident_desk.projects import ProjectProfile
from incident_desk.settings import Settings
from incident_desk.tools.live_evidence import LiveEvidence


@pytest.mark.asyncio
async def test_real_socialengine_collectors_loopback_bind_failure_and_shutdown(tmp_path):
    configured = os.environ.get("SOCIALENGINE_ROOT")
    if not configured:
        pytest.skip("SOCIALENGINE_ROOT not configured; explicit real binary integration only")
    root = await asyncio.to_thread(Path(configured).resolve)
    binary = root / "bin/server"
    assert binary.is_file(), "Build the explicitly selected SocialEngine checkout first"
    local = tmp_path / "state"
    managed = local / "managed/socialengine"
    managed.mkdir(parents=True)
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    args = [
        str(binary),
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--nodes",
        "100",
        "--degree",
        "2",
        "--pool",
        "2",
        "--queue",
        "32",
        "--reactors",
        "1",
        "--no-redis",
        "--wal",
        str(tmp_path / "graph.wal"),
        "--snapshot",
        str(tmp_path / "graph.snapshot"),
    ]
    with (managed / "service.log").open("wb") as log:
        child = await asyncio.to_thread(subprocess.Popen, args, cwd=root, stdout=log, stderr=log)
        try:
            async with httpx.AsyncClient(timeout=1, trust_env=False) as client:
                for _attempt in range(50):
                    assert child.poll() is None, "isolated target exited before readiness"
                    try:
                        response = await client.get(f"http://127.0.0.1:{port}/healthz")
                        if response.status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    await asyncio.sleep(0.1)
                else:
                    pytest.fail("isolated target failed readiness")
            identity_result = await asyncio.to_thread(
                subprocess.run,
                ["ps", "-p", str(child.pid), "-o", "lstart=", "-o", "command="],
                capture_output=True,
                text=True,
                check=True,
            )
            identity = identity_result.stdout.strip()
            (managed / "process.json").write_text(
                json.dumps(
                    {
                        "pid": child.pid,
                        "identity": identity,
                        "project_version": 1,
                        "operation_id": "isolated-test",
                    }
                )
            )
            project = ProjectProfile(
                id="socialengine",
                name="Isolated real binary",
                root=root,
                start_argv=args,
                health_url=f"http://127.0.0.1:{port}/healthz",
                metrics_url=f"http://127.0.0.1:{port}/metrics",
                stats_url=f"http://127.0.0.1:{port}/stats",
            )
            collector = LiveEvidence(
                project, Settings(_env_file=None, api_key=None, local_dir=local)
            )
            now = datetime.now(UTC)
            batches = {
                source: await collector.get(source, now - timedelta(minutes=1), now)
                for source in ("health", "metrics", "stats", "process", "logs")
            }
            assert all(batch["items"] for batch in batches.values())
            observed = json.loads(batches["process"]["items"][0]["content"])
            assert any(
                p["pid"] == child.pid and p["ownership"] == "managed_identity_verified"
                for p in observed["matching_processes"]
            )
            assert batches["metrics"]["coverage"]["historical"] is False
            if shutil.which("lsof"):
                listener_result = await asyncio.to_thread(
                    subprocess.run,
                    ["lsof", "-nP", "-a", "-p", str(child.pid), "-iTCP", "-sTCP:LISTEN"],
                    capture_output=True,
                    text=True,
                    check=True,
                )
                listener = listener_result.stdout
                assert f"127.0.0.1:{port}" in listener and f"*:{port}" not in listener
            conflict = await asyncio.to_thread(
                subprocess.run,
                [
                    str(binary),
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(port),
                    "--nodes",
                    "30",
                    "--degree",
                    "2",
                    "--pool",
                    "1",
                    "--no-redis",
                ],
                cwd=root,
                capture_output=True,
                timeout=10,
            )
            assert conflict.returncode == 2
        finally:
            if child.poll() is None:
                child.terminate()
            try:
                await asyncio.to_thread(child.wait, timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
                await asyncio.to_thread(child.wait, timeout=3)
    assert child.returncode == 0
    assert "server exited cleanly" in (managed / "service.log").read_text()
