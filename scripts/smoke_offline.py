"""Run the CP01 graph with network access forbidden; emit raw JSON evidence."""

import json
import socket
from datetime import UTC, datetime
from unittest.mock import patch

from incident_desk.agent.offline_smoke import run_offline_contracts


def main() -> None:
    with (
        patch.object(
            socket.socket, "connect", side_effect=AssertionError("Offline smoke forbids network")
        ),
        patch.object(
            socket, "create_connection", side_effect=AssertionError("Offline smoke forbids network")
        ),
    ):
        report = run_offline_contracts()
    report["checked_at"] = datetime.now(UTC).isoformat()
    report["network_guard"] = "socket connect forbidden during graph execution"
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
