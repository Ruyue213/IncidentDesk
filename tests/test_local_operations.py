"""Stopped-state recovery and launcher argument contracts; never start real services."""

import fcntl
import hashlib
import importlib
import json
import sqlite3
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load_script(name):
    module_name = {"backup_state": "maintenance", "local_service": "service"}[name]
    return importlib.import_module(f"incident_desk.{module_name}")


@pytest.fixture
def backup(tmp_path, monkeypatch):
    module = load_script("backup_state")
    monkeypatch.setattr(module, "application_processes", lambda state: [])
    monkeypatch.setattr(module, "process_identity", lambda pid: None)
    return module


@pytest.fixture
def source(tmp_path):
    state = tmp_path / "source"
    state.mkdir(mode=0o700)
    for name in ("app", "graph", "knowledge"):
        with sqlite3.connect(state / f"{name}.db") as database:
            database.execute("CREATE TABLE example(value TEXT)")
            database.execute("INSERT INTO example VALUES (?)", (name,))
    (state / "model-config").mkdir()
    (state / "model-config/profiles.json").write_text('{"active_version":1}')
    (state / "model-config/credential-example").write_text("synthetic-private-key")
    (state / "model-config/.lock").touch()
    (state / "project-profiles/example").mkdir(parents=True)
    (state / "project-profiles/example/1.json").write_text('{"id":"example","version":1}')
    (state / "managed/example").mkdir(parents=True)
    (state / "managed/example/process.json").write_text(
        json.dumps(
            {
                "pid": 987654321,
                "identity": "an old process identity",
                "operation_id": "approved-operation",
                "project_version": 1,
            }
        )
    )
    (state / "processes.json").write_text(
        json.dumps(
            {
                "port": 9999,
                "mode": "offline-scripted",
                "api": 987654320,
                "worker": 987654322,
            }
        )
    )
    (state / "worker.lock").write_text("987654322")
    return state


def test_backup_round_trip_preserves_databases_credentials_and_omits_process_identity(
    backup, source, tmp_path
):
    archive = tmp_path / "private" / "state.zip"
    manifest = backup.backup_state(source, archive)
    assert archive.stat().st_mode & 0o777 == 0o600
    assert archive.parent.stat().st_mode & 0o777 == 0o700
    assert "processes.json" in manifest["omitted_runtime_files"]
    with zipfile.ZipFile(archive) as zipped:
        assert "state/processes.json" not in zipped.namelist()
        assert "state/worker.lock" not in zipped.namelist()
        assert "state/managed/example/process.json" not in zipped.namelist()
        assert str(source).encode() not in zipped.read("manifest.json")
    target = tmp_path / "restored"
    result = backup.restore_state(target, archive)
    assert result["previous_backup"] is None
    assert (target / "model-config/credential-example").read_text() == "synthetic-private-key"
    assert (target / "project-profiles/example/1.json").exists()
    assert json.loads((target / "service-settings.json").read_text())["port"] == 9999
    metadata = json.loads((target / "managed/example/backup-metadata.json").read_text())
    assert metadata["operation_id"] == "approved-operation"
    assert "pid" not in metadata and "identity" not in metadata
    for name in ("app", "graph", "knowledge"):
        with sqlite3.connect(target / f"{name}.db") as database:
            assert database.execute("SELECT value FROM example").fetchone()[0] == name
    for path in target.rglob("*"):
        assert path.stat().st_mode & 0o777 == (0o700 if path.is_dir() else 0o600)


def test_restore_requires_replace_and_backs_up_old_state_before_replacing(backup, source, tmp_path):
    archive = tmp_path / "private" / "state.zip"
    backup.backup_state(source, archive)
    target = tmp_path / "target"
    target.mkdir()
    (target / "old-file").write_text("original state")
    with pytest.raises(FileExistsError, match="--replace"):
        backup.restore_state(target, archive)
    assert (target / "old-file").read_text() == "original state"
    result = backup.restore_state(target, archive, replace=True)
    previous = Path(result["previous_backup"])
    assert previous.is_file()
    with zipfile.ZipFile(previous) as zipped:
        assert zipped.read("state/old-file") == b"original state"
    assert not (target / "old-file").exists()


def test_embedding_cache_is_not_backed_up_or_followed(backup, source, tmp_path):
    (source / "models/cache").mkdir(parents=True)
    (source / "models/cache/weights.bin").write_bytes(b"synthetic-model-cache")
    (source / "models/cache/hub-link").symlink_to(tmp_path / "missing-model")
    archive = tmp_path / "private/state.zip"
    manifest = backup.backup_state(source, archive)
    assert "models/" in manifest["omitted_runtime_files"]
    assert not any(name.startswith("models/") for name in manifest["files"])
    target = tmp_path / "restored"
    backup.restore_state(target, archive)
    assert not (target / "models").exists()
    assert (target / "model-config/credential-example").is_file()


def corrupt(archive, destination, transform):
    with zipfile.ZipFile(archive) as incoming, zipfile.ZipFile(destination, "w") as output:
        files = {name: incoming.read(name) for name in incoming.namelist()}
        transform(files)
        for name, value in files.items():
            output.writestr(name, value)


