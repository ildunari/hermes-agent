"""Checkpoint 4 deletion-readiness gates for the Kosta-specific runtime leaves.

Checkpoint 4 deletes core leaves whose capability has moved to the Poke plugin.
Operator tooling met that bar and was deleted. These tests are the executable
transfer contract for the runtime capabilities that had blocked leaf deletion.

Each test asserts the property that must hold **before**
``gateway/contact_memory/``, ``gateway/proactive_*``, ``gateway/guest_access.py``
and ``gateway/conversation_texture_v2.py`` may be removed. They remain green
only while the generic executable-tool seam is typed, consumed by production,
and available to a turn-policy extension.

The plan forbids deleting a leaf while its capability has no owner: Review 3
already rejected the "resolved to extension, nothing actually runs" shape as a
functional outage rather than a conservative scope choice. Deleting the runtime
leaves today would reproduce it for contact recall, the interest digest, Lane B
retrieval, and the guest sandbox tool.
"""

from __future__ import annotations

import dataclasses

import pytest

from agent.request_scoped_tools import RequestScopedTool
from gateway import conversation_extensions as ce


def test_turn_augmentation_can_carry_an_executable_request_scoped_tool():
    """Lane B needs a *callable* tool to leave core; names cannot dispatch.

    Lane B builds a ``RequestScopedTool``
    (schema + handler + on_success) and ``bind_request_scoped_tools`` binds
    exactly that object. The generic seam must retain the executable object,
    not reduce it to a name or silently discard it.
    """
    tool = RequestScopedTool(
        schema={"name": "contact_lane_b", "parameters": {}},
        handler=lambda args: "ok",
    )

    augmentation = ce.GatewayTurnAugmentation(request_tools=(tool,))
    collected, degraded = ce._clean_request_tools(augmentation.request_tools)

    assert collected == [tool], (
        "the generic turn-augmentation seam discards executable request-scoped "
        "tools, so Lane B retrieval has no way out of core"
    )
    assert degraded is False


def test_turn_augmentation_request_tools_is_consumed_in_production():
    """An unconsumed field cannot be an owner.

    ``collect_turn_augmentation`` populates ``request_tools`` and production
    must merge those objects into the request-scoped binding boundary.

    Scope note: only importable Python under the runtime packages counts. An
    earlier version grepped the whole tree, so writing *documentation* about
    this very gap created "consumers" in ``docs/`` and flipped the result --
    prose is not an owner.
    """
    import subprocess
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    hits = subprocess.run(
        [
            "git", "grep", "-n", r"\.request_tools", "--",
            "*.py", ":!tests/", ":!docs/",
        ],
        cwd=root,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    consumers = [
        line for line in hits
        if "conversation_extensions.py" not in line.split(":")[0]
    ]

    assert consumers, (
        "no production code reads GatewayTurnAugmentation.request_tools; it is "
        "an unconsumed stub, not a working ownership seam"
    )


def test_augmentation_field_type_admits_tool_objects():
    """The declared type is the contract a plugin author codes against."""
    field = {f.name: f for f in dataclasses.fields(ce.GatewayTurnAugmentation)}[
        "request_tools"
    ]

    assert field.type != "tuple[str, ...]", (
        "request_tools is typed as a tuple of names; a plugin cannot express an "
        "executable tool against this contract"
    )


@pytest.mark.parametrize(
    "capability, bundle_field",
    [("turn_policy", "augment_turn")],
)
def test_core_still_offers_the_turn_policy_capability(capability, bundle_field):
    """Guard the seam the future owner must declare.

    This one passes today. It is here so that trimming 'unused' extension
    surface during deletion cannot quietly remove the only route by which the
    per-turn contact lane could ever be owned outside core.
    """
    assert ce.CAPABILITY_FIELDS.get(capability) == bundle_field
    assert bundle_field in {
        f.name for f in dataclasses.fields(ce.GatewayConversationExtension)
    }
