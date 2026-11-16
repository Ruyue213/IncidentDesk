"""Private stopped-state backup/restore. Never restore runtime process identities.

Usage: python scripts/backup_state.py backup --archive /private/backup/state.zip
       python scripts/backup_state.py restore --archive /private/backup/state.zip
Stop the API/worker first. Restore also requires stopping all managed services.
Only the state directory is included; external project files and .env are not.
"""

import argparse
import fcntl
import hashlib
import importlib.metadata
import json
import os
import shutil
import stat
import subprocess
import tempfile
import uuid
import zipfile
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from incident_desk.runtime_paths import ROOT, default_home

DEFAULT_STATE = default_home()
FORMAT = "incidentdesk-local-state"
FORMAT_VERSION = 1
MAX_ARCHIVE_BYTES = 2 * 1024**3
MAX_FILES = 100000


def application_version():
    return importlib.metadata.version("incident-desk")


def process_identity(pid):
    if type(pid) is not int or pid <= 0:
        raise ValueError("Invalid saved process identity")
    result = subprocess.run(
        ["ps", "-p", str(pid), "-o", "lstart=", "-o", "command="],
        capture_output=True,
        text=True,
        timeout=3,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def application_processes(state):
    result = subprocess.run(
        ["ps", "-ax", "-o", "command="], capture_output=True, text=True, timeout=5, check=True
    )
    found = []
    for command in result.stdout.splitlines():
        if "-m incident_desk " not in command or not any(
            f" {role}" in command for role in ("api", "worker")
        ):
            continue
        if "--local-dir" in command:
            if str(state) in command:
                found.append(command)
        elif state == DEFAULT_STATE.resolve() and str(ROOT / ".venv") in command:
            found.append(command)
    return found


def ensure_stopped(state, *, restoring=False):
    process_file = state / "processes.json"
    if process_file.exists():
        saved = json.loads(process_file.read_text())
        for role in ("api", "worker"):
            pid = saved.get(role)
            identity = process_identity(pid) if pid is not None else None
            if identity and f"-m incident_desk {role}" in identity:
                raise RuntimeError("Stop the IncidentDesk API and worker before state maintenance")
    if application_processes(state):
        raise RuntimeError("Stop the IncidentDesk API and worker before state maintenance")
    if restoring:
        for path in state.glob("managed/*/process.json"):
            data = json.loads(path.read_text())
            if process_identity(data["pid"]) is not None:
                raise RuntimeError("Stop all managed services before restoring state")


@contextmanager
def stopped_state(state, *, restoring=False):
    state.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(
        state.parent / f".{state.name}.maintenance.lock",
        os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
        0o600,
    )
    worker = None
    try:
        os.fchmod(descriptor, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Another local state maintenance operation is running") from None
        ensure_stopped(state, restoring=restoring)
        if (state / "worker.lock").exists():
            worker = os.open(state / "worker.lock", os.O_RDWR | os.O_NOFOLLOW)
            try:
                fcntl.flock(worker, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError(
                    "Stop the IncidentDesk worker before state maintenance"
                ) from None
        yield
        ensure_stopped(state, restoring=restoring)
    finally:
        if worker is not None:
            os.close(worker)
        os.close(descriptor)


def private_parent(path):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.parent.is_symlink() or path.parent.stat().st_mode & 0o077:
        raise ValueError("Use a private backup directory with permissions 700")


def runtime_file(relative):
    return (
        relative == "processes.json"
        or relative == "models"
        or relative.startswith("models/")
        or relative.endswith(".lock")
        or relative.endswith((".db-shm", ".sqlite-shm", ".sqlite3-shm"))
        or (relative.startswith("managed/") and relative.endswith("/process.json"))
    )


def inventory(state):
    files = {}
    omitted = []
    paths = []
    for directory, names, filenames in os.walk(state, followlinks=False):
        current = Path(directory)
        if current == state and "models" in names:
            names.remove("models")
            omitted.append("models/")
        for name in names:
            if (current / name).is_symlink():
                raise ValueError("Symlinks are not supported in private state backups")
        paths.extend(current / name for name in filenames)
    for path in sorted(paths):
        if path.is_symlink():
            raise ValueError("Symlinks are not supported in private state backups")
        if path.is_dir():
            continue
        if not path.is_file():
            raise ValueError("Only regular state files can be backed up")
        relative = path.relative_to(state).as_posix()
        if runtime_file(relative):
            omitted.append(relative)
            if relative == "processes.json":
                data = json.loads(path.read_text())
                # Declarative launch preferences are safe to restore; PID values are not.
                preference = {key: data[key] for key in ("port", "mode", "url") if key in data}
                files.setdefault("service-settings.json", json.dumps(preference).encode())
            elif relative.startswith("managed/") and relative.endswith("/process.json"):
                data = json.loads(path.read_text())
                metadata = {
                    key: data[key] for key in ("operation_id", "project_version") if key in data
                }
                metadata["restored_process_identity"] = False
                files[str(PurePosixPath(relative).parent / "backup-metadata.json")] = json.dumps(
                    metadata
                ).encode()
            continue
        files[relative] = path
    if not files:
        raise ValueError("State directory contains no restorable files")
    return files, omitted


def write_backup(state, archive):
    private_parent(archive)
    if archive.exists():
        raise FileExistsError("Backup archive already exists; choose a new filename")
    if archive.is_relative_to(state):
        raise ValueError("Store backups outside the state directory")
    files, omitted = inventory(state)
    if len(files) > MAX_FILES:
        raise ValueError("State contains too many files")
    manifest = {
        "format": FORMAT,
        "format_version": FORMAT_VERSION,
        "application_version": application_version(),
        "created_at": datetime.now(UTC).isoformat(),
        "files": {},
        "omitted_runtime_files": omitted,
    }
    descriptor, temporary = tempfile.mkstemp(prefix=".state-backup-", dir=archive.parent)
    total = 0
    try:
        os.fchmod(descriptor, 0o600)
        with (
            os.fdopen(descriptor, "wb") as output,
            zipfile.ZipFile(
                output, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True
            ) as zipped,
        ):
            for relative, source in sorted(files.items()):
                hasher = hashlib.sha256()
                size = 0
                if isinstance(source, bytes):
                    zipped.writestr(f"state/{relative}", source)
                    hasher.update(source)
                    size = len(source)
                else:
                    with (
                        source.open("rb") as incoming,
                        zipped.open(f"state/{relative}", "w") as target,
                    ):
                        while chunk := incoming.read(1024**2):
                            size += len(chunk)
                            if total + size > MAX_ARCHIVE_BYTES:
                                raise ValueError("State exceeds the backup size limit")
                            hasher.update(chunk)
                            target.write(chunk)
                total += size
                manifest["files"][relative] = {"sha256": hasher.hexdigest(), "size": size}
            manifest_bytes = json.dumps(manifest, ensure_ascii=False).encode()
            zipped.writestr("manifest.json", manifest_bytes)
            zipped.writestr("manifest.sha256", hashlib.sha256(manifest_bytes).hexdigest())
        with open(temporary, "rb") as complete:
            os.fsync(complete.fileno())
        # Exclusive publication: another backup cannot be overwritten by a race.
        os.link(temporary, archive)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return manifest


def backup_state(state, archive):
    if Path(state).is_symlink():
        raise ValueError("State directory must not be a symlink")
    state = Path(state).resolve()
    archive = Path(archive).parent.resolve() / Path(archive).name
    if not state.is_dir():
        raise ValueError("State directory does not exist")
    with stopped_state(state):
        return write_backup(state, archive)


def safe_relative(value):
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or ".." in path.parts
        or "\\" in value
        or path.as_posix() != value
        or runtime_file(value)
    ):
        raise ValueError("Unsafe or runtime-only file in archive")
    return path


def unpack_verified(archive, staging):
    with zipfile.ZipFile(archive) as zipped:
        infos = zipped.infolist()
        names = [item.filename for item in infos]
        if len(names) != len(set(names)) or len(names) > MAX_FILES + 2:
            raise ValueError("Duplicate entries or too many archive files")
        if any(stat.S_ISLNK(item.external_attr >> 16) for item in infos):
            raise ValueError("Archive symlinks are not allowed")
        if "manifest.json" not in names or zipped.getinfo("manifest.json").file_size > 16 * 1024**2:
            raise ValueError("Missing or oversized backup manifest")
        if "manifest.sha256" not in names or zipped.getinfo("manifest.sha256").file_size != 64:
            raise ValueError("Missing or invalid backup manifest digest")
        manifest_bytes = zipped.read("manifest.json")
        if hashlib.sha256(manifest_bytes).hexdigest().encode() != zipped.read("manifest.sha256"):
            raise ValueError("Backup manifest digest verification failed")
        manifest = json.loads(manifest_bytes)
        if manifest.get("format") != FORMAT or manifest.get("format_version") != FORMAT_VERSION:
            raise ValueError("Unsupported backup format version")
        if manifest.get("application_version") != application_version():
            raise ValueError(
                "Backup application version differs; restore with its original release"
            )
        files = manifest.get("files")
        if not isinstance(files, dict) or not files or len(files) > MAX_FILES:
            raise ValueError("Invalid backup file manifest")
        if set(names) != {"manifest.json", "manifest.sha256"} | {f"state/{name}" for name in files}:
            raise ValueError("Archive contents do not match its manifest")
        total = 0
        for relative, expected in files.items():
            path = safe_relative(relative)
            size = expected.get("size")
            if type(size) is not int or size < 0:
                raise ValueError("Invalid declared file size")
            total += size
            if total > MAX_ARCHIVE_BYTES or zipped.getinfo(f"state/{relative}").file_size != size:
                raise ValueError("Backup exceeds size limit or contains inconsistent file sizes")
            destination = staging.joinpath(*path.parts)
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            # pathlib's parents=True uses default permissions for intermediate
            # directories; tighten each one rather than relying on the umask.
            for directory in destination.parents:
                directory.chmod(0o700)
                if directory == staging:
                    break
            hasher = hashlib.sha256()
            written = 0
            descriptor = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with (
                os.fdopen(descriptor, "wb") as output,
                zipped.open(f"state/{relative}") as incoming,
            ):
                os.fchmod(output.fileno(), 0o600)
                while chunk := incoming.read(1024**2):
                    written += len(chunk)
                    if written > size:
                        raise ValueError("Unpacked file exceeded declared size")
                    hasher.update(chunk)
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            if written != size or hasher.hexdigest() != expected.get("sha256"):
                raise ValueError("Backup file digest verification failed")
    return manifest


def restore_state(state, archive, *, replace=False):
    state, archive = Path(state).absolute(), Path(archive).absolute()
    if state.is_symlink() or archive.is_symlink():
        raise ValueError("State and archive must not be symlinks")
    state, archive = state.resolve(), archive.resolve()
    if archive.is_relative_to(state):
        raise ValueError("Restore archive must be outside the state directory")
    with stopped_state(state, restoring=True):
        if state.exists() and any(state.iterdir()) and not replace:
            raise FileExistsError(
                "State already exists; use --replace to back it up before restoring"
            )
        staging = Path(tempfile.mkdtemp(prefix=f".{state.name}-restore-", dir=state.parent))
        previous = state.parent / f".{state.name}-previous-{uuid.uuid4().hex}"
        saved_archive = None
        try:
            manifest = unpack_verified(archive, staging)
            if state.exists() and any(state.iterdir()):
                saved_archive = archive.parent / f"pre-restore-{uuid.uuid4().hex}.zip"
                write_backup(state, saved_archive)
            ensure_stopped(state, restoring=True)
            if state.exists():
                os.replace(state, previous)
            try:
                os.replace(staging, state)
            except BaseException:
                if previous.exists():
                    os.replace(previous, state)
                raise
            if previous.exists():
                shutil.rmtree(previous)
            return {
                "manifest": manifest,
                "previous_backup": str(saved_archive) if saved_archive else None,
            }
        finally:
            if staging.exists():
                shutil.rmtree(staging)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["backup", "restore"])
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--replace", action="store_true")
    args = parser.parse_args(argv)
    if args.action == "backup":
        if args.replace:
            parser.error("--replace is only valid for restore")
        manifest = backup_state(args.state_dir, args.archive)
        print(json.dumps({"status": "backed_up", "files": len(manifest["files"])}))
    else:
        result = restore_state(args.state_dir, args.archive, replace=args.replace)
        print(json.dumps({"status": "restored", "previous_backup": result["previous_backup"]}))


if __name__ == "__main__":
    main()
