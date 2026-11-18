"""Small local worker status snapshot; this is observation, never task ownership."""

import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path


def write_runtime_status(local_dir: Path, value: dict):
    directory = Path(local_dir)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, name = tempfile.mkstemp(prefix=".worker-heartbeat-", dir=directory)
    try:
        with os.fdopen(descriptor, "w") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(value, stream)
        os.replace(name, directory / "worker-heartbeat.json")
    finally:
        Path(name).unlink(missing_ok=True)


def read_runtime_status(settings):
    try:
        with (settings.local_dir / "worker-heartbeat.json").open("rb") as stream:
            raw = stream.read(16385)
        if len(raw) > 16384:
            raise ValueError("Worker status is too large")
        result = json.loads(raw)
        if not isinstance(result, dict) or type(result.get("pid")) is not int:
            raise ValueError("Invalid worker status")
        if result["pid"] <= 0:
            raise ValueError("Invalid worker PID")
        stamp = datetime.fromisoformat(result["at"])
        if stamp.tzinfo is None:
            raise ValueError("Worker status timestamp requires timezone")
        age = max(0, (datetime.now(UTC) - stamp).total_seconds())
        os.kill(result["pid"], 0)
        return {
            **result,
            "status": "stale"
            if age >= 15
            else "stopping"
            if result.get("shutting_down")
            else "ready",
            "age_seconds": age,
        }
    except (OSError, ValueError, KeyError, TypeError):
        return {
            "status": "unavailable",
            "active_count": 0,
            "queued_count": None,
            "capacity": settings.worker_concurrency,
            "active_run_ids": [],
        }
