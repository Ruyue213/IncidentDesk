"""Record every read-only comparison attempt; acceptance requires a verified freeze."""

import argparse
import asyncio
import hashlib
import inspect
import json
import os
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

from langchain_core.callbacks import BaseCallbackHandler

from incident_desk.agent.budget import BudgetExceeded, CallLedger
from incident_desk.agent.diagnostics import SYSTEM_PROMPT, DiagnosticSession
from incident_desk.agent.models import make_model
from incident_desk.knowledge.store import KnowledgeStore
from incident_desk.schemas import RunInput
from incident_desk.settings import ROOT, Settings, load_settings


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def canonical_hash(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def public_configuration(settings: Settings) -> dict:
    return {
        "requested_model": settings.model,
        "provider_base_url_sha256": hashlib.sha256(settings.base_url.encode()).hexdigest(),
        "thinking": "disabled",
        "streaming": False,
        "max_retries": 0,
        "max_output_tokens": settings.max_output_tokens,
        "request_timeout_seconds": settings.request_timeout_seconds,
        "active_seconds": settings.active_seconds,
        "model_call_limit": settings.model_call_limit,
        "tool_call_limit": settings.tool_call_limit,
        "run_budget_cny": settings.run_budget_cny,
        "project_budget_cny": settings.project_budget_cny,
        "evidence_limit_per_source": 10,
        "knowledge_top_k": 5,
    }


def git_commit() -> str | None:
    command = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return command.stdout.strip() if command.returncode == 0 else None


def code_hashes() -> dict[str, str]:
    paths = sorted(
        {
            *ROOT.glob("src/**/*.py"),
            *ROOT.glob("scripts/**/*.py"),
            ROOT / "pyproject.toml",
            ROOT / "uv.lock",
        }
    )
    return {path.relative_to(ROOT).as_posix(): digest(path) for path in paths}


def verify_dataset() -> str:
    path = ROOT / "evals/dataset-manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    patterns = (
        "data/knowledge/*.md",
        "data/fixtures/dev/*.json",
        "data/fixtures/acceptance/*.json",
        "evals/dev/*.json",
        "evals/acceptance/*.json",
        "evals/expected/*.json",
        "evals/rubric.json",
        "evals/README.md",
    )
    actual_paths = {
        file.relative_to(ROOT).as_posix() for pattern in patterns for file in ROOT.glob(pattern)
    }
    if actual_paths != set(manifest["files"]):
        raise ValueError("Frozen dataset file inventory differs from the CP03 manifest")
    for relative, expected in manifest["files"].items():
        source = (ROOT / relative).resolve()
        if not source.is_relative_to(ROOT) or digest(source) != expected:
            raise ValueError("Frozen dataset differs from the CP03 manifest")
    return digest(path)


def freeze_metadata(settings: Settings) -> dict:
    commit = git_commit()
    if commit is None:
        raise ValueError("Acceptance freeze requires an actual Git commit")
    return {
        "version": "evaluation-freeze-v1",
        "created_at": datetime.now(UTC).isoformat(),
        "git_commit": commit,
        "dataset_manifest_sha256": verify_dataset(),
        "source_hashes": code_hashes(),
        "system_prompt_sha256": hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest(),
        "configuration": public_configuration(settings),
        "configuration_sha256": canonical_hash(public_configuration(settings)),
    }


def verify_freeze(path: Path, settings: Settings) -> dict:
    frozen = json.loads(path.read_text(encoding="utf-8"))
    actual = freeze_metadata(settings)
    for field in (
        "version",
        "git_commit",
        "dataset_manifest_sha256",
        "source_hashes",
        "system_prompt_sha256",
        "configuration_sha256",
    ):
        if frozen.get(field) != actual[field]:
            raise ValueError(f"Acceptance freeze mismatch: {field}")
    return {"path": str(path), "sha256": digest(path), "git_commit": actual["git_commit"]}


def usage_summary(rows: list[dict]) -> dict:
    return {
        "model_calls": len(rows),
        "input_tokens_known_sum": sum(row["input_tokens"] or 0 for row in rows),
        "output_tokens_known_sum": sum(row["output_tokens"] or 0 for row in rows),
        "cache_read_tokens_known_sum": sum(row["cache_read_tokens"] or 0 for row in rows),
        "unknown_usage_calls": sum(row["known_cost_cny"] is None for row in rows),
        "known_cost_cny_peak_estimate": sum(row["known_cost_cny"] or 0 for row in rows),
        "unreconciled_reservations_cny": sum(
            row["reserved_cny"] for row in rows if row["known_cost_cny"] is None
        ),
        "returned_models": sorted({row["returned_model"] for row in rows if row["returned_model"]}),
    }


