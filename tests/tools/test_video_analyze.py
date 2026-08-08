"""Tests for video_analyze tool in tools/vision_tools.py."""

import asyncio
import base64
import inspect
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest


from tools.vision_tools import (
    _detect_video_mime_type,
    _gemini_video_api_key,
    _prepare_video_for_analysis,
    _redact_video_source,
    _video_analysis_limits,
    _video_to_base64_data_url,
    _handle_video_analyze,
    _MAX_VIDEO_BASE64_BYTES,
    video_analyze_tool,
    VIDEO_ANALYZE_SCHEMA,
)


# ---------------------------------------------------------------------------
# _detect_video_mime_type
# ---------------------------------------------------------------------------


class TestDetectVideoMimeType:
    """Extension-based MIME detection for video files."""

    def test_mp4(self, tmp_path):
        p = tmp_path / "clip.mp4"
        p.write_bytes(b"\x00" * 10)
        assert _detect_video_mime_type(p) == "video/mp4"

    def test_webm(self, tmp_path):
        p = tmp_path / "clip.webm"
        p.write_bytes(b"\x00" * 10)
        assert _detect_video_mime_type(p) == "video/webm"


    def test_case_insensitive(self, tmp_path):
        p = tmp_path / "clip.MP4"
        p.write_bytes(b"\x00" * 10)
        assert _detect_video_mime_type(p) == "video/mp4"

    def test_mov(self, tmp_path):
        p = tmp_path / "clip.mov"
        p.write_bytes(b"\x00" * 10)
        assert _detect_video_mime_type(p) == "video/quicktime"

    def test_avi_mime(self, tmp_path):
        p = tmp_path / "clip.avi"
        p.write_bytes(b"\x00" * 10)
        assert _detect_video_mime_type(p) == "video/x-msvideo"

    def test_mkv_mime(self, tmp_path):
        p = tmp_path / "clip.mkv"
        p.write_bytes(b"\x00" * 10)
        assert _detect_video_mime_type(p) == "video/x-matroska"


# ---------------------------------------------------------------------------
# _video_to_base64_data_url
# ---------------------------------------------------------------------------


class TestVideoToBase64DataUrl:
    """Base64 encoding of video files."""

    def test_produces_data_url(self, tmp_path):
        p = tmp_path / "test.mp4"
        p.write_bytes(b"\x00\x01\x02\x03")
        result = _video_to_base64_data_url(p)
        assert result.startswith("data:video/mp4;base64,")


    def test_default_mime_for_unknown_ext(self, tmp_path):
        p = tmp_path / "test.xyz"
        p.write_bytes(b"\x00\x01\x02\x03")
        result = _video_to_base64_data_url(p)
        # Falls back to video/mp4
        assert result.startswith("data:video/mp4;base64,")


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------


class TestVideoAnalyzeSchema:
    """Schema structure is correct."""

    def test_schema_name(self):
        assert VIDEO_ANALYZE_SCHEMA["name"] == "video_analyze"


    def test_schema_description_mentions_video(self):
        assert "video" in VIDEO_ANALYZE_SCHEMA["description"].lower()


# ---------------------------------------------------------------------------
# _handle_video_analyze handler
# ---------------------------------------------------------------------------


class TestHandleVideoAnalyze:
    """Tests for the registry handler wrapper."""

    def test_returns_awaitable(self, tmp_path, monkeypatch):
        video_file = tmp_path / "test.mp4"
        video_file.write_bytes(b"\x00" * 100)
        monkeypatch.setenv("AUXILIARY_VIDEO_MODEL", "")
        monkeypatch.setenv("AUXILIARY_VISION_MODEL", "")

        with patch("tools.vision_tools.video_analyze_tool", new_callable=AsyncMock) as mock_tool:
            mock_tool.return_value = json.dumps({"success": True, "analysis": "test"})
            result = _handle_video_analyze({"video_url": str(video_file), "question": "what is this?"})
            # Should return an awaitable (coroutine)
            assert asyncio.iscoroutine(result)
            # Clean up the unawaited coroutine
            result.close()


    def test_falls_back_to_vision_model_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AUXILIARY_VIDEO_MODEL", "")
        monkeypatch.setenv("AUXILIARY_VISION_MODEL", "google/gemini-flash")

        with patch("tools.vision_tools.video_analyze_tool", new_callable=AsyncMock) as mock_tool:
            mock_tool.return_value = json.dumps({"success": True, "analysis": "ok"})
            asyncio.get_event_loop().run_until_complete(
                _handle_video_analyze({"video_url": "/tmp/test.mp4", "question": "test"})
            )
            args = mock_tool.call_args[0]
            assert args[2] == "google/gemini-flash"


# ---------------------------------------------------------------------------
# video_analyze_tool — integration-style tests with mocked LLM
# ---------------------------------------------------------------------------


