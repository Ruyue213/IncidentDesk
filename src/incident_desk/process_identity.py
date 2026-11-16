"""Proof of ownership while a directly spawned child completes its initial exec.

These helpers apply only while retaining that exact Popen handle. Persisted
process ownership still requires an exact complete start-time/command match.
"""

import asyncio
import ctypes
import os
import subprocess
import sys
from pathlib import Path


class _BSDInfo(ctypes.Structure):
    # macOS public sys/proc_info.h: struct proc_bsdinfo / PROC_PIDTBSDINFO.
    _fields_ = (
        [
            (name, ctypes.c_uint32)
            for name in (
                "flags",
                "status",
                "xstatus",
                "pid",
                "ppid",
                "uid",
                "gid",
                "ruid",
                "rgid",
                "svuid",
                "svgid",
                "rfu",
            )
        ]
        + [("comm", ctypes.c_char * 16), ("name", ctypes.c_char * 32)]
        + [(name, ctypes.c_uint32) for name in ("nfiles", "pgid", "jobc", "tdev", "tpgid")]
        + [
            ("nice", ctypes.c_int32),
            ("start_sec", ctypes.c_uint64),
            ("start_usec", ctypes.c_uint64),
        ]
    )


def _birth_and_parent(pid):
    if type(pid) is not int or not 0 < pid <= 2**31 - 1:
        return None
    try:
        if sys.platform == "darwin":
            library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
            library.proc_pidinfo.argtypes = [
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_uint64,
                ctypes.c_void_p,
                ctypes.c_int,
            ]
            library.proc_pidinfo.restype = ctypes.c_int
            info = _BSDInfo()
            length = library.proc_pidinfo(pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info))
            if length != ctypes.sizeof(info) or info.pid != pid or not info.start_sec:
                return None
            return ("darwin", pid, info.start_sec, info.start_usec), info.ppid, info.status == 5
        if sys.platform.startswith("linux"):
            with (Path("/proc") / str(pid) / "stat").open("rb") as stream:
                raw = stream.read(4097)
            if len(raw) > 4096:
                return None
            fields = raw.rsplit(b")", 1)[1].split()
            return ("linux", pid, int(fields[19])), int(fields[1]), fields[0] in {b"Z", b"X"}
    except (OSError, ValueError, IndexError, AttributeError):
        return None
    return None


def process_birth(pid):
    """Persistent birth invariant; reparenting does not create a different process."""
    info = _birth_and_parent(pid)
    return info[0] if info else None


def process_exists(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except OSError:
        return None


def process_has_exited(pid, birth=None):
    """Only positive kernel evidence permits declaring the original process gone."""
    info = _birth_and_parent(pid)
    if info is None:
        if process_exists(pid) is False:
            return True
        # Darwin proc_pidinfo can withhold BSD info for an unreaped zombie,
        # while kill(pid, 0) still succeeds. ps exposes its kernel Z state.
        try:
            result = subprocess.run(
                ["ps", "-p", str(pid), "-o", "stat="],
                capture_output=True,
                text=True,
                check=False,
                timeout=3,
            )
            return result.returncode == 0 and result.stdout.strip().startswith("Z")
        except (OSError, subprocess.TimeoutExpired):
            return False
    return bool(info[2] or (birth is not None and info[0] != birth))


def owned_child_identity(process, birth, reader):
    # Popen retains an unreaped child until poll/wait. Once reaped, returncode
    # stays set: a reused PID can never become this handle's live child again.
    if birth is None or process.poll() is not None:
        return None
    snapshot = _birth_and_parent(process.pid)
    if snapshot is None or snapshot[:2] != (birth, os.getpid()) or snapshot[2]:
        return None
    current = reader(process.pid)
    snapshot = _birth_and_parent(process.pid)
    return current if current and snapshot == (birth, os.getpid(), False) else None


async def finish_cleanup(task):
    """Retain spawn/cleanup ownership even if cancellation is requested again."""
    while True:
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.done():
                return task.result()


def terminate_owned_child(process, birth, reader, *, grace=5):
    """Clean up this spawn only; argv may change, but birth and handle must agree."""
    if process.poll() is not None:
        return "exited"
    if owned_child_identity(process, birth, reader) is None:
        return "identity_unverified"
    try:
        process.terminate()
        try:
            process.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            if owned_child_identity(process, birth, reader) is None:
                return "identity_unverified"
            process.kill()
            process.wait(timeout=grace)
        return "terminated"
    except ProcessLookupError:
        return "exited" if process.poll() is not None else "identity_unverified"
