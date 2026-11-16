"""Bounded read-only local collectors. Every response carries honest source coverage."""

import asyncio
import hashlib
import json
import os
import re
import shutil
import stat
import time
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from incident_desk.process_identity import process_birth
from incident_desk.projects import ProjectProfile, project_path, secret_path
from incident_desk.settings import Settings

MAX_FILE_BYTES = 256 * 1024
MAX_HTTP_BYTES = 64 * 1024
HTTP_DEADLINE_SECONDS = 3
MAX_CONTENT = 12000
TEXT_SUFFIXES = {
    ".py",
    ".cpp",
    ".cc",
    ".c",
    ".h",
    ".hpp",
    ".rs",
    ".go",
    ".java",
    ".kt",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".vue",
    ".svelte",
    ".md",
    ".txt",
    ".rst",
    ".json",
    ".toml",
    ".yaml",
    ".yml",
    ".ini",
    ".conf",
    ".cfg",
    ".sh",
    ".sql",
    ".html",
    ".css",
    ".xml",
    ".cmake",
    ".log",
}
TEXT_NAMES = {"Makefile", "Dockerfile", "CMakeLists.txt", "LICENSE", "README"}
IGNORED_DIRS = {".git", ".local", ".venv", "node_modules", "build", "dist", "__pycache__"}


def redact(value: str) -> str:
    """Apply before snapshots and tool messages, including credentials inside URLs."""
    value = re.sub(
        r"-----BEGIN [^-\n]*PRIVATE KEY-----.*?-----END [^-\n]*PRIVATE KEY-----",
        "[REDACTED PRIVATE KEY]",
        value,
        flags=re.S,
    )
    value = re.sub(r"(https?://)[^\s/@:]+:[^\s/@]+@", r"\1[REDACTED]@", value)
    value = re.sub(
        r"(?i)(authorization\s*[:=]\s*(?:bearer\s+|basic\s+)?)[^\s,;]+", r"\1[REDACTED]", value
    )
    value = re.sub(
        r"(?i)([\"']?(?:api[-_]?key|password|passwd|secret|access[-_]?token|"
        r"refresh[-_]?token|client[-_]?secret|token)[\"']?\s*[:=]\s*)"
        r"(?:\"[^\"]*\"|'[^']*'|[^\s,;}]+)",
        r"\1[REDACTED]",
        value,
    )
    value = re.sub(r"\b(?:sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9]{20,})\b", "[REDACTED]", value)
    value = re.sub(
        r"(?i)(--(?:api[-_]?key|password|secret|token)\s+)[^\s]+", r"\1[REDACTED]", value
    )
    return value


def utc(value: datetime | str) -> datetime:
    parsed = (
        datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
    )
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("time bounds require an explicit timezone")
    return parsed.astimezone(UTC)


def _read_bounded(path: Path, *, tail: bool = False, maximum: int = MAX_FILE_BYTES):
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as handle:
        size = os.fstat(handle.fileno()).st_size
        offset = max(0, size - maximum) if tail else 0
        handle.seek(offset)
        raw = handle.read(maximum + (0 if tail else 1))
    if b"\0" in raw:
        raise ValueError("binary files are not readable by the diagnostic tools")
    if offset:
        # Discard a partial first line; do not fabricate a timestamp from its suffix.
        raw = raw.split(b"\n", 1)[-1] if b"\n" in raw else b""
    return raw[:maximum].decode("utf-8", errors="replace"), size > maximum, offset


def _read_source(path: Path):
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as handle:
        stat = os.fstat(handle.fileno())
        raw = handle.read(MAX_FILE_BYTES + 1)
    if b"\0" in raw:
        raise ValueError("binary files are not readable by the diagnostic tools")
    clipped = len(raw) > MAX_FILE_BYTES or stat.st_size > MAX_FILE_BYTES
    digest = None if clipped else hashlib.sha256(raw).hexdigest()
    text = raw[:MAX_FILE_BYTES].decode("utf-8")
    return text, clipped, digest, stat.st_mtime


