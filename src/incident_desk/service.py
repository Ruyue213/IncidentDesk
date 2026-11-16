"""Start/stop the local API and worker; no shell, cloud hosting, or login-time installation."""

import argparse
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
    expected = saved().get("identities", {}).get(role)
    if expected is not None and expected != identity:
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
    for role in ("worker", "api"):
        pid = state.get(role)
        if pid and alive(pid, role):
            os.kill(pid, signal.SIGTERM)
            deadline = time.monotonic() + 5
            while alive(pid, role) and time.monotonic() < deadline:
                time.sleep(0.1)
            if alive(pid, role):
                os.kill(pid, signal.SIGKILL)
    STATE.unlink(missing_ok=True)


def status():
    state = saved()
    return {
        **state,
        "api_running": bool(state.get("api") and alive(state["api"], "api")),
        "worker_running": bool(state.get("worker") and alive(state["worker"], "worker")),
    }


def start(port, offline):
    current = status()
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
    for role in ("api", "worker"):
        command = [str(PYTHON), "-m", "incident_desk", "--local-dir", str(LOCAL), role]
        if role == "api":
            command += ["--port", str(port)]
        elif offline:
            command += ["--offline"]
        with (LOCAL / f"{role}.log").open("a") as log:
            os.fchmod(log.fileno(), 0o600)
            process = subprocess.Popen(
                command, cwd=LOCAL, stdout=log, stderr=log, start_new_session=True
            )
        state[role] = process.pid
        state.setdefault("identities", {})[role] = process_identity(process.pid)
        write_private(STATE, state)
    for _ in range(50):
        if not all(alive(state[role], role) for role in ("api", "worker")):
            stop()
            raise RuntimeError("A process exited; inspect .local/api.log and .local/worker.log")
        try:
            with urllib.request.urlopen(state["url"] + "/health", timeout=1) as response:
                if response.status == 200 and response.headers.get("X-IncidentDesk-PID") == str(
                    state["api"]
                ):
                    return status()
        except OSError:
            pass
        time.sleep(0.1)
    stop()
    raise RuntimeError("API health timeout; inspect local logs")


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
