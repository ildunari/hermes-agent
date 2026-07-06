import json

from tools.tts_text_formatter import (
    deterministic_spoken_cleanup,
    model_spoken_rewrite,
    prepare_spoken_text,
)


def test_deterministic_cleanup_removes_media_paths_and_generated_filenames():
    sample = """Fixed and verified.

- provider: `chatterbox-turbo`
- output: `/Users/Kosta/.hermes/profiles/coding/audio_cache/tts_20260701_075402.ogg`
- voice-compatible delivery worked: `[[audio_as_voice]]`
MEDIA:/Users/Kosta/.hermes/profiles/coding/audio_cache/tts_20260701_075402.ogg
- generated 10.0 seconds of audio in 2.69 seconds wall time

So yes: Chatterbox Turbo is now the active path, and the timeout is longer."""

    spoken = deterministic_spoken_cleanup(sample)

    assert "Chatterbox Turbo" in spoken
    assert "10.0 seconds" in spoken
    assert "2.69 seconds" in spoken
    assert "/Users/Kosta" not in spoken
    assert "tts_20260701_075402" not in spoken
    assert "MEDIA:" not in spoken
    assert "[[audio_as_voice]]" not in spoken


def test_prepare_spoken_text_rewrite_off_uses_deterministic_cleanup():
    spoken = prepare_spoken_text("- output: `/Users/Kosta/file.mp3`", rewrite="off")

    assert spoken == "output: the generated file"


def test_model_rewrite_uses_best_candidate_and_cleans_it():
    calls = []

    def fake_runner(name, cmd, timeout):
        calls.append((name, timeout))
        if name == "codex-spark":
            return {"ok": True, "text": "MEDIA:/Users/Kosta/bad.mp3", "score": -50}
        return {"ok": True, "text": "The audio path is fixed and now uses Chatterbox Turbo.", "score": 20}

    spoken = model_spoken_rewrite("raw text", runner=fake_runner)

    assert calls
    assert spoken == "The audio path is fixed and now uses Chatterbox Turbo."


def test_prepare_spoken_text_falls_back_when_model_route_unavailable():
    def fake_runner(name, cmd, timeout):
        return {"ok": False, "text": "", "score": -100, "error": "timeout"}

    spoken = prepare_spoken_text(
        "- MEDIA:/Users/Kosta/.hermes/audio_cache/tts_20260701_075402.ogg\n- timeout: `600s`",
        source="read-aloud",
        rewrite="on",
        model_enabled=True,
        runner=fake_runner,
    )

    assert "600s" in spoken
    assert "MEDIA:" not in spoken
    assert "/Users/Kosta" not in spoken


def test_prepare_spoken_text_auto_is_deterministic_unless_model_enabled():
    def fail_runner(name, cmd, timeout):
        raise AssertionError("model formatter should be config-gated")

    spoken = prepare_spoken_text(
        "- output: `/Users/Kosta/file.mp3`",
        source="read-aloud",
        rewrite="auto",
        runner=fail_runner,
    )

    assert spoken == "output: the generated file"


def test_empty_input_stays_empty():
    assert deterministic_spoken_cleanup("   \n\n") == ""
    assert prepare_spoken_text("```python\nprint('x')\n```", rewrite="off") == ""
