import socket

import pytest

from incident_desk.agent.harness import build_harness
from incident_desk.agent.offline import VALID_QUERY, make_offline_evidence_tool
from incident_desk.agent.offline_smoke import exercise_call, exercise_isolation
from incident_desk.agent.scripted import ScriptedToolModel


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("CP01 tests cannot access the network")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


def test_actual_evidence_reaches_next_request():
    report = exercise_call("get_service_evidence", VALID_QUERY)
    assert len(report["business_executions"]) == 1
    assert report["tool_messages"][0]["status"] == "success"
    assert "cp01-metrics-1" in report["final_answer"]
    assert report["model_requests"][1]["messages"][-1]["type"] == "tool"


@pytest.mark.parametrize("name", ["unknown_capability", "execute", "task", "delete", "grep"])
def test_unavailable_capability_cannot_execute(name):
    report = exercise_call(name, {})
    assert report["business_executions"] == []
    assert report["tool_messages"][0]["status"] == "error"


@pytest.mark.parametrize(
    "changes",
    [
        {"limit": 0},
        {"limit": 101},
        {"limit": "5"},
        {"source": "shell"},
        {"service_id": "other-service"},
        {"start": "2026-11-09T00:00:00"},
        {"start": "2026-11-09T02:00:00Z"},
        {"unexpected": "field"},
    ],
)
def test_invalid_arguments_do_not_reach_business_function(changes):
    report = exercise_call("get_service_evidence", {**VALID_QUERY, **changes})
    assert report["business_executions"] == []
    assert report["tool_messages"][0]["status"] == "error"


def test_two_threads_keep_virtual_files_separate():
    report = exercise_isolation()
    assert report["thread_b_files"] == []


def test_builder_refuses_unapproved_business_tool():
    tool = make_offline_evidence_tool([]).model_copy(update={"name": "unsafe_host_access"})
    with pytest.raises(ValueError, match="explicitly permitted"):
        build_harness(ScriptedToolModel(), [tool])
