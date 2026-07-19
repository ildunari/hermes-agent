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


def api_key() -> str:
    value = os.environ.get("API_SERVER_KEY")
    if value:
        return value
    candidates = (
        Path.home() / ".hermes" / "profiles" / "gpt" / ".env",
        Path.home() / ".hermes" / ".env",
    )
    for path in candidates:
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            key, separator, raw = line.partition("=")
            if separator and key.strip() == "API_SERVER_KEY":
                return raw.strip().strip("'\"")
    raise RuntimeError("API_SERVER_KEY is unavailable")


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
    parser.add_argument("--base-url", default="http://127.0.0.1:8642")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--expected-commit", required=True)
    args = parser.parse_args()
    key = api_key()
    session_id = f"update_verify_{uuid.uuid4().hex}"
    marker = hashlib.sha256(f"{args.run_id}:{args.expected_commit}".encode()).hexdigest()[:16]
    created = request(
        args.base_url,
        "/api/sessions",
        key,
        method="POST",
        payload={"id": session_id, "title": "Update verification"},
    )
    if created.get("session", {}).get("id") != session_id:
        raise RuntimeError("session creation did not return the expected id")
    try:
        turn = request(
            args.base_url,
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
            args.base_url,
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
                args.base_url,
                f"/api/sessions/{session_id}",
                key,
                method="DELETE",
            )
        except (OSError, urllib.error.URLError):
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
