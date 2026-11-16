"""Single backend configuration source; secrets are never serialized to reports."""

import os
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from incident_desk.runtime_paths import RESOURCE_ROOT, ROOT, default_env_file, default_home

__all__ = ["RESOURCE_ROOT", "ROOT", "Settings", "load_settings"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=default_env_file(), env_ignore_empty=True, extra="ignore", populate_by_name=True
    )
    api_key: SecretStr | None = Field(default=None, validation_alias="DEEPSEEK_API_KEY")
    model: str = Field(default="deepseek-flash", validation_alias="INCIDENT_DESK_MODEL")
    base_url: str = Field(
        default="https://api.deepseek.com", validation_alias="INCIDENT_DESK_BASE_URL"
    )
    local_dir: Path = Field(default_factory=default_home, validation_alias="INCIDENT_DESK_HOME")
    # Legacy defaults keep the offline fixtures reproducible. New model profiles
    # have their own, larger defaults and are frozen for each submitted run.
    max_output_tokens: int = Field(default=1500, ge=1, le=16384)
    request_timeout_seconds: float = Field(default=60, gt=0, le=180)
    active_seconds: float = Field(default=180, gt=0, le=1800)
    model_call_limit: int = Field(default=8, ge=1, le=64)
    tool_call_limit: int = Field(default=12, ge=1, le=128)
    run_budget_cny: float = Field(default=2, gt=0, le=50)
    project_budget_cny: float = Field(default=300, gt=0, le=300)
    smoke_budget_cny: float = Field(default=5, gt=0, le=5)
    reasoning: Literal["off", "low", "high", "max"] = "off"
    model_profile_version: int | None = None
    input_per_million_cny: float = Field(default=2, gt=0, le=10000)
    output_per_million_cny: float = Field(default=8, gt=0, le=10000)
    cache_read_per_million_cny: float = Field(default=0.04, ge=0, le=10000)
    max_request_bytes: int = Field(default=64000, ge=1024, le=262144)

    @property
    def app_db(self) -> Path:
        return self.local_dir / "app.db"

    @property
    def graph_db(self) -> Path:
        return self.local_dir / "graph.db"


def load_settings() -> Settings:
    # Disable both old and new tracing flags, including inherited environment settings.
    os.environ["LANGSMITH_TRACING"] = "false"
    os.environ["LANGCHAIN_TRACING_V2"] = "false"
    return Settings()
