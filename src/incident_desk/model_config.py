"""Versioned, local-only model configuration; credentials never enter public DTOs.

The manifest and credential files are private to the OS user. Revisions are
immutable: changing the active profile never changes a queued or resumed run.
This is file-permission based local storage, not encryption at rest.
"""

import fcntl
import json
import os
import tempfile
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from incident_desk.settings import Settings


class ModelConfigConflict(ValueError):
    """A settings editor tried to overwrite a newer active revision."""


class Pricing(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    input_per_million_cny: float = Field(default=2, gt=0, le=10000)
    output_per_million_cny: float = Field(default=8, gt=0, le=10000)
    cache_read_per_million_cny: float = Field(default=0.04, ge=0, le=10000)

    @model_validator(mode="after")
    def cache_not_more_than_uncached(self):
        if self.cache_read_per_million_cny > self.input_per_million_cny:
            raise ValueError("Cache input price must not exceed uncached input price")
        return self


def normalize_endpoint(value: str) -> str:
    parts = urlsplit(value.strip())
    try:
        port = parts.port
    except ValueError as error:
        raise ValueError("Invalid endpoint port") from error
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
        or "\\" in value
        or any(character.isspace() for character in value)
    ):
        raise ValueError("Endpoint must be an HTTPS URL without credentials, query or fragment")
    host = parts.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    netloc = host if port in (None, 443) else f"{host}:{port}"
    return urlunsplit(("https", netloc, parts.path.rstrip("/"), "", ""))


def endpoint_origin(endpoint: str) -> tuple[str, str, int]:
    parsed = urlsplit(endpoint)
    return parsed.scheme, parsed.hostname or "", parsed.port or 443


def official_endpoint(endpoint: str) -> bool:
    parsed = urlsplit(endpoint)
    return endpoint_origin(endpoint) == ("https", "api.deepseek.com", 443) and parsed.path in (
        "",
        "/v1",
        "/beta",
    )


