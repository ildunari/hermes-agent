#!/usr/bin/env python3
"""Read-only Mac Studio Hermes health/safety smoke check.

This intentionally reports counts and status only. It must never print secret
values from configs or logs.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

HOME = Path.home()
HERMES = HOME / ".hermes"
GPT = HERMES / "profiles" / "gpt"
AGENT = HERMES / "hermes-agent"
STATE = HOME / ".config" / "hermes-state"

SECRET_PATTERNS = {
    "telegram_bot_url": re.compile(r"api\.telegram\.org/bot\d+:[^/\s\"']+"),
    "telegram_bot_token": re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{20,}\b"),
    "anthropic_key": re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}"),
    "openai_key": re.compile(r"sk-(?!ant-)[A-Za-z0-9_-]{20,}"),
    "google_api_key": re.compile(r"AIza[0-9A-Za-z_-]{30,}"),
    "bearer_token": re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]{20,}"),
    "dsn_password": re.compile(r"(?i)(?:postgres|postgresql|mysql|neo4j|redis)://[^:\s/@]+:[^\s/@]{3,}@"),
    "named_secret": re.compile(
        r"(?i)[\"']?(?:password|api[_-]?key|token|secret|authorization|bearer)"
        r"[\w.-]{0,40}[\"']?\s*[:=]\s*[\"']?[^\s\"']{8,}[\"']?"
    ),
}


def run(args: list[str], cwd: Path | None = None, timeout: int = 10) -> dict[str, Any]:
    try:
        proc = subprocess.run(
            args,
            cwd=str(cwd) if cwd else None,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        return {"ok": proc.returncode == 0, "code": proc.returncode, "stdout": redact_text(proc.stdout.strip()), "stderr": redact_text(proc.stderr.strip())}
    except Exception as exc:
        return {"ok": False, "error": redact_text(f"{type(exc).__name__}: {exc}")}


def redact_text(value: str) -> str:
    redacted = value
    for name, pattern in SECRET_PATTERNS.items():
        redacted = pattern.sub(f"[{name.upper()}_REDACTED]", redacted)
    return redacted


def redact_obj(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, list):
        return [redact_obj(item) for item in value]
    if isinstance(value, dict):
        return {redact_text(str(key)): redact_obj(item) for key, item in value.items()}
    return value


def summarize_body(value: Any) -> Any:
    value = redact_obj(value)
    if isinstance(value, dict):
        safe: dict[str, Any] = {"keys": sorted(str(key) for key in value.keys())}
        for key in ("status", "gateway_state", "platform", "version", "active_agents"):
            if key in value and isinstance(value[key], (str, int, float, bool, type(None))):
                safe[key] = value[key]
        if isinstance(value.get("platforms"), dict):
            safe["platforms"] = {
                str(name): {"state": details.get("state")}
                for name, details in value["platforms"].items()
                if isinstance(details, dict)
            }
        if isinstance(value.get("checks"), dict):
            safe["checks"] = {
                str(name): {"status": details.get("status")}
                for name, details in value["checks"].items()
                if isinstance(details, dict)
            }
        if isinstance(value.get("data"), list):
            safe["data_count"] = len(value["data"])
            safe["ids"] = [item.get("id") for item in value["data"][:20] if isinstance(item, dict) and isinstance(item.get("id"), str)]
        if "Browser" in value:
            safe["Browser"] = value.get("Browser")
            safe["Protocol-Version"] = value.get("Protocol-Version")
        return safe
    if isinstance(value, list):
        return {"items": len(value)}
    if isinstance(value, str):
        return {"text_preview": redact_text(value[:160]), "text_length": len(value)}
    return value


def http_json(url: str, timeout: int = 3, headers: dict[str, str] | None = None) -> dict[str, Any]:
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read(512_000).decode("utf-8", errors="replace")
            try:
                parsed: Any = json.loads(body)
            except Exception:
                parsed = body[:500]
            return {"ok": 200 <= resp.status < 300, "status": resp.status, "body": summarize_body(parsed)}
    except urllib.error.HTTPError as exc:
        return {"ok": False, "status": exc.code, "expected_auth_failure": exc.code in {401, 403}}
    except Exception as exc:
        return {"ok": False, "error": redact_text(f"{type(exc).__name__}: {exc}")}


def git_status(path: Path) -> dict[str, Any]:
    if not (path / ".git").exists():
        return {"ok": False, "error": "not a git checkout"}
    status = run(["git", "status", "--short", "--branch"], cwd=path)
    return status


def file_mode(path: Path) -> str | None:
    try:
        return oct(path.stat().st_mode & 0o777)
    except FileNotFoundError:
        return None


def check_symlink(path: Path, expected: Path) -> dict[str, Any]:
    try:
        resolved = path.resolve(strict=True)
        return {"exists": True, "is_symlink": path.is_symlink(), "target_ok": resolved == expected.resolve(strict=True), "target": str(resolved)}
    except Exception as exc:
        return {"exists": path.exists(), "is_symlink": path.is_symlink(), "target_ok": False, "error": redact_text(f"{type(exc).__name__}: {exc}")}


def existing_files_newest_first(paths: list[Path]) -> list[Path]:
    """Return existing files sorted newest-first."""
    return sorted(
        (path for path in paths if path.exists() and path.is_file()),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )


def scan_secret_patterns(paths: list[Path], max_bytes: int | None = 8_000_000) -> dict[str, Any]:
    findings: dict[str, dict[str, int]] = {}
    for path in paths:
        if not path.exists() or not path.is_file():
            continue
        try:
            raw = path.read_bytes()
            if max_bytes is not None:
                raw = raw[-max_bytes:]
            data = raw.decode("utf-8", errors="ignore")
        except Exception as exc:
            findings[str(path)] = {"read_error": 1, "error_hash": abs(hash(type(exc).__name__))}
            continue
        counts = {name: len(pattern.findall(data)) for name, pattern in SECRET_PATTERNS.items()}
        if any(counts.values()):
            findings[str(path)] = counts
    return {"ok": not findings, "findings": findings}


def main() -> int:
    mini_src = STATE / "miniapp" / "index.html"
    token_log = HOME / "Library" / "Logs" / "claude-hermes-telegram-supervisor.staging.out.log"
    redacted_logs = existing_files_newest_first(
        list((HOME / "Library" / "Logs").glob("claude-hermes-telegram-supervisor.staging.out.*.redacted.log"))
    )
    scanned_logs = [token_log, *redacted_logs]
    mem0_cfg = HERMES / "mem0-oss.json"

    report: dict[str, Any] = {
        "host": run(["hostname"]),
        "repos": {
            "hermes_agent": git_status(AGENT),
            "hermes_state": git_status(STATE),
        },
        "permissions": {
            "mem0_oss_json": {"path": str(mem0_cfg), "mode": file_mode(mem0_cfg), "ok": file_mode(mem0_cfg) in {"0o600", "0o400"}},
        },
        "health": {
            "webui_deep": http_json("http://127.0.0.1:8787/health?deep=1"),
            "default_gateway": http_json("http://127.0.0.1:8642/health/detailed"),
            "gpt_gateway": http_json("http://127.0.0.1:8643/health/detailed"),
            "api_auth_gate": http_json("http://127.0.0.1:8643/v1/models"),
            "chrome_cdp": http_json("http://127.0.0.1:9222/json/version"),
            "cli_proxy_8318": http_json("http://127.0.0.1:8318/v1/models"),
        },
        "miniapp": {
            "source_exists": mini_src.exists(),
            "gpt_symlink": check_symlink(GPT / "miniapp" / "index.html", mini_src),
            "default_symlink": check_symlink(HERMES / "miniapp" / "index.html", mini_src),
        },
        "secret_scan_counts_only": {
            "logs": scan_secret_patterns(scanned_logs, max_bytes=None),
            "configs": scan_secret_patterns([HOME / ".codex" / "config.toml", mem0_cfg], max_bytes=None),
        },
        "log_sizes": {
            str(path): path.stat().st_size for path in scanned_logs if path.exists()
        },
    }

    failures: list[str] = []
    if not report["permissions"]["mem0_oss_json"]["ok"]:
        failures.append("mem0-oss.json permissions are not owner-only")
    if not report["miniapp"]["gpt_symlink"]["target_ok"] or not report["miniapp"]["default_symlink"]["target_ok"]:
        failures.append("miniapp symlink target mismatch")
    if not report["secret_scan_counts_only"]["logs"]["ok"]:
        failures.append("secret-like patterns found in scanned logs")
    if not report["health"]["api_auth_gate"].get("expected_auth_failure"):
        failures.append("unauthenticated /v1/models did not return expected auth failure")

    report["summary"] = {"ok": not failures, "failures": failures}
    print(json.dumps(report, indent=2, sort_keys=True))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
