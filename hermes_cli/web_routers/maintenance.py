"""Authenticated maintenance for this serve/dashboard and its native compute child."""
from __future__ import annotations

import sys

from fastapi import APIRouter, HTTPException, Request

from hermes_cli.dashboard_auth.token_auth import register_token_route
from tui_gateway.owner_maintenance import MaintenanceConflict, get_owner, local_status

router = APIRouter(prefix="/api/maintenance")
for _action in ("status", "begin", "release"):
    register_token_route(f"/api/maintenance/{_action}")


def _child(action="status", **params):
    server = sys.modules.get("tui_gateway.server")
    if server is None:
        return None
    reply = server._existing_compute_owner_maintenance(action, **params)
    if reply is None:
        return None
    if reply.get("type") == "maintenance.error":
        raise MaintenanceConflict(reply.get("message") or "compute maintenance rejected")
    if reply.get("type") != "maintenance.ack" or not isinstance(reply.get("owner"), dict):
        raise RuntimeError("invalid compute maintenance response")
    return reply["owner"]


def inventory():
    owners = [local_status()]
    child = _child()
    if child is not None:
        owners.append(child)
    return owners


def control(action: str, body: dict):
    """Internal entry point; HTTP callers must pass the existing drain auth gate."""
    owner = get_owner()
    if action not in {"status", "begin", "release"}:
        raise MaintenanceConflict("unsupported maintenance action")
    # Serializes local admissions with the complete generation check and action.
    with owner.lock:
        owners = inventory()
        if action == "status":
            generation = body.get("owner_generation")
            if generation is not None and generation not in {o["owner_generation"] for o in owners}:
                raise MaintenanceConflict("stale owner generation")
            return {"protocol_version": 1, "owners": owners}
        generation, token = body.get("owner_generation"), body.get("request_token")
        if generation == owner.generation:
            getattr(owner, action)(generation, token)
        elif any(o["owner_generation"] == generation for o in owners):
            _child(action, owner_generation=generation, request_token=token)
        else:
            raise MaintenanceConflict("stale owner generation")
        owners = inventory()
        if body.get("finalize_bootstrap"):
            expected = body.get("expected_owner_generations")
            actual = [o["owner_generation"] for o in owners]
            if (action != "release" or not isinstance(expected, list)
                    or len(expected) != len(actual) or set(expected) != set(actual)
                    or any(o["admissions_closed"] or o["released_request_token"] != token for o in owners)):
                raise MaintenanceConflict("all current owners must be verified and released before bootstrap finalization")
            child_generation = next((o["owner_generation"] for o in owners
                                     if o["owner_kind"] == "compute_host"), None)
            server = sys.modules.get("tui_gateway.server")
            if server is None:
                owner.clear_bootstrap(token)
            else:
                server._finalize_compute_owner_maintenance(child_generation, lambda: owner.clear_bootstrap(token))
            owners = inventory()
        return {"protocol_version": 1, "owners": owners}


@router.post("/{action}")
def maintenance(action: str, body: dict, request: Request):
    principal = getattr(request.state, "token_principal", None)
    if principal is None:
        raise HTTPException(401, "drain credential required")
    if "drain" not in principal.scopes:
        raise HTTPException(403, "drain scope required")
    try:
        return control(action, body)
    except MaintenanceConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(503, "owner maintenance unavailable") from exc
