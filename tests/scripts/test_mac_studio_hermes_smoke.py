from pathlib import Path
import os

from scripts import mac_studio_hermes_smoke as smoke


def test_check_symlink_reports_matching_target(tmp_path):
    source = tmp_path / "source.txt"
    source.write_text("ok")
    link = tmp_path / "link.txt"
    link.symlink_to(source)

    result = smoke.check_symlink(link, source)

    assert result["exists"] is True
    assert result["is_symlink"] is True
    assert result["target_ok"] is True


def test_scan_secret_patterns_reports_counts_without_values(tmp_path):
    log = tmp_path / "log.txt"
    secret = "https://api.telegram.org/bot123456:SECRET/sendMessage"
    log.write_text(f"HTTP Request: POST {secret}\n")

    result = smoke.scan_secret_patterns([log])

    assert result["ok"] is False
    counts = result["findings"][str(log)]
    assert counts["telegram_bot_url"] == 1
    assert secret not in str(result)






def test_scan_secret_patterns_can_scan_whole_file_when_requested(tmp_path):
    log = tmp_path / "big.log"
    secret = "https://api.telegram.org/bot123456:SECRET/sendMessage"
    log.write_text(f"{secret}\n" + ("x" * 100))

    tail_only = smoke.scan_secret_patterns([log], max_bytes=10)
    whole_file = smoke.scan_secret_patterns([log], max_bytes=None)

    assert tail_only["ok"] is True
    assert whole_file["ok"] is False
    assert whole_file["findings"][str(log)]["telegram_bot_url"] == 1


def test_existing_files_newest_first_returns_all_existing_files_newest_first(tmp_path):
    older = tmp_path / "older.log"
    newer = tmp_path / "newer.log"
    missing = tmp_path / "missing.log"
    older.write_text("older")
    newer.write_text("newer")
    os.utime(older, (1, 1))
    os.utime(newer, (2, 2))

    result = smoke.existing_files_newest_first([older, missing, newer])

    assert result == [newer, older]


def test_redact_obj_removes_secret_patterns_recursively():
    openai_key = "sk-" + "a" * 30
    google_key = "AIza" + "b" * 35
    bearer = "Bearer " + "c" * 32
    dsn = "postgres://user:" + "d" * 12 + "@localhost/db"
    bare_token = "123456789:" + "e" * 32
    raw = {
        "url": "https://api.telegram.org/bot123456:ABCsecret/sendMessage",
        "items": [openai_key],
        "google": google_key,
        "db": "password = \"supersecret-db-password\"",
        "bearer": bearer,
        "dsn": dsn,
        "bare_token": bare_token,
    }

    redacted = smoke.redact_obj(raw)

    rendered = str(redacted)
    assert "123456:ABCsecret" not in rendered
    assert openai_key not in rendered
    assert google_key not in rendered
    assert "supersecret-db-password" not in rendered
    assert bearer not in rendered
    assert dsn not in rendered
    assert bare_token not in rendered
    assert "[TELEGRAM_BOT_URL_REDACTED]" in rendered
    assert "[OPENAI_KEY_REDACTED]" in rendered
    assert "[GOOGLE_API_KEY_REDACTED]" in rendered
    assert "[NAMED_SECRET_REDACTED]" in rendered


def test_redaction_labels_anthropic_and_ignores_plain_git_sha():
    anthropic_key = "sk-ant-" + "a" * 30
    git_sha = "a" * 40

    assert smoke.redact_text(anthropic_key) == "[ANTHROPIC_KEY_REDACTED]"
    assert smoke.redact_text(git_sha) == git_sha


def test_summarize_body_keeps_safe_health_fields_without_raw_body():
    token = "123456789:" + "a" * 32
    raw = {
        "status": token,
        "gateway_state": "running",
        "platforms": {"telegram": {"state": "connected", "token": token}},
        token: "secret-as-key",
        "secret_url": "https://api.telegram.org/bot123456:SECRET/sendMessage",
    }

    summarized = smoke.summarize_body(raw)

    rendered = str(summarized)
    assert summarized["status"] == "[TELEGRAM_BOT_TOKEN_REDACTED]"
    assert summarized["platforms"] == {"telegram": {"state": "connected"}}
    assert "[TELEGRAM_BOT_TOKEN_REDACTED]" in summarized["keys"]
    assert "secret_url" in summarized["keys"]
    assert "123456:SECRET" not in rendered
    assert token not in rendered


def test_file_mode_missing_path_returns_none(tmp_path):
    assert smoke.file_mode(tmp_path / "missing") is None
