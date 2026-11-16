"""Executable CP01 contract scenarios; all results come from graph execution."""

from typing import Any

from langchain_core.messages import ToolMessage
from langgraph.checkpoint.memory import InMemorySaver

from incident_desk.agent.harness import VIRTUAL_TOOLS, build_harness
from incident_desk.agent.offline import VALID_QUERY, make_offline_evidence_tool
from incident_desk.agent.scripted import ScriptedToolModel


def exercise_call(name: str, arguments: dict) -> dict[str, Any]:
    calls: list[dict] = []
    model = ScriptedToolModel(plans={"probe": [{"name": name, "args": arguments}]})
    graph = build_harness(model, [make_offline_evidence_tool(calls)])
    result = graph.invoke({"messages": [("human", "probe")]})
    messages = list(result["messages"])
    tool_messages = [m for m in messages if isinstance(m, ToolMessage)]
    assert len(tool_messages) == 1
    schemas = model.tool_schemas
    expected = VIRTUAL_TOOLS | {"get_service_evidence"}
    for request in schemas:
        assert {schema["function"]["name"] for schema in request} == expected
    assert len(model.transcript) == 2
    second_request_tools = [
        message for message in model.transcript[1]["messages"] if message["type"] == "tool"
    ]
    assert second_request_tools[0]["tool_call_id"] == tool_messages[0].tool_call_id
    return {
        "business_executions": calls,
        "tool_messages": [m.model_dump(mode="json") for m in tool_messages],
        "final_answer": str(messages[-1].content),
        "model_requests": model.transcript,
    }


def exercise_isolation() -> dict[str, Any]:
    marker = "RUN_A_ONLY_7e32"
    virtual_path = "/cp01-note.txt"
    model = ScriptedToolModel(
        plans={
            "write-note": [
                {"name": "write_file", "args": {"file_path": virtual_path, "content": marker}}
            ],
            "read-note": [{"name": "read_file", "args": {"file_path": virtual_path}}],
            "read-own": [{"name": "read_file", "args": {"file_path": virtual_path}}],
        }
    )
    graph = build_harness(model, [], checkpointer=InMemorySaver())
    thread_a = {"configurable": {"thread_id": "cp01-run-A"}}
    thread_b = {"configurable": {"thread_id": "cp01-run-B"}}
    written = graph.invoke({"messages": [("human", "write-note")]}, thread_a)
    isolated = graph.invoke({"messages": [("human", "read-note")]}, thread_b)
    read_back = graph.invoke({"messages": [("human", "read-own")]}, thread_a)
    assert virtual_path in written["files"]
    assert not isolated.get("files", {}).get(virtual_path)
    assert marker not in isolated["messages"][-1].content
    assert "not found" in isolated["messages"][-1].content.lower()
    assert marker in read_back["messages"][-1].content
    for request in model.tool_schemas:
        assert {schema["function"]["name"] for schema in request} == VIRTUAL_TOOLS
    return {
        "virtual_path": virtual_path,
        "thread_a_files": sorted(written["files"]),
        "thread_b_files": sorted(isolated.get("files", {})),
        "thread_b_answer": str(isolated["messages"][-1].content),
        "thread_a_answer": str(read_back["messages"][-1].content),
        "model_requests": model.transcript,
    }


def run_offline_contracts() -> dict[str, Any]:
    normal = exercise_call("get_service_evidence", VALID_QUERY)
    assert len(normal["business_executions"]) == 1
    assert "cp01-metrics-1" in normal["final_answer"]
    unknown = exercise_call("unknown_capability", {})
    hidden = exercise_call("execute", {"command": "false"})
    invalid = exercise_call("get_service_evidence", {**VALID_QUERY, "limit": 0})
    for negative in (unknown, hidden, invalid):
        assert negative["business_executions"] == []
        assert negative["tool_messages"][0]["status"] == "error"
    return {
        "normal": normal,
        "unknown_tool": unknown,
        "hidden_host_tool": hidden,
        "invalid_arguments": invalid,
        "run_isolation": exercise_isolation(),
        "model_api_calls": 0,
        "model_type": "offline deterministic script; not DeepSeek quality evaluation",
    }
