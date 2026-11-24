# Telemetry and anonymous usage tracking have been removed.
# This module provides backward-compatible no-op stubs.

from typing import Any, Dict, Optional

g_tracking_enabled = False
entrypoint_name = "Python API"
dbt_user_id = None
dbt_version = None
dbt_project_id = None


def disable_tracking() -> None:
    global g_tracking_enabled
    g_tracking_enabled = False


def is_tracking_enabled() -> bool:
    return False


def set_entrypoint_name(s: str) -> None:
    global entrypoint_name
    entrypoint_name = s


def set_dbt_user_id(s: str) -> None:
    global dbt_user_id
    dbt_user_id = s


def set_dbt_version(s: str) -> None:
    global dbt_version
    dbt_version = s


def set_dbt_project_id(s: str) -> None:
    global dbt_project_id
    dbt_project_id = s


def get_anonymous_id() -> str:
    return ""


def create_start_event_json(diff_options: Dict[str, Any]) -> Dict[str, Any]:
    return {}


def create_end_event_json(*args, **kwargs) -> Dict[str, Any]:
    return {}


def create_email_signup_event_json(email: str) -> Dict[str, Any]:
    return {}


def convert_sets_to_lists(obj: Any) -> Any:
    if isinstance(obj, set):
        return list(obj)
    elif isinstance(obj, dict):
        return {k: convert_sets_to_lists(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [convert_sets_to_lists(elem) for elem in obj]
    return obj


def send_event_json(event_json: Any) -> None:
    pass


def bool_ask_for_email() -> bool:
    return False


def bool_notify_about_extension() -> bool:
    return False
