#!/usr/bin/env python3

from __future__ import annotations

import argparse
import hashlib
import json
import os
import urllib.error
import urllib.request
import uuid
from pathlib import Path


DESCRIPTION = "Verify a new authenticated Hermes API turn without retaining transcript content."
ENV_CANDIDATES = (
    Path.home() / ".hermes" / "profiles" / "gpt" / ".env",
    Path.home() / ".hermes" / ".env",
)


def env_value(name: str, candidates: tuple[Path, ...] = ENV_CANDIDATES) -> str:
    value = os.environ.get(name)
    if value:
        return value
    for path in candidates:
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            key, separator, raw = line.partition("=")
            if separator and key.strip() == name:
                return raw.strip().strip("'\"")
    return ""


def api_key() -> str:
    value = env_value("API_SERVER_KEY")
    if value:
        return value
    raise RuntimeError("API_SERVER_KEY is unavailable")


def api_base_url() -> str:
    # The profile file comes first for credentials, but may describe its own
    # non-running profile port. The deployed shared listener lives in the
    # global file, so resolve its address in the opposite order.
    address_candidates = tuple(reversed(ENV_CANDIDATES))
    host = env_value("API_SERVER_HOST", address_candidates) or "127.0.0.1"
    port = env_value("API_SERVER_PORT", address_candidates) or "8642"
    return f"http://{host}:{port}"


def request(
    base_url: str,
    path: str,
    key: str,
    *,
    method: str = "GET",
    payload: dict[str, object] | None = None,
) -> dict[str, object]:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Authorization": f"Bearer {key}"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}{path}",
        data=body,
        headers=headers,
        method=method,
    )
    with urllib.request.urlopen(req, timeout=180) as response:
        return json.loads(response.read())


def main() -> int:
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--expected-commit", required=True)
    args = parser.parse_args()
    key = api_key()
    base_url = args.base_url or api_base_url()
    session_id = f"update_verify_{uuid.uuid4().hex}"
    marker = hashlib.sha256(f"{args.run_id}:{args.expected_commit}".encode()).hexdigest()[:16]
    created = request(
        base_url,
        "/api/sessions",
        key,
        method="POST",
        payload={"id": session_id, "title": "Update verification"},
    )
    if created.get("session", {}).get("id") != session_id:
        raise RuntimeError("session creation did not return the expected id")
    try:
        turn = request(
            base_url,
            f"/api/sessions/{session_id}/chat",
            key,
            method="POST",
            payload={
                "message": f"Reply exactly UPDATE_OK_{marker}",
                "reasoning_effort": "low",
            },
        )
        content = str(turn.get("message", {}).get("content", ""))
        if f"UPDATE_OK_{marker}" not in content:
            raise RuntimeError("representative turn did not return the expected marker")
        transcript = request(
            base_url,
            f"/api/sessions/{session_id}/messages",
            key,
        )
        messages = transcript.get("data", [])
        if not isinstance(messages, list) or len(messages) < 2:
            raise RuntimeError("representative transcript was not persisted")
        print(
            json.dumps(
                {
                    "status": "PASS",
                    "run_id": args.run_id,
                    "expected_commit": args.expected_commit,
                    "message_count": len(messages),
                    "response_sha256": hashlib.sha256(content.encode()).hexdigest(),
                },
                sort_keys=True,
            )
        )
    finally:
        try:
            request(
                base_url,
                f"/api/sessions/{session_id}",
                key,
                method="DELETE",
            )
        except (OSError, urllib.error.URLError):
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
