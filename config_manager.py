"""Centralized persistent application configuration for sovraan."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional


def _application_root() -> Path:
    """Directory for config.json (script dir or frozen executable dir)."""
    if getattr(sys, "frozen", False):
        return Path(os.path.dirname(sys.executable))
    return Path(os.path.dirname(os.path.abspath(__file__)))


DEFAULT_SETTINGS: Dict[str, Any] = {
    "copy_method": "copy2",
    "collision_policy": "skip",
    "vault_path": "",
    "last_source": "",
    "last_dest": "",
    "scan_mode": "Exact Match (Fast)",
    "threshold": 0,
    "threads": 4,
    "ignore_exts": "",
    "ignore_folders": "",
    "theme": "light",
    "merge_master": "",
    "merge_incoming": "",
    "journey_export_mode": "Standard Mode",
    "simulate_only": False,
    "proLicenseKey": "",
}


class AppConfig:
    """Load, update, and persist application settings in ``config.json``."""

    def __init__(self, filename: str = "config.json") -> None:
        self._base_path = _application_root()
        self._config_path = self._base_path / filename
        self._defaults = DEFAULT_SETTINGS.copy()
        self._data: Dict[str, Any] = self._load()
        self._import_legacy_settings()

    def _load(self) -> Dict[str, Any]:
        data = self._defaults.copy()
        if not self._config_path.is_file():
            return data
        try:
            with open(self._config_path, "r", encoding="utf-8") as handle:
                loaded = json.load(handle)
            if isinstance(loaded, dict):
                data.update(loaded)
        except (OSError, json.JSONDecodeError, TypeError):
            pass
        return data

    def _save(self) -> None:
        try:
            with open(self._config_path, "w", encoding="utf-8") as handle:
                json.dump(self._data, handle, indent=4)
        except OSError:
            pass

    def _import_legacy_settings(self) -> None:
        """One-time merge from legacy ``settings.json`` if present."""
        legacy_path = self._base_path / "settings.json"
        if not legacy_path.is_file():
            return
        try:
            with open(legacy_path, "r", encoding="utf-8") as handle:
                legacy = json.load(handle)
        except (OSError, json.JSONDecodeError, TypeError):
            return
        if not isinstance(legacy, dict):
            return
        changed = False
        for key, value in legacy.items():
            if key not in self._data or self._data[key] == self._defaults.get(key):
                self._data[key] = value
                changed = True
        if legacy.get("last_dest") and not self._data.get("vault_path"):
            self._data["vault_path"] = legacy["last_dest"]
            changed = True
        if changed:
            self._save()

    def get(self, key: str, default: Any = None) -> Any:
        if key in self._data:
            return self._data[key]
        if key in self._defaults:
            return self._defaults[key]
        return default

    def set(self, key: str, value: Any) -> None:
        self._data[key] = value
        if key == "vault_path":
            self._data["last_dest"] = value
        elif key == "last_dest":
            self._data["vault_path"] = value
        self._save()

    def update(self, values: Dict[str, Any]) -> None:
        for key, value in values.items():
            self._data[key] = value
        if "vault_path" in values:
            self._data["last_dest"] = values["vault_path"]
        if "last_dest" in values and "vault_path" not in values:
            self._data["vault_path"] = values["last_dest"]
        self._save()

    def reset_to_defaults(self) -> None:
        self._data = self._defaults.copy()
        self._save()

    def defaults(self) -> Dict[str, Any]:
        return self._defaults.copy()
