"""Exercise fresh source installs and release builds through the real build backend."""

import os
import shutil
import subprocess
from pathlib import Path


def test_clean_source_editable_install_precedes_ui_but_release_wheel_requires_it(tmp_path):
    root = Path(__file__).resolve().parents[1]
    checkout = tmp_path / "clean-checkout"
    checkout.mkdir()
    for name in (
        "pyproject.toml",
        "uv.lock",
        "hatch_build.py",
        "README.md",
        "README.zh-CN.md",
        "LICENSE",
        "THIRD_PARTY_NOTICES.md",
        "CONTRIBUTING.md",
        "SECURITY.md",
    ):
        shutil.copyfile(root / name, checkout / name)
    shutil.copytree(
        root / "src",
        checkout / "src",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "resources"),
    )
    shutil.copytree(root / "third_party/licenses", checkout / "third_party/licenses")
    assert not (checkout / "frontend/dist").exists()
    assert not (checkout / "src/incident_desk/resources").exists()
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("DEEPSEEK_", "INCIDENT_DESK_", "LANGCHAIN_", "LANGSMITH_"))
        and key not in {"VIRTUAL_ENV", "PYTHONPATH"}
    }
    uv = shutil.which("uv")
    assert uv, "Install uv to run the real source-distribution contract test"

    def run(arguments):
        return subprocess.run(
            [str(value) for value in arguments],
            cwd=checkout,
            env=environment,
            capture_output=True,
            text=True,
            timeout=120,
        )

    installed = run([uv, "sync", "--frozen", "--no-dev", "--no-progress"])
    assert installed.returncode == 0, installed.stderr
    help_result = run([checkout / ".venv/bin/incidentdesk", "--help"])
    assert help_result.returncode == 0, help_result.stderr
    assert "serve" in help_result.stdout and "doctor" in help_result.stdout
    assert not (checkout / "frontend/dist").exists()
    wheel = run([uv, "build", "--wheel", "--out-dir", tmp_path / "release"])
    assert wheel.returncode != 0, "A release must not omit its built user interface"
    assert "frontend/dist" in wheel.stderr, wheel.stderr
    assert not list((tmp_path / "release").glob("*.whl"))
