"""Finite operator-registered read-only business checks, never model-authored HTTP."""

import asyncio
import fcntl
import hashlib
import json
import math
import os
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx

from incident_desk.projects import local_url
from incident_desk.tools.live_evidence import redact

MAX_BYTES = 65536
REQUEST_SECONDS = 3
FORBIDDEN = {
    "edge",
    "admin",
    "shutdown",
    "restart",
    "config",
    "delete",
    "update",
    "create",
    "mutation",
}


def _redacted_json(value):
    if isinstance(value, dict):
        return {key: _redacted_json(child) for key, child in value.items()}
    if isinstance(value, list):
        return [_redacted_json(child) for child in value]
    return redact(value) if isinstance(value, str) else value


def _invalid_constant(_value):
    raise ValueError("Non-finite JSON value")


def _profile(project):
    value = getattr(project, "business_probe", None)
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return dict(value) if isinstance(value, dict) else None


def _subset(actual, expected):
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            key in actual and _subset(actual[key], value) for key, value in expected.items()
        )
    if isinstance(expected, list):
        return (
            isinstance(actual, list)
            and len(actual) == len(expected)
            and all(_subset(a, b) for a, b in zip(actual, expected, strict=True))
        )
    return type(actual) is type(expected) and actual == expected


def validate_probe(project):
    config = _profile(project)
    if not config or not getattr(project, "health_url", None):
        raise ValueError("Register a business probe and a loopback health origin first")
    origin = urlsplit(local_url(str(project.health_url)))
    path = config.get("path", "")
    if not isinstance(path, str):
        raise ValueError("Probe path must be a string")
    parsed = urlsplit(path)
    if (
        not isinstance(path, str)
        or not path.startswith("/")
        or path.startswith("//")
        or parsed.scheme
        or parsed.netloc
        or parsed.query
        or parsed.fragment
        or "%" in path
        or "\\" in path
        or any(part in {".", ".."} for part in path.split("/"))
        or any(part.casefold() in FORBIDDEN for part in path.split("/"))
    ):
        raise ValueError("Probe path must be a fixed non-mutating relative route")
    method = config.get("method", "GET")
    body = config.get("body") or {}
    expected = config.get("expected_json") or {}
    if (
        method not in {"GET", "POST"}
        or not isinstance(body, dict)
        or not isinstance(expected, dict)
    ):
        raise ValueError("Unsupported probe contract")
    if (
        len(json.dumps(body, allow_nan=False)) > 4096
        or len(json.dumps(expected, allow_nan=False)) > 4096
    ):
        raise ValueError("Probe request/assertions exceed bounds")
    if method == "POST":
        if path != "/query" or body.get("type") not in {"cf", "sd", "dist"} or not expected:
            raise ValueError("POST probes support only the SocialEngine read-only query route")
        if set(body) - {"type", "a", "b"}:
            raise ValueError("Unexpected business query parameters")
        fields = ("a",) if body["type"] == "sd" else ("a", "b")
        if any(type(body.get(key)) is not int or not 1 <= body[key] <= 2**31 - 1 for key in fields):
            raise ValueError("Business query requires registered bounded node IDs")
        if body["type"] == "sd" and "b" in body:
            raise ValueError("Second-degree probes use only node a")
    elif (
        body or not expected or path not in {"/health", "/healthz", "/readyz", "/stats", "/metrics"}
    ):
        raise ValueError("GET probes require explicit JSON assertions and no request body")
    expected_status = config.get("expected_status", 200)
    latency = config.get("max_latency_ms")
    if type(expected_status) is not int or not 200 <= expected_status < 300:
        raise ValueError("Business probe expected status must be a successful HTTP response")
    if latency is not None and (
        not isinstance(latency, (int, float))
        or isinstance(latency, bool)
        or not math.isfinite(latency)
        or not 0 < latency <= 60000
    ):
        raise ValueError("Probe latency limit is invalid")
    url = urlunsplit((origin.scheme, origin.netloc, path, "", ""))
    return config | {
        "method": method,
        "body": body,
        "expected_json": expected,
        "expected_status": expected_status,
    }, url


def _business_shape(body, config):
    if not isinstance(body, dict) or body.get("error"):
        return False
    if config["method"] != "POST":
        return True
    if config["body"]["type"] == "dist":
        return type(body.get("distance")) is int and -1 <= body["distance"] < 2**31
    friends = body.get("friends")
    return (
        isinstance(friends, list)
        and len(friends) <= 5000
        and all(type(value) is int and 1 <= value <= 2**32 - 1 for value in friends)
    )


