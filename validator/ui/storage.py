"""Local per-user persistent storage for saved connections, presets, and isolated sessions."""

import json
import os
from pathlib import Path
from typing import Dict, List

from validator.config.manifest import ConnectionConfig, TableConfig


def get_user_data_dir() -> Path:
    """Return the per-user local data directory (~/.data-validator), isolated from shared codebases."""
    base = os.environ.get("DATA_VALIDATOR_HOME")
    data_dir = Path(base) if base else (Path.home() / ".data-validator")
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        return data_dir
    except PermissionError:
        fallback = Path(".validator_state") / "user_profile"
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


def get_session_reports_dir(session_id: str) -> Path:
    """Return a session-isolated reports directory under the user's home directory."""
    rep_dir = get_user_data_dir() / "sessions" / session_id / "reports"
    rep_dir.mkdir(parents=True, exist_ok=True)
    return rep_dir


def _connections_file() -> Path:
    return get_user_data_dir() / "connections.json"


def _presets_file() -> Path:
    return get_user_data_dir() / "presets.json"


def load_saved_connections() -> Dict[str, ConnectionConfig]:
    """Load saved database connections from the user's local profile directory."""
    path = _connections_file()
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return {name: ConnectionConfig.model_validate(cfg) for name, cfg in raw.items()}
    except Exception:
        return {}


def save_connections_to_disk(connections: Dict[str, ConnectionConfig]) -> None:
    """Persist database connections to the user's local profile directory."""
    path = _connections_file()
    serialized = {
        name: cfg.model_dump(exclude_none=True)
        for name, cfg in connections.items()
    }
    path.write_text(json.dumps(serialized, indent=2), encoding="utf-8")


def load_saved_presets() -> Dict[str, List[TableConfig]]:
    """Load saved multi-table batch presets."""
    path = _presets_file()
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return {
            name: [TableConfig.model_validate(t) for t in tbls]
            for name, tbls in raw.items()
        }
    except Exception:
        return {}


def save_preset_to_disk(preset_name: str, tables: List[TableConfig]) -> None:
    """Save a named multi-table batch preset locally."""
    presets = load_saved_presets()
    presets[preset_name] = tables
    path = _presets_file()
    serialized = {
        name: [t.model_dump(exclude_none=True) for t in tbls]
        for name, tbls in presets.items()
    }
    path.write_text(json.dumps(serialized, indent=2), encoding="utf-8")


def delete_preset_from_disk(preset_name: str) -> None:
    """Delete a saved preset locally."""
    presets = load_saved_presets()
    if preset_name in presets:
        del presets[preset_name]
        path = _presets_file()
        serialized = {
            name: [t.model_dump(exclude_none=True) for t in tbls]
            for name, tbls in presets.items()
        }
        path.write_text(json.dumps(serialized, indent=2), encoding="utf-8")
