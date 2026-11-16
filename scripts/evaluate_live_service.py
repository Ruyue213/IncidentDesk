"""Isolated, bounded SocialEngine collector acceptance. No model or external calls.

Provide a locally built compatible SocialEngine binary. Only its temporary copies
are started. No existing service, source tree, WAL, snapshot or Redis is used.
Results are local evidence for these parameters, never a production accuracy score.
"""

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import socket
import tempfile
import time
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from incident_desk.actions import identity
from incident_desk.observations import ObservationStore
from incident_desk.process_probe import owns_health_listener
from incident_desk.projects import ProjectProfile
from incident_desk.settings import Settings
from incident_desk.tools.business_probe import BusinessProbe
from incident_desk.tools.live_evidence import LiveEvidence


def now():
    return datetime.now(UTC)


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    path.chmod(0o600)


class IsolatedService:
    def __init__(self, binary, directory, settings, name, args, *, pool=2, queue=64):
        port = free_port()
        self.command = [
            str(binary),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--nodes",
            str(args.nodes),
            "--degree",
            str(args.degree),
            "--seed",
            str(args.seed),
            "--reactors",
            "2",
            "--pool",
            str(pool),
            "--queue",
            str(queue),
            "--no-redis",
        ]
        base = f"http://127.0.0.1:{port}"
        self.project = ProjectProfile(
            id=name,
            name=name,
            root=directory,
            start_argv=self.command,
            health_url=base + "/healthz",
            metrics_url=base + "/metrics",
            stats_url=base + "/stats",
            business_probe={
                "method": "POST",
                "path": "/query",
                "body": {"type": "dist", "a": 1, "b": 1},
                "expected_json": {"distance": 0},
            },
        )
        self.settings = settings
        self.process = None
        self.receipt = None
        self.log = None

    async def start(self):
        managed = self.settings.local_dir / "managed" / self.project.id
        await asyncio.to_thread(managed.mkdir, parents=True, exist_ok=True, mode=0o700)
        self.log = await asyncio.to_thread((managed / "service.log").open, "wb")
        self.process = await asyncio.create_subprocess_exec(
            *self.command,
            cwd=self.project.root,
            env={"PATH": "/usr/bin:/bin", "LANG": "C"},
            stdout=self.log,
            stderr=self.log,
        )
        try:
            deadline = time.monotonic() + 10
            async with httpx.AsyncClient(timeout=0.5, trust_env=False) as client:
                while time.monotonic() < deadline:
                    if self.process.returncode is not None:
                        raise RuntimeError(f"isolated child exited {self.process.returncode}")
                    try:
                        response = await client.get(self.project.health_url)
                        if response.status_code == 200 and await owns_health_listener(
                            self.process.pid, self.project.health_url
                        ):
                            self.receipt = {
                                "pid": self.process.pid,
                                "identity": await asyncio.to_thread(identity, self.process.pid),
                                "source": "isolated_acceptance_child",
                            }
                            if not self.receipt["identity"]:
                                raise RuntimeError("cannot freeze isolated child identity")
                            await asyncio.to_thread(
                                write_json, managed / "process.json", self.receipt
                            )
                            return
                    except httpx.HTTPError:
                        pass
                    await asyncio.sleep(0.05)
            raise RuntimeError("isolated child did not become owned and healthy within ten seconds")
        except BaseException:
            await self.stop()
            raise

    async def stop(self):
        if self.process is not None and self.process.returncode is None:
            current = await asyncio.to_thread(identity, self.process.pid)
            if self.receipt and current != self.receipt["identity"]:
                raise RuntimeError("child identity changed; refusing to signal an unverified PID")
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), timeout=3)
            except TimeoutError:
                current = await asyncio.to_thread(identity, self.process.pid)
                if self.receipt and current != self.receipt["identity"]:
                    raise RuntimeError("child identity changed before cleanup escalation") from None
                self.process.kill()
                await asyncio.wait_for(self.process.wait(), timeout=3)
        if self.log:
            await asyncio.to_thread(self.log.close)
            self.log = None


async def collect(service, store, *, probe=True):
    collector = LiveEvidence(service.project, service.settings)
    start, end = now() - timedelta(minutes=5), now() + timedelta(seconds=1)
    batches = await asyncio.gather(
        *(
            collector.get(source, start, end)
            for source in ("health", "metrics", "stats", "process", "logs")
        )
    )
    result = {batch["source"]: batch for batch in batches}
    result["sample"] = await store.sample(service.project)
    result["business_probe"] = (
        await BusinessProbe(service.settings).run(service.project) if probe else None
    )
    result["history"] = await store.query(
        service.project.id, service.project.version, start, now() + timedelta(seconds=1)
    )
    return result


def process_content(batch):
    return json.loads(batch["items"][0]["content"]) if batch["items"] else {}


