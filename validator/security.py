"""Security & PII Hygiene utilities for secret masking and data redaction."""

import copy
import logging
import re
from typing import Any, Dict, List, Optional, Tuple, Union

# Regex patterns for sensitive credentials in URIs and connection parameters
_URI_USERPASS_RE = re.compile(r"(://[^:/@]+:)([^@]+)(@)")
_QUERY_SECRET_RE = re.compile(
    r"(?i)(password|secret|secret_key|access_key|accesskey|token|auth_token|apikey|api_key)=([^&]+)"
)
_KEY_VALUE_SECRET_RE = re.compile(
    r"(?i)(['\"]?(?:password|secret|secret_key|access_key|token|api_key)['\"]?\s*[:=]\s*['\"])([^'\"]+)(['\"])"
)
_AWS_ACCESS_KEY_RE = re.compile(r"\b(AKIA[0-9A-Z]{16})\b")

# Sensitive dictionary keys to mask
SENSITIVE_KEYS = {
    "password",
    "pass",
    "secret",
    "secret_key",
    "access_key",
    "accesskey",
    "token",
    "auth_token",
    "api_key",
    "apikey",
    "private_key",
    "credentials",
}


def mask_uri(uri: Optional[str]) -> str:
    """Mask password or credentials in a connection URI string.

    e.g. 'postgresql://user:secret123@localhost:5432/db' -> 'postgresql://user:***@localhost:5432/db'
    e.g. 'athena://?access_key=AKIA123&secret_key=xyz' -> 'athena://?access_key=***&secret_key=***'
    """
    if not uri:
        return ""

    # Mask user:pass@
    masked = _URI_USERPASS_RE.sub(r"\1***\3", uri)

    # Mask query parameters
    masked = _QUERY_SECRET_RE.sub(r"\1=***", masked)

    return masked


def mask_secrets_in_text(text: str) -> str:
    """Scan and mask any secret keys, passwords, or credentials in unstructured text."""
    if not text:
        return ""

    masked = mask_uri(text)
    masked = _KEY_VALUE_SECRET_RE.sub(r"\1***\3", masked)
    masked = _AWS_ACCESS_KEY_RE.sub(r"***AWS_KEY***", masked)
    return masked


def mask_dict_secrets(data: Dict[str, Any]) -> Dict[str, Any]:
    """Recursively traverse a dictionary and mask sensitive values."""
    sanitized: Dict[str, Any] = {}
    for k, v in data.items():
        if isinstance(k, str) and k.lower() in SENSITIVE_KEYS:
            sanitized[k] = "***"
        elif isinstance(v, dict):
            sanitized[k] = mask_dict_secrets(v)
        elif isinstance(v, list):
            sanitized[k] = [
                mask_dict_secrets(item) if isinstance(item, dict) else item
                for item in v
            ]
        elif isinstance(v, str):
            sanitized[k] = mask_uri(v)
        else:
            sanitized[k] = v
    return sanitized


def sanitize_error_message(err: Union[Exception, str]) -> str:
    """Redact passwords and secrets from an error message or exception string."""
    err_str = str(err)
    return mask_secrets_in_text(err_str)


class SecretMaskingFilter(logging.Filter):
    """Logging filter that intercepts and sanitizes secret credentials in log records."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = mask_secrets_in_text(record.msg)

        if record.args:
            if isinstance(record.args, dict):
                record.args = mask_dict_secrets(record.args)
            elif isinstance(record.args, tuple):
                record.args = tuple(
                    mask_secrets_in_text(str(a)) if isinstance(a, str) else a
                    for a in record.args
                )

        if record.exc_text:
            record.exc_text = mask_secrets_in_text(record.exc_text)

        return True


def redact_sample_values(
    column_diffs: Optional[Dict[str, Tuple[Any, Any]]],
    include_sample_values: bool = False,
) -> Dict[str, Tuple[Any, Any]]:
    """Redact row sample values if include_sample_values is False (default).

    When False, keeps the column name indicating difference, but masks the values.
    """
    if not column_diffs:
        return {}

    if include_sample_values:
        return column_diffs

    # Redact raw data values
    return {col: ("[REDACTED]", "[REDACTED]") for col in column_diffs.keys()}
