"""Paths and configuration for mise.

Config lives at ~/.mise/config.json with two keys: YOUTUBE_API_KEY and
ANTHROPIC_API_KEY. Either may be overridden by an environment variable of the
same name, which is handy for one-off runs and CI.

Set MISE_HOME to relocate the whole ~/.mise directory (used by tests).
"""

import json
import os
from pathlib import Path

from .errors import MiseError

MISE_DIR = Path(os.environ.get("MISE_HOME", Path.home() / ".mise"))
CONFIG_PATH = MISE_DIR / "config.json"
DB_PATH = MISE_DIR / "mise.db"

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
    if not CONFIG_PATH.exists() and not data:
        raise MiseError("No config found. Run `mise init` first.")
    return data


def require(config: dict, key: str) -> str:
    """Return a required config value or raise a friendly MiseError."""
    value = config.get(key)
    if not value:
        raise MiseError(
            f"{key} is not set. Run `mise init` or set the {key} "
            f"environment variable."
        )
    return value


def model(config: dict) -> str:
    return config.get("EXTRACTION_MODEL") or EXTRACTION_MODEL
