"""Tests for agent.title_generator — auto-generated session titles."""

from unittest.mock import MagicMock, patch


from agent.title_generator import (
    generate_title,
    auto_title_session,
    maybe_auto_title,
    _title_language,
    _title_transcript_context,
    _is_prompt_fragment_title,
    _clean_generated_title,
)


class TestGenerateTitle:
    """Unit tests for generate_title()."""

    def test_returns_title_on_success(self):
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "Debugging Python Import Errors"

        with patch("agent.title_generator.call_llm", return_value=mock_response):
            title = generate_title("help me fix this import", "Sure, let me check...")
            assert title == "Debugging Python Import Errors"

    def test_default_prompt_matches_user_language(self):
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "Some Title"

        with patch("agent.title_generator.call_llm", return_value=mock_response) as llm:
            generate_title("質問です", "回答です")

        system_prompt = llm.call_args.kwargs["messages"][0]["content"]
        assert "session label (3-6 words)" in system_prompt
        assert "sidebar/history list" in system_prompt
        assert "object + action" in system_prompt
        assert "plain user-facing words" in system_prompt
        assert "same language the user is writing in" in system_prompt

    def test_configured_language_pins_prompt(self):
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "Some Title"

        with (
            patch("agent.title_generator.call_llm", return_value=mock_response) as llm,
            patch("agent.title_generator._title_language", return_value="Japanese"),
        ):
            generate_title("hello", "hi")

        system_prompt = llm.call_args.kwargs["messages"][0]["content"]
        assert "session label (3-6 words)" in system_prompt
        assert "sidebar/history list" in system_prompt
        assert "Write the label in Japanese" in system_prompt
        assert "same language the user" not in system_prompt

    def test_title_language_reads_config(self):
        cfg = {"auxiliary": {"title_generation": {"language": "  French "}}}

        with patch("hermes_cli.config.load_config", return_value=cfg):
            assert _title_language() == "French"
        with patch("hermes_cli.config.load_config", return_value={}):
            assert _title_language() == ""
        with patch("hermes_cli.config.load_config", side_effect=RuntimeError("bad config")):
            assert _title_language() == ""

    def test_default_timeout_delegates_to_auxiliary_config(self):
        captured_kwargs = {}

        def mock_call_llm(**kwargs):
            captured_kwargs.update(kwargs)
            resp = MagicMock()
            resp.choices = [MagicMock()]
            resp.choices[0].message.content = "Configured Timeout"
            return resp

        with patch("agent.title_generator.call_llm", side_effect=mock_call_llm):
            assert generate_title("question", "answer") == "Configured Timeout"

        assert captured_kwargs["task"] == "title_generation"
        assert captured_kwargs["timeout"] is None

    def test_explicit_timeout_still_overrides_config(self):
        captured_kwargs = {}

        def mock_call_llm(**kwargs):
            captured_kwargs.update(kwargs)
            resp = MagicMock()
            resp.choices = [MagicMock()]
            resp.choices[0].message.content = "Explicit Timeout"
            return resp

        with patch("agent.title_generator.call_llm", side_effect=mock_call_llm):
            assert generate_title("question", "answer", timeout=123.0) == "Explicit Timeout"

        assert captured_kwargs["timeout"] == 123.0

    def test_strips_quotes(self):
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = '"Setting Up Docker Environment"'

        with patch("agent.title_generator.call_llm", return_value=mock_response):
            title = generate_title("how do I set up docker", "First install...")
            assert title == "Setting Up Docker Environment"

    def test_strips_think_blocks(self):
        """Reasoning-model output wrapped in <think>...</think> must not
        leak into the session title."""
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = (
            "<think>The user wants a title. I'll summarize the topic "
            "concisely.</think>Debugging Python Import Errors"
        )

        with patch("agent.title_generator.call_llm", return_value=mock_response):
            title = generate_title("help me fix this import", "Sure...")
            assert title == "Debugging Python Import Errors"
            assert "<think>" not in title
            assert "summarize" not in title

    def test_strips_unterminated_think_block(self):
        """An unterminated <think> block (no close tag) must still be
        stripped so the leaked reasoning doesn't become the title."""
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = (
            "<think>Let me reason about a good title for this session"
        )

        with patch("agent.title_generator.call_llm", return_value=mock_response):
            title = generate_title("hello", "hi there")
            # Everything from the unterminated open tag onward is stripped,
            # leaving nothing → None.
            assert title is None

    def test_strips_title_prefix(self):
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "Title: Kubernetes Pod Debugging"

        with patch("agent.title_generator.call_llm", return_value=mock_response):
            title = generate_title("my pod keeps crashing", "Let me look...")
            assert title == "Kubernetes Pod Debugging"

    def test_strips_label_prefix(self):
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "Label: Hermes Settings Backend"

        with patch("agent.title_generator.call_llm", return_value=mock_response):
            title = generate_title("fix the Hermes settings backend", "I found the config issue...")
            assert title == "Hermes Settings Backend"

    def test_strips_session_label_prefix(self):
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "Session label: Hermes Settings Backend"

        with patch("agent.title_generator.call_llm", return_value=mock_response):
            title = generate_title("fix the Hermes settings backend", "I found the config issue...")
            assert title == "Hermes Settings Backend"

    def test_truncates_long_titles(self):
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "A" * 100

        with patch("agent.title_generator.call_llm", return_value=mock_response):
            title = generate_title("question", "answer")
            assert len(title) == 80
            assert title.endswith("...")

    def test_rewrites_common_internal_jargon_titles(self):
        assert _clean_generated_title("Hermes Pre-Prompt Memory Injection") == "Hermes memory prompt setup"
        assert _clean_generated_title("Dynamic Tool Summary Automation") == "tool summary job"
        assert _clean_generated_title("Casual Greeting to Kosta") == "quick greeting"
        assert _clean_generated_title("Carlos Extraction and Setup Review") == "Carlos setup review"

    def test_returns_none_on_empty_response(self):
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = ""

        with patch("agent.title_generator.call_llm", return_value=mock_response):
            assert generate_title("question", "answer") is None

    def test_returns_none_on_exception(self):
        with patch("agent.title_generator.call_llm", side_effect=RuntimeError("no provider")):
            assert generate_title("question", "answer") is None

    def test_invokes_failure_callback_on_exception(self):
        """failure_callback must fire so the user sees a warning (issue #15775)."""
        captured = []

        def _cb(task, exc):
            captured.append((task, exc))

        exc = RuntimeError("openrouter 402: credits exhausted")
        with patch("agent.title_generator.call_llm", side_effect=exc):
            result = generate_title("question", "answer", failure_callback=_cb)

        assert result is None
        assert len(captured) == 1
        assert captured[0][0] == "title generation"
        assert captured[0][1] is exc

    def test_failure_callback_errors_are_swallowed(self):
        """A broken callback must not crash title generation."""

        def _bad_cb(task, exc):
            raise ValueError("callback bug")

        with patch("agent.title_generator.call_llm", side_effect=RuntimeError("nope")):
            # Should return None without re-raising the callback error
            assert generate_title("q", "a", failure_callback=_bad_cb) is None

    def test_no_callback_matches_legacy_behavior(self):
        """Omitting failure_callback preserves the silent-None return."""
        with patch("agent.title_generator.call_llm", side_effect=RuntimeError("nope")):
            assert generate_title("q", "a") is None

    def test_truncates_long_messages(self):
        """Long user/assistant messages should be truncated in the LLM request."""
        captured_kwargs = {}

        def mock_call_llm(**kwargs):
            captured_kwargs.update(kwargs)
            resp = MagicMock()
            resp.choices = [MagicMock()]
            resp.choices[0].message.content = "Short Title"
            return resp

        long_user = "x" * 2000
        long_assistant = "y" * 2000
        with patch("agent.title_generator.call_llm", side_effect=mock_call_llm):
            generate_title(long_user, long_assistant)

        # Keep substantially more than the old 500-char snippets, but still cap
        # the auxiliary request.
        user_content = captured_kwargs["messages"][1]["content"]
        assert "x" * 1000 in user_content
        assert "y" * 1000 in user_content
        assert "x" * 1300 not in user_content
        assert len(user_content) < 3000

    def test_uses_early_transcript_not_only_latest_turn(self):
        """Retries should see the real session topic, not only the latest prompt."""
        captured_kwargs = {}

        def mock_call_llm(**kwargs):
            captured_kwargs.update(kwargs)
            resp = MagicMock()
            resp.choices = [MagicMock()]
            resp.choices[0].message.content = "MoA VibeProxy Routing Debug"
            return resp

        history = [
            {"role": "user", "content": "What’s going on. Is it an issue with the moa model?"},
            {"role": "assistant", "content": "I’ll check the screenshot first."},
            {"role": "user", "content": "The actual issue is VibeProxy returning 502 for xhigh reasoning."},
            {"role": "assistant", "content": "The fix is nested reasoning.effort for VibeProxy."},
        ]

        with patch("agent.title_generator.call_llm", side_effect=mock_call_llm):
            title = generate_title(
                "The actual issue is VibeProxy returning 502 for xhigh reasoning.",
                "The fix is nested reasoning.effort for VibeProxy.",
                conversation_history=history,
            )

        assert title == "MoA VibeProxy Routing Debug"
        user_content = captured_kwargs["messages"][1]["content"]
        assert "What’s going on" in user_content
        assert "VibeProxy returning 502" in user_content
        assert "nested reasoning.effort" in user_content

    def test_rejects_prompt_fragment_title(self):
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "What going"
        history = [
            {"role": "user", "content": "What’s going on. Is it an issue with the moa model?"},
            {"role": "assistant", "content": "It is a VibeProxy xhigh routing issue."},
        ]

        with patch("agent.title_generator.call_llm", return_value=mock_response):
            assert generate_title(
                "What’s going on. Is it an issue with the moa model?",
                "It is a VibeProxy xhigh routing issue.",
                conversation_history=history,
            ) is None

    def test_transcript_context_extracts_multimodal_text(self):
        context = _title_transcript_context(
            [{"type": "text", "text": "What is wrong here?"}, {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}],
            "The screenshot shows a model routing error.",
        )

        assert "What is wrong here?" in context
        assert "[image]" in context
        assert "model routing error" in context

    def test_transcript_context_skips_assistant_tool_call_preambles(self):
        history = [
            {"role": "user", "content": "What’s going on. Is it an issue with the moa model?"},
            {
                "role": "assistant",
                "content": "I’ll check the screenshot first.",
                "tool_calls": [{"id": "call_1", "type": "function"}],
            },
            {"role": "tool", "tool_name": "vision_analyze", "content": "huge screenshot analysis"},
            {"role": "assistant", "content": "The screenshot shows an active stream lock."},
            {"role": "user", "content": "Figure out what is happening with Opus and VibeProxy."},
            {
                "role": "assistant",
                "content": "I’m narrowing logs and config.",
                "tool_calls": [{"id": "call_2", "type": "function"}],
            },
            {"role": "assistant", "content": "VibeProxy rejects top-level xhigh reasoning."},
        ]

        context = _title_transcript_context(
            "Figure out what is happening with Opus and VibeProxy.",
            "VibeProxy rejects top-level xhigh reasoning.",
            history,
        )

        assert "What’s going on" in context
        assert "Opus and VibeProxy" in context
        assert "active stream lock" in context
        assert "VibeProxy rejects top-level xhigh reasoning" in context
        assert "I’ll check the screenshot" not in context
        assert "I’m narrowing logs" not in context
        assert "huge screenshot analysis" not in context


