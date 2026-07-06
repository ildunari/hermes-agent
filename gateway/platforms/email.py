"""Compatibility exports for the plugin-backed email platform."""

from plugins.platforms.email.adapter import (  # noqa: F401
    EmailAdapter,
    cache_document_from_bytes,
    cache_image_from_bytes,
    _strip_html,
)


def check_email_requirements() -> bool:
    """Legacy gateway.platforms.email shim: verify command-backed passwords."""
    import os
    from gateway.email_auth import resolve_email_password

    addr = os.getenv("EMAIL_ADDRESS", "").strip()
    pwd = resolve_email_password().strip()
    imap = os.getenv("EMAIL_IMAP_HOST", "").strip()
    smtp = os.getenv("EMAIL_SMTP_HOST", "").strip()
    return all([addr, pwd, imap, smtp])