class ModelProfileInput(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, hide_input_in_errors=True)
    expected_version: int | None = Field(default=None, ge=1)
    provider: Literal["deepseek"] = "deepseek"
    base_url: str = Field(default="https://api.deepseek.com", min_length=8, max_length=500)
    model: str = Field(default="deepseek-flash", min_length=1, max_length=150)
    reasoning: Literal["off", "low", "high", "max"] = "off"
    max_output_tokens: int = Field(default=4096, ge=128, le=16384)
    request_timeout_seconds: float = Field(default=90, ge=1, le=180)
    active_seconds: float = Field(default=600, ge=10, le=1800)
    model_call_limit: int = Field(default=16, ge=2, le=64)
    tool_call_limit: int = Field(default=40, ge=1, le=128)
    run_budget_cny: float = Field(default=5, gt=0, le=50)
    project_budget_cny: float = Field(default=300, gt=0, le=300)
    max_request_bytes: int = Field(default=262144, ge=16384, le=262144)
    pricing: Pricing = Field(default_factory=Pricing)
    api_key: SecretStr | None = Field(default=None, max_length=2048)
    clear_credential: bool = False

    @field_validator("base_url")
    @classmethod
    def endpoint_is_https(cls, value: str) -> str:
        return normalize_endpoint(value)

    @field_validator("model")
    @classmethod
    def model_is_identifier(cls, value: str) -> str:
        value = value.strip()
        if not value or any(character.isspace() for character in value):
            raise ValueError("Model must be a nonempty identifier without whitespace")
        return value

    @field_validator("api_key")
    @classmethod
    def key_is_single_line(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None:
            return None
        key = value.get_secret_value().strip()
        if not key or any(character.isspace() for character in key):
            raise ValueError("API key must be nonempty and contain no whitespace")
        return SecretStr(key)

    @model_validator(mode="after")
    def valid_combination(self):
        if self.clear_credential and self.api_key is not None:
            raise ValueError("Choose either a new API key or clearing the credential")
        if self.reasoning != "off" and (
            not official_endpoint(self.base_url) or self.model != "deepseek-flash"
        ):
            raise ValueError("Reasoning options are verified only for official deepseek-flash")
        if self.run_budget_cny > self.project_budget_cny:
            raise ValueError("Run budget must not exceed the project budget")
        if self.model != "deepseek-flash" and "pricing" not in self.model_fields_set:
            raise ValueError("Experimental models require explicitly supplied model pricing")
        return self


class ModelConfigStore:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.directory = settings.local_dir / "model-config"
        self.manifest = self.directory / "profiles.json"

    @contextmanager
    def _locked(self):
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.directory.is_symlink():
            raise ValueError("Model configuration directory must not be a symlink")
        self.directory.chmod(0o700)
        descriptor = os.open(
            self.directory / ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
        )
        try:
            os.fchmod(descriptor, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _atomic_write(self, destination: Path, content: bytes):
        descriptor, temporary = tempfile.mkstemp(prefix=".pending-", dir=self.directory)
        try:
            with os.fdopen(descriptor, "wb") as output:
                os.fchmod(output.fileno(), 0o600)
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, destination)
            directory_fd = os.open(self.directory, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _new_credential(self, key: SecretStr) -> str:
        reference = uuid.uuid4().hex
        self._atomic_write(
            self.directory / f"credential-{reference}", key.get_secret_value().encode()
        )
        return reference

    def _credential(self, reference: str | None) -> SecretStr | None:
        if reference is None:
            return None
        if len(reference) != 32 or any(
            character not in "0123456789abcdef" for character in reference
        ):
            raise ValueError("Invalid stored credential reference")
        path = self.directory / f"credential-{reference}"
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "r") as source:
            os.fchmod(source.fileno(), 0o600)
            return SecretStr(source.read())

    def _write_manifest(self, data: dict):
        self._atomic_write(self.manifest, json.dumps(data, ensure_ascii=False).encode())

    def _load(self) -> dict:
        if self.manifest.exists():
            if self.manifest.is_symlink():
                raise ValueError("Model configuration manifest must not be a symlink")
            self.manifest.chmod(0o600)
            return json.loads(self.manifest.read_text())
        # Import the server's existing credential once; never rewrite its .env.
        initial = ModelProfileInput(
            base_url=self.settings.base_url,
            model=self.settings.model,
            reasoning=self.settings.reasoning,
            project_budget_cny=self.settings.project_budget_cny,
            pricing=Pricing(
                input_per_million_cny=self.settings.input_per_million_cny,
                output_per_million_cny=self.settings.output_per_million_cny,
                cache_read_per_million_cny=self.settings.cache_read_per_million_cny,
            ),
        )
        key = self.settings.api_key
        reference = self._new_credential(key) if key and key.get_secret_value().strip() else None
        profile = self._revision(initial, 1, reference)
        data = {"active_version": 1, "revisions": {"1": profile}}
        self._write_manifest(data)
        return data

    @staticmethod
    def _revision(profile: ModelProfileInput, version: int, reference: str | None) -> dict:
        return {
            **profile.model_dump(
                mode="json", exclude={"api_key", "clear_credential", "expected_version"}
            ),
            "version": version,
            "credential_ref": reference,
            "created_at": datetime.now(UTC).isoformat(),
        }

    @staticmethod
    def _public(data: dict) -> dict:
        revision = data["revisions"][str(data["active_version"])]
        profile = {key: value for key, value in revision.items() if key != "credential_ref"}
        experimental = (
            not official_endpoint(profile["base_url"]) or profile["model"] != "deepseek-flash"
        )
        profile.update(has_credential=bool(revision["credential_ref"]), experimental=experimental)
        return {
            "active_version": data["active_version"],
            "profile": profile,
            "capabilities": {
                "providers": ["deepseek"],
                "reasoning": ["off"] if experimental else ["off", "low", "high", "max"],
                "tool_calling": "requires_connection_test",
                "fixed_model_revision": False,
                "credential_storage": "local_private_file",
            },
        }

    def public(self) -> dict:
        with self._locked():
            return self._public(self._load())

    def active_version(self) -> int:
        with self._locked():
            return self._load()["active_version"]

    def freeze(self) -> int:
        return self.active_version()

    def save(self, profile: ModelProfileInput | dict) -> dict:
        profile = ModelProfileInput.model_validate(profile)
        with self._locked():
            data = self._load()
            active = data["active_version"]
            if profile.expected_version is not None and profile.expected_version != active:
                raise ModelConfigConflict("Model settings changed; reload before saving")
            previous = data["revisions"][str(active)]
            if profile.api_key is not None:
                reference = self._new_credential(profile.api_key)
            elif profile.clear_credential:
                reference = None
            elif endpoint_origin(profile.base_url) != endpoint_origin(previous["base_url"]):
                if previous["credential_ref"]:
                    raise ValueError(
                        "Changing endpoint host requires a new API key or clear_credential"
                    )
                reference = None
            else:
                reference = previous["credential_ref"]
            version = active + 1
            data["revisions"][str(version)] = self._revision(profile, version, reference)
            data["active_version"] = version
            self._write_manifest(data)
            return self._public(data)

    def settings_for_version(self, version: int) -> Settings:
        with self._locked():
            data = self._load()
            try:
                profile = data["revisions"][str(version)]
            except KeyError as error:
                raise ValueError("Unknown model profile version") from error
            key = self._credential(profile["credential_ref"])
            fields = {
                name: profile[name]
                for name in (
                    "base_url",
                    "model",
                    "reasoning",
                    "max_output_tokens",
                    "request_timeout_seconds",
                    "active_seconds",
                    "model_call_limit",
                    "tool_call_limit",
                    "run_budget_cny",
                    "project_budget_cny",
                    "max_request_bytes",
                )
            }
            return self.settings.model_copy(
                update={
                    **fields,
                    **profile["pricing"],
                    "api_key": key,
                    "model_profile_version": version,
                }
            )
