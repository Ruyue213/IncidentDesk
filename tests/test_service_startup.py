"""Real isolated API/worker bootstrap exec and unresolved-start ownership guards."""

import json
import shutil
import socket
import sys
import urllib.request
from pathlib import Path

import pytest

from incident_desk import service


@pytest.fixture
def isolated_launcher(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    checkout = tmp_path / "source-copy"
    shutil.copytree(
        root / "src/incident_desk",
        checkout / "src/incident_desk",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "resources"),
    )
    (checkout / "pyproject.toml").write_text("# Temporary source marker; no credentials.\n")
    shutil.copytree(root / "data/knowledge", checkout / "data/knowledge")
    local = tmp_path / "private-state"
    local.mkdir(mode=0o700)
    wrapper = tmp_path / "bootstrap-python"
    wrapper.write_text(
        f"#!{sys.executable}\n"
        "import os, sys, time\n"
        "from pathlib import Path\n"
        f"real = {sys.executable!r}\n"
        f"source = {str(checkout / 'src')!r}\n"
        f"gates = Path({str(tmp_path)!r})\n"
        "clean = {k: v for k, v in os.environ.items() if not "
        "k.startswith(('DEEPSEEK_', 'INCIDENT_DESK_', 'LANGCHAIN_', 'LANGSMITH_'))}\n"
        "clean['PYTHONPATH'] = source\n"
        "clean['LANGSMITH_TRACING'] = 'false'\n"
        "clean['LANGCHAIN_TRACING_V2'] = 'false'\n"
        "os.environ.clear(); os.environ.update(clean)\n"
        "args = sys.argv[1:]\n"
        "if 'init' not in args:\n"
        " role = 'api' if 'api' in args else 'worker'\n"
        " deadline = time.monotonic() + 30\n"
        " while not (gates / ('gate-' + role)).exists():\n"
        "  if time.monotonic() >= deadline: raise RuntimeError('bootstrap gate timeout')\n"
        "  time.sleep(0.01)\n"
        "os.execv(real, [real, *args])\n"
    )
    wrapper.chmod(0o700)
    monkeypatch.setattr(service, "LOCAL", local)
    monkeypatch.setattr(service, "STATE", local / "processes.json")
    monkeypatch.setattr(service, "SERVICE_SETTINGS", local / "service-settings.json")
    monkeypatch.setattr(service, "PYTHON", wrapper)
    original_popen = service.Popen
    original_identity = service.process_identity
    original_birth = service.process_birth
    original_cleanup = service.terminate_owned_child
    children, births, bootstrap = {}, {}, {}

    def capture(command, **kwargs):
        role = "api" if "api" in command else "worker"
        child = original_popen(command, **kwargs)
        children[role] = child
        births[role] = original_birth(child.pid)
        return child

    def observe(pid):
        current = original_identity(pid)
        for role, child in children.items():
            if child.pid == pid and current and str(wrapper) in current:
                bootstrap.setdefault(role, current)
                (tmp_path / f"gate-{role}").touch()
        return current

    monkeypatch.setattr(service, "Popen", capture)
    monkeypatch.setattr(service, "process_identity", observe)
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    try:
        yield {
            "port": port,
            "children": children,
            "births": births,
            "bootstrap": bootstrap,
            "identity": original_identity,
        }
    finally:
        for role, child in reversed(list(children.items())):
            result = original_cleanup(child, births[role], original_identity, grace=3)
            assert result in {"exited", "terminated"}, (role, result)


def test_api_worker_same_pid_exec_freezes_ready_identity_and_stops(isolated_launcher):
    launch = isolated_launcher
    result = service.start(launch["port"], offline=True)
    assert result["phase"] == "READY" and result["mode"] == "offline-scripted"
    assert set(launch["bootstrap"]) == {"api", "worker"}
    for role, child in launch["children"].items():
        assert result[role] == child.pid
        assert result["identities"][role] == launch["identity"](child.pid)
        assert result["identities"][role] != launch["bootstrap"][role]
        assert tuple(result["births"][role]) == launch["births"][role]
    assert service.status()["api_running"] and service.status()["worker_running"]
    with urllib.request.urlopen(result["url"] + "/health", timeout=3) as response:
        health = json.loads(response.read())
    assert health["worker"]["mode"] == "offline" and health["worker"]["status"] == "ready"
    service.stop()
    assert not service.STATE.exists()
    assert not service.status()["api_running"] and not service.status()["worker_running"]
    assert all(child.wait(timeout=3) is not None for child in launch["children"].values())


def test_unproven_start_cleanup_keeps_gate_and_rejects_another_start_or_stop(
    isolated_launcher, monkeypatch
):
    launch = isolated_launcher
    attempted = []
    monkeypatch.setattr(service, "process_identity", lambda pid: None)

    def refuse_cleanup(child, birth, reader):
        attempted.append(child.pid)
        return "identity_unverified"

    monkeypatch.setattr(service, "terminate_owned_child", refuse_cleanup)
    with pytest.raises(RuntimeError):
        service.start(launch["port"], offline=True)
    state = service.STATE.read_bytes()
    pending = json.loads(state)
    assert pending["phase"] == "STARTING"
    assert {pending[role] for role in ("api", "worker")} == set(attempted)
    assert len(launch["children"]) == 2
    assert all(child.poll() is None for child in launch["children"].values())
    with pytest.raises(RuntimeError, match="Previous startup is unresolved"):
        service.start(launch["port"], offline=True)
    with pytest.raises(RuntimeError, match="Previous startup is unresolved"):
        service.stop()
    assert service.STATE.read_bytes() == state
    assert len(launch["children"]) == 2
    assert all(child.poll() is None for child in launch["children"].values())
