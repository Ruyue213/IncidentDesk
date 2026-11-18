"""Shared backend contracts. Schema validity and evidence support are distinct checks."""

import re
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from incident_desk.cloud_policy import CloudPolicy
from incident_desk.knowledge.retrieval_config import RetrievalConfig


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RunInput(StrictModel):
    question: str = Field(min_length=5, max_length=4000)
    service_id: str = Field(default="checkout-api", pattern=r"^[a-zA-Z0-9_-]{1,80}$")
    mode: Literal["fixture", "live"] = "fixture"
    incident_id: str | None = Field(
        default=None, min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9_-]+$"
    )
    project_version: int | None = Field(default=None, ge=1)
    model_profile_version: int | None = Field(default=None, ge=1)
    parent_run_id: str | None = Field(default=None, min_length=1, max_length=100)
    knowledge_generation: str | None = Field(default=None, min_length=1, max_length=100)
    retrieval_mode: Literal["lexical", "dense", "hybrid"] | None = None
    retrieval_config: RetrievalConfig | None = None
    cloud_policy: CloudPolicy | None = None
    start: datetime
    end: datetime

    @field_validator("start", "end")
    @classmethod
    def timezone_required(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("time bounds must include a timezone")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def ordered(self):
        if self.start >= self.end:
            raise ValueError("start must precede end")
        if self.end - self.start > timedelta(hours=24):
            raise ValueError("diagnostic window cannot exceed 24 hours")
        if self.mode == "fixture" and (self.service_id != "checkout-api" or not self.incident_id):
            raise ValueError("fixture runs require checkout-api and an incident ID")
        return self


class Reference(StrictModel):
    id: str = Field(min_length=1, max_length=200)
    version: str = Field(min_length=1, max_length=100)
    quote: str = Field(min_length=1, max_length=300)

    @field_validator("id", "version", "quote")
    @classmethod
    def not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("reference fields must not be blank")
        return value


class TicketDraft(StrictModel):
    title: str = Field(min_length=3, max_length=120)
    description: str = Field(min_length=5, max_length=1000)
    priority: Literal["low", "medium", "high"] = "medium"


class TicketRequest(StrictModel):
    draft: TicketDraft
    references: list[Reference] = Field(min_length=1, max_length=10)


class ServiceActionRequest(StrictModel):
    """A bounded proposal; execution requires a frozen preview and explicit approval."""

    action: Literal["restart_service", "update_config"]
    reason: str = Field(min_length=5, max_length=1000)
    path: str | None = Field(default=None, max_length=500)
    expected_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    content: str | None = Field(default=None, max_length=16000)
    references: list[Reference] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def parameters(self):
        if self.action == "update_config":
            if not self.path or self.expected_sha256 is None or self.content is None:
                raise ValueError(
                    "config changes require path, expected SHA256 and full new content"
                )
        elif any(value is not None for value in (self.path, self.expected_sha256, self.content)):
            raise ValueError(
                "restart must use the registered command without model-supplied parameters"
            )
        return self


class Diagnosis(StrictModel):
    """Observed facts, tentative causes, opposing evidence, and next checks."""

    summary: str = Field(min_length=5, max_length=600)
    hypotheses: list[str] = Field(max_length=5)
    supporting_evidence: list[Reference] = Field(max_length=10)
    contradicting_evidence: list[Reference] = Field(max_length=10)
    missing_information: list[str] = Field(max_length=8)
    recommended_checks: list[str] = Field(min_length=1, max_length=8)
    needs_human: bool
    ticket_draft: TicketDraft | None = None


def snapshot(item: dict) -> dict:
    """Normalize tool results to a common, versioned reference target."""
    return {
        **{key: value for key, value in item.items() if key != "bm25"},
        "id": item.get("evidence_id") or item.get("chunk_id") or item.get("id"),
        "version": item.get("doc_version") or item.get("version"),
    }


def _compact(value: str) -> str:
    return re.sub(r"\s+", "", value)


def validate_references(references: list[Reference], snapshots: dict[str, dict]) -> None:
    """Reject invented IDs, changed versions and quotes absent from the obtained content.

    This does not prove that a quote supports a causal claim; that is evaluated separately.
    """
    for reference in references:
        source = snapshots.get(reference.id)
        if source is None:
            raise ValueError(f"Reference not obtained by this run: {reference.id}")
        if str(source["version"]) != reference.version:
            raise ValueError(f"Reference version mismatch: {reference.id}")
        content = source.get("content", "")
        if not isinstance(content, str):
            import json

            content = json.dumps(content, ensure_ascii=False, sort_keys=True)
        if _compact(reference.quote) not in _compact(content):
            raise ValueError(f"Quoted text absent from source: {reference.id}")


TopK = Annotated[int, Field(ge=1, le=10, strict=True)]
