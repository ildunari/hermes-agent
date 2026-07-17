"""Explicit, request-scoped contact recall (Lane B)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agent.request_scoped_tools import (
    RequestScopedTool,
    record_current_request_scoped_usage,
)

from .broker import RetrievalScope
from .runtime import get_broker

TOOL_NAME = "contact_memory_search"
_MAX_QUERY_CHARS = 500

_TOOL_SCHEMA = {
    "name": TOOL_NAME,
    "description": (
        "Search private facts for the contact authenticated for this request. "
        "You MUST use this before answering a direct question about Kosta, the authenticated contact, "
        "or their shared relationship/history when the answer is not already in visible context. "
        "Do not claim no memory/history or tell the contact to ask Kosta until this search returns no match."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "What contact fact to look for.",
                "minLength": 1,
                "maxLength": _MAX_QUERY_CHARS,
            }
        },
        "required": ["query"],
        "additionalProperties": False,
    },
}


def build_lane_b_tool(
    *,
    root: str | Path,
    config: dict[str, Any],
    scope: RetrievalScope,
    turn_index: int,
) -> RequestScopedTool:
    """Close immutable authorization scope into a query-only tool handler."""

    def search(args: dict[str, Any]) -> str:
        # Do not silently ignore model-supplied namespace/principal fields.  The
        # JSON schema excludes them and this runtime check protects adapters that
        # do not enforce ``additionalProperties``.
        if set(args) != {"query"}:
            return json.dumps({"error": "contact_memory_search accepts only query"})
        query = args.get("query")
        if not isinstance(query, str) or not query.strip():
            return json.dumps({"error": "query must be a non-empty string"})
        if len(query) > _MAX_QUERY_CHARS:
            return json.dumps({"error": f"query exceeds {_MAX_QUERY_CHARS} characters"})
        try:
            bundle = get_broker(root, config).search(
                scope,
                query.strip(),
                limit=3,
                turn_index=turn_index,
                direct_ask=True,
            )
            if bundle.fact_ids:
                record_current_request_scoped_usage(TOOL_NAME, bundle.fact_ids)
            return bundle.rendered or "No visible contact facts matched the query."
        except Exception:
            # Retrieval is advisory. A corrupt/absent local service must not
            # prevent the gateway from completing the reply.
            return json.dumps({"error": "contact memory is temporarily unavailable"})

    def commit_success(values: Any) -> None:
        fact_ids = tuple(dict.fromkeys(
            fact_id for group in values for fact_id in group
        ))
        if fact_ids:
            get_broker(root, config).record_usage(
                scope, fact_ids, turn_index=turn_index,
            )

    return RequestScopedTool(
        schema=_TOOL_SCHEMA, handler=search, on_success=commit_success
    )
