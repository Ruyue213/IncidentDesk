"""Local-only process entry points; paid calls require an explicit worker/diagnose command."""

import argparse
import asyncio
import json
import os
import sys
import time
import uuid
from pathlib import Path

from incident_desk.settings import RESOURCE_ROOT, Settings, load_settings


def request_for(incident_id: str) -> dict:
    if not incident_id.startswith("dev-") or "/" in incident_id or ".." in incident_id:
        raise ValueError("interactive commands only use development incidents")
    item = json.loads((RESOURCE_ROOT / "evals/dev" / f"{incident_id}.json").read_text())
    return {
        "question": item["question"],
        "service_id": item["service_id"],
        "incident_id": item["incident_id"],
        **item["window"],
    }


async def execute(args, settings):
    from incident_desk.knowledge.store import KnowledgeStore
    from incident_desk.storage.repository import Repository

    settings.local_dir.mkdir(parents=True, exist_ok=True)
    repository = Repository(settings.app_db)
    await repository.init()
    try:
        if args.command == "init":
            result = KnowledgeStore(settings.local_dir / "knowledge.db").ingest_dir(
                RESOURCE_ROOT / "data/knowledge"
            )
            print(json.dumps(result, ensure_ascii=False))
        elif args.command == "submit":
            payload = request_for(args.incident)
            if args.ticket:
                payload["question"] += " 请创建人工交接工单。"
            run, created = await repository.create_run(payload, args.key or str(uuid.uuid4()))
            print(json.dumps({"created": created, "run": run}, ensure_ascii=False, indent=2))
        elif args.command == "status":
            result = (
                await repository.get_run(args.run_id)
                if args.run_id
                else await repository.list_runs()
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
        elif args.command == "diagnose":
            from incident_desk.agent.diagnostics import DiagnosticSession
            from incident_desk.schemas import RunInput

            run_id = f"dev-{uuid.uuid4()}"
            session = DiagnosticSession(
                settings, run_id, RunInput.model_validate(request_for(args.incident))
            )
            async with asyncio.timeout(settings.active_seconds):
                if args.baseline:
                    report, metadata = await session.fixed_baseline()
                else:
                    result = await session.graph().ainvoke(session.input(), {"recursion_limit": 50})
                    report, metadata = session.validate(result), {"snapshots": session.snapshots}
            output = {"run_id": run_id, "report": report.model_dump(mode="json"), **metadata}
            if args.output:
                Path(args.output).parent.mkdir(parents=True, exist_ok=True)
                await asyncio.to_thread(
                    Path(args.output).write_text,
                    json.dumps(output, ensure_ascii=False, indent=2),
                )
            print(json.dumps(output, ensure_ascii=False, indent=2))
    finally:
        await repository.close()


def main(*, service_defaults=False):
    os.umask(0o077)
    from incident_desk.observability import configure_logging

    configure_logging()
    parser = argparse.ArgumentParser(description="IncidentDesk local processes")
    parser.add_argument("--local-dir", type=Path, help="isolate local runtime state")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="initialize application storage and lexical knowledge index")
    api = commands.add_parser("api", help="serve only on 127.0.0.1")
    api.add_argument("--port", type=int, default=8787)
    worker = commands.add_parser("worker", help="single graph executor")
    worker.add_argument(
        "--offline", action="store_true", help="scripted reliability model, no API calls"
    )
    worker.add_argument("--once", action="store_true")
    worker.add_argument(
        "--crash-at",
        choices=[
            "after_graph_interrupt_before_operation",
            "after_decision_before_resume",
            "after_ticket_commit",
            "after_graph_complete_before_app",
        ],
    )
    submit = commands.add_parser("submit")
    submit.add_argument("--incident", default="dev-001")
    submit.add_argument("--key")
    submit.add_argument("--ticket", action="store_true")
    status = commands.add_parser("status")
    status.add_argument("run_id", nargs="?")
    commands.add_parser("runs", help="list persisted diagnostic tasks")
    commands.add_parser("doctor", help="check installation resources without any model calls")
    embeddings = commands.add_parser("embeddings", help="manage optional pinned local embeddings")
    embeddings.add_argument("action", choices=["status", "download"])
    reranker = commands.add_parser(
        "reranker", help="manage the optional pinned local Cross-Encoder"
    )
    reranker.add_argument("action", choices=["status", "download"])
    for action in ("start", "serve", "stop", "restart"):
        command = commands.add_parser(action, help="manage local API and worker processes")
        command.add_argument("--port", type=int)
        modes = command.add_mutually_exclusive_group()
        modes.add_argument("--offline", action="store_true")
        modes.add_argument("--online", action="store_true")
    for action in ("backup", "restore"):
        command = commands.add_parser(action, help="maintain private stopped runtime state")
        command.add_argument("--archive", type=Path, required=True)
        command.add_argument("--replace", action="store_true")
    diagnose = commands.add_parser(
        "diagnose", help="read-only development diagnosis; uses paid API"
    )
    diagnose.add_argument("--incident", default="dev-001")
    diagnose.add_argument("--baseline", action="store_true")
    diagnose.add_argument("--output")
    args = parser.parse_args()
    settings = (
        Settings(_env_file=None, api_key=None)
        if args.command == "worker" and args.offline
        else load_settings()
    )
    if args.local_dir:
        settings.local_dir = args.local_dir.resolve()
    if args.command == "reranker":
        from incident_desk.knowledge.reranker import LocalCrossEncoder, download_model

        result = (
            download_model(settings.local_dir)
            if args.action == "download"
            else LocalCrossEncoder(settings.local_dir).status()
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if args.command == "embeddings":
        from incident_desk.knowledge.embeddings import LocalEmbeddings, download_model

        result = (
            download_model(settings.local_dir)
            if args.action == "download"
            else LocalEmbeddings(settings.local_dir).status()
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if args.command == "doctor":
        import importlib.metadata
        import shutil
        import sqlite3

        from incident_desk.runtime_paths import SOURCE_ROOT

        with sqlite3.connect(":memory:") as connection:
            connection.execute("CREATE VIRTUAL TABLE probe USING fts5(content)")
        resources = {
            "web": (RESOURCE_ROOT / "frontend/dist/index.html").is_file(),
            "demo_inputs": (RESOURCE_ROOT / "evals/dev/dev-001.json").is_file(),
            "demo_evidence": (RESOURCE_ROOT / "data/fixtures/dev/dev-001.json").is_file(),
            "knowledge": (RESOURCE_ROOT / "data/knowledge").is_dir(),
        }
        print(
            json.dumps(
                {
                    "version": importlib.metadata.version("incident-desk"),
                    "installation": "source" if SOURCE_ROOT else "package",
                    "state_dir": str(settings.local_dir),
                    "resources": resources,
                    "sqlite_fts5": True,
                    "ps_available": bool(shutil.which("ps")),
                    "model_api_calls": 0,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        if not all(resources.values()):
            raise SystemExit("Installation resources are incomplete; use a built release wheel")
        return
    if args.command in {"backup", "restore"}:
        from incident_desk.maintenance import main as maintain

        arguments = [
            args.command,
            "--state-dir",
            str(settings.local_dir),
            "--archive",
            str(args.archive),
        ]
        if args.replace:
            arguments.append("--replace")
        maintain(arguments)
        return
    service_status = args.command == "status" and not args.run_id and service_defaults
    if args.command in {"start", "serve", "stop", "restart"} or service_status:
        from incident_desk import service

        service.configure_home(settings.local_dir)
        arguments = [
            "status" if service_status else "start" if args.command == "serve" else args.command
        ]
        if getattr(args, "port", None) is not None:
            arguments += ["--port", str(args.port)]
        if getattr(args, "offline", False):
            arguments.append("--offline")
        elif getattr(args, "online", False):
            arguments.append("--online")
        service.main(arguments)
        if args.command == "serve":
            try:
                while service.status()["api_running"] and service.status()["worker_running"]:
                    time.sleep(0.5)
            except KeyboardInterrupt:
                pass
            finally:
                with service.maintenance_lock():
                    service.stop()
        return
    if args.command == "runs":
        args.command = "status"
        args.run_id = None
    if args.command == "api":
        import uvicorn

        from incident_desk.api.app import create_app

        uvicorn.run(create_app(settings), host="127.0.0.1", port=args.port, log_level="info")
    elif args.command == "worker":
        from incident_desk.agent.reliability_model import make_reliability_model
        from incident_desk.worker.runtime import Worker

        asyncio.run(
            Worker(
                settings,
                model_factory=make_reliability_model if args.offline else None,
                crash_at=args.crash_at,
            ).run(once=args.once, handle_signals=True)
        )
    else:
        asyncio.run(execute(args, settings))


def console_main():
    """Installed UX: status describes service processes; module entry stays compatible."""
    try:
        main(service_defaults=True)
    except (OSError, ValueError, RuntimeError) as error:
        print(f"IncidentDesk: {error}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
