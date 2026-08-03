"""Email credential helpers.

Supports plaintext EMAIL_PASSWORD for backward compatibility and
EMAIL_PASSWORD_CMD for command-backed passwords (for example, 1Password CLI).
The resolved password is never logged or stored by this module.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from collections.abc import Callable


_DEFAULT_TIMEOUT_SECONDS = 10
SecretGetter = Callable[[str, str], str]


def resolve_email_password(*, getenv: SecretGetter = os.getenv) -> str:
    """Return the configured email password without logging it.

    EMAIL_PASSWORD remains the backward-compatible direct value. If it is not
    set, EMAIL_PASSWORD_CMD is executed with shell-like quoting via shlex, and
    stdout (minus trailing newlines) is used as the password.
    """

    direct = getenv("EMAIL_PASSWORD", "")
    if direct:
        return direct

    command = getenv("EMAIL_PASSWORD_CMD", "").strip()
    if not command:
        return ""

    try:
        timeout = float(getenv("EMAIL_PASSWORD_CMD_TIMEOUT", str(_DEFAULT_TIMEOUT_SECONDS)))
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


def email_password_configured(*, getenv: SecretGetter = os.getenv) -> bool:
    """Return True when either direct or command-backed password config exists.

    This is deliberately side-effect-free: config discovery must not execute
    secret manager commands. The command is resolved only when IMAP/SMTP login
    is actually attempted.
    """

    return bool(getenv("EMAIL_PASSWORD", "") or getenv("EMAIL_PASSWORD_CMD", "").strip())


def email_password_available(*, getenv: SecretGetter = os.getenv) -> bool:
    """Return True when the configured password resolves successfully."""

    return bool(resolve_email_password(getenv=getenv))


def _split_email_list(raw: str) -> set[str]:
    return {addr.strip().lower() for addr in raw.split(",") if addr.strip()}


def allowed_inbound_email_senders(*, getenv: SecretGetter = os.getenv) -> set[str]:
    """Return EMAIL_ALLOWED_USERS as normalized email addresses."""

    return _split_email_list(getenv("EMAIL_ALLOWED_USERS", ""))


def allowed_outbound_email_recipients(*, getenv: SecretGetter = os.getenv) -> set[str]:
    """Return normalized outbound recipients allowed for email sends.

    EMAIL_ALLOWED_RECIPIENTS is the explicit outbound control. When unset, we
    intentionally fall back to EMAIL_ALLOWED_USERS so the stock email gateway's
    reply-to-sender behavior remains safe for a Kosta-only allowlist profile.
    EMAIL_HOME_ADDRESS is also allowed because cron and reporting workflows use
    it as the user's delivery target.
    """

    recipients = _split_email_list(getenv("EMAIL_ALLOWED_RECIPIENTS", ""))
    if not recipients:
        recipients = set(allowed_inbound_email_senders(getenv=getenv))
    home = getenv("EMAIL_HOME_ADDRESS", "").strip().lower()
    if home:
        recipients.add(home)
    return recipients


def email_recipient_allowed(address: str, *, getenv: SecretGetter = os.getenv) -> bool:
    """Return True when *address* is permitted by outbound email policy."""

    allowed = allowed_outbound_email_recipients(getenv=getenv)
    if not allowed:
        return True
    return address.strip().lower() in allowed
