"""Start/stop the local API and worker; no shell, cloud hosting, or login-time installation."""

import argparse
import asyncio
import fcntl
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from subprocess import Popen

from incident_desk.process_identity import (
    owned_child_identity,
    process_birth,
    process_has_exited,
    terminate_owned_child,
)
from incident_desk.runtime_paths import default_home

LOCAL = default_home()
STATE = LOCAL / "processes.json"
SERVICE_SETTINGS = LOCAL / "service-settings.json"
PYTHON = Path(sys.executable)


def configure_home(directory: Path):
    global LOCAL, STATE, SERVICE_SETTINGS
    LOCAL = Path(directory).expanduser().resolve()
    STATE = LOCAL / "processes.json"
    SERVICE_SETTINGS = LOCAL / "service-settings.json"


def process_identity(pid):
    if type(pid) is not int or pid <= 0:
        return None
    result = subprocess.run(
        ["ps", "-p", str(pid), "-o", "lstart=", "-o", "command="],
        capture_output=True,
        text=True,
        check=False,
        timeout=3,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def alive(pid, role):
    identity = process_identity(pid)
    if not identity:
        return False
    state = saved()
    expected = state.get("identities", {}).get(role)
    if not expected or expected != identity:
        return False
    birth = state.get("births", {}).get(role)
    if birth is not None and process_birth(pid) != tuple(birth):
        return False
    return f"-m incident_desk {role}" in identity or (
        "-m incident_desk --local-dir " in identity
        and str(LOCAL) in identity
        and f" {role}" in identity
    )


def saved():
    path = STATE if STATE.exists() else SERVICE_SETTINGS
    return json.loads(path.read_text()) if path.exists() else {}


@contextmanager
def maintenance_lock():
    """Coordinate launcher operations with stopped-state backup and restore."""
    LOCAL.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = LOCAL.parent / f".{LOCAL.name}.maintenance.lock"
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError(
                "Local state maintenance is in progress; try again after it finishes"
            ) from None
        yield
    finally:
        os.close(descriptor)


def write_private(path, value):
    # These files contain local metadata only, never model credentials.
    descriptor, temporary = tempfile.mkstemp(prefix=".service-state-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(value, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def stop():
    state = saved()
    if unresolved_start(state):
        raise RuntimeError("Previous startup is unresolved; inspect its processes before stopping")
    for role in ("worker", "api"):
        pid = state.get(role)
        if not pid or recorded_process_exited(state, role):
            continue
        if not alive(pid, role):
            raise RuntimeError("Saved process identity is unverified; its claim was retained")
        os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + 10
        while not recorded_process_exited(state, role):
            # Shutdown can make complete identity temporarily unreadable. Only
            # positive exit proof releases the claim; do not signal a second time.
            if time.monotonic() >= deadline:
                raise RuntimeError("Process exit could not be confirmed; claim was retained")
            time.sleep(0.1)
    STATE.unlink(missing_ok=True)


def recorded_process_exited(state, role):
    pid = state.get(role)
    birth = state.get("births", {}).get(role)
    return not pid or process_has_exited(pid, tuple(birth) if birth is not None else None)


def status():
    state = saved()
    return {
        **state,
        "api_running": bool(state.get("api") and alive(state["api"], "api")),
        "worker_running": bool(state.get("worker") and alive(state["worker"], "worker")),
    }


def unresolved_start(state):
    if state.get("phase") != "STARTING":
        return False
    for role in ("api", "worker"):
        pid = state.get(role)
        if type(pid) is not int:
            continue
        if recorded_process_exited(state, role):
            continue
        birth = process_birth(pid)
        expected = state.get("births", {}).get(role)
        if birth is not None and expected is not None and birth != tuple(expected):
            continue
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            continue
        return True
    return False


def start(port, offline):
    from incident_desk.process_probe import owns_health_listener

    current = status()
    if unresolved_start(current):
        raise RuntimeError("Previous startup is unresolved; inspect it before starting another")
    if current["api_running"] or current["worker_running"]:
        raise RuntimeError("Service already partially/fully running; use status or restart")
    LOCAL.mkdir(exist_ok=True, mode=0o700)
    LOCAL.chmod(0o700)
    subprocess.run(
        [str(PYTHON), "-m", "incident_desk", "--local-dir", str(LOCAL), "init"],
        cwd=LOCAL,
        check=True,
    )
    state = {
        "port": port,
        "mode": "offline-scripted" if offline else "deepseek",
        "url": f"http://127.0.0.1:{port}",
    }
    write_private(SERVICE_SETTINGS, state)
    state["phase"] = "STARTING"
    children, births = {}, {}
    try:
        for role in ("api", "worker"):
            command = [str(PYTHON), "-m", "incident_desk", "--local-dir", str(LOCAL), role]
            if role == "api":
                command += ["--port", str(port)]
            elif offline:
                command += ["--offline"]
            with (LOCAL / f"{role}.log").open("a") as log:
                os.fchmod(log.fileno(), 0o600)
                process = Popen(command, cwd=LOCAL, stdout=log, stderr=log, start_new_session=True)
            children[role] = process
            births[role] = process_birth(process.pid)
            state[role] = process.pid
            state["births"] = births
            write_private(STATE, state)
            if births[role] is None:
                raise RuntimeError("Cannot prove the newly spawned child's birth identity")
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            current = {
                role: owned_child_identity(child, births[role], process_identity)
                for role, child in children.items()
            }
            if not all(current.values()):
                raise RuntimeError("A child exited; inspect local api.log and worker.log")
            ready = False
            try:
                with urllib.request.urlopen(state["url"] + "/health", timeout=1) as response:
                    body = response.read(65537)
                    if len(body) <= 65536:
                        worker = json.loads(body).get("worker", {})
                        ready = (
                            response.status == 200
                            and response.headers.get("X-IncidentDesk-PID") == str(state["api"])
                            and worker.get("pid") == state["worker"]
                            and worker.get("status") == "ready"
                        )
            except (OSError, ValueError):
                pass
            if ready and asyncio.run(owns_health_listener(state["api"], state["url"] + "/health")):
                settled = {
                    role: owned_child_identity(child, births[role], process_identity)
                    for role, child in children.items()
                }
                if settled == current:
                    state["identities"] = settled
                    state["births"] = births
                    state["phase"] = "READY"
                    write_private(STATE, state)
                    return status()
            time.sleep(0.1)
        raise RuntimeError("Owned API/worker readiness timeout; inspect local logs")
    except BaseException:
        # A failed new launch must not call generic stop() on saved/stale PIDs.
        for role, child in reversed(list(children.items())):
            birth = births.get(role) or process_birth(child.pid)
            terminate_owned_child(child, birth, process_identity)
        actual = saved()
        if children and all(
            child.poll() is not None and actual.get(role) == child.pid
            for role, child in children.items()
        ):
            STATE.unlink(missing_ok=True)
        raise


def port_number(value):
    port = int(value)
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["start", "stop", "restart", "status"])
    parser.add_argument("--port", type=port_number, default=None)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--offline", dest="offline", action="store_true")
    modes.add_argument("--online", dest="offline", action="store_false")
    parser.set_defaults(offline=None)
    args = parser.parse_args(argv)
    if args.action == "status":
        result = status()
    else:
        with maintenance_lock():
            previous = saved() if args.action == "restart" else {}
            port = args.port if args.port is not None else previous.get("port", 8787)
            offline = (
                args.offline
                if args.offline is not None
                else (previous.get("mode") == "offline-scripted")
            )
            if args.action in {"stop", "restart"}:
                stop()
            result = start(port, offline) if args.action in {"start", "restart"} else status()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"Local service failed: {type(error).__name__}: {error}", file=sys.stderr)
        sys.exit(1)