class TestAutoTitleSession:
    """Tests for auto_title_session() — the sync worker function."""

    def test_skips_if_no_session_db(self):
        auto_title_session(None, "sess-1", "hi", "hello")  # should not crash

    def test_skips_if_title_exists(self):
        db = MagicMock()
        db.get_session_title.return_value = "Existing Title"

        with patch("agent.title_generator.generate_title") as gen:
            auto_title_session(db, "sess-1", "hi", "hello")
            gen.assert_not_called()

    def test_generates_and_sets_title(self):
        db = MagicMock()
        db.get_session_title.return_value = None

        with patch("agent.title_generator.generate_title", return_value="New Title"):
            auto_title_session(db, "sess-1", "hi", "hello")
            db.set_session_title.assert_called_once_with("sess-1", "New Title")

    def test_replaces_prompt_fragment_title(self):
        db = MagicMock()
        db.get_session_title.return_value = "Can you check if we’re passing the sub agent sessions"
        history = [
            {
                "role": "user",
                "content": "Can you check if we’re passing the sub agent sessions to the WebUI session list?",
            },
            {"role": "assistant", "content": "I’ll inspect the session plumbing."},
            {"role": "user", "content": "Here is the follow-up screenshot."},
            {"role": "assistant", "content": "The WebUI title path needs a retry."},
        ]

        with patch("agent.title_generator.generate_title", return_value="WebUI Session Visibility Cleanup") as gen:
            auto_title_session(
                db,
                "sess-1",
                "Here is the follow-up screenshot.",
                "The WebUI title path needs a retry.",
                conversation_history=history,
            )

        assert gen.call_args.kwargs["conversation_history"] is history
        db.set_session_title.assert_called_once_with(
            "sess-1", "WebUI Session Visibility Cleanup"
        )

    def test_replaces_reported_what_going_title_from_webui_transcript(self):
        db = MagicMock()
        db.get_session_title.return_value = "What going"
        history = [
            {"role": "user", "content": "What’s going on. Is it an issue with the moa model?"},
            {"role": "assistant", "content": "I’ll check the screenshot first."},
            {"role": "user", "content": "It looks like VibeProxy rejects top-level xhigh reasoning."},
            {"role": "assistant", "content": "Root cause is reasoning.effort must be nested for VibeProxy."},
        ]

        with patch("agent.title_generator.generate_title", return_value="VibeProxy XHigh Routing Fix"):
            auto_title_session(
                db,
                "sess-1",
                "It looks like VibeProxy rejects top-level xhigh reasoning.",
                "Root cause is reasoning.effort must be nested for VibeProxy.",
                conversation_history=history,
            )

        db.set_session_title.assert_called_once_with("sess-1", "VibeProxy XHigh Routing Fix")

    def test_invokes_title_callback_after_setting_title(self):
        db = MagicMock()
        db.get_session_title.return_value = None
        seen = []
        with patch("agent.title_generator.generate_title", return_value="Readable Session"):
            auto_title_session(
                db,
                "sess-1",
                "hello",
                "hi there",
                title_callback=seen.append,
            )
        db.set_session_title.assert_called_once_with("sess-1", "Readable Session")
        assert seen == ["Readable Session"]

    def test_skips_if_generation_fails(self):
        db = MagicMock()
        db.get_session_title.return_value = None

        with patch("agent.title_generator.generate_title", return_value=None):
            auto_title_session(db, "sess-1", "hi", "hello")
            db.set_session_title.assert_not_called()


