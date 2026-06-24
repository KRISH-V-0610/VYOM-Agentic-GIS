"""
Shared configuration: project paths, .env loading, registry loaders.

Every module resolves paths and DB connection through here so the codebase has a
single source of truth for where things live.
"""

from pathlib import Path

import yaml
from dotenv import dotenv_values

PROJECT_ROOT = Path(__file__).resolve().parents[2]

CONFIG_DIR = PROJECT_ROOT / "config"
DATA_RAW = PROJECT_ROOT / "data" / "raw"
DATA_ARD = PROJECT_ROOT / "data" / "ard"
DATA_MANIFESTS = PROJECT_ROOT / "data" / "manifests"

BAND_REGISTRY_PATH = CONFIG_DIR / "band_registry.yaml"
EVENTS_REGISTRY_PATH = CONFIG_DIR / "events_registry.yaml"

# Bumped whenever preprocessing changes — stale cached metrics are auto-invalidated.
PIPELINE_VERSION = "v1.0-dos"

_env = dotenv_values(PROJECT_ROOT / ".env")


def get_db_url() -> str:
    url = _env.get("DB_URL")
    if not url or "<" in url:
        raise RuntimeError(
            "DB_URL not set or still a template in .env — fill in user/password."
        )
    return url


def get_env(key: str, default=None):
    return _env.get(key, default)


def load_band_registry() -> dict:
    with open(BAND_REGISTRY_PATH) as f:
        return yaml.safe_load(f)["sensors"]


def load_events_registry() -> dict:
    """Return {event_key: event_dict}."""
    with open(EVENTS_REGISTRY_PATH) as f:
        events = yaml.safe_load(f)["events"]
    return {e["key"]: e for e in events}
