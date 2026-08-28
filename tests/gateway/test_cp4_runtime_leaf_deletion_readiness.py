"""Checkpoint 4 deletion-readiness gates for the Kosta-specific runtime leaves.

Checkpoint 4 deletes core leaves whose capability has moved to the Poke plugin.
Operator tooling met that bar and was deleted. The *runtime* leaves did not,
and these tests are the executable statement of why, so the gap is a failing
contract rather than a paragraph in a handoff note.

Each test asserts the property that must hold **before**
``gateway/contact_memory/``, ``gateway/proactive_*``, ``gateway/guest_access.py``
and ``gateway/conversation_texture_v2.py`` may be removed. They are written to
pass the moment the seam is finished, so completing the work turns them green
instead of requiring them to be rewritten.

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


# These three gates describe a seam that does not exist yet. They are marked
# ``xfail(strict=True)`` rather than left red so the suite stays honest in both
# directions: the gap cannot be ignored, and the day someone finishes the seam
# the run turns XPASS-as-failure and forces this file (and the deletion
# decision it blocks) to be revisited.
NOT_YET_IMPLEMENTED = pytest.mark.xfail(
    strict=True,
    reason=(
        "CP4 blocker: the generic turn seam cannot carry an executable "
        "request-scoped tool, and no plugin owns augment_turn, so the "
        "Kosta-specific runtime leaves cannot be deleted without dropping "
        "contact recall, the interest digest, and Lane B retrieval."
    ),
)


@NOT_YET_IMPLEMENTED
def test_turn_augmentation_can_carry_an_executable_request_scoped_tool():
    """Lane B needs a *callable* tool to leave core; names cannot dispatch.

    ``gateway/contact_memory/lane_b.py`` builds a ``RequestScopedTool``
    (schema + handler + on_success) and ``bind_request_scoped_tools`` binds
    exactly that object. The generic seam declares
    ``request_tools: tuple[str, ...]`` and filters values through
    ``_clean_strings``, which keeps only non-empty ``str``. A real tool is
    therefore silently dropped: no error, no handler, no tool.

    Until the seam carries the executable object, deleting contact_memory
    removes Lane B with nothing able to replace it.
    """
    tool = RequestScopedTool(
        schema={"name": "contact_lane_b", "parameters": {}},
        handler=lambda args: "ok",
    )

    augmentation = ce.GatewayTurnAugmentation(request_tools=(tool,))
    collected = ce._clean_strings(augmentation.request_tools)

    assert collected == [tool], (
        "the generic turn-augmentation seam discards executable request-scoped "
        "tools, so Lane B retrieval has no way out of core"
    )


@NOT_YET_IMPLEMENTED
def test_turn_augmentation_request_tools_is_consumed_in_production():
    """An unconsumed field cannot be an owner.

    ``collect_turn_augmentation`` populates ``request_tools``, but nothing in
    production reads it: ``_collect_extension_turn_context`` returns only the
    joined ``user_context``. A field that is written and never read cannot take
    ownership of Lane B, so the capability would simply vanish on deletion.

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


@NOT_YET_IMPLEMENTED
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
