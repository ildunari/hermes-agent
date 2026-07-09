from tools.voice_mode import AudioRecorder


def test_effective_threshold_rises_with_noise_floor():
    rec = AudioRecorder()
    rec._silence_threshold = 200
    rec._adaptive_threshold_margin = 160
    rec._adaptive_threshold_multiplier = 2.2
    rec._noise_floor_rms = 300.0

    assert rec._effective_speech_threshold() == 660


def test_noise_floor_updates_only_for_subthreshold_audio():
    rec = AudioRecorder()
    rec._noise_floor_smoothing = 0.5
    rec._noise_floor_rms = 200.0

    rec._update_noise_floor(150, threshold=300)
    assert rec._noise_floor_rms == 175.0

    rec._update_noise_floor(500, threshold=300)
    assert rec._noise_floor_rms == 175.0


def test_quiet_recording_uses_effective_threshold(monkeypatch):
    rec = AudioRecorder()
    rec._recording = True
    rec._frames = [object()]
    rec._start_time = 0.0
    rec._peak_rms = 500
    rec._silence_threshold = 200
    rec._adaptive_threshold_margin = 160
    rec._adaptive_threshold_multiplier = 2.2
    rec._noise_floor_rms = 300.0

    monkeypatch.setattr('tools.voice_mode.time.monotonic', lambda: 1.0)

    class NPStub:
        @staticmethod
        def concatenate(frames, axis=0):
            return [1] * 10000

    monkeypatch.setattr('tools.voice_mode._import_audio', lambda: (None, NPStub()))
    called = {"value": False}

    def _unexpected_write(_audio_data):
        called["value"] = True
        return '/tmp/should-not-write.wav'

    monkeypatch.setattr(rec, '_write_wav', _unexpected_write)

    assert rec.stop() is None
    assert called["value"] is False
