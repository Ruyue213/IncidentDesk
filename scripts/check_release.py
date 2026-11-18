"""Black-box a release wheel in a clean venv, private home, and unrelated directory.

No provider credentials, real model calls, or user projects are used. Requires uv.
"""

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path


def check(wheel: Path, constraints: Path | None = None, offline: bool = False) -> dict:
    uv = shutil.which("uv")
    if not uv:
        raise RuntimeError("Install uv before running the isolated release check")
    with tempfile.TemporaryDirectory(prefix="incidentdesk-release-") as temporary:
        root = Path(temporary)
        unrelated = root / "unrelated-directory"
        unrelated.mkdir()
        state = root / "state"
        environment = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("INCIDENT_DESK_", "DEEPSEEK_", "LANGCHAIN_", "LANGSMITH_"))
            and key not in {"PYTHONPATH", "VIRTUAL_ENV"}
        }
        environment.update(
            INCIDENT_DESK_HOME=str(state),
            LANGSMITH_TRACING="false",
            LANGCHAIN_TRACING_V2="false",
            HF_HUB_OFFLINE="1",
            TRANSFORMERS_OFFLINE="1",
        )

        def run(command, *, timeout=180):
            result = subprocess.run(
                [str(item) for item in command],
                cwd=unrelated,
                env=environment,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            if result.returncode:
                raise RuntimeError(
                    f"Release command failed ({result.returncode}): {command[0]}\n"
                    f"{result.stdout[-3000:]}{result.stderr[-3000:]}"
                )
            return result.stdout

        run([uv, "venv", "--python", "3.12", root / "venv"])
        python = root / "venv/bin/python"
        cli = root / "venv/bin/incidentdesk"
        install = [uv, "pip", "install", "--python", python]
        if constraints:
            install += ["--constraint", constraints]
        if offline:
            install.append("--offline")
        run([*install, wheel])
        doctor = json.loads(run([cli, "doctor"]))
        assert doctor["installation"] == "package", doctor
        assert all(doctor["resources"].values()), doctor
        assert doctor["sqlite_fts5"] and doctor["ps_available"], doctor
        assert not state.exists(), "doctor must not initialize persistent state"
        installed = Path(
            run([python, "-c", "import incident_desk; print(incident_desk.__file__)"]).strip()
        )
        package_dir = installed.parent
        original_package_files = {
            str(item.relative_to(package_dir))
            for item in package_dir.rglob("*")
            if item.is_file() and item.suffix != ".pyc"
        }
        for item in package_dir.rglob("*"):
            if item.is_file() and item.suffix != ".pyc":
                item.chmod(0o444)
        run([cli, "init"])
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        base = f"http://127.0.0.1:{port}"

        def request(path, payload=None, headers=None):
            value = json.dumps(payload).encode() if payload is not None else None
            selected_headers = {"Content-Type": "application/json", **(headers or {})}
            with urllib.request.urlopen(
                urllib.request.Request(base + path, data=value, headers=selected_headers),
                timeout=10,
            ) as response:
                return response.read()

        try:
            run([cli, "start", "--offline", "--port", port])
            process_status = json.loads(run([cli, "status"]))
            assert process_status["api_running"] and process_status["worker_running"]
            page = request("/").decode()
            asset = re.search(r'<script[^>]+src="([^"]+\.js)"', page)
            assert asset and len(request(asset.group(1))) > 100
            assert json.loads(request("/api/projects"))["items"] == []
            readiness = json.loads(request("/api/readiness"))
            assert readiness["model"]["status"] == "unconfigured", readiness
            assert readiness["worker"]["capacity"] == 2
            reranker = json.loads(run([cli, "reranker", "status"]))
            assert reranker["download_required"] and reranker["local_only"]
            project = root / "example-project"
            project.mkdir()
            (project / "RUNBOOK.md").write_text(
                "# Release service\nConnection pool saturation produces timeout errors.\n"
            )
            payload = {
                "name": "Synthetic release check",
                "root": str(project),
                "health_url": base + "/health",
                "knowledge_paths": ["RUNBOOK.md"],
                "retrieval_mode": "lexical",
                "business_probe": {
                    "method": "GET",
                    "path": "/health",
                    "expected_json": {"status": "ok"},
                },
            }
            value = urllib.request.Request(
                base + "/api/projects/release-check",
                data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"},
                method="PUT",
            )
            with urllib.request.urlopen(value, timeout=10) as response:
                assert response.status == 200
            indexed = json.loads(
                request("/api/projects/release-check/knowledge/index", {"embed": False})
            )
            assert indexed["documents"] == 1 and indexed["chunks"] > 0, indexed
            knowledge = json.loads(request("/api/projects/release-check/knowledge"))
            assert knowledge["usable_generation"] == indexed["generation"], knowledge
            found = json.loads(
                request(
                    "/api/projects/release-check/knowledge/search",
                    {"query": "connection pool timeout", "mode": "lexical"},
                )
            )
            assert found["items"], found
            degraded = json.loads(
                request(
                    "/api/projects/release-check/knowledge/search",
                    {
                        "query": "connection pool timeout",
                        "mode": "lexical",
                        "retrieval_config": {"reranker": "cross_encoder", "top_k": 1},
                    },
                )
            )
            assert degraded["status"] == "degraded" and len(degraded["items"]) == 1
            assert degraded["retrieval"]["reranker"]["effective"] == "off"
            assert json.loads(request("/api/outcomes/aggregate"))["annotated_runs"] == 0
            json.loads(request("/api/projects/release-check/sample", {}))
            now = datetime.now(UTC)
            bounds = urllib.parse.urlencode(
                {"start": (now - timedelta(minutes=1)).isoformat(), "end": now.isoformat()}
            )
            history = json.loads(request("/api/projects/release-check/observations?" + bounds))
            assert history["coverage"]["historical"], history
            probe = json.loads(request("/api/projects/release-check/probe-business", {}))
            assert probe["status"] == "complete", probe
            incidents = json.loads(request("/api/incidents"))
            assert incidents["items"], "Demo input resources must be included"
            run([cli, "submit", "--incident", "dev-001", "--key", "release-smoke"])
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                runs = json.loads(run([cli, "runs"]))
                status = runs[0]["status"]
                if status in {"SUCCEEDED", "FAILED", "CANCELLED", "EXPIRED"}:
                    break
                time.sleep(0.3)
            assert status == "SUCCEEDED", runs
        finally:
            run([cli, "stop"])
        stopped = json.loads(run([cli, "status"]))
        assert not stopped["api_running"] and not stopped["worker_running"]
        archive = root / "private-backup/state.zip"
        run([cli, "backup", "--archive", archive])
        restored = root / "restored-state"
        run([cli, "--local-dir", restored, "restore", "--archive", archive])
        assert (restored / "app.db").is_file()
        assert not (restored / "processes.json").exists()
        assert not (package_dir / ".local").exists()
        assert original_package_files == {
            str(item.relative_to(package_dir))
            for item in package_dir.rglob("*")
            if item.is_file() and item.suffix != ".pyc"
        }, "Runtime state must never be added to the installed package"
        return {
            "version": doctor["version"],
            "clean_install": True,
            "unrelated_cwd": True,
            "bundled_ui_and_assets": True,
            "new_project_registration": True,
            "knowledge_index_and_search": True,
            "reranker_missing_model_falls_back": True,
            "worker_capacity_and_empty_outcome_aggregate": True,
            "historical_observations": True,
            "fixed_readonly_business_probe": True,
            "offline_fixture_run": "SUCCEEDED",
            "start_status_stop": True,
            "backup_restore": True,
            "package_state_writes": False,
            "model_api_calls": 0,
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--constraints", type=Path)
    parser.add_argument(
        "--offline", action="store_true", help="only use cached Python dependencies"
    )
    args = parser.parse_args()
    result = check(
        args.wheel.resolve(), args.constraints.resolve() if args.constraints else None, args.offline
    )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