def test_corrupted_file_or_manifest_is_rejected_without_touching_existing_state(
    backup, source, tmp_path
):
    archive = tmp_path / "private" / "state.zip"
    backup.backup_state(source, archive)
    target = tmp_path / "target"
    target.mkdir()
    (target / "keep").write_text("untouched")
    damaged = archive.parent / "damaged.zip"
    corrupt(
        archive,
        damaged,
        lambda files: files.update(
            {"state/model-config/credential-example": b"different-private-key"}
        ),
    )
    with pytest.raises(ValueError, match="sizes|digest"):
        backup.restore_state(target, damaged, replace=True)
    assert (target / "keep").read_text() == "untouched"
    broken_manifest = archive.parent / "broken-manifest.zip"
    corrupt(archive, broken_manifest, lambda files: files.update({"manifest.json": b"{}"}))
    with pytest.raises(ValueError, match="manifest digest"):
        backup.restore_state(target, broken_manifest, replace=True)
    assert (target / "keep").exists()
    assert not list(archive.parent.glob("pre-restore-*"))


def test_archive_traversal_and_unknown_versions_are_rejected(backup, source, tmp_path):
    archive = tmp_path / "private" / "state.zip"
    backup.backup_state(source, archive)

    def change_manifest(files, transform):
        manifest = json.loads(files["manifest.json"])
        transform(manifest)
        files["manifest.json"] = json.dumps(manifest).encode()
        files["manifest.sha256"] = hashlib.sha256(files["manifest.json"]).hexdigest().encode()

    wrong_version = archive.parent / "wrong-version.zip"
    corrupt(
        archive,
        wrong_version,
        lambda files: change_manifest(
            files, lambda manifest: manifest.update(application_version="999.0")
        ),
    )
    with pytest.raises(ValueError, match="application version"):
        backup.restore_state(tmp_path / "target", wrong_version)

    def traversal(files):
        files["state/../escape"] = b"malicious"
        change_manifest(
            files,
            lambda manifest: manifest["files"].update(
                {"../escape": {"size": 9, "sha256": hashlib.sha256(b"malicious").hexdigest()}}
            ),
        )

    unsafe = archive.parent / "unsafe.zip"
    corrupt(archive, unsafe, traversal)
    with pytest.raises(ValueError, match="Unsafe"):
        backup.restore_state(tmp_path / "target", unsafe)
    assert not (tmp_path / "escape").exists()


def test_running_app_worker_lock_and_managed_service_block_maintenance(
    backup, source, tmp_path, monkeypatch
):
    archive = tmp_path / "private" / "state.zip"
    monkeypatch.setattr(backup, "application_processes", lambda state: ["running API"])
    with pytest.raises(RuntimeError, match="Stop the IncidentDesk"):
        backup.backup_state(source, archive)
    monkeypatch.setattr(backup, "application_processes", lambda state: [])
    backup.backup_state(source, archive)
    monkeypatch.setattr(backup, "process_identity", lambda pid: "live process")
    (source / "processes.json").unlink()
    with pytest.raises(RuntimeError, match="managed services"):
        backup.restore_state(source, archive, replace=True)


def test_symlink_in_state_cannot_pull_external_files_into_backup(backup, source, tmp_path):
    external = tmp_path / "external-secret"
    external.write_text("do not include")
    (source / "symlink").symlink_to(external)
    with pytest.raises(ValueError, match="Symlinks"):
        backup.backup_state(source, tmp_path / "private" / "state.zip")


def test_live_worker_lock_prevents_backup_even_if_saved_pid_is_stale(backup, source, tmp_path):
    with (source / "worker.lock").open("a+") as worker:
        fcntl.flock(worker.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(RuntimeError, match="Stop the IncidentDesk worker"):
            backup.backup_state(source, tmp_path / "private" / "state.zip")


@pytest.mark.parametrize(
    "args,expected",
    [
        (["restart"], (9999, True)),
        (["restart", "--online"], (9999, False)),
        (["restart", "--port", "8788", "--offline"], (8788, True)),
        (["start"], (8787, False)),
    ],
)
def test_restart_inherits_saved_port_and_mode_with_explicit_overrides(
    tmp_path, monkeypatch, capsys, args, expected
):
    service = load_script("local_service")
    monkeypatch.setattr(service, "LOCAL", tmp_path / "state")
    monkeypatch.setattr(service, "saved", lambda: {"port": 9999, "mode": "offline-scripted"})
    called = []
    monkeypatch.setattr(service, "stop", lambda: called.append("stop"))
    monkeypatch.setattr(
        service, "start", lambda port, offline: called.append((port, offline)) or {}
    )
    monkeypatch.setattr("sys.argv", ["local_service.py", *args])
    service.main()
    assert called[-1] == expected
    assert ("stop" in called) == (args[0] == "restart")
    capsys.readouterr()


def test_launcher_rejects_reused_pid_even_when_command_matches(monkeypatch):
    service = load_script("local_service")
    command = f"-m incident_desk --local-dir {service.LOCAL} api"
    monkeypatch.setattr(service, "saved", lambda: {"identities": {"api": "original " + command}})
    monkeypatch.setattr(service, "process_identity", lambda pid: "reused " + command)
    assert not service.alive(12345, "api")
    monkeypatch.setattr(service, "process_identity", lambda pid: "original " + command)
    assert service.alive(12345, "api")


def test_stop_retains_claim_and_never_signals_an_unverified_live_process(tmp_path, monkeypatch):
    service = load_script("local_service")
    state = tmp_path / "processes.json"
    state.write_text(json.dumps({"phase": "READY", "api": 12345}))
    monkeypatch.setattr(service, "STATE", state)
    monkeypatch.setattr(service, "SERVICE_SETTINGS", tmp_path / "preferences.json")
    monkeypatch.setattr(service, "recorded_process_exited", lambda *_: False)
    monkeypatch.setattr(service, "alive", lambda *_: False)
    signalled = []
    monkeypatch.setattr(service.os, "kill", lambda *args: signalled.append(args))
    with pytest.raises(RuntimeError, match="claim was retained"):
        service.stop()
    assert state.exists() and signalled == []
