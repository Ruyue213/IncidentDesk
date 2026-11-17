"""A Darwin interpreter alias is a same-file exception, never a basename match."""

import ctypes
import os
from types import SimpleNamespace

import pytest

from incident_desk.tools import live_evidence as evidence


def interpreter_files(tmp_path):
    launcher = tmp_path / "launcher/bin/python3"
    base = tmp_path / "base/bin/python3"
    other = tmp_path / "other/bin/python3"
    image = tmp_path / "Python.app/Contents/MacOS/Python"
    for path in (launcher, base, other, image):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"synthetic executable identity")
    alias = tmp_path / "venv/bin/python3"
    alias.parent.mkdir(parents=True)
    alias.symlink_to(launcher)
    return launcher, base, other, image, alias


@pytest.mark.parametrize("registered", ["alias", "base"])
def test_only_current_launcher_or_base_samefile_can_supply_framework_image(
    tmp_path, monkeypatch, registered
):
    launcher, base, _, image, alias = interpreter_files(tmp_path)
    monkeypatch.setattr(
        evidence,
        "sys",
        SimpleNamespace(platform="darwin", executable=str(launcher), _base_executable=str(base)),
    )
    calls = []

    def actual_image(pid):
        calls.append(pid)
        return image

    monkeypatch.setattr(evidence, "_process_image", actual_image)
    assert evidence._registered_interpreter_image(alias if registered == "alias" else base) == image
    assert calls == [os.getpid()]


def test_different_same_named_python_binary_cannot_borrow_current_image(tmp_path, monkeypatch):
    launcher, base, other, _, _ = interpreter_files(tmp_path)
    assert other.name == launcher.name and not other.samefile(launcher)
    monkeypatch.setattr(
        evidence,
        "sys",
        SimpleNamespace(platform="darwin", executable=str(launcher), _base_executable=str(base)),
    )

    def forbidden(_pid):
        pytest.fail("An unrelated same-named executable must not trigger an image alias")

    monkeypatch.setattr(evidence, "_process_image", forbidden)
    assert evidence._registered_interpreter_image(other) is None


def test_current_launcher_alias_fails_closed_when_image_cannot_be_read(tmp_path, monkeypatch):
    launcher, base, _, _, alias = interpreter_files(tmp_path)
    monkeypatch.setattr(
        evidence,
        "sys",
        SimpleNamespace(platform="darwin", executable=str(launcher), _base_executable=str(base)),
    )
    monkeypatch.setattr(evidence, "_process_image", lambda _pid: None)
    assert evidence._registered_interpreter_image(alias) is None
    assert evidence._registered_interpreter_image(tmp_path / "missing/python3") is None


@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_non_darwin_does_not_use_framework_alias_or_native_probe(tmp_path, monkeypatch, platform):
    launcher, base, _, _, _ = interpreter_files(tmp_path)
    monkeypatch.setattr(
        evidence,
        "sys",
        SimpleNamespace(platform=platform, executable=str(launcher), _base_executable=str(base)),
    )

    def forbidden(*_args, **_kwargs):
        pytest.fail("Other platforms must not invoke the Darwin image API")

    monkeypatch.setattr(ctypes, "CDLL", forbidden)
    assert evidence._process_image(os.getpid()) is None
    monkeypatch.setattr(evidence, "_process_image", forbidden)
    assert evidence._registered_interpreter_image(launcher) is None


def test_darwin_native_image_failure_is_not_a_process_match(monkeypatch):
    monkeypatch.setattr(evidence, "sys", SimpleNamespace(platform="darwin"))

    def unavailable(*_args, **_kwargs):
        raise OSError("Synthetic native image lookup failure")

    monkeypatch.setattr(ctypes, "CDLL", unavailable)
    assert evidence._process_image(os.getpid()) is None


@pytest.mark.parametrize("status", [0, -1])
def test_darwin_native_api_without_a_path_fails_closed(monkeypatch, status):
    monkeypatch.setattr(evidence, "sys", SimpleNamespace(platform="darwin"))

    def pidpath(_pid, _buffer, _size):
        return status

    monkeypatch.setattr(
        ctypes, "CDLL", lambda *_args, **_kwargs: SimpleNamespace(proc_pidpath=pidpath)
    )
    assert evidence._process_image(os.getpid()) is None
