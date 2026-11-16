"""Read-only fixture adapter with trusted incident selection and bounded queries."""

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

EvidenceSource = Literal["metrics", "logs", "changes"]
INCIDENT_ID = re.compile(r"(?:dev|acc)-[0-9]{3}\Z")


def aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must include a timezone")
    return value.astimezone(UTC)


class EvidenceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    service_id: Literal["checkout-api"]
    start: datetime
    end: datetime
    source: EvidenceSource
    limit: Annotated[int, Field(ge=1, le=100, strict=True)] = 20

    @field_validator("start", "end")
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        return aware_utc(value)

    @model_validator(mode="after")
    def validate_window(self):
        if self.start >= self.end:
            raise ValueError("start must precede end")
        if self.end - self.start > timedelta(hours=24):
            raise ValueError("evidence window cannot exceed 24 hours")
        return self


class FixtureWindow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: datetime
    end: datetime

    @field_validator("start", "end")
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        return aware_utc(value)

    @model_validator(mode="after")
    def ordered_window(self):
        if self.start >= self.end:
            raise ValueError("fixture window must be ordered")
        return self


class EvidenceItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evidence_id: str = Field(min_length=1, max_length=120)
    source: EvidenceSource
    observed_at: datetime
    version: str = Field(min_length=1, max_length=80)
    content: str = Field(min_length=1, max_length=12000)

    @field_validator("observed_at")
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        return aware_utc(value)


class IncidentFixture(BaseModel):
    model_config = ConfigDict(extra="forbid")

    incident_id: str
    service_id: Literal["checkout-api"]
    version: str = Field(min_length=1, max_length=80)
    window: FixtureWindow
    evidence: list[EvidenceItem] = Field(max_length=1000)

    @model_validator(mode="after")
    def validate_identity(self):
        if not INCIDENT_ID.fullmatch(self.incident_id):
            raise ValueError("invalid fixture incident identity")
        identifiers = [item.evidence_id for item in self.evidence]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("duplicate evidence ID in fixture")
        for item in self.evidence:
            if not item.evidence_id.startswith(self.incident_id + "-"):
                raise ValueError("evidence ID must be scoped to its incident")
            if not self.window.start <= item.observed_at < self.window.end:
                raise ValueError("observation falls outside fixture window")
        return self


class EvidenceStore:
    """Only reads neutral-named fixture JSON, never expected answers or arbitrary paths.

    The worker selects incident_id from trusted run context; models only choose
    source and bounded query parameters. A missing source returns an empty list.
    Time windows are [start, end), at most 24 hours; output timestamps use UTC.
    """

    def __init__(self, data_directory: str | Path):
        self.root = Path(data_directory).resolve()
        if not self.root.is_dir():
            raise ValueError("fixture directory does not exist")
        self._paths: dict[str, Path] = {}
        # Discover names without opening acceptance fixtures. Development runs
        # never load acceptance inputs/answers merely by constructing the store.
        for split in ("dev", "acceptance"):
            for path in sorted((self.root / split).glob("*.json")):
                if not path.resolve().is_relative_to(self.root):
                    raise ValueError("fixture symlinks cannot escape the fixture directory")
                if not INCIDENT_ID.fullmatch(path.stem):
                    raise ValueError("fixture filename must be a neutral incident ID")
                prefix = "dev-" if split == "dev" else "acc-"
                if not path.stem.startswith(prefix):
                    raise ValueError("incident ID does not match its dataset split")
                if path.stem in self._paths:
                    raise ValueError("incident cannot occur in multiple splits")
                self._paths[path.stem] = path

    def load(self, incident_id: str) -> IncidentFixture:
        if not isinstance(incident_id, str) or not INCIDENT_ID.fullmatch(incident_id):
            raise ValueError("invalid incident ID")
        path = self._paths.get(incident_id)
        if path is None:
            raise KeyError(f"Unknown incident: {incident_id}")
        if not path.resolve().is_relative_to(self.root):
            raise ValueError("fixture path escaped its trusted root")
        fixture = IncidentFixture.model_validate(json.loads(path.read_text(encoding="utf-8")))
        if fixture.incident_id != incident_id:
            raise ValueError("fixture content does not match requested incident")
        return fixture

    def get(
        self,
        incident_id: str,
        service_id: str,
        start: datetime | str,
        end: datetime | str,
        source: str,
        limit: int = 20,
    ) -> list[dict]:
        query = EvidenceRequest(
            service_id=service_id,
            start=start,
            end=end,
            source=source,
            limit=limit,
        )
        fixture = self.load(incident_id)
        if fixture.service_id != query.service_id:
            raise ValueError("incident does not belong to the requested service")
        rows = [
            item
            for item in fixture.evidence
            if (item.source == query.source and query.start <= item.observed_at < query.end)
        ]
        rows.sort(key=lambda item: (item.observed_at, item.evidence_id))
        return [item.model_dump(mode="json") for item in rows[: query.limit]]