class OutputCapture(BaseCallbackHandler):
    """Capture model outputs, including malformed structured calls, without HTTP details."""

    def __init__(self):
        self.outputs = []

    def on_llm_end(self, response, **kwargs):
        for generations in response.generations:
            for generation in generations:
                message = getattr(generation, "message", None)
                if message is not None:
                    self.outputs.append(
                        {
                            "type": message.type,
                            "content": message.content,
                            "tool_calls": getattr(message, "tool_calls", []),
                            "invalid_tool_calls": [
                                {key: value for key, value in call.items() if key != "error"}
                                for call in getattr(message, "invalid_tool_calls", [])
                            ],
                        }
                    )


def save(path: Path, data: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


async def close_model(model) -> list[str]:
    errors = []
    closed = set()
    for attribute in ("root_async_client", "root_client"):
        client = getattr(model, attribute, None)
        if client is None or id(client) in closed:
            continue
        closed.add(id(client))
        try:
            result = client.close()
            if inspect.isawaitable(result):
                await result
        except Exception as error:
            errors.append(type(error).__name__)
    return errors


async def attempt(settings, given, method, repeat, batch_id, artifacts) -> dict:
    request = RunInput(
        question=given["question"],
        service_id=given["service_id"],
        incident_id=given["incident_id"],
        start=given["window"]["start"],
        end=given["window"]["end"],
    )
    run_id = f"eval-{batch_id}-{request.incident_id}-{method}-{repeat}"
    record = {
        "run_id": run_id,
        "incident_id": request.incident_id,
        "method": method,
        "repeat": repeat,
        "started_at": datetime.now(UTC).isoformat(),
        "status": "STARTED",
        "request": request.model_dump(mode="json"),
    }
    path = artifacts / f"{request.incident_id}-{method}-{repeat}.json"
    save(path, record)
    started = time.monotonic()
    session = DiagnosticSession(settings, run_id, request)
    capture = OutputCapture()
    phase = "model_generation"
    model = None
    try:
        model = make_model(settings, run_id, scope="EVALUATION")
        model.callbacks = [capture]
        async with asyncio.timeout(settings.active_seconds):
            if method == "agent":
                result = await session.graph(model=model).ainvoke(session.input())
                record["structured_output"] = result.get("structured_response")
                if hasattr(record["structured_output"], "model_dump"):
                    record["structured_output"] = record["structured_output"].model_dump(
                        mode="json"
                    )
                record["messages"] = [
                    {
                        "type": message.type,
                        "content": message.content,
                        "tool_calls": getattr(message, "tool_calls", []),
                        "tool_call_id": getattr(message, "tool_call_id", None),
                        "status": getattr(message, "status", None),
                    }
                    for message in result["messages"]
                ]
                phase = "reference_validation"
                report = session.validate(result)
                collection_count = len(session.trace)
                emitted_tool_calls = session.tool_calls
            else:
                report, metadata = await session.fixed_baseline(model=model)
                collection_count = len(session.trace)
                emitted_tool_calls = sum(len(output["tool_calls"]) for output in capture.outputs)
                record["structured_output"] = report.model_dump(mode="json")
            record["report"] = report.model_dump(mode="json")
            record["collection_tool_calls"] = collection_count
            record["model_emitted_tool_calls"] = emitted_tool_calls
            record["status"] = "SUCCEEDED"
    except Exception as error:
        # Never serialize HTTP exception messages, headers, Settings or model reprs.
        record.update(status="FAILED", error_type=type(error).__name__, error_phase=phase)
    finally:
        record["client_cleanup_error_types"] = await close_model(model)
        rows = [
            row for row in CallLedger(settings.app_db, settings).rows() if row["run_id"] == run_id
        ]
        record.update(
            {
                "finished_at": datetime.now(UTC).isoformat(),
                "elapsed_seconds": time.monotonic() - started,
                "trace": session.trace,
                "snapshots": session.snapshots,
                "model_outputs": capture.outputs,
                "calls": rows,
                "usage": usage_summary(rows),
            }
        )
        if record["status"] != "SUCCEEDED":
            record["collection_tool_calls"] = len(session.trace)
            record["model_emitted_tool_calls"] = session.tool_calls
        save(path, record)
    return record


async def evaluate(args, settings: Settings) -> int:
    if not 1 <= args.repeats <= 2:
        raise ValueError("repeats must be 1 or 2; preserve all planned attempts")
    if not 0 < args.batch_budget_cny <= 5:
        raise ValueError("This evaluation batch is limited to an additional 5 CNY")
    dataset_sha = verify_dataset()
    frozen = None
    if args.split == "acceptance":
        if args.freeze_metadata is None:
            raise ValueError("Acceptance requires --freeze-metadata before opening its inputs")
        frozen = verify_freeze(args.freeze_metadata, settings)
    elif args.freeze_metadata is not None:
        frozen = verify_freeze(args.freeze_metadata, settings)
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive output creation prevents silently selecting a rerun's best results.
    with output.open("x", encoding="utf-8") as stream:
        batch_id = str(uuid.uuid4())
        artifacts = output.parent / f"{output.stem}-attempts"
        artifacts.mkdir(exist_ok=False)
        ledger = CallLedger(settings.app_db, settings)
        existing = ledger.rows()
        starting_cost = sum(
            row["known_cost_cny"] if row["known_cost_cny"] is not None else row["reserved_cny"]
            for row in existing
        )
        # Every model request still reserves in the shared durable ledger. Temporarily
        # cap its whole-project ceiling so this batch cannot add over 5 CNY even
        # inside a multi-call task. Concurrent other work may make it stop earlier.
        batch_settings = settings.model_copy(
            update={
                "project_budget_cny": min(
                    settings.project_budget_cny, starting_cost + args.batch_budget_cny
                ),
            }
        )
        configuration = public_configuration(settings)
        metadata = {
            "batch_id": batch_id,
            "split": args.split,
            "repeats": args.repeats,
            "started_at": datetime.now(UTC).isoformat(),
            "git_commit": git_commit(),
            "dataset_manifest_sha256": dataset_sha,
            "source_hashes": code_hashes(),
            "configuration": configuration,
            "configuration_sha256": canonical_hash(configuration),
            "freeze": frozen,
            "batch_budget_cny": args.batch_budget_cny,
            "starting_project_cost_and_reservations_cny": starting_cost,
            "plan": [],
            "completed_attempts": 0,
        }
        source_paths = sorted((ROOT / "evals" / args.split).glob("*.json"))
        expected_size = 8 if args.split == "dev" else 12
        if len(source_paths) != expected_size:
            raise ValueError("Evaluation split is incomplete")
        inputs = [json.loads(path.read_text(encoding="utf-8")) for path in source_paths]
        for repeat in range(1, args.repeats + 1):
            for given in inputs:
                for method in ("agent", "fixed_baseline"):
                    metadata["plan"].append(
                        {
                            "incident_id": given["incident_id"],
                            "method": method,
                            "repeat": repeat,
                        }
                    )
        metadata_path = output.with_suffix(".metadata.json")
        save(metadata_path, metadata)
        KnowledgeStore(settings.local_dir / "knowledge.db").ingest_dir(ROOT / "data/knowledge")
        records = []
        for planned in metadata["plan"]:
            given = next(item for item in inputs if item["incident_id"] == planned["incident_id"])
            record = await attempt(
                batch_settings,
                given,
                planned["method"],
                planned["repeat"],
                batch_id,
                artifacts,
            )
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
            records.append(record)
            metadata["completed_attempts"] = len(records)
            save(metadata_path, metadata)
            print(
                json.dumps(
                    {
                        "incident_id": record["incident_id"],
                        "method": record["method"],
                        "repeat": record["repeat"],
                        "status": record["status"],
                        "error_type": record.get("error_type"),
                        "usage": record["usage"],
                    }
                ),
                flush=True,
            )
            if record.get("error_type") == BudgetExceeded.__name__:
                metadata["stopped_reason"] = "budget_exceeded; remaining planned attempts not run"
                break
        rows = [row for row in ledger.rows() if batch_id in row["run_id"]]
        metadata.update(
            {
                "finished_at": datetime.now(UTC).isoformat(),
                "usage": usage_summary(rows),
                "schema_and_reference_passed": sum(
                    record["status"] == "SUCCEEDED" for record in records
                ),
                "planned_attempts": len(metadata["plan"]),
                "human_quality_review": "required separately; schema success is not task quality",
            }
        )
        save(metadata_path, metadata)
    return 0 if len(records) == len(metadata["plan"]) else 1


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--split", choices=["dev", "acceptance"], default="dev")
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--freeze-metadata", type=Path)
    parser.add_argument("--freeze-only", type=Path)
    parser.add_argument("--batch-budget-cny", type=float, default=5)
    args = parser.parse_args()
    settings = load_settings()
    try:
        if args.freeze_only is not None:
            frozen = freeze_metadata(settings)
            with args.freeze_only.open("x", encoding="utf-8") as stream:
                json.dump(frozen, stream, ensure_ascii=False, indent=2)
            print(json.dumps({"freeze": str(args.freeze_only), "sha256": digest(args.freeze_only)}))
            return 0
        if args.output is None:
            raise ValueError("--output is required for a durable evaluation run")
        return await evaluate(args, settings)
    except Exception as error:
        print(json.dumps({"status": "failed", "error_type": type(error).__name__}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
