"""Synthetic business/outcome loop through real loopback API and worker processes.

No cloud model, existing service, SocialEngine, user credential or project is used.
The scripted model validates plumbing, never diagnostic accuracy.
"""

import argparse
import asyncio
import ipaddress
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def forbid_remote_network():
    original = socket.socket.connect

    def connect(connection, address):
        if isinstance(address, tuple) and not ipaddress.ip_address(address[0]).is_loopback:
            raise RuntimeError("Synthetic acceptance blocked a non-loopback connection")
        return original(connection, address)

    socket.socket.connect = connect


def child_service(directory, port):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def respond(self, payload, content_type="application/json"):
            raw = payload.encode() if isinstance(payload, str) else json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            if self.path == "/metrics":
                self.respond("synthetic_request_total 1\n", "text/plain")
            else:
                self.respond({"status": "ok", "synthetic": True})

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            assert self.path == "/query" and body == {"type": "dist", "a": 1, "b": 1}
            # Ground truth comes from this test-owned flag, never a real service.
            self.respond({"distance": 0 if (directory / "recovered").exists() else 1})

    class NumericLoopbackServer(ThreadingHTTPServer):
        def server_bind(self):
            # HTTPServer.server_bind calls getfqdn(); this test needs no DNS.
            socket.socket.bind(self.socket, self.server_address)
            self.server_name, self.server_port = self.server_address

    NumericLoopbackServer(("127.0.0.1", port), Handler).serve_forever()


def child_api(directory, port):
    import uvicorn

    from incident_desk.api.app import create_app
    from incident_desk.settings import Settings

    forbid_remote_network()
    uvicorn.run(
        create_app(Settings(_env_file=None, api_key=None, local_dir=directory)),
        host="127.0.0.1",
        port=port,
        log_level="warning",
    )


def child_worker(directory):
    from langchain_core.messages import ToolMessage

    from incident_desk.agent.budget import CallLedger
    from incident_desk.agent.reliability_model import ReliabilityModel
    from incident_desk.settings import Settings
    from incident_desk.worker.runtime import Worker

    class OutcomeLoopModel(ReliabilityModel):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            # The shipping fixture script expects a list; live collection returns
            # an EvidenceBatch. Adapt only this test-owned model's input contract.
            converted = []
            for message in messages:
                if isinstance(message, ToolMessage) and message.name == "get_service_evidence":
                    value = json.loads(message.content)
                    if isinstance(value, dict) and "items" in value:
                        message = message.model_copy(update={"content": json.dumps(value["items"])})
                converted.append(message)
            return super()._generate(converted, stop=stop, run_manager=run_manager, **kwargs)

    def make_model(settings, run_id):
        model = OutcomeLoopModel()
        model._ledger = CallLedger(settings.app_db, settings)
        model._run_id = run_id
        return model

    forbid_remote_network()
    asyncio.run(
        Worker(
            Settings(_env_file=None, api_key=None, local_dir=directory),
            model_factory=make_model,
        ).run(once=True, handle_signals=True)
    )


def wait_http(url):
    with httpx.Client(timeout=0.3, trust_env=False) as client:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                if client.get(url).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.05)
    raise RuntimeError("Isolated child did not become ready")


