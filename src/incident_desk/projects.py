"""Versioned local project registrations. Profiles contain no credentials or shell code."""

import hashlib
import json
import os
import re
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from incident_desk.cloud_policy import CloudPolicy
from incident_desk.knowledge.retrieval_config import RetrievalConfig

PROJECT_ID = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}\Z")
SECRET_NAME = re.compile(
    r"(^\.env(?:\.|$)|^\.git$|^\.ssh$|^\.aws$|^\.kube$|"
    r"credentials?|secrets?|tokens?|(?:^|[_.-])keys?(?:[_.-]|$)|^id_rsa|^id_ed25519)",
    re.IGNORECASE,
)
SECRET_SUFFIXES = {".pem", ".key", ".p12", ".pfx", ".keystore", ".db", ".sqlite"}


def secret_path(path: str | Path) -> bool:
    return any(SECRET_NAME.search(part) for part in Path(path).parts) or (
        Path(path).suffix.lower() in SECRET_SUFFIXES
    )


def project_path(root: Path, value: str | Path, *, must_exist: bool = False) -> Path:
    """Resolve registered paths with the same restrictions used by the read tools."""
    given = Path(value)
    logical = (
        given.relative_to(root) if given.is_absolute() and given.is_relative_to(root) else given
    )
    if not given.is_absolute() and secret_path(logical):
        raise ValueError("credential and private state paths cannot be registered or read")
    candidate = given if given.is_absolute() else root / given
    resolved = candidate.resolve(strict=must_exist)
    if (
        not resolved.is_relative_to(root.resolve())
        or secret_path(resolved.relative_to(root))
        or (given.is_relative_to(root) and secret_path(logical))
    ):
        raise ValueError("path must remain inside the registered project root")
    return resolved


def safe_project_path(project: "ProjectProfile", path: str | Path) -> Path:
    return project_path(project.root, path)


def local_url(value: str | None) -> str | None:
    if value is None:
        return None
    parsed = urlsplit(value)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or not parsed.path.startswith("/")
    ):
        raise ValueError("collector URL must be credential-free local HTTP with a fixed path")
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError("invalid collector port") from error
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("invalid collector port")
    if parsed.hostname == "localhost":
        authority = f"127.0.0.1:{port}" if port is not None else "127.0.0.1"
        return urlunsplit(("http", authority, parsed.path, "", ""))
    return value


