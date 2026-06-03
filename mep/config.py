"""Paths and configuration for mep.

Config lives at ~/.mep/config.json with two keys: YOUTUBE_API_KEY and
ANTHROPIC_API_KEY. Either may be overridden by an environment variable of the
same name, which is handy for one-off runs and CI.

Set MEP_HOME to relocate the whole ~/.mep directory (used by tests).
"""

import json
import os
import shutil
from pathlib import Path

from .errors import MepError

MEP_DIR = Path(os.environ.get("MEP_HOME", Path.home() / ".mep"))
CONFIG_PATH = MEP_DIR / "config.json"
DB_PATH = MEP_DIR / "mep.db"

# The pre-rename home, kept for a one-time copy into ~/.mep.
_LEGACY_DIR = Path.home() / ".mise"


def migrate_legacy_home() -> None:
    """One-time copy of a pre-rename ~/.mise into ~/.mep, renaming the database.
    Non-destructive (the old directory is left in place). Skipped when MEP_HOME
    is overridden (tests) or ~/.mep already exists."""
    if os.environ.get("MEP_HOME") or MEP_DIR.exists() or not _LEGACY_DIR.exists():
        return
    shutil.copytree(_LEGACY_DIR, MEP_DIR)
    legacy_db = MEP_DIR / "mise.db"
    if legacy_db.exists():
        legacy_db.rename(MEP_DIR / "mep.db")

# Overridable via config.json key of the same name.
EXTRACTION_MODEL = "claude-sonnet-4-20250514"

CONFIG_KEYS = ("YOUTUBE_API_KEY", "ANTHROPIC_API_KEY", "EXTRACTION_MODEL")


def load_config() -> dict:
    """Load config.json, layering environment variables on top."""
    data = {}
    if CONFIG_PATH.exists():
        data = json.loads(CONFIG_PATH.read_text())
    for key in CONFIG_KEYS:
        env = os.environ.get(key)
        if env:
            data[key] = env
    return data


def require(config: dict, key: str) -> str:
    """Return a required config value or raise a friendly MepError."""
    value = config.get(key)
    if not value:
        raise MepError(
            f"{key} is not set. Run `mep init` or set the {key} "
            f"environment variable."
        )
    return value


def model(config: dict) -> str:
    return config.get("EXTRACTION_MODEL") or EXTRACTION_MODEL
