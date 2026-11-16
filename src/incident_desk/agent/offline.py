"""CP01 fixture tool: deliberately simulated, independent of CP03's evidence corpus."""

from datetime import datetime
from typing import Annotated, Literal

from langchain_core.tools import BaseTool, tool
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class EvidenceQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    service_id: Literal["checkout-api"]
    source: Literal["metrics", "logs", "changes"]
    start: datetime
    end: datetime
    limit: Annotated[int, Field(ge=1, le=100, strict=True)] = 10

    @field_validator("start", "end")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("time bounds must include a timezone")
        return value

    @model_validator(mode="after")
    def ordered_window(self):
        if self.start >= self.end:
            raise ValueError("start must precede end")
        return self


def make_offline_evidence_tool(calls: list[dict]) -> BaseTool:
    """Make a closure-bound simulated source and expose the execution audit to tests."""

    @tool(args_schema=EvidenceQuery)
    def get_service_evidence(
        service_id: str,
        source: str,
        start: datetime,
        end: datetime,
        limit: int = 10,
    ) -> dict:
        """Read bounded simulated checkout-api evidence for one source and time window."""
        calls.append(
            {
                "service_id": service_id,
                "source": source,
                "start": start.isoformat(),
                "end": end.isoformat(),
                "limit": limit,
            }
        )
        timestamp = datetime.fromisoformat("2026-11-09T00:30:00+00:00")
        return {
            "simulation": True,
            "incident_id": "cp01-fixture",
            "evidence": [
                {
                    "evidence_id": f"cp01-{source}-1",
                    "source": source,
                    "observed_at": timestamp.isoformat(),
                    "version": "cp01-v1",
                    "content": "Simulated checkout error rate is 12%"
                    if source == "metrics"
                    else "Simulated observation; not production evidence",
                }
            ][:limit]
            if start <= timestamp < end
            else [],
        }

    return get_service_evidence


VALID_QUERY = {
    "service_id": "checkout-api",
    "source": "metrics",
    "start": "2026-11-09T00:00:00Z",
    "end": "2026-11-09T01:00:00Z",
    "limit": 5,
}
