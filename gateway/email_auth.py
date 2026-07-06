"""Email credential helpers.

Supports plaintext EMAIL_PASSWORD for backward compatibility and
EMAIL_PASSWORD_CMD for command-backed passwords (for example, 1Password CLI).
The resolved password is never logged or stored by this module.
"""

from __future__ import annotations

import os
import shlex
import subprocess


_DEFAULT_TIMEOUT_SECONDS = 10


def resolve_email_password() -> str:
    """Return the configured email password without logging it.

    EMAIL_PASSWORD remains the backward-compatible direct value. If it is not
    set, EMAIL_PASSWORD_CMD is executed with shell-like quoting via shlex, and
    stdout (minus trailing newlines) is used as the password.
    """

    direct = os.getenv("EMAIL_PASSWORD", "")
    if direct:
        return direct

    command = os.getenv("EMAIL_PASSWORD_CMD", "").strip()
    if not command:
        return ""

    try:
        timeout = float(os.getenv("EMAIL_PASSWORD_CMD_TIMEOUT", str(_DEFAULT_TIMEOUT_SECONDS)))
    except (TypeError, ValueError):
        timeout = _DEFAULT_TIMEOUT_SECONDS

    try:
        argv = shlex.split(command)
    except ValueError:
        return ""
    if not argv:
        return ""

    try:
        proc = subprocess.run(
            argv,
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except Exception:
        return ""

    return proc.stdout.rstrip("\r\n")


def email_password_configured() -> bool:
    """Return True when either direct or command-backed password config exists.

    This is deliberately side-effect-free: config discovery must not execute
    secret manager commands. The command is resolved only when IMAP/SMTP login
    is actually attempted.
    """

    return bool(os.getenv("EMAIL_PASSWORD", "") or os.getenv("EMAIL_PASSWORD_CMD", "").strip())


def email_password_available() -> bool:
    """Return True when the configured password resolves successfully."""

    return bool(resolve_email_password())


def _split_email_list(raw: str) -> set[str]:
    return {addr.strip().lower() for addr in raw.split(",") if addr.strip()}


def allowed_inbound_email_senders() -> set[str]:
    """Return EMAIL_ALLOWED_USERS as normalized email addresses."""

    return _split_email_list(os.getenv("EMAIL_ALLOWED_USERS", ""))


def allowed_outbound_email_recipients() -> set[str]:
    """Return normalized outbound recipients allowed for email sends.

    EMAIL_ALLOWED_RECIPIENTS is the explicit outbound control. When unset, we
    intentionally fall back to EMAIL_ALLOWED_USERS so the stock email gateway's
    reply-to-sender behavior remains safe for a Kosta-only allowlist profile.
    EMAIL_HOME_ADDRESS is also allowed because cron and reporting workflows use
    it as the user's delivery target.
    """

    recipients = _split_email_list(os.getenv("EMAIL_ALLOWED_RECIPIENTS", ""))
    if not recipients:
        recipients = set(allowed_inbound_email_senders())
    home = os.getenv("EMAIL_HOME_ADDRESS", "").strip().lower()
    if home:
        recipients.add(home)
    return recipients


def email_recipient_allowed(address: str) -> bool:
    """Return True when *address* is permitted by outbound email policy."""

    allowed = allowed_outbound_email_recipients()
    if not allowed:
        return True
    return address.strip().lower() in allowed