class TestVideoAnalysisLimits:
    def test_gemini_api_key_alias_is_supported(self, monkeypatch):
        monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
        monkeypatch.setenv("GEMINI_API_KEY", "alias-key")
        assert _gemini_video_api_key() == "alias-key"

    def test_signed_url_is_redacted_for_logs(self):
        redacted = _redact_video_source(
            "https://user:pass@cdn.example/video.mp4?token=SUPERSECRET#frag"
        )
        assert redacted == "https://cdn.example/video.mp4"
        assert "SUPERSECRET" not in redacted
        assert "user" not in redacted

    def test_non_finite_values_fall_back_to_safe_defaults(self):
        config = {
            "video_analysis": {
                "max_input_mb": float("nan"),
                "compression_target_mb": float("inf"),
                "max_download_mb": float("-inf"),
            }
        }
        with patch("hermes_cli.config.load_config", return_value=config):
            assert _video_analysis_limits() == (
                100 * 1024 * 1024,
                96 * 1024 * 1024,
                2 * 1024 * 1024 * 1024,
            )

class TestVideoAnalyzeTool:
    """Core video analysis function tests."""

    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    def test_local_file_success(self, tmp_path, monkeypatch):
        """Analyze a local video file — happy path."""
        video = tmp_path / "demo.mp4"
        video.write_bytes(b"\x00" * 1024)

        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "A short video showing a demo."

        with patch("tools.vision_tools.async_call_llm", new_callable=AsyncMock, return_value=mock_response):
            with patch("tools.vision_tools.extract_content_or_reasoning", return_value="A short video showing a demo."):
                result = self._run(video_analyze_tool(str(video), "What is this?"))

        data = json.loads(result)
        assert data["success"] is True
        assert "demo" in data["analysis"].lower()

    def test_local_file_read_guard_blocks_env_via_video_extension(self, tmp_path):
        """A .env file symlinked with a video extension must still be blocked.

        _detect_video_mime_type only checks the file extension, not file
        content, so without a read guard a model could point video_url at
        any credential-store file (renamed/symlinked to look like a video)
        and have its raw bytes base64-encoded and sent to the vision
        provider. Regression for the shared agent.file_safety chokepoint
        added to video_analyze_tool's local-file branch.
        """
        secret = tmp_path / ".env"
        secret.write_text("OPENAI_API_KEY=sk-super-secret\n", encoding="utf-8")
        disguised = tmp_path / "video.mp4"
        disguised.symlink_to(secret)

        with patch("tools.vision_tools.async_call_llm", new_callable=AsyncMock) as mock_llm:
            result = self._run(video_analyze_tool(str(disguised), "What is this?"))

        data = json.loads(result)
        assert data["success"] is False
        assert "secret-bearing environment file" in data["error"]
        mock_llm.assert_not_awaited()


    def test_unsupported_format(self, tmp_path):
        """Unsupported extension raises error."""
        video = tmp_path / "clip.flv"
        video.write_bytes(b"\x00" * 100)

        result = self._run(video_analyze_tool(str(video), "What is this?"))
        data = json.loads(result)
        assert data["success"] is False
        assert "unsupported video format" in data["analysis"].lower()


    def test_api_message_format(self, tmp_path):
        """Verify Gemini Files references are sent without inline base64 copies."""
        video = tmp_path / "test.mp4"
        video.write_bytes(b"\x00" * 100)

        captured_kwargs = {}

        async def capture_llm(**kwargs):
            captured_kwargs.update(kwargs)
            mock_response = MagicMock()
            mock_response.choices = [MagicMock()]
            mock_response.choices[0].message.content = "OK"
            return mock_response

        with patch("tools.vision_tools.async_call_llm", side_effect=capture_llm):
            with patch("tools.vision_tools.extract_content_or_reasoning", return_value="OK"):
                self._run(video_analyze_tool(str(video), "Describe this"))

        messages = captured_kwargs["messages"]
        assert len(messages) == 1
        content = messages[0]["content"]
        assert len(content) == 2
        assert content[0]["type"] == "text"
        assert content[1]["type"] == "video_file"
        assert content[1]["video_file"] == {
            "uri": "https://files.example/video",
            "mime_type": "video/mp4",
        }
        self.mock_upload.assert_awaited_once_with(video, "video/mp4", "")
        self.mock_delete.assert_awaited_once_with("files/test-video", "")

    def setup_method(self):
        self._upload_patcher = patch(
            "tools.vision_tools._upload_video_to_gemini",
            new=AsyncMock(return_value=("https://files.example/video", "files/test-video")),
        )
        self._delete_patcher = patch(
            "tools.vision_tools._delete_gemini_file", new=AsyncMock()
        )
        self.mock_upload = self._upload_patcher.start()
        self.mock_delete = self._delete_patcher.start()

    def teardown_method(self):
        self._delete_patcher.stop()
        self._upload_patcher.stop()

    def test_under_limit_is_never_compressed(self, tmp_path):
        video = tmp_path / "under-limit.mp4"
        video.write_bytes(b"x" * 128)

        with patch("tools.vision_tools._video_analysis_limits", return_value=(256, 240, 4096)), \
             patch("tools.vision_tools._compress_video_for_analysis") as compress:
            prepared, cleanup = self._run(_prepare_video_for_analysis(video))

        assert prepared == video
        assert cleanup is False
        compress.assert_not_called()

    def test_exact_limit_is_not_compressed(self, tmp_path):
        video = tmp_path / "at-limit.mp4"
        video.write_bytes(b"x" * 256)

        with patch("tools.vision_tools._video_analysis_limits", return_value=(256, 240, 4096)), \
             patch("tools.vision_tools._compress_video_for_analysis") as compress:
            prepared, cleanup = self._run(_prepare_video_for_analysis(video))

        assert prepared == video
        assert cleanup is False
        compress.assert_not_called()

    def test_over_limit_uses_temporary_derivative_and_preserves_original(self, tmp_path):
        video = tmp_path / "over-limit.mp4"
        original = b"original-video-bytes"
        video.write_bytes(original)
        derivative = tmp_path / "compressed.mp4"
        derivative.write_bytes(b"small")

        with patch("tools.vision_tools._video_analysis_limits", return_value=(8, 7, 4096)), \
             patch("tools.vision_tools._compress_video_for_analysis", return_value=derivative) as compress:
            prepared, cleanup = self._run(_prepare_video_for_analysis(video))

        assert prepared == derivative
        assert cleanup is True
        assert video.read_bytes() == original
        compress.assert_called_once_with(video, 7)

    def test_compressor_output_must_fit_target(self, tmp_path):
        video = tmp_path / "over-limit.mp4"
        video.write_bytes(b"x" * 9)
        derivative = tmp_path / "still-too-large.mp4"
        derivative.write_bytes(b"x" * 8)

        with patch("tools.vision_tools._video_analysis_limits", return_value=(8, 7, 4096)), \
             patch("tools.vision_tools._compress_video_for_analysis", return_value=derivative):
            try:
                self._run(_prepare_video_for_analysis(video))
            except ValueError as exc:
                assert "could not be reduced" in str(exc).lower()
            else:
                raise AssertionError("oversized derivative should be rejected")

    def test_video_calls_dedicated_video_auxiliary_route(self, tmp_path):
        video = tmp_path / "test.mp4"
        video.write_bytes(b"x" * 100)
        captured = {}

        async def capture_llm(**kwargs):
            captured.update(kwargs)
            response = MagicMock()
            response.choices = [MagicMock()]
            response.choices[0].message.content = "OK"
            return response

        with patch("tools.vision_tools.async_call_llm", side_effect=capture_llm), \
             patch("tools.vision_tools.extract_content_or_reasoning", return_value="OK"):
            self._run(video_analyze_tool(str(video), "Describe"))

        assert captured["task"] == "video"

    def test_signed_remote_url_is_redacted_before_policy_checks(self):
        signed = "https://cdn.example/video.mp4?X-Amz-Signature=SUPERSECRET"
        with patch("tools.vision_tools._validate_image_url_async", new=AsyncMock(return_value=True)), \
             patch("tools.vision_tools.check_website_access", return_value=None) as policy, \
             patch("tools.vision_tools._download_video", new=AsyncMock(side_effect=ValueError("stop"))):
            self._run(video_analyze_tool(signed, "What?"))

        checked_urls = [call.args[0] for call in policy.call_args_list]
        assert checked_urls
        assert all("SUPERSECRET" not in value for value in checked_urls)
        assert checked_urls[0] == "https://cdn.example/video.mp4"

    def test_video_uses_files_api_without_base64_allocation(self, tmp_path):
        video = tmp_path / "large-enough-to-matter.mp4"
        video.write_bytes(b"\x00" * 100)
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "OK"

        with patch("tools.vision_tools._video_to_base64_data_url") as inline_encode, \
             patch("tools.vision_tools.async_call_llm", new_callable=AsyncMock, return_value=mock_response), \
             patch("tools.vision_tools.extract_content_or_reasoning", return_value="OK"):
            result = self._run(video_analyze_tool(str(video), "What?"))

        assert json.loads(result)["success"] is True
        inline_encode.assert_not_called()
        self.mock_upload.assert_awaited_once()

    def test_non_local_backend_reads_video_from_terminal_backend(self, tmp_path, monkeypatch):
        """Non-local terminal backends upload sandbox bytes, never host bytes."""
        if "task_id" not in inspect.signature(video_analyze_tool).parameters:
            pytest.skip("requires the incoming terminal-backend task routing")

        host_video = tmp_path / "clip.mp4"
        host_video.write_bytes(b"HOST-VIDEO")
        remote_bytes = b"REMOTE-SANDBOX-VIDEO"
        remote_b64 = base64.b64encode(remote_bytes).decode("ascii")
        monkeypatch.setenv("TERMINAL_ENV", "docker")
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))

        import tools.image_source as isrc
        import tools.terminal_tool as tt

        env_lookups = []

        def fake_get_active(task_id):
            env_lookups.append(task_id)
            return SimpleNamespace(
                execute=lambda cmd, **kw: {"returncode": 0, "output": remote_b64}
            )

        monkeypatch.setattr(tt, "ensure_task_env", lambda *a, **k: None)
        monkeypatch.setattr(isrc, "_get_active_env", fake_get_active)

        captured_kwargs = {}
        uploaded_payload = {}

        async def capture_upload(path, mime_type, api_key):
            uploaded_payload["bytes"] = path.read_bytes()
            uploaded_payload["mime_type"] = mime_type
            uploaded_payload["api_key"] = api_key
            return "https://files.example/video", "files/test-video"

        self.mock_upload.side_effect = capture_upload

        async def capture_llm(**kwargs):
            captured_kwargs.update(kwargs)
            mock_response = MagicMock()
            mock_response.choices = [MagicMock()]
            mock_response.choices[0].message.content = "sandbox video"
            return mock_response

        with (
            patch("tools.vision_tools.async_call_llm", side_effect=capture_llm),
            patch("tools.vision_tools.extract_content_or_reasoning", return_value="sandbox video"),
        ):
            result = self._run(
                video_analyze_tool(str(host_video), "Describe this", task_id="task-123")
            )

        data = json.loads(result)
        assert data["success"] is True
        assert env_lookups == ["task-123"]
        assert captured_kwargs["messages"][0]["content"][1]["video_file"] == {
            "uri": "https://files.example/video",
            "mime_type": "video/mp4",
        }
        assert uploaded_payload["bytes"] == remote_bytes
        assert uploaded_payload["bytes"] != host_video.read_bytes()
        assert uploaded_payload["mime_type"] == "video/mp4"
        assert uploaded_payload["api_key"] == ""