class LiveEvidence:
    def __init__(self, project: ProjectProfile, settings: Settings):
        self.project = project
        self.settings = settings

    def _batch(self, source: str, start: datetime, end: datetime) -> dict:
        return {
            "source": source,
            "project_id": self.project.id,
            "project_version": self.project.version,
            "status": "complete",
            "items": [],
            "error": None,
            "truncated": False,
            "collected_at": datetime.now(UTC).isoformat(),
            "coverage": {"requested_start": start.isoformat(), "requested_end": end.isoformat()},
        }

    def _item(self, source: str, content: str, provenance: dict, *, observed_at=None) -> dict:
        content = redact(content)
        clipped = len(content) > MAX_CONTENT
        content = content[:MAX_CONTENT]
        provenance = {
            **provenance,
            "project_id": self.project.id,
            "project_version": self.project.version,
            "config_digest": self.project.digest,
            "content_truncated": clipped,
        }
        raw = json.dumps(
            {"content": content, "provenance": provenance, "observed_at": observed_at},
            sort_keys=True,
        ).encode()
        digest = hashlib.sha256(raw).hexdigest()
        provenance["source_content_hash"] = digest
        collected = datetime.now(UTC).isoformat()
        # A frozen snapshot includes its collection instant. The source hash remains
        # stable for deduplication, while a later collection receives a distinct ID.
        snapshot = hashlib.sha256(f"{digest}:{collected}".encode()).hexdigest()
        identity = f"live-{self.project.id}-{source}-{snapshot[:24]}"
        return {
            "id": identity,
            "evidence_id": identity,
            "source": source,
            "version": f"sha256:{digest}",
            "content": content,
            "observed_at": observed_at,
            "collected_at": collected,
            "provenance": provenance,
            "truncated": clipped,
        }

    async def get(
        self, source: str, start: datetime | str, end: datetime | str, limit: int = 20
    ) -> dict:
        start, end = utc(start), utc(end)
        if start >= end or not 1 <= limit <= 100:
            raise ValueError("ordered time window and limit 1..100 required")
        if source not in {"logs", "health", "metrics", "stats", "process", "changes"}:
            raise ValueError("unsupported live evidence source")
        batch = self._batch(source, start, end)
        try:
            if source == "logs":
                await asyncio.to_thread(self._logs, batch, start, end, limit)
            elif source in {"health", "metrics", "stats"}:
                async with asyncio.timeout(HTTP_DEADLINE_SECONDS):
                    await self._http(source, batch)
            elif source == "process":
                await self._process(batch)
            else:
                await self._changes(batch, limit)
        except (OSError, ValueError, TimeoutError, httpx.HTTPError) as error:
            batch["status"] = "unavailable"
            batch["error"] = {"code": type(error).__name__, "message": "collector failed safely"}
        batch["truncated"] |= any(item["truncated"] for item in batch["items"])
        return batch

    def _logs(self, batch: dict, start: datetime, end: datetime, limit: int):
        batch["coverage"].update(
            {
                "kind": "bounded_log_tail",
                "timezone": self.project.timezone,
                "historical": True,
                "complete_window": False,
            }
        )
        names = [(name, False) for name in self.project.log_files]
        managed = self.settings.local_dir / "managed" / self.project.id / "service.log"
        if managed.is_file():
            names.append((str(managed), True))
        if not names:
            batch.update(
                status="unavailable",
                error={"code": "not_configured", "message": "no log files registered"},
            )
            return
        candidates = []
        failures = []
        undated = 0
        for name, internal in names:
            try:
                if internal:
                    path = Path(name)
                    base = (self.settings.local_dir / "managed" / self.project.id).resolve()
                    if path.is_symlink() or path.resolve().parent != base:
                        raise ValueError("invalid managed log")
                else:
                    path = project_path(self.project.root, name, must_exist=True)
                text, clipped, offset = _read_bounded(path, tail=True)
                batch["truncated"] |= clipped
                stat = path.stat()
                for index, line in enumerate(text.splitlines()):
                    # ISO timestamps or SocialEngine's local wall clock prefix.
                    match = re.search(
                        r"\b(\d{4}-\d\d-\d\d[T ]\d\d:\d\d:\d\d"
                        r"(?:[.,]\d+)?(?:Z|[+-]\d\d:\d\d)?)",
                        line,
                    )
                    observed = None
                    assumed = False
                    if match:
                        try:
                            stamp = datetime.fromisoformat(
                                match[1].replace(",", ".").replace("Z", "+00:00")
                            )
                            if stamp.tzinfo is None:
                                stamp = stamp.replace(tzinfo=ZoneInfo(self.project.timezone))
                                assumed = True
                            observed = stamp.astimezone(UTC)
                        except ValueError:
                            pass
                    if observed is None:
                        undated += 1
                        continue
                    if start <= observed < end:
                        candidates.append(
                            (
                                observed,
                                line,
                                {
                                    "collector": "managed_log" if internal else "local_log",
                                    "path": "managed/service.log"
                                    if internal
                                    else str(path.relative_to(self.project.root)),
                                    "file_inode": stat.st_ino,
                                    "tail_byte_offset": offset,
                                    "tail_line_index": index,
                                    "timestamp_timezone_assumed": assumed,
                                    "assumed_timezone": self.project.timezone if assumed else None,
                                },
                            )
                        )
            except (OSError, ValueError):
                failures.append({"path": redact(name), "code": "log_unreadable"})
        candidates.sort(key=lambda value: value[0])
        batch["truncated"] |= len(candidates) > limit
        batch["items"] = [
            self._item("logs", text, origin, observed_at=stamp.isoformat())
            for stamp, text, origin in candidates[-limit:]
        ]
        batch["coverage"]["undated_lines_excluded"] = undated
        batch["coverage"]["note"] = (
            "Only timestamped lines in the bounded tail are queried; gaps are unknown."
        )
        # A tail cannot prove completeness for a historical time window.
        batch["status"] = "partial"
        if failures:
            batch["error"] = {"code": "log_unreadable", "files": failures}
            if len(failures) == len(names):
                batch["status"] = "unavailable"

    async def _http(self, source: str, batch: dict):
        url = getattr(self.project, f"{source}_url")
        batch["coverage"].update(
            {
                "kind": "current_snapshot",
                "historical": False,
                "complete_window": False,
                "note": "Current scrape only; this endpoint provides no historical samples.",
            }
        )
        if not url:
            batch.update(
                status="unavailable",
                error={"code": "not_configured", "message": f"{source} URL not registered"},
            )
            return
        # Literal IPv4 loopback avoids DNS changes; localhost is normalized here.
        parsed = httpx.URL(url)
        if parsed.host == "localhost":
            parsed = parsed.copy_with(host="127.0.0.1")
        chunks, size, clipped = [], 0, False
        async with httpx.AsyncClient(timeout=3, follow_redirects=False, trust_env=False) as client:
            async with client.stream("GET", parsed) as response:
                async for chunk in response.aiter_bytes():
                    remaining = MAX_HTTP_BYTES - size
                    chunks.append(chunk[:remaining])
                    size += min(len(chunk), remaining)
                    if len(chunk) > remaining:
                        clipped = True
                        break
                content = b"".join(chunks).decode("utf-8", errors="replace")
                now = datetime.now(UTC).isoformat()
                batch["items"] = [
                    self._item(
                        source,
                        content or "(empty HTTP response)",
                        {
                            "collector": "local_http",
                            "url": str(parsed),
                            "http_status": response.status_code,
                            "redirect_followed": False,
                            "response_truncated": clipped,
                            "snapshot_at": now,
                        },
                        observed_at=now,
                    )
                ]
                batch["status"] = "partial" if response.is_success else "unavailable"
                if not response.is_success:
                    batch["error"] = {"code": "http_status", "status": response.status_code}
        batch["truncated"] = clipped

    async def _command(self, *argv: str, max_bytes: int = MAX_HTTP_BYTES) -> tuple[int, str]:
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=self.project.root,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            assert process.stdout is not None

            async def read_output():
                chunks, total = [], 0
                while True:
                    chunk = await process.stdout.read(min(8192, max_bytes + 1 - total))
                    if not chunk:
                        return b"".join(chunks)
                    chunks.append(chunk)
                    total += len(chunk)
                    if total > max_bytes:
                        return b"".join(chunks)

            data = await asyncio.wait_for(read_output(), timeout=3)
            if len(data) > max_bytes:
                process.kill()
                await process.wait()
                return 0, data[:max_bytes].decode(errors="replace") + "\n[TRUNCATED]"
            await asyncio.wait_for(process.wait(), timeout=3)
            return process.returncode or 0, data.decode(errors="replace")
        except BaseException:
            if process.returncode is None:
                process.kill()
                await process.wait()
            raise

    async def _process(self, batch: dict):
        from incident_desk.process_probe import owns_health_listener

        batch["coverage"].update(kind="current_snapshot", historical=False, complete_window=False)
        if not self.project.start_argv:
            batch.update(
                status="unavailable",
                error={"code": "not_configured", "message": "no executable registered"},
            )
            return
        executable_path = Path(self.project.start_argv[0])
        if not executable_path.is_absolute():
            executable_path = self.project.root / executable_path
        resolved_executable = await asyncio.to_thread(executable_path.resolve)
        executables = {Path(self.project.start_argv[0]).name, resolved_executable.name}
        code, listing = await self._command("ps", "-axo", "pid=,comm=", max_bytes=256 * 1024)
        if code:
            raise OSError("process inspection failed")
        state = await asyncio.to_thread(self._managed_state)
        managed_pid = state.get("pid") if state else None
        if type(managed_pid) is not int or not 0 < managed_pid <= 2**31 - 1:
            managed_pid = None
        candidate_pids = [managed_pid] if managed_pid else []
        for line in listing.splitlines():
            parts = line.strip().split(maxsplit=1)
            if len(parts) != 2 or Path(parts[1]).name not in executables or not parts[0].isdigit():
                continue
            pid = int(parts[0])
            if pid not in candidate_pids:
                candidate_pids.append(pid)
            if len(candidate_pids) >= 60:
                batch["truncated"] = True
                break
        if "[TRUNCATED]" in listing:
            batch["truncated"] = True
        target, others, managed_identity = [], [], None
        expected_commands = {
            " ".join(self.project.start_argv),
            " ".join([str(resolved_executable), *self.project.start_argv[1:]]),
        }
        for pid in candidate_pids:
            code, process_identity = await self._command(
                "ps", "-p", str(pid), "-o", "lstart=", "-o", "command="
            )
            if pid == managed_pid:
                managed_identity = process_identity.strip() if code == 0 else ""
            if code or not process_identity.strip():
                continue
            cwd = None
            if shutil.which("lsof"):
                _, result = await self._command("lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn")
                cwd = next((line[1:] for line in result.splitlines() if line.startswith("n")), None)
            else:
                try:
                    cwd = str(await asyncio.to_thread(Path(f"/proc/{pid}/cwd").resolve, True))
                except OSError:
                    pass
            if cwd is None or await asyncio.to_thread(Path(cwd).resolve) != self.project.root:
                continue
            _, command = await self._command("ps", "-p", str(pid), "-o", "command=")
            _, detail = await self._command("ps", "-p", str(pid), "-o", "pid=,lstart=,comm=,args=")
            managed = bool(
                state
                and state.get("phase") != "STARTING"
                and pid == managed_pid
                and state.get("identity") == process_identity.strip()
                and (
                    "birth" not in state
                    or await asyncio.to_thread(process_birth, pid) == tuple(state["birth"])
                )
            )
            argv_matches = command.strip() in expected_commands
            listener = (
                await owns_health_listener(pid, self.project.health_url)
                if self.project.health_url
                else False
            )
            confirmed = managed if state else argv_matches and listener
            entry = {
                "pid": pid,
                "cwd": cwd,
                "detail": redact(detail.strip())[:2000],
                "ownership": "managed_identity_verified"
                if managed
                else "observed_only_not_authorized_for_signal",
                "registered_argv_matches": argv_matches,
                "registered_health_listener_owned": listener,
            }
            if confirmed:
                target.append(entry)
            elif len(others) < 10:
                others.append(entry)
            else:
                batch["truncated"] = True
        if state:
            if target:
                running, scope = True, "managed_pid_identity_verified"
            elif (
                managed_pid
                and managed_identity is not None
                and managed_identity != state.get("identity")
            ):
                running, scope = False, "managed_identity_no_longer_present"
            else:
                running, scope = None, "indeterminate_managed_identity_not_verified"
        elif target:
            running, scope = True, "registered_argv_and_listener_verified"
        else:
            running, scope = None, "indeterminate_no_target_ownership_proof"
        if running is None or batch["truncated"]:
            batch["status"] = "partial"
        now = datetime.now(UTC).isoformat()
        batch["items"] = [
            self._item(
                "process",
                json.dumps(
                    {
                        "matching_processes": target,
                        "other_observed": others,
                        "running": running,
                        "target_scope": scope,
                        "managed_pid": managed_pid,
                        "note": (
                            "Only the registered target can establish running. "
                            "Other same-directory instances do not establish target health; "
                            "signals require a launch receipt."
                        ),
                    },
                    ensure_ascii=False,
                ),
                {"collector": "local_process", "snapshot_at": now, "target_scope": scope},
                observed_at=now,
            )
        ]

    def _managed_state(self) -> dict | None:
        path = self.settings.local_dir / "managed" / self.project.id / "process.json"
        try:
            if path.is_symlink() or not path.is_file():
                return None
            text, clipped, _ = _read_bounded(path, maximum=8192)
            if clipped:
                return None
            state = json.loads(text)
            return state if isinstance(state, dict) else None
        except (OSError, ValueError):
            return None

    async def _changes(self, batch: dict, limit: int):
        batch["coverage"].update(
            kind="current_worktree_metadata",
            historical=False,
            complete_window=False,
            note="Git status is current metadata; no file contents or diff are read.",
        )
        if not await asyncio.to_thread((self.project.root / ".git").exists):
            batch.update(
                status="unavailable",
                error={"code": "not_git_repository", "message": "no Git metadata in project root"},
            )
            return
        code, output = await self._command(
            "git",
            "-c",
            "core.fsmonitor=false",
            "-C",
            str(self.project.root),
            "status",
            "--porcelain=v1",
            "--untracked-files=no",
        )
        if code:
            raise OSError("git metadata unavailable")
        lines = [line for line in output.splitlines() if not secret_path(line[3:])]
        now = datetime.now(UTC).isoformat()
        batch["truncated"] = len(lines) > limit or "[TRUNCATED]" in output
        batch["items"] = [
            self._item(
                "changes",
                "\n".join(lines[:limit]) or "No tracked worktree changes.",
                {"collector": "git_status", "snapshot_at": now},
                observed_at=now,
            )
        ]

    def _source_path(self, value: str) -> Path:
        path = project_path(self.project.root, value, must_exist=True)
        if not path.is_file() or (
            path.suffix.lower() not in TEXT_SUFFIXES and path.name not in TEXT_NAMES
        ):
            raise ValueError("only allowed project text files can be read")
        if any(part in IGNORED_DIRS for part in path.relative_to(self.project.root).parts):
            raise ValueError("generated, dependency, and private state directories are excluded")
        return path

    async def read_file(self, path: str, offset: int = 0, limit: int = 200) -> dict:
        if not 0 <= offset <= 100000 or not 1 <= limit <= 300:
            raise ValueError("line offset/limit out of bounds")
        resolved = await asyncio.to_thread(self._source_path, path)
        text, clipped, file_hash, modified = await asyncio.to_thread(_read_source, resolved)
        lines = text.splitlines()
        selected = lines[offset : offset + limit]
        content = "\n".join(f"{offset + n + 1}: {line}" for n, line in enumerate(selected))
        registered = any(
            project_path(self.project.root, name) == resolved for name in self.project.config_files
        )
        complete = not clipped and offset == 0 and len(lines) <= limit
        editable = registered and complete and len(text) <= MAX_CONTENT and redact(text) == text
        if editable:
            content = text
        item = self._item(
            "project_file",
            content or "(no lines in requested range)",
            {
                "collector": "project_read",
                "path": str(resolved.relative_to(self.project.root)),
                "line_start": offset + 1,
                "line_end": offset + len(selected),
                "file_truncated": clipped,
                "file_mtime": modified,
                "file_sha256": file_hash,
                "registered_config_file": registered,
                "editable": editable,
                "content_format": "full_file_plain_text" if editable else "numbered_excerpt",
            },
        )
        return {
            "status": "partial" if clipped or item["truncated"] else "complete",
            "items": [item],
            "error": None,
            "truncated": clipped or item["truncated"] or offset + limit < len(lines),
            "collected_at": item["collected_at"],
            "file_sha256": file_hash,
            "registered_config_file": registered,
            "editable": editable,
            "coverage": {
                "kind": "current_file",
                "historical": False,
                "line_offset": offset,
                "line_limit": limit,
            },
        }

    async def overview(self) -> dict:
        """Inspect real project metadata without opening source or executable contents."""

        def inspect():
            executable = {
                "registered": bool(self.project.start_argv),
                "path": None,
                "exists": False,
                "is_file": False,
                "executable": False,
                "size": None,
                "readable_by_source_tool": False,
            }
            if self.project.start_argv:
                path = Path(self.project.start_argv[0])
                if not path.is_absolute():
                    path = self.project.root / path
                executable["path"] = str(path)
                try:
                    metadata = path.stat()
                    executable.update(
                        exists=True,
                        is_file=stat.S_ISREG(metadata.st_mode),
                        executable=os.access(path, os.X_OK),
                        size=metadata.st_size,
                    )
                    self._source_path(str(path))
                    executable["readable_by_source_tool"] = True
                except (OSError, ValueError):
                    pass
            excluded = IGNORED_DIRS | {
                "third_party",
                "vendor",
                "vendors",
                "venv",
                "env",
                "target",
                ".claude",
                ".codex",
                ".incidentdesk",
                ".pytest_cache",
                ".mypy_cache",
                ".ruff_cache",
                ".idea",
                ".vscode",
                "coverage",
                "__pypackages__",
            }
            paths, pending, scanned, truncated, failures = [], [self.project.root], 0, False, 0
            deadline = time.monotonic() + 3
            while pending and not truncated:
                directory = pending.pop()
                try:
                    if directory.is_symlink() or not directory.resolve().is_relative_to(
                        self.project.root
                    ):
                        continue
                    with os.scandir(directory) as entries:
                        for entry in entries:
                            if scanned >= 500 or time.monotonic() > deadline:
                                truncated = True
                                break
                            scanned += 1
                            if entry.is_symlink() or secret_path(entry.name):
                                continue
                            if entry.is_dir(follow_symlinks=False):
                                if entry.name not in excluded:
                                    pending.append(Path(entry.path))
                                continue
                            try:
                                allowed = self._source_path(entry.path)
                            except (OSError, ValueError):
                                continue
                            paths.append(str(allowed.relative_to(self.project.root)))
                            if len(paths) >= 200:
                                truncated = True
                                break
                except OSError:
                    failures += 1
            content = {
                "project_id": self.project.id,
                "name": self.project.name,
                "root": str(self.project.root),
                "start_executable": executable,
                "registered_endpoints": {
                    name: getattr(self.project, f"{name}_url")
                    for name in ("health", "metrics", "stats")
                },
                "registered_log_files": self.project.log_files,
                "registered_config_files": self.project.config_files,
                "allowed_text_files": sorted(paths),
                "note": "Executable metadata is stat-only. Binary contents cannot be read. "
                "The bounded text list excludes secrets, dependencies and symlinks; "
                "an unlisted path is not proof that the file is absent.",
            }
            # Preserve complete metadata/JSON when long file paths reach the tool text cap.
            while (
                content["allowed_text_files"]
                and len(json.dumps(content, ensure_ascii=False)) > MAX_CONTENT
            ):
                content["allowed_text_files"].pop()
                truncated = True
            return content, scanned, truncated, failures

        content, scanned, truncated, failures = await asyncio.to_thread(inspect)
        now = datetime.now(UTC).isoformat()
        item = self._item(
            "project_overview",
            json.dumps(content, ensure_ascii=False),
            {
                "collector": "project_overview",
                "snapshot_at": now,
                "entries_scanned": scanned,
                "scan_limit": 500,
                "path_limit": 200,
                "scan_truncated": truncated,
                "unreadable_directories": failures,
                "executable_contents_read": False,
            },
            observed_at=now,
        )
        return {
            "status": "partial" if truncated or failures or item["truncated"] else "complete",
            "items": [item],
            "error": {"code": "directory_unreadable", "count": failures} if failures else None,
            "truncated": truncated or item["truncated"],
            "collected_at": item["collected_at"],
            "coverage": {
                "kind": "bounded_current_project_metadata",
                "historical": False,
                "entries_scanned": scanned,
                "scan_limit": 500,
                "path_limit": 200,
            },
        }

    async def search(self, query: str, limit: int = 30) -> dict:
        if not 2 <= len(query) <= 200 or not 1 <= limit <= 100 or "\x00" in query:
            raise ValueError("bounded literal search query required")

        def scan():
            matches, checked, truncated = [], 0, False
            entries, directories_checked = 0, 0
            deadline = time.monotonic() + 3
            for directory, directories, files in os.walk(self.project.root, followlinks=False):
                directories_checked += 1
                if directories_checked > 1000 or time.monotonic() > deadline:
                    return matches, True, checked
                directories[:] = sorted(
                    d
                    for d in directories
                    if d not in IGNORED_DIRS
                    and not secret_path(d)
                    and not (Path(directory) / d).is_symlink()
                )
                for name in sorted(files):
                    entries += 1
                    if entries > 5000 or time.monotonic() > deadline:
                        return matches, True, checked
                    try:
                        path = self._source_path(str(Path(directory) / name))
                    except (ValueError, OSError):
                        continue
                    checked += 1
                    if checked > 300:
                        return matches, True, checked - 1
                    try:
                        text, clipped, _ = _read_bounded(path, maximum=64 * 1024)
                    except (OSError, ValueError):
                        truncated = True
                        continue
                    truncated |= clipped
                    for number, line in enumerate(text.splitlines(), 1):
                        if query.casefold() in line.casefold():
                            matches.append(
                                {
                                    "path": str(path.relative_to(self.project.root)),
                                    "line": number,
                                    "text": redact(line)[:1000],
                                }
                            )
                            if len(matches) >= limit:
                                return matches, True, checked
            return matches, truncated, checked

        matches, truncated, checked = await asyncio.to_thread(scan)
        item = self._item(
            "project_search",
            json.dumps(matches, ensure_ascii=False),
            {
                "collector": "project_search",
                "query": redact(query),
                "files_checked": checked,
                "scan_truncated": truncated,
                "note": "Literal search over allowed text files; no private state or dependencies.",
            },
        )
        return {
            "status": "partial" if truncated or item["truncated"] else "complete",
            "items": [item],
            "error": None,
            "truncated": truncated or item["truncated"],
            "collected_at": item["collected_at"],
            "coverage": {
                "kind": "bounded_current_source_search",
                "historical": False,
                "files_checked": checked,
            },
        }
