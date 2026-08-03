from __future__ import annotations

from gateway.email_auth import (
    allowed_inbound_email_senders,
    email_password_configured,
    email_recipient_allowed,
    resolve_email_password,
)


def _getter(values: dict[str, str]):
    return lambda name, default="": values.get(name, default)


def test_email_auth_helpers_use_supplied_secret_getter(monkeypatch):
    monkeypatch.setenv("EMAIL_PASSWORD", "wrong-profile-password")
    monkeypatch.setenv("EMAIL_ALLOWED_USERS", "wrong@example.com")
    scoped = _getter(
        {
            "EMAIL_PASSWORD": "scoped-password",
            "EMAIL_ALLOWED_USERS": "Allowed@Example.com",
        }
    )

    assert resolve_email_password(getenv=scoped) == "scoped-password"
    assert email_password_configured(getenv=scoped) is True
    assert allowed_inbound_email_senders(getenv=scoped) == {"allowed@example.com"}
    assert email_recipient_allowed("allowed@example.com", getenv=scoped) is True
    assert email_recipient_allowed("wrong@example.com", getenv=scoped) is False


def test_command_password_uses_scoped_command_and_timeout(monkeypatch):
    monkeypatch.setenv("EMAIL_PASSWORD_CMD", "printf wrong-profile")
    scoped = _getter(
        {
            "EMAIL_PASSWORD_CMD": "printf scoped-password",
            "EMAIL_PASSWORD_CMD_TIMEOUT": "2",
        }
    )

    assert resolve_email_password(getenv=scoped) == "scoped-password"
