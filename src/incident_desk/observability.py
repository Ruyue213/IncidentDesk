"""Structured lifecycle logs with explicit fields, never model payloads or credentials."""

import json
import logging
from datetime import UTC, datetime


class JSONFormatter(logging.Formatter):
    def format(self, record):
        return json.dumps(
            {
                "timestamp": datetime.now(UTC).isoformat(),
                "level": record.levelname,
                "event": record.getMessage(),
                **{
                    name: getattr(record, name)
                    for name in ("run_id", "status", "error_type", "stage")
                    if hasattr(record, name)
                },
            },
            ensure_ascii=False,
        )


def configure_logging():
    logger = logging.getLogger("incident_desk")
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(JSONFormatter())
        logger.addHandler(handler)
        logger.propagate = False
        logger.setLevel(logging.INFO)


logger = logging.getLogger("incident_desk")
