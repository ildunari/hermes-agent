"""Checkpoint 4 runtime-leaf deletion and surviving generic seam gates."""
from __future__ import annotations

import dataclasses
from pathlib import Path

from agent.request_scoped_tools import RequestScopedTool
from gateway import conversation_extensions as ce


ROOT = Path(__file__).resolve().parents[2]
DELETED_RUNTIME_PATHS = (
    "gateway/contact_memory",
    "gateway/guest_access.py",
    "gateway/conversation_texture_v2.py",
    "gateway/proactive_checkin.py",
    "gateway/proactive_fetch.py",
    "gateway/proactive_scheduler.py",
    "gateway/proactive_status.py",
    "gateway/proactive_transport.py",
    "tools/guest_workspace_tools.py",
)


def test_runtime_policy_leaves_are_physically_deleted():
    assert [path for path in DELETED_RUNTIME_PATHS if (ROOT / path).exists()] == []


def test_turn_augmentation_carries_an_executable_request_scoped_tool():
    tool = RequestScopedTool(
        schema={"name": "extension_lane", "parameters": {}},
        handler=lambda args: "ok",
    )
    augmentation = ce.GatewayTurnAugmentation(request_tools=(tool,))
    collected, degraded = ce._clean_request_tools(augmentation.request_tools)
    assert collected == [tool]
    assert degraded is False


def test_turn_augmentation_request_tools_is_consumed_in_production():
    source = (ROOT / "gateway/run.py").read_text(encoding="utf-8")
    assert "_extension_augmentation.request_tools" in source
    assert "bind_request_scoped_tools(agent, _lane_b_tools)" in source


def test_augmentation_field_type_admits_tool_objects():
    field = {f.name: f for f in dataclasses.fields(ce.GatewayTurnAugmentation)}[
        "request_tools"
    ]
    assert field.type != "tuple[str, ...]"


def test_core_still_offers_the_turn_policy_capability():
    assert ce.CAPABILITY_FIELDS.get("turn_policy") == "augment_turn"
    assert "augment_turn" in {
        field.name for field in dataclasses.fields(ce.GatewayConversationExtension)
    }


def test_untrusted_metadata_cannot_supply_extension_identity():
    source = (ROOT / "gateway/run.py").read_text(encoding="utf-8")
    assert '"_hermes_extension_identity" in event.metadata' in source
    assert 'if k != "_hermes_extension_identity"' in source
    assert "_hermes_contact_scope" not in source