class BusinessProbeProfile(BaseModel):
    """An operator-registered, fixed read-only probe, never a model-authored request."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    method: Literal["GET", "POST"] = "POST"
    path: str = "/query"
    body: dict = Field(default_factory=dict)
    expected_status: int = Field(default=200, ge=200, le=299)
    expected_json: dict = Field(default_factory=dict)
    max_latency_ms: float | None = Field(default=None, gt=0, le=60000)

    @model_validator(mode="after")
    def fixed_read_only_request(self):
        parsed = urlsplit(self.path)
        if (
            not self.path.startswith("/")
            or self.path.startswith("//")
            or parsed.netloc
            or parsed.scheme
            or parsed.query
            or parsed.fragment
            or "%" in self.path
            or ".." in self.path
            or "\\" in self.path
        ):
            raise ValueError("probe requires a fixed local relative path")
        if len(json.dumps(self.body)) > 4096 or len(json.dumps(self.expected_json)) > 4096:
            raise ValueError("probe request/expectation exceeds the size limit")
        if self.method == "POST":
            if (
                self.path != "/query"
                or self.body.get("type") not in {"cf", "sd", "dist"}
                or set(self.body) - {"type", "a", "b"}
                or not self.expected_json
            ):
                raise ValueError(
                    "POST probes only support fixed read-only graph queries with JSON assertions"
                )
            keys = ["a"] if self.body["type"] == "sd" else ["a", "b"]
            if any(
                type(self.body.get(key)) is not int or not 1 <= self.body[key] <= 2**31 - 1
                for key in keys
            ):
                raise ValueError("probe node IDs must be bounded positive integers")
        elif self.path not in {"/health", "/healthz", "/readyz", "/stats", "/metrics"} or self.body:
            raise ValueError("GET probes require an allowlisted read-only endpoint and no body")
        return self


class ProjectProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")
    name: str = Field(min_length=1, max_length=100)
    root: Path
    log_files: list[str] = Field(default_factory=list, max_length=20)
    health_url: str | None = None
    metrics_url: str | None = None
    stats_url: str | None = None
    start_argv: list[str] = Field(default_factory=list, max_length=100)
    config_files: list[str] = Field(default_factory=list, max_length=20)
    knowledge_paths: list[str] = Field(default_factory=list, max_length=30)
    retrieval_mode: Literal["lexical", "dense", "hybrid"] = "hybrid"
    retrieval_config: RetrievalConfig = Field(default_factory=RetrievalConfig)
    cloud_policy: CloudPolicy = Field(default_factory=CloudPolicy)
    history_enabled: bool = True
    business_probe: BusinessProbeProfile | None = None
    timezone: str = "Asia/Shanghai"
    version: int = Field(default=1, ge=1)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("root")
    @classmethod
    def valid_root(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("project root must be an absolute directory")
        return value.resolve()

    @field_validator("health_url", "metrics_url", "stats_url")
    @classmethod
    def valid_url(cls, value: str | None) -> str | None:
        return local_url(value)

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as error:
            raise ValueError("unknown timezone") from error
        return value

    @field_validator("start_argv")
    @classmethod
    def safe_argv(cls, value: list[str]) -> list[str]:
        for item in value:
            if not item or len(item) > 2000 or "\x00" in item or "\n" in item:
                raise ValueError("invalid process argument")
            if re.search(r"(?:api[-_]?key|password|secret|bearer|access[-_]?token)", item, re.I):
                raise ValueError("process arguments must not contain credentials")
            if re.search(r"://[^/\s]+@|\bsk-[A-Za-z0-9_-]{12,}", item):
                raise ValueError("process arguments must not contain credentials")
        if value and Path(value[0]).name in {"sh", "bash", "zsh", "fish", "cmd", "powershell"}:
            raise ValueError("register a direct executable instead of a shell")
        return value

    @model_validator(mode="after")
    def bounded_paths(self):
        for value in self.log_files + self.config_files + self.knowledge_paths:
            project_path(self.root, value)
        return self

    @property
    def digest(self) -> str:
        raw = self.model_dump(mode="json", exclude={"created_at"})
        return hashlib.sha256(json.dumps(raw, sort_keys=True).encode()).hexdigest()


class ProjectNotFound(ValueError):
    pass


class ProjectConflict(ValueError):
    pass


class ProjectStore:
    """Immutable numbered profiles; latest is derived, avoiding torn pointer updates."""

    def __init__(self, local_dir: str | Path):
        self.directory = Path(local_dir) / "project-profiles"
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory.chmod(0o700)

    def _directory(self, project_id: str) -> Path:
        if not PROJECT_ID.fullmatch(project_id):
            raise ValueError("invalid project ID")
        path = self.directory / project_id
        if path.is_symlink():
            raise ValueError("project storage must not be a symbolic link")
        return path

    def get(self, project_id: str, version: int | None = None) -> ProjectProfile:
        directory = self._directory(project_id)
        versions = sorted(int(p.stem) for p in directory.glob("*.json") if p.stem.isdigit())
        selected = version if version is not None else (versions[-1] if versions else None)
        if selected is None or selected < 1:
            raise ProjectNotFound("registered project not found")
        path = directory / f"{selected}.json"
        if not path.is_file() or path.is_symlink():
            raise ProjectNotFound("registered project version not found")
        profile = ProjectProfile.model_validate_json(path.read_text())
        if profile.id != project_id or profile.version != selected:
            raise ValueError("project profile identity is inconsistent")
        return profile

    def save(
        self, profile: ProjectProfile | dict, expected_version: int | None = None
    ) -> ProjectProfile:
        data = (
            profile.model_dump(mode="json")
            if isinstance(profile, ProjectProfile)
            else dict(profile)
        )
        checked = ProjectProfile.model_validate(data)
        if not checked.root.is_dir():
            raise ValueError("project root must be an existing directory when registered")
        directory = self._directory(checked.id)
        directory.mkdir(mode=0o700, exist_ok=True)
        directory.chmod(0o700)
        # Exclusive numbered creation is also the optimistic concurrency check across processes.
        try:
            current = self.get(checked.id).version
        except ProjectNotFound:
            current = 0
        if expected_version is not None and expected_version != current:
            raise ProjectConflict("project profile changed; reload before saving")
        checked = checked.model_copy(
            update={"version": current + 1, "created_at": datetime.now(UTC)}
        )
        fd, temporary = tempfile.mkstemp(prefix=".profile-", dir=directory)
        try:
            with os.fdopen(fd, "w") as handle:
                handle.write(checked.model_dump_json(indent=2))
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temporary, directory / f"{checked.version}.json")
            except FileExistsError as error:
                raise ProjectConflict("project profile changed; reload before saving") from error
        finally:
            Path(temporary).unlink(missing_ok=True)
        return checked

    @staticmethod
    def public(profile: ProjectProfile) -> dict:
        return profile.model_dump(mode="json") | {"config_digest": profile.digest}

    def list_public(self) -> list[dict]:
        result = []
        for path in sorted(self.directory.iterdir()):
            if path.is_dir() and not path.is_symlink() and PROJECT_ID.fullmatch(path.name):
                try:
                    result.append(self.public(self.get(path.name)))
                except ProjectNotFound:
                    continue
        return result