class TestMaybeAutoTitle:
    """Tests for maybe_auto_title() — the fire-and-forget entry point."""

    def test_skips_after_retry_window(self):
        """Should not keep firing once an untitled conversation is no longer young."""
        db = MagicMock()
        history = [
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "response 1"},
            {"role": "user", "content": "second"},
            {"role": "assistant", "content": "response 2"},
            {"role": "user", "content": "third"},
            {"role": "assistant", "content": "response 3"},
            {"role": "user", "content": "fourth"},
            {"role": "assistant", "content": "response 4"},
            {"role": "user", "content": "fifth"},
            {"role": "assistant", "content": "response 5"},
        ]

        with patch("agent.title_generator.auto_title_session") as mock_auto:
            maybe_auto_title(db, "sess-1", "fifth", "response 5", history)
            # Wait briefly for any thread to start
            import time
            time.sleep(0.1)
            mock_auto.assert_not_called()

    def test_retries_through_fourth_exchange(self):
        """A transient early title-generation failure should get a few retries."""
        db = MagicMock()
        history = [
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "response 1"},
            {"role": "user", "content": "second"},
            {"role": "assistant", "content": "response 2"},
            {"role": "user", "content": "third"},
            {"role": "assistant", "content": "response 3"},
            {"role": "user", "content": "fourth"},
            {"role": "assistant", "content": "response 4"},
        ]

        with patch("agent.title_generator.auto_title_session") as mock_auto:
            maybe_auto_title(db, "sess-1", "fourth", "response 4", history)
            import time
            time.sleep(0.3)
            mock_auto.assert_called_once()

    def test_fires_on_first_exchange(self):
        """Should fire a background thread for the first exchange."""
        db = MagicMock()
        db.get_session_title.return_value = None
        history = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi there"},
        ]

        with patch("agent.title_generator.auto_title_session") as mock_auto:
            maybe_auto_title(db, "sess-1", "hello", "hi there", history)
            # Wait for the daemon thread to complete
            import time
            time.sleep(0.3)
            mock_auto.assert_called_once_with(
                db,
                "sess-1",
                "hello",
                "hi there",
                conversation_history=history,
                failure_callback=None,
                main_runtime=None,
                title_callback=None,
            )

    def test_forwards_failure_callback_to_worker(self):
        """maybe_auto_title must forward failure_callback into the thread."""
        db = MagicMock()
        db.get_session_title.return_value = None
        history = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi there"},
        ]

        def _cb(task, exc):
            pass

        with patch("agent.title_generator.auto_title_session") as mock_auto:
            maybe_auto_title(db, "sess-1", "hello", "hi there", history, failure_callback=_cb)
            import time
            time.sleep(0.3)
            mock_auto.assert_called_once_with(
                db,
                "sess-1",
                "hello",
                "hi there",
                conversation_history=history,
                failure_callback=_cb,
                main_runtime=None,
                title_callback=None,
            )

    def test_skips_if_no_response(self):
        db = MagicMock()
        maybe_auto_title(db, "sess-1", "hello", "", [])  # empty response

    def test_skips_if_no_session_db(self):
        maybe_auto_title(None, "sess-1", "hello", "response", [])  # no db


class TestPromptFragmentDetection:
    def test_detects_literal_first_prompt_prefix(self):
        assert _is_prompt_fragment_title(
            "Can you check if we’re passing the sub agent sessions",
            "Can you check if we’re passing the sub agent sessions to the WebUI session list?",
        )

    def test_detects_prompt_fragment_with_skipped_fillers(self):
        assert _is_prompt_fragment_title(
            "Can you check passing",
            "Can you check if we’re passing the sub agent sessions to the WebUI session list?",
        )

    def test_detects_short_generic_prompt_fragment(self):
        assert _is_prompt_fragment_title(
            "Help me",
            "Help me debug why session titles are generic.",
        )

    def test_preserves_real_manual_title(self):
        assert not _is_prompt_fragment_title(
            "WebUI Session Visibility Cleanup",
            "Can you check if we’re passing the sub agent sessions to the WebUI session list?",
        )

    def test_preserves_short_non_generic_manual_title(self):
        assert not _is_prompt_fragment_title(
            "UI Fix",
            "UI Fix for the composer layout",
        )
