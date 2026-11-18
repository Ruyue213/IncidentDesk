"""Cancellable single-host exclusion for approved effects against the same project."""

import asyncio
import fcntl
import os
import stat
from contextlib import asynccontextmanager
from pathlib import Path

from incident_desk.projects import PROJECT_ID


@asynccontextmanager
async def project_action_lock(local_dir: Path, project_id: str):
    if not PROJECT_ID.fullmatch(project_id):
        raise ValueError("Invalid project identity")
    directory = Path(local_dir) / "project-action-locks"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if directory.is_symlink():
        raise ValueError("Project action lock directory cannot be a symlink")
    descriptor = os.open(
        directory / f"{project_id}.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
    )
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError("Project action lock must be a regular file")
        os.fchmod(descriptor, 0o600)
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                await asyncio.sleep(0.05)
        yield
    finally:
        os.close(descriptor)
