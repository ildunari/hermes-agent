"""Tests for deterministic Discord rich UI payload builders."""

from plugins.platforms.discord.rich_ui import (
    COMPONENTS_V2_FLAG,
    COPY_UNAUTHORIZED,
    approval_card,
    build_components_v2_payload,
    build_legacy_embed_payload,
    clarify_prompt_card,
    confirmation_card,
    error_card,
    model_picker_card,
    run_status_card,
)


def _walk(component):
    yield component
    for child in component.get("components", []):
        yield from _walk(child)


def test_components_v2_payload_has_flag_and_no_content_or_embeds():
    payload = build_components_v2_payload(approval_card("rm -rf /tmp/demo", "dangerous shell command"))

    assert payload["flags"] == COMPONENTS_V2_FLAG
    assert "content" not in payload
    assert "embeds" not in payload
    assert payload["components"][0]["type"] == 17
    assert any(c.get("type") == 10 and "Approval required" in c.get("content", "") for c in _walk(payload["components"][0]))
    assert len(list(_walk(payload["components"][0]))) <= 40


def test_approval_card_copy_and_actions_are_plain_and_safe():
    card = approval_card("echo hi", "needs shell access")
    legacy = build_legacy_embed_payload(card)
    action_labels = [a["label"] for a in legacy["actions"]]

    assert legacy["embed"]["title"] == "Approval required"
    assert {"Allow once", "Allow for session", "Always allow", "Deny"} == set(action_labels)
    assert all(len(a["custom_id"]) <= 100 for a in legacy["actions"])

def test_confirmation_card_uses_cancel_copy():
    card = confirmation_card("Confirm", "Proceed?")
    legacy = build_legacy_embed_payload(card)
    action_labels = [a["label"] for a in legacy["actions"]]

    assert "Cancel" in action_labels
    assert "Deny" not in action_labels


def test_clarify_card_caps_choices_and_includes_other():
    choices = [f"choice {idx}" for idx in range(40)]
    payload = build_components_v2_payload(clarify_prompt_card("Pick one", choices, "clarify-1"))
    buttons = [c for c in _walk(payload["components"][0]) if c.get("type") == 2]

    assert len(buttons) == 25
    assert buttons[-1]["label"] == "Other (type answer)"
    assert len(list(_walk(payload["components"][0]))) <= 40


def test_model_picker_uses_select_component_and_provider_options():
    card = model_picker_card(
        providers=[{"slug": "codex", "name": "Codex", "is_current": True}],
        current_model="gpt-5.5",
        current_provider="openai-codex",
    )
    payload = build_components_v2_payload(card)
    selects = [c for c in _walk(payload["components"][0]) if c.get("type") == 3]

    assert selects
    assert selects[0]["placeholder"] == "Choose a provider"
    assert selects[0]["options"][0]["label"] == "Codex"


def test_model_picker_prioritizes_current_provider_when_truncated():
    providers = [
        {"slug": f"p{idx}", "name": f"Provider {idx}", "is_current": False}
        for idx in range(30)
    ]
    providers[-1]["is_current"] = True
    card = model_picker_card(
        providers=providers,
        current_model="m",
        current_provider="Provider 29",
    )
    payload = build_components_v2_payload(card)
    selects = [c for c in _walk(payload["components"][0]) if c.get("type") == 3]
    text = "\n".join(c.get("content", "") for c in _walk(payload["components"][0]))

    assert selects[0]["options"][0]["label"] == "Provider 29"
    assert "Showing 25 of 30 providers" in text


def test_long_prompt_cards_include_truncation_notice():
    card = clarify_prompt_card("x" * 3000)
    assert "truncated for Discord UI" in card.body


def test_link_buttons_use_url_not_custom_id():
    from plugins.platforms.discord.rich_ui import DiscordRichAction

    button = DiscordRichAction("ignored", "Open", style="link", url="https://example.com").to_component()

    assert button["url"] == "https://example.com"
    assert "custom_id" not in button


def test_run_status_and_error_cards_are_available():
    run_payload = build_components_v2_payload(run_status_card("run-1", "running", "terminal"))
    err_payload = build_components_v2_payload(error_card("boom"))

    assert run_payload["components"][0]["accent_color"]
    assert err_payload["components"][0]["accent_color"]
    assert COPY_UNAUTHORIZED == "You don’t have permission to use this control."