def evaluate(output):
    output = Path(output)
    if output.exists():
        raise ValueError("Use a new output directory; preserve prior attempts")
    output.mkdir(parents=True, mode=0o700)
    steps, processes = [], []
    report = {
        "version": "synthetic-outcome-loop-v1",
        "synthetic": True,
        "started_at": datetime.now(UTC).isoformat(),
        "steps": steps,
        "model": "scripted reliability model, no neural inference",
        "scope": "test-owned service/API/worker and data; no SocialEngine or real diagnosis",
        "cloud_model_calls": 0,
    }
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LANG": "en_US.UTF-8",
        "LANGSMITH_TRACING": "false",
        "LANGCHAIN_TRACING_V2": "false",
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
    }
    logs = []
    with tempfile.TemporaryDirectory(prefix="incidentdesk-outcomes-") as temp:
        home, project = Path(temp) / "home", Path(temp) / "project"
        home.mkdir()
        project.mkdir()
        api_port, service_port = free_port(), free_port()

        def spawn(kind, directory, port=None):
            log = (output / f"{kind}-{len(logs)}.log").open("wb")
            logs.append(log)
            command = [
                sys.executable,
                str(Path(__file__).resolve()),
                f"--child-{kind}",
                str(directory),
            ]
            if port:
                command.append(str(port))
            child = subprocess.Popen(command, cwd=ROOT, env=env, stdout=log, stderr=log)
            processes.append(child)
            return child

        def step(name, **details):
            steps.append({"name": name, "passed": True, **details})

        try:
            spawn("service", project, service_port)
            api = spawn("api", home, api_port)
            wait_http(f"http://127.0.0.1:{service_port}/healthz")
            wait_http(f"http://127.0.0.1:{api_port}/health")
            with httpx.Client(
                base_url=f"http://127.0.0.1:{api_port}", timeout=10, trust_env=False
            ) as client:

                def call(method, path, expected=200, **kwargs):
                    response = client.request(method, path, **kwargs)
                    if response.status_code != expected:
                        raise AssertionError(
                            f"{method} {path}: {response.status_code}: {response.text[:400]}"
                        )
                    return response.json()

                call(
                    "PUT",
                    "/api/model-settings",
                    json={
                        "api_key": "synthetic-noncredential-never-sent",
                    },
                )
                profile = {
                    "id": "synthetic-outcome",
                    "name": "Synthetic outcome service",
                    "root": str(project),
                    "health_url": f"http://127.0.0.1:{service_port}/healthz",
                    "metrics_url": f"http://127.0.0.1:{service_port}/metrics",
                    "business_probe": {
                        "method": "POST",
                        "path": "/query",
                        "body": {"type": "dist", "a": 1, "b": 1},
                        "expected_json": {"distance": 0},
                    },
                }
                call("PUT", "/api/projects/synthetic-outcome", json=profile)
                now = datetime.now(UTC)
                request = {
                    "mode": "live",
                    "service_id": "synthetic-outcome",
                    "question": (
                        "Synthetic observation only; no action or diagnosis accuracy claim."
                    ),
                    "start": (now - timedelta(minutes=5)).isoformat(),
                    "end": (now + timedelta(seconds=1)).isoformat(),
                }
                run = call(
                    "POST",
                    "/api/runs",
                    202,
                    json=request,
                    headers={"Idempotency-Key": "synthetic-outcome-loop"},
                )
                run_id = run["id"]
                call("POST", f"/api/runs/{run_id}/verify-business", 409, json={})
                call(
                    "POST",
                    f"/api/runs/{run_id}/outcome",
                    409,
                    json={
                        "diagnosis_assessment": "inconclusive",
                        "diagnosis_reason": "Not finished",
                        "recovery_assessment": "not_attempted",
                        "recovery_reason": "Not attempted",
                    },
                )
                step("unfinished_run_cannot_verify_or_label")
                worker = spawn("worker", home)
                worker.wait(timeout=60)
                assert worker.returncode == 0
                run = call("GET", f"/api/runs/{run_id}")
                report["run_after_worker"] = {
                    "status": run["status"],
                    "error": run["error"],
                    "has_diagnosis": bool(run["diagnosis"]),
                }
                assert run["status"] == "SUCCEEDED" and run["diagnosis"], report["run_after_worker"]
                assert call("GET", f"/api/runs/{run_id}/outcome")["outcome"] is None
                aggregate = call("GET", "/api/outcomes/aggregate")
                assert aggregate["task_succeeded_runs"] == 1 and aggregate["annotated_runs"] == 0
                step("workflow_success_does_not_label_quality", status=run["status"])
                failed = call("POST", f"/api/runs/{run_id}/verify-business", json={})
                assert failed["status"] == "complete" and failed["business_ok"] is False
                first = failed["items"][0]
                assert first["provenance"]["purpose"] == "operator_outcome_verification"
                reference = {
                    "id": first["id"],
                    "version": first["version"],
                    "quote": '"business_ok": false',
                }
                payload = {
                    "diagnosis_assessment": "inconclusive",
                    "diagnosis_reason": (
                        "Scripted workflow cannot establish a diagnosis: private synthetic reason."
                    ),
                    "recovery_assessment": "failed",
                    "recovery_reason": "Private synthetic business assertion still fails.",
                    "verification_references": [reference],
                    "expected_revision": 0,
                }
                wrong = payload | {"recovery_assessment": "verified"}
                rejected = call("POST", f"/api/runs/{run_id}/outcome", 422, json=wrong)
                assert "contradicts" in rejected["detail"]
                step("failed_business_check_cannot_be_mislabeled_verified")
                annotation = call("POST", f"/api/runs/{run_id}/outcome", json=payload)["outcome"]
                assert annotation["revision"] == 1 and not annotation["independent_verification"]
                repeat = call("POST", f"/api/runs/{run_id}/outcome", json=payload)["outcome"]
                assert repeat["id"] == annotation["id"]
                step(
                    "health_200_business_failure_and_explicit_failed_annotation", business_ok=False
                )
                (project / "recovered").touch()
                recovered = call("POST", f"/api/runs/{run_id}/verify-business", json={})
                assert recovered["business_ok"] is True
                second = recovered["items"][0]
                payload.update(
                    recovery_assessment="verified",
                    recovery_reason="Private synthetic flag changed; fixed assertion now passes.",
                    expected_revision=1,
                    verification_references=[
                        {
                            "id": second["id"],
                            "version": second["version"],
                            "quote": '"business_ok": true',
                        }
                    ],
                )
                latest = call("POST", f"/api/runs/{run_id}/outcome", json=payload)["outcome"]
                assert latest["revision"] == 2 and latest["diagnosis_assessment"] == "inconclusive"
                step("fresh_same_run_probe_supports_explicit_recovery_revision", business_ok=True)
                stale = payload | {
                    "expected_revision": 0,
                    "recovery_reason": "Stale editor must not overwrite.",
                }
                call("POST", f"/api/runs/{run_id}/outcome", 409, json=stale)
                step("stale_revision_rejected")
                api.terminate()
                api.wait(timeout=5)
                api = spawn("api", home, api_port)
                wait_http(f"http://127.0.0.1:{api_port}/health")
                latest = call("GET", f"/api/runs/{run_id}/outcome")["outcome"]
                assert latest["revision"] == 2
                aggregate = call("GET", "/api/outcomes/aggregate")
                assert aggregate["diagnosis_assessments"] == {
                    "confirmed": 0,
                    "rejected": 0,
                    "inconclusive": 1,
                }
                assert aggregate["recovery_assessments"] == {
                    "verified": 1,
                    "failed": 0,
                    "not_attempted": 0,
                }
                encoded = json.dumps(aggregate)
                assert all(
                    token not in encoded
                    for token in (
                        run_id,
                        str(project),
                        "Private synthetic",
                        reference["id"],
                        second["id"],
                    )
                )
                assert call("GET", "/api/outcomes/aggregate?mode=fixture")["annotated_runs"] == 0
                with sqlite3.connect(home / "app.db") as db:
                    assert db.execute("SELECT COUNT(*) FROM outcomes").fetchone()[0] == 2
                step("restart_persistence_latest_only_and_aggregate_privacy")
                profile["expected_version"] = 1
                call("PUT", "/api/projects/synthetic-outcome", json=profile)
                call("POST", f"/api/runs/{run_id}/verify-business", 409, json={})
                step("changed_project_version_cannot_verify_old_run")
                report["aggregate"] = aggregate
                report["business_checks"] = {
                    "before": {"http_status": 200, "business_ok": False},
                    "after": {"http_status": 200, "business_ok": True},
                }
                report["passed"] = True
        except Exception as error:
            report.update(
                passed=False, error={"type": type(error).__name__, "message": str(error)[:1000]}
            )
            raise
        finally:
            for process in reversed(processes):
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
            for log in logs:
                log.close()
            report["finished_at"] = datetime.now(UTC).isoformat()
            report["children_reaped"] = all(process.poll() is not None for process in processes)
            (output / "report.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2) + "\n"
            )
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--child-service", nargs=2, metavar=("DIRECTORY", "PORT"))
    parser.add_argument("--child-api", nargs=2, metavar=("DIRECTORY", "PORT"))
    parser.add_argument("--child-worker", type=Path)
    args = parser.parse_args()
    if args.child_service:
        child_service(Path(args.child_service[0]), int(args.child_service[1]))
    elif args.child_api:
        child_api(Path(args.child_api[0]), int(args.child_api[1]))
    elif args.child_worker:
        child_worker(args.child_worker)
    elif args.output:
        report = evaluate(args.output)
        print(
            json.dumps(
                {
                    "passed": report["passed"],
                    "steps": len(report["steps"]),
                    "output": str(args.output),
                }
            )
        )
    else:
        parser.error("Provide a new --output directory")


if __name__ == "__main__":
    main()