# ---------------------------------------------------------------------------
# Toolset registration
# ---------------------------------------------------------------------------


class TestVideoAuxiliaryRouting:
    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    def test_rejects_non_gemini_provider_before_dispatch(self):
        from agent.auxiliary_client import async_call_llm

        with patch("agent.auxiliary_client._get_cached_client") as get_client:
            try:
                self._run(async_call_llm(
                    task="video",
                    provider="xai-oauth",
                    model="grok-4.5",
                    messages=[{"role": "user", "content": "video"}],
                ))
            except RuntimeError as exc:
                assert "requires auxiliary.video.provider: gemini" in str(exc)
            else:
                raise AssertionError("xAI video route should have been rejected")
        get_client.assert_not_called()


    def test_native_gemini_failure_never_enters_generic_fallback(self):
        from agent.auxiliary_client import async_call_llm
        from agent.gemini_native_adapter import AsyncGeminiNativeClient, GeminiNativeClient

        client = AsyncGeminiNativeClient(GeminiNativeClient(api_key="test-key"))
        client.chat.completions.create = AsyncMock(
            side_effect=httpx.ConnectError("offline")
        )
        with patch("agent.auxiliary_client._get_cached_client", return_value=(client, "gemini-3.5-flash")), \
             patch("agent.auxiliary_client._try_configured_fallback_chain") as configured_fb, \
             patch("agent.auxiliary_client._try_main_agent_model_fallback") as main_fb, \
             patch("agent.auxiliary_client._try_payment_fallback") as payment_fb:
            try:
                self._run(async_call_llm(
                    task="video",
                    provider="gemini",
                    model="gemini-3.5-flash",
                    messages=[{"role": "user", "content": "video"}],
                ))
            except httpx.ConnectError:
                pass
            else:
                raise AssertionError("native Gemini failure should be returned directly")
        configured_fb.assert_not_called()
        main_fb.assert_not_called()
        payment_fb.assert_not_called()

class TestVideoToolsetRegistration:
    """Verify the tool is registered correctly."""

    def test_registered_in_video_toolset(self):
        from tools.registry import registry
        entry = registry.get_entry("video_analyze")
        assert entry is not None
        assert entry.toolset == "video"
        assert entry.is_async is True
        assert entry.emoji == "🎬"


    def test_in_video_toolset_definition(self):
        """Toolset 'video' should contain video_analyze."""
        from toolsets import TOOLSETS
        assert "video" in TOOLSETS
        assert "video_analyze" in TOOLSETS["video"]["tools"]