class BusinessProbe:
    def __init__(self, settings=None):
        self.settings = settings

    async def run(self, project, attempts=1):
        if type(attempts) is not int or not 1 <= attempts <= 3:
            raise ValueError("Business probe attempts must be 1..3")
        config, url = validate_probe(project)
        items, outcomes, transport_failures, log_errors = [], [], 0, 0
        async with httpx.AsyncClient(timeout=3, trust_env=False, follow_redirects=False) as client:
            for attempt in range(attempts):
                transport_error = False
                started = time.monotonic()
                observed = datetime.now(UTC).isoformat()
                result = {
                    "request": {
                        "method": config["method"],
                        "path": config["path"],
                        "body": config["body"],
                    },
                    "attempt": attempt + 1,
                    "http_status": None,
                    "response": None,
                    "business_ok": False,
                    "assertions": {},
                    "error": None,
                    "truncated": False,
                }
                try:
                    kwargs = {"json": config["body"]} if config["method"] == "POST" else {}
                    async with (
                        asyncio.timeout(REQUEST_SECONDS),
                        client.stream(config["method"], url, **kwargs) as response,
                    ):
                        raw = bytearray()
                        async for chunk in response.aiter_bytes():
                            room = MAX_BYTES - len(raw)
                            raw.extend(chunk[:room])
                            if len(chunk) > room:
                                result["truncated"] = True
                                break
                        result["http_status"] = response.status_code
                        text = redact(bytes(raw).decode("utf-8", errors="replace"))
                        try:
                            result["response"] = json.loads(text, parse_constant=_invalid_constant)
                        except ValueError:
                            result["response"] = text[:1000]
                            result["error"] = "invalid_json"
                    elapsed = (time.monotonic() - started) * 1000
                    result["elapsed_ms"] = round(elapsed, 3)
                    result["assertions"] = {
                        "http_status": result["http_status"] == config["expected_status"],
                        "body_schema": not result["error"]
                        and _business_shape(result["response"], config),
                        "expected_json": _subset(result["response"], config["expected_json"]),
                        "latency": config.get("max_latency_ms") is None
                        or elapsed <= config["max_latency_ms"],
                        "complete_response": not result["truncated"],
                    }
                    result["business_ok"] = all(result["assertions"].values())
                except (OSError, httpx.HTTPError) as error:
                    result.update(
                        error=type(error).__name__,
                        elapsed_ms=round((time.monotonic() - started) * 1000, 3),
                    )
                    transport_failures += 1
                    transport_error = True
                result["outcome"] = (
                    "unavailable"
                    if transport_error
                    else "passed"
                    if result["business_ok"]
                    else "unavailable"
                    if result["http_status"] is None
                    else "failed"
                )
                result["assertion_strength"] = (
                    "specific_json" if config["expected_json"] else "schema_only"
                )
                result = _redacted_json(result)
                if len(json.dumps(result, ensure_ascii=False)) > 11000:
                    result["response"] = {"excerpt": text[:8000], "omitted_from_evidence": True}
                    result["evidence_output_truncated"] = True
                content = json.dumps(result, ensure_ascii=False, sort_keys=True)
                digest = hashlib.sha256(content.encode()).hexdigest()
                identity = f"business-{project.id}-{uuid.uuid4().hex[:24]}"
                items.append(
                    {
                        "id": identity,
                        "evidence_id": identity,
                        "source": "business_probe",
                        "version": f"sha256:{digest}",
                        "content": content,
                        "observed_at": observed,
                        "collected_at": datetime.now(UTC).isoformat(),
                        "provenance": {
                            "collector": "registered_readonly_business_probe",
                            "project_id": project.id,
                            "project_version": project.version,
                            "origin": urlsplit(url).netloc,
                        },
                        "truncated": result["truncated"]
                        or result.get("evidence_output_truncated", False),
                    }
                )
                outcomes.append(result["business_ok"])
                try:
                    await asyncio.to_thread(
                        self._log,
                        {
                            "project_id": project.id,
                            "project_version": project.version,
                            "observed_at": observed,
                            "result": result,
                        },
                    )
                except (OSError, ValueError):
                    log_errors += 1
        return {
            "source": "business_probe",
            "project_id": project.id,
            "project_version": project.version,
            "status": "unavailable"
            if transport_failures == attempts
            else "partial"
            if transport_failures or any(item["truncated"] for item in items)
            else "complete",
            "items": items,
            "business_ok": all(outcomes),
            "coverage": {
                "kind": "finite_registered_readonly_probes",
                "attempts": attempts,
                "log_write_failures": log_errors,
                "note": (
                    "Only the registered body/assertions were checked; "
                    "this is not an availability or performance SLA."
                ),
            },
            "truncated": any(item["truncated"] for item in items),
        }

    def _log(self, record):
        if self.settings is None:
            return
        directory = Path(self.settings.local_dir)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(
            directory / ".business-probes.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
        )
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            path = directory / "business-probes.jsonl"
            if path.is_symlink():
                raise ValueError("Business probe log must not be a symbolic link")
            if path.exists() and path.stat().st_size > 1024 * 1024:
                os.replace(path, path.with_suffix(".jsonl.1"))
            output = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            with os.fdopen(output, "a") as stream:
                stream.write(json.dumps(record, ensure_ascii=False) + "\n")
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)
