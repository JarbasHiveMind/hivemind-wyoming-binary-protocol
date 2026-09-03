"""Live round trip against real Wyoming ASR + TTS servers.

Skipped unless the servers are reachable, so it never runs in the default suite
(``pyproject.toml`` also ``--ignore=tests/e2e``). Point it at the docker-compose
stack in the repo root:

    docker compose up -d          # wyoming-faster-whisper :10300, wyoming-piper :10200
    WYOMING_ASR_URI=tcp://127.0.0.1:10300 \
    WYOMING_TTS_URI=tcp://127.0.0.1:10200 \
        pytest tests/e2e -v

The TTS→ASR loop is a genuine end-to-end proof: synthesize a phrase with Piper,
feed the audio straight into faster-whisper, and check the transcript contains
the words. It needs no HiveMind topology — it validates the Wyoming backend the
plugin relies on.
"""
import os
import socket
import wave

import pytest

from hivemind_wyoming_binary_protocol.client import (wyoming_synthesize,
                                                     wyoming_transcribe)

ASR_URI = os.environ.get("WYOMING_ASR_URI", "tcp://127.0.0.1:10300")
TTS_URI = os.environ.get("WYOMING_TTS_URI", "tcp://127.0.0.1:10200")


def _reachable(uri: str) -> bool:
    try:
        host, port = uri.replace("tcp://", "").split(":")
        with socket.create_connection((host, int(port)), timeout=2):
            return True
    except OSError:
        return False


asr_up = pytest.mark.skipif(not _reachable(ASR_URI),
                            reason=f"no Wyoming ASR server at {ASR_URI}")
tts_up = pytest.mark.skipif(not _reachable(TTS_URI),
                            reason=f"no Wyoming TTS server at {TTS_URI}")


@tts_up
def test_tts_returns_audio():
    audio = wyoming_synthesize(TTS_URI, "hello world")
    assert audio is not None and audio.audio, "Piper returned no audio"
    assert audio.rate > 0 and audio.width in (1, 2)


@asr_up
@tts_up
def test_tts_then_asr_round_trip():
    audio = wyoming_synthesize(TTS_URI, "turn on the kitchen lights")
    assert audio is not None

    # faster-whisper expects 16 kHz 16-bit mono; resample Piper output if needed
    pcm, rate, width, channels = audio.audio, audio.rate, audio.width, audio.channels
    if (rate, width, channels) != (16000, 2, 1):
        import audioop
        if channels != 1:
            pcm = audioop.tomono(pcm, width, 0.5, 0.5)
        if width != 2:
            pcm = audioop.lin2lin(pcm, width, 2)
            width = 2
        if rate != 16000:
            pcm, _ = audioop.ratecv(pcm, width, 1, rate, 16000, None)
            rate = 16000

    text = wyoming_transcribe(ASR_URI, pcm, rate, width, 1, "en")
    assert text, "faster-whisper returned no transcript"
    assert "light" in text.lower(), f"unexpected transcript: {text!r}"