def metric_value(batch, name):
    for source in batch["sources"]:
        for metric in source.get("metrics", []):
            if metric["name"] == name and metric.get("labels", {}).get("status") == "503":
                return metric["value"]
    return 0


async def bounded_load(service, requests, concurrency):
    """Read-only distance queries under fixed concurrency and total request bounds."""
    gate = asyncio.Semaphore(concurrency)
    # Second-degree expansion of the seeded BA graph's hub does useful work;
    # dist(1,2) often follows one edge and cannot exercise queue pressure.
    body = {"type": "sd", "a": 1}
    statuses, latencies, errors = Counter(), [], Counter()
    async with httpx.AsyncClient(
        timeout=2,
        trust_env=False,
        limits=httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency),
    ) as client:

        async def query(_index):
            async with gate:
                started = time.monotonic()
                try:
                    response = await client.post(
                        service.project.health_url.removesuffix("/healthz") + "/query", json=body
                    )
                    statuses[str(response.status_code)] += 1
                    await response.aclose()
                except httpx.HTTPError as error:
                    errors[type(error).__name__] += 1
                latencies.append(round((time.monotonic() - started) * 1000, 3))

        started = time.monotonic()
        await asyncio.wait_for(
            asyncio.gather(*(query(index) for index in range(requests))), timeout=30
        )
    values = sorted(latencies)
    return {
        "requests": requests,
        "concurrency": concurrency,
        "body": body,
        "elapsed_seconds": time.monotonic() - started,
        "statuses": dict(statuses),
        "errors": dict(errors),
        "latencies_ms": values,
        "p95_ms": values[min(len(values) - 1, int(len(values) * 0.95))] if values else None,
        "note": (
            "Client-observed finite load; not a capacity estimate or server histogram quantile."
        ),
    }


