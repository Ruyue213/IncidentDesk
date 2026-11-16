"""Separate immutable installed resources from private, user-owned runtime state."""

import os
import sys
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent
_candidate = PACKAGE_ROOT.parents[1]
SOURCE_ROOT = (
    _candidate
    if (_candidate / "pyproject.toml").is_file()
    and (_candidate / "src/incident_desk").resolve() == PACKAGE_ROOT
    else None
)
# Kept for development scripts/tests. Runtime state must use default_home(), not ROOT.
ROOT = SOURCE_ROOT or PACKAGE_ROOT
_packaged = PACKAGE_ROOT / "resources"
RESOURCE_ROOT = _packaged if _packaged.is_dir() else ROOT


def default_home() -> Path:
    override = os.environ.get("INCIDENT_DESK_HOME", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    if SOURCE_ROOT:
        return SOURCE_ROOT / ".local"
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/IncidentDesk"
    base = Path(os.environ.get("XDG_STATE_HOME", "~/.local/state")).expanduser()
    return (base / "incidentdesk").resolve()


def default_env_file() -> Path:
    # Never discover credentials from whichever directory the command happened to use.
    return (SOURCE_ROOT if SOURCE_ROOT else default_home()) / ".env"
