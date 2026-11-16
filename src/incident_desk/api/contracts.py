"""Public response schemas exposed in OpenAPI and mirrored by frontend types."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from incident_desk.schemas import Diagnosis, RunInput, TicketRequest


class View(BaseModel):
    model_config = ConfigDict(extra="allow")


class UsageView(View):
    model_calls: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    known_cost_cny: float
    unreconciled_reservations_cny: float
    unknown_usage_calls: int = 0


class OperationView(View):
    id: str
    operation_id: str
    run_id: str
    interrupt_id: str
    draft_version: int
    params_digest: str
    params: TicketRequest | dict[str, Any]
    kind: str = "create_ticket"
    decision: str | None
    status: str


class RunView(View):
    id: str
    status: Literal["QUEUED", "RUNNING", "WAITING_APPROVAL", "SUCCEEDED", "FAILED", "CANCELLED"]
    request: RunInput
    created_at: str
    updated_at: str
    cancel_requested: bool
    active_seconds: float
    active_started_at: str | None
    tool_calls: int
    revision: int
    diagnosis: Diagnosis | None
    error: dict[str, Any] | None
    operation: OperationView | None
    ticket_id: str | None
    usage: UsageView
    report_repairs: int = 0
    action_receipt: dict[str, Any] | None = None


class RunPage(View):
    items: list[RunView]
    limit: int
    offset: int


class EventView(View):
    event_id: int
    id: int
    run_id: str
    kind: str
    payload: dict[str, Any]
    created_at: str


class EventPage(View):
    items: list[EventView]
    next_cursor: int


class ResumeView(View):
    accepted: bool
    operation: OperationView