async def evaluate(args):
    binary = args.binary.expanduser().resolve(strict=True)
    if (
        not binary.is_file()
        or not os.access(binary, os.X_OK)
        or binary.stat().st_size > 100 * 1024 * 1024
    ):
        raise ValueError("binary must be an executable file no larger than 100 MiB")
    output = args.output.expanduser().resolve()
    if output.exists():
        raise ValueError("output must be a new directory, preserving prior results")
    await asyncio.to_thread(output.mkdir, parents=True, mode=0o700)
    config = Settings(_env_file=None, api_key=None, local_dir=output / "state")
    store = ObservationStore(config.local_dir, sampling_interval_seconds=1)
    result = {
        "schema_version": 1,
        "started_at": now().isoformat(),
        "model_api_calls": 0,
        "binary_sha256": hashlib.sha256(await asyncio.to_thread(binary.read_bytes)).hexdigest(),
        "parameters": {
            key: str(value) if isinstance(value, Path) else value
            for key, value in vars(args).items()
        },
        "scenarios": [],
        "cleanup": [],
        "scope": (
            "Isolated copied binary; no existing PID/port, source, Redis, WAL or snapshot is used."
        ),
        "limitations": [
            "Results only validate collectors, fixed probes and retained samples for this run.",
            "No diagnosis model quality, remediation safety or production accuracy is measured.",
            "Overload is scheduler-dependent; missing rejection evidence records a failure.",
        ],
    }
    children = []
    with tempfile.TemporaryDirectory(prefix="incidentdesk-isolated-") as temporary:
        directory = await asyncio.to_thread(Path(temporary).resolve)
        isolated = directory / "server"
        await asyncio.to_thread(shutil.copy2, binary, isolated)
        try:
            target = IsolatedService(isolated, directory, config, "lab-target", args)
            sibling = IsolatedService(isolated, directory, config, "lab-sibling", args)
            children += [target, sibling]
            await target.start()
            await sibling.start()
            await store.sample(target.project)
            await asyncio.sleep(0.15)
            evidence = await collect(target, store)
            target_state = process_content(evidence["process"])
            checks = {
                "business_passed": evidence["business_probe"]["business_ok"],
                "managed_target_running": target_state.get("running") is True,
                "only_target_pid_attributed": [
                    row["pid"] for row in target_state.get("matching_processes", [])
                ]
                == [target.process.pid],
                "at_least_two_retained_samples": evidence["history"]["coverage"][
                    "samples_in_window"
                ]
                >= 2,
            }
            result["scenarios"].append(
                {
                    "name": "healthy",
                    "checks": checks,
                    "passed": all(checks.values()),
                    "evidence": evidence,
                }
            )

            original = target.project
            target.project = original.model_copy(
                update={
                    "version": 2,
                    "business_probe": original.business_probe.model_copy(
                        update={"expected_json": {"distance": 1}}
                    ),
                }
            )
            evidence = await collect(target, store)
            probe_data = json.loads(evidence["business_probe"]["items"][0]["content"])
            checks = {
                "health_http_200": evidence["sample"]["sources"][0]["http_status"] == 200,
                "business_http_200": probe_data["http_status"] == 200,
                "registered_json_assertion_failed": not probe_data["assertions"].get(
                    "expected_json", True
                ),
                "business_not_accepted": not evidence["business_probe"]["business_ok"],
                "profile_history_isolated": evidence["history"]["coverage"]["samples_in_window"]
                == 1,
            }
            result["scenarios"].append(
                {
                    "name": "health_ok_business_assertion_failed",
                    "ground_truth": (
                        "Registered contract intentionally expects distance 1 for self-distance 0; "
                        "this does not assert a server implementation defect."
                    ),
                    "checks": checks,
                    "passed": all(checks.values()),
                    "evidence": evidence,
                }
            )
            target.project = original
            await target.stop()
            evidence = await collect(target, store)
            stopped = process_content(evidence["process"])
            sibling_health = await BusinessProbe(config).run(sibling.project)
            checks = {
                "target_not_running": stopped.get("running") is False,
                "no_pid_attributed_to_stopped_target": not stopped.get("matching_processes"),
                "sibling_still_business_healthy": sibling_health["business_ok"],
                "sibling_only_other_observed": sibling.process.pid
                in {row["pid"] for row in stopped.get("other_observed", [])},
                "target_business_unavailable": evidence["business_probe"]["status"]
                == "unavailable",
            }
            result["scenarios"].append(
                {
                    "name": "stopped_target_with_other_instance_alive",
                    "checks": checks,
                    "passed": all(checks.values()),
                    "evidence": evidence,
                    "sibling_probe": sibling_health,
                }
            )
            await sibling.stop()

            overloaded = IsolatedService(
                isolated, directory, config, "lab-overload", args, pool=1, queue=1
            )
            children.append(overloaded)
            await overloaded.start()
            before = await store.sample(overloaded.project)
            load = await bounded_load(overloaded, args.requests, args.concurrency)
            evidence = await collect(overloaded, store)
            rejection_delta = metric_value(
                evidence["sample"], "http_responses_total"
            ) - metric_value(before, "http_responses_total")
            checks = {
                "http_503_observed": load["statuses"].get("503", 0) > 0,
                "exported_503_counter_increased": rejection_delta > 0,
                "finite_request_accounting": sum(load["statuses"].values())
                + sum(load["errors"].values())
                == args.requests,
                "business_recovers_after_load": evidence["business_probe"]["business_ok"],
            }
            result["scenarios"].append(
                {
                    "name": "bounded_overload",
                    "checks": checks,
                    "passed": all(checks.values()),
                    "load": load,
                    "http_503_counter_delta": rejection_delta,
                    "evidence": evidence,
                }
            )
        except Exception as error:
            result["execution_error"] = {"type": type(error).__name__, "message": str(error)}
        finally:
            for service in reversed(children):
                try:
                    await service.stop()
                    result["cleanup"].append(
                        {
                            "project_id": service.project.id,
                            "pid": service.process.pid if service.process else None,
                            "exit_code": service.process.returncode if service.process else None,
                            "stopped": service.process is None
                            or service.process.returncode is not None,
                        }
                    )
                except Exception as error:
                    result["cleanup"].append(
                        {
                            "project_id": service.project.id,
                            "stopped": False,
                            "error": type(error).__name__,
                        }
                    )
            result["finished_at"] = now().isoformat()
            result["passed"] = (
                not result.get("execution_error")
                and len(result["scenarios"]) == 4
                and all(row["passed"] for row in result["scenarios"])
                and all(row["stopped"] for row in result["cleanup"])
            )
            await asyncio.to_thread(write_json, output / "results.json", result)
    print(
        json.dumps(
            {
                "output": str(output / "results.json"),
                "passed": result["passed"],
                "scenarios": [
                    {"name": row["name"], "passed": row["passed"]} for row in result["scenarios"]
                ],
                "execution_error": result.get("execution_error"),
                "model_api_calls": 0,
            },
            ensure_ascii=False,
        )
    )
    return 0 if result["passed"] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--binary",
        type=Path,
        required=True,
        help="Locally built compatible SocialEngine server executable",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(".local/evaluations") / ("live-service-" + now().strftime("%Y%m%dT%H%M%S%fZ")),
    )
    parser.add_argument("--nodes", type=int, default=5000)
    parser.add_argument("--degree", type=int, default=8)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--requests", type=int, default=192)
    parser.add_argument("--concurrency", type=int, default=32)
    args = parser.parse_args()
    if not 100 <= args.nodes <= 20000 or not 1 <= args.degree <= 32:
        parser.error("nodes must be 100..20000 and degree 1..32")
    if not 16 <= args.requests <= 512 or not 1 <= args.concurrency <= 64:
        parser.error("requests must be 16..512 and concurrency 1..64")
    return asyncio.run(evaluate(args))


if __name__ == "__main__":
    raise SystemExit(main())
