"""Runtime state belongs to the user; invocation directories never discover credentials."""

from pathlib import Path

from incident_desk import runtime_paths


def test_source_defaults_preserve_existing_checkout(monkeypatch):
    monkeypatch.delenv("INCIDENT_DESK_HOME", raising=False)
    assert runtime_paths.default_home() == runtime_paths.SOURCE_ROOT / ".local"
    assert runtime_paths.default_env_file() == runtime_paths.SOURCE_ROOT / ".env"


def test_installed_linux_home_is_independent_of_cwd(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime_paths, "SOURCE_ROOT", None)
    monkeypatch.setattr(runtime_paths.sys, "platform", "linux")
    monkeypatch.delenv("INCIDENT_DESK_HOME", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.chdir(tmp_path)
    assert runtime_paths.default_home() == tmp_path / "state/incidentdesk"
    assert runtime_paths.default_env_file() == tmp_path / "state/incidentdesk/.env"


def test_explicit_home_and_macos_default(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime_paths, "SOURCE_ROOT", None)
    monkeypatch.setattr(runtime_paths.sys, "platform", "darwin")
    monkeypatch.setenv("INCIDENT_DESK_HOME", str(tmp_path / "private-home"))
    assert runtime_paths.default_home() == tmp_path / "private-home"
    monkeypatch.delenv("INCIDENT_DESK_HOME")
    assert runtime_paths.default_home() == Path.home() / "Library/Application Support/IncidentDesk"
