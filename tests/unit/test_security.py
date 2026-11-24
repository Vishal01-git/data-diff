"""Unit tests for security, PII hygiene, and secret masking."""

import logging
from validator.security import (
    SecretMaskingFilter,
    mask_dict_secrets,
    mask_secrets_in_text,
    mask_uri,
    redact_sample_values,
    sanitize_error_message,
)


def test_mask_uri_user_password():
    uri = "postgresql://admin:super_secret_password123@db.prod.internal:5432/analytics"
    masked = mask_uri(uri)
    assert "super_secret_password123" not in masked
    assert "postgresql://admin:***@db.prod.internal:5432/analytics" == masked


def test_mask_uri_query_parameters():
    uri = "athena://?access_key=AKIAIOSFODNN7EXAMPLE&secret_key=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY&region=us-east-1"
    masked = mask_uri(uri)
    assert "wJalrXUtnFEMI" not in masked
    assert "access_key=***" in masked
    assert "secret_key=***" in masked
    assert "region=us-east-1" in masked


def test_mask_uri_empty_or_none():
    assert mask_uri(None) == ""
    assert mask_uri("") == ""


def test_mask_secrets_in_text():
    text = "Failed connecting to postgresql://user:my_secret@host:5432/db with key AKIA1234567890ABCDEF"
    masked = mask_secrets_in_text(text)
    assert "my_secret" not in masked
    assert "user:***@host" in masked
    assert "AKIA1234567890ABCDEF" not in masked
    assert "***AWS_KEY***" in masked


def test_mask_dict_secrets():
    data = {
        "user": "alice",
        "password": "plain_password",
        "nested": {
            "token": "bearer_xyz",
            "normal_field": 42,
            "secret_key": "topsecret",
        },
        "list_items": [
            {"access_key": "key123", "name": "service_account"},
            "simple_string",
        ],
    }

    masked = mask_dict_secrets(data)
    assert masked["password"] == "***"
    assert masked["nested"]["token"] == "***"
    assert masked["nested"]["secret_key"] == "***"
    assert masked["nested"]["normal_field"] == 42
    assert masked["list_items"][0]["access_key"] == "***"
    assert masked["list_items"][0]["name"] == "service_account"


def test_sanitize_error_message():
    err = Exception("Connection to mysql://root:root_pass_123@127.0.0.1:3306/db failed: password wrong")
    sanitized = sanitize_error_message(err)
    assert "root_pass_123" not in sanitized
    assert "root:***@127.0.0.1" in sanitized


def test_secret_masking_filter():
    logger = logging.getLogger("test.security")
    filter_ = SecretMaskingFilter()

    record = logger.makeRecord(
        name="test.security",
        level=logging.INFO,
        fn="test.py",
        lno=1,
        msg="Connecting to postgres://admin:secret123@host:5432/db",
        args=(),
        exc_info=None,
    )
    filter_.filter(record)
    assert "secret123" not in record.msg
    assert "admin:***@host" in record.msg


def test_redact_sample_values():
    diffs = {
        "amount": (100.50, 100.51),
        "customer_name": ("John Doe", "Jane Doe"),
    }

    # When include_sample_values is False (default)
    redacted = redact_sample_values(diffs, include_sample_values=False)
    assert redacted["amount"] == ("[REDACTED]", "[REDACTED]")
    assert redacted["customer_name"] == ("[REDACTED]", "[REDACTED]")

    # When include_sample_values is True
    preserved = redact_sample_values(diffs, include_sample_values=True)
    assert preserved["amount"] == (100.50, 100.51)
    assert preserved["customer_name"] == ("John Doe", "Jane Doe")
