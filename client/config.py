"""Client configuration — reads ~/.opti/config.yaml for server URL and API key."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

import yaml


_CONFIG_DIR = Path.home() / ".opti"
_CONFIG_FILE = _CONFIG_DIR / "config.yaml"

_DEFAULT_CONFIG = {
    "server_url": "http://localhost:8000",
    "api_key": "changeme",
}


def _load_config() -> dict:
    """Load config from ~/.opti/config.yaml, creating defaults if missing."""
    if not _CONFIG_FILE.exists():
        _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        _CONFIG_FILE.write_text(yaml.dump(_DEFAULT_CONFIG, default_flow_style=False))
    raw = yaml.safe_load(_CONFIG_FILE.read_text()) or {}
    return {**_DEFAULT_CONFIG, **raw}


_cfg = _load_config()

SERVER_URL: str = os.getenv("OPTI_SERVER_URL", _cfg.get("server_url", "http://localhost:8000"))
API_KEY: str = os.getenv("OPTI_API_KEY", _cfg.get("api_key", "changeme"))


def get_headers() -> dict:
    return {"X-API-Key": API_KEY}
