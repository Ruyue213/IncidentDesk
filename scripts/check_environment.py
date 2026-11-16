"""Verify the installed baseline without constructing a model or making API calls."""

import asyncio
import importlib.metadata
import json
import platform
import sqlite3
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite
from deepagents import create_deep_agent
from langchain.agents import create_agent
from langchain_core.messages import AIMessage, ToolMessage
from langchain_deepseek import ChatDeepSeek
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import StateGraph
from pydantic import BaseModel

PACKAGES = (
    "incident-desk",
    "deepagents",
    "langchain-deepseek",
    "langchain",
    "langchain-core",
    "langgraph",
    "langgraph-checkpoint",
    "langgraph-checkpoint-sqlite",
    "aiosqlite",
    "pydantic",
    "ruff",
)


async def check_sqlite() -> dict:
    with tempfile.TemporaryDirectory(prefix="incident-desk-env-") as directory:
        async with aiosqlite.connect(Path(directory) / "probe.db") as connection:
            await connection.execute("CREATE VIRTUAL TABLE probe USING fts5(content)")
            await connection.execute("INSERT INTO probe VALUES (?)", ("checkout evidence",))
            async with connection.execute(
                "SELECT content FROM probe WHERE probe MATCH ?", ("evidence",)
            ) as cursor:
                rows = await cursor.fetchall()
            assert rows == [("checkout evidence",)], rows
    return {"version": sqlite3.sqlite_version, "fts5_insert_and_match": "passed"}


def main() -> None:
    assert sys.version_info[:2] == (3, 12), sys.version
    symbols = (
        create_deep_agent,
        create_agent,
        AIMessage,
        ToolMessage,
        ChatDeepSeek,
        AsyncSqliteSaver,
        StateGraph,
        BaseModel,
    )
    report = {
        "checked_at": datetime.now(UTC).isoformat(),
        "python": platform.python_version(),
        "executable": sys.executable,
        "platform": platform.platform(),
        "architecture": platform.machine(),
        "versions": {name: importlib.metadata.version(name) for name in PACKAGES},
        "imports": {symbol.__name__: "passed" for symbol in symbols},
        "sqlite": asyncio.run(check_sqlite()),
        "model_api_calls": 0,
        "scope": "environment/import checks only; no model contract or graph runtime tested",
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
