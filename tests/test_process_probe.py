"""Listener attribution checks, with a real local socket and no external requests."""

import asyncio
import os
import shutil
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from incident_desk import process_probe as probe

HAS_PROCFS = sys.platform.startswith("linux") and Path("/proc/self/fd").is_dir()


class QuietHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass


@pytest.mark.asyncio
async def test_actual_http_listener_belongs_to_current_pid_and_not_wrong_pid_or_port():
    if not shutil.which("lsof") and not HAS_PROCFS:
        pytest.skip("This OS has neither lsof nor inspectable Linux procfs")
    with HTTPServer(("127.0.0.1", 0), QuietHandler) as server:
        # HTTPServer binds/listens during construction; no HTTP request is needed.
        port = server.server_address[1]
        assert await probe.owns_health_listener(os.getpid(), f"http://127.0.0.1:{port}/health")
        assert not await probe.owns_health_listener(2**31 - 1, f"http://127.0.0.1:{port}/health")
        with HTTPServer(("127.0.0.1", 0), QuietHandler) as other:
            unused = other.server_address[1]
        assert not await probe.owns_health_listener(
            os.getpid(), f"http://127.0.0.1:{unused}/health"
        )


def test_lsof_parser_requires_matching_pid_family_and_port():
    output = b"p123\nf4\ntIPv4\nn127.0.0.1:8080\nf5\ntIPv6\nn[::1]:9090\n"
    assert probe._lsof_bindings(output, 123, 8080) == {"127.0.0.1"}
    assert probe._lsof_bindings(output, 999, 8080) == set()
    assert probe._lsof_bindings(output, 123, 9090) == {"::1"}
    assert probe._lsof_bindings(b"p123\nf4\ntIPv4\nn*:8080\n", 123, 8080) == {"0.0.0.0"}
    assert probe._covered({"127.0.0.1"}, {"0.0.0.0"})
    assert not probe._covered({"127.0.0.1"}, {"::"})
    assert not probe._covered({"127.0.0.1", "::1"}, {"127.0.0.1"})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/health",
        "http://192.168.1.1:8080/health",
        "http://user:secret@127.0.0.1:8080/health",
        "file:///tmp/private",
        "http://127.0.0.1:invalid/health",
    ],
)
async def test_non_loopback_or_invalid_urls_never_start_inspection(url, monkeypatch):
    async def forbidden(*args):
        raise AssertionError("must not inspect for an invalid or non-loopback URL")

    monkeypatch.setattr(probe, "_lsof", forbidden)
    assert not await probe.owns_health_listener(os.getpid(), url)


@pytest.mark.asyncio
async def test_unavailable_inspection_fails_closed(monkeypatch):
    async def absent(*args):
        return None

    monkeypatch.setattr(probe, "_lsof", absent)
    monkeypatch.setattr(probe, "_proc_bindings", lambda *args: None)
    assert not await probe.owns_health_listener(os.getpid(), "http://127.0.0.1:8080")


@pytest.mark.asyncio
async def test_slow_lsof_is_killed_and_does_not_block_event_loop(monkeypatch):
    state = {"killed": False, "ticks": 0}
    killed = asyncio.Event()

    class Process:
        returncode = None

        async def read(self, maximum):
            assert 0 < maximum <= probe.MAX_OUTPUT + 1
            await asyncio.sleep(10)

        stdout = property(lambda self: self)

        async def wait(self):
            await killed.wait()
            self.returncode = -9
            return self.returncode

        def kill(self):
            state["killed"] = True
            killed.set()

    async def create(*args, **kwargs):
        return Process()

    async def ticker():
        for _ in range(5):
            await asyncio.sleep(0.002)
            state["ticks"] += 1

    monkeypatch.setattr(probe.shutil, "which", lambda name: "/synthetic/lsof")
    monkeypatch.setattr(probe.asyncio, "create_subprocess_exec", create)
    monkeypatch.setattr(probe, "PROBE_SECONDS", 0.02)
    result, _ = await asyncio.gather(probe._lsof(123, 8080), ticker())
    assert result is None
    assert state["killed"] is True
    assert state["ticks"] == 5


@pytest.mark.asyncio
async def test_linux_procfs_fallback_with_real_listener(monkeypatch):
    if not HAS_PROCFS:
        pytest.skip("Linux procfs fallback is not available on this OS")
    monkeypatch.setattr(probe.shutil, "which", lambda name: None)
    with HTTPServer(("127.0.0.1", 0), QuietHandler) as server:
        port = server.server_address[1]
        assert await probe.owns_health_listener(os.getpid(), f"http://127.0.0.1:{port}/health")


@pytest.mark.asyncio
async def test_excessive_lsof_output_is_bounded_and_process_is_terminated(monkeypatch):
    state = {"bytes": 0, "killed": False}

    class Process:
        returncode = None
        stdout = property(lambda self: self)

        async def read(self, maximum):
            state["bytes"] += maximum
            return b"x" * maximum

        async def wait(self):
            self.returncode = -9
            return self.returncode

        def kill(self):
            state["killed"] = True

    async def create(*args, **kwargs):
        return Process()

    monkeypatch.setattr(probe.shutil, "which", lambda name: "/synthetic/lsof")
    monkeypatch.setattr(probe.asyncio, "create_subprocess_exec", create)
    assert await probe._lsof(123, 8080) is None
    assert state == {"bytes": probe.MAX_OUTPUT + 1, "killed": True}
