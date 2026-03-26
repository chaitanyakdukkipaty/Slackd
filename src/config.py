"""
Configuration loader — reads config.yaml and exposes a single `cfg` dict.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional
import yaml

_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.yaml"


def load_config(path: Optional[str] = None) -> dict:
    target = Path(path) if path else _CONFIG_PATH
    with open(target, "r") as f:
        return yaml.safe_load(f)


def save_config(data: dict, path: Optional[str] = None) -> None:
    """Write *data* back to config.yaml (or a custom path)."""
    target = Path(path) if path else _CONFIG_PATH
    with open(target, "w") as f:
        yaml.dump(data, f, default_flow_style=False, allow_unicode=True)


# Module-level singleton loaded at import time.
cfg: dict = load_config()
