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

# Default extraction model per provider; EXTRACTION_MODEL in config overrides.
DEFAULT_MODELS = {
    "anthropic": "claude-sonnet-4-20250514",
    "openai": "gpt-4o",
}
# Backwards-compatible alias for the historical default.
EXTRACTION_MODEL = DEFAULT_MODELS["anthropic"]

_API_KEY_FOR = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}

CONFIG_KEYS = (
    "YOUTUBE_API_KEY",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "LLM_PROVIDER",
    "EXTRACTION_MODEL",
)


def load_config() -> dict:
    """Load config.json, layering environment variables on top."""
    data = {}
    if CONFIG_PATH.exists():
        try:
            data = json.loads(CONFIG_PATH.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            raise MepError(
                f"Could not read config at {CONFIG_PATH}: {exc}. "
                "Fix the file, or run `mep init` to recreate it."
            )
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


def provider(config: dict) -> str:
    """Which LLM backend to use. An explicit LLM_PROVIDER wins; otherwise infer
    from whichever single API key is set. If both or neither are set, default to
    'anthropic' (set LLM_PROVIDER to disambiguate)."""
    name = (config.get("LLM_PROVIDER") or "").strip().lower()
    if name:
        if name not in _API_KEY_FOR:
            raise MepError(f"Unknown LLM_PROVIDER '{name}'. Use 'anthropic' or 'openai'.")
        return name
    if config.get("OPENAI_API_KEY") and not config.get("ANTHROPIC_API_KEY"):
        return "openai"
    return "anthropic"


def require_api_key(config: dict) -> str:
    """The API key for the configured provider."""
    return require(config, _API_KEY_FOR[provider(config)])


def model(config: dict) -> str:
    return config.get("EXTRACTION_MODEL") or DEFAULT_MODELS[provider(config)]
