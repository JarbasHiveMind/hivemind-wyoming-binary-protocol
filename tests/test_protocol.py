"""Unit tests for WyomingBinaryProtocol with a mocked Wyoming backend.

No live Wyoming server is contacted: ``wyoming_transcribe`` / ``wyoming_synthesize``
are patched at the protocol module boundary, so these tests exercise the
plugin's buffering, dispatch, format-rejection, and error handling — not the
neural backends.
"""
import struct
from unittest.mock import MagicMock

import pytest
from hivemind_bus_client.message import HiveMessageType

import hivemind_wyoming_binary_protocol.protocol as protocol_module
from hivemind_wyoming_binary_protocol.protocol import (SAMPLE_RATE, SAMPLE_WIDTH,
                                                       WyomingBinaryProtocol,
                                                       pcm_to_wav)


def _make_protocol(asr_uri="tcp://asr:10300", tts_uri="tcp://tts:10200"):
    proto = object.__new__(WyomingBinaryProtocol)
    proto.asr_uri = asr_uri
    proto.tts_uri = tts_uri
    proto.tts_voice = None
    proto.sample_rate = SAMPLE_RATE
    proto.sample_width = SAMPLE_WIDTH
    proto.sample_channels = 1
    proto.refused_streams = set()
    proto.buffers = {}
    proto.hm_protocol = MagicMock()
    proto.hm_protocol.clients = {}
    return proto


def _make_client(peer="sat::1", lang="en-us"):
    client = MagicMock()
    client.peer = peer
    client.sess.lang = lang
    client.sent = []
    client.send = client.sent.append
    return client


def _bus(client):
    return [m for m in client.sent if m.msg_type == HiveMessageType.BUS]


def _rejections(client):
    return [m for m in _bus(client)
            if m.payload.msg_type == "recognizer_loop:speech.recognition.unknown"]


def _loud(n_samples):
    """Signed 16-bit PCM well above the silence threshold."""
    return struct.pack(f"<{n_samples}h", *([8000] * n_samples))


def _quiet(n_samples):
    return struct.pack(f"<{n_samples}h", *([0] * n_samples))


# ── one-shot STT: transcribe request ──────────────────────────────────────

def test_transcribe_request_sends_response(monkeypatch):
    monkeypatch.setattr(protocol_module, "wyoming_transcribe",
                        lambda *a, **k: "hello world")
    proto = _make_protocol()
    client = _make_client()

    proto.handle_stt_transcribe_request(_loud(160), SAMPLE_RATE, SAMPLE_WIDTH,
                                        "en-us", client)

    replies = [m for m in _bus(client)
               if m.payload.msg_type == "recognizer_loop:transcribe.response"]
    assert len(replies) == 1
    assert replies[0].payload.data["transcriptions"][0][0] == "hello world"


def test_transcribe_request_unsupported_format_rejected(monkeypatch):
    called = []
    monkeypatch.setattr(protocol_module, "wyoming_transcribe",
                        lambda *a, **k: called.append(1) or "x")
    proto = _make_protocol()
    client = _make_client()

    proto.handle_stt_transcribe_request(_loud(160), 44100, 2, "en-us", client)

    assert not called, "unsupported audio was sent to the ASR server"
    assert _rejections(client)


# ── one-shot STT: handle request (inject utterance) ────────────────────────

def test_handle_request_injects_utterance(monkeypatch):
    monkeypatch.setattr(protocol_module, "wyoming_transcribe",
                        lambda *a, **k: "  turn on the lights  ")
    proto = _make_protocol()
    client = _make_client()

    proto.handle_stt_handle_request(_loud(160), SAMPLE_RATE, SAMPLE_WIDTH,
                                    "en-us", client)

    proto.hm_protocol.handle_inject_agent_msg.assert_called_once()
    msg = proto.hm_protocol.handle_inject_agent_msg.call_args[0][0]
    assert msg.msg_type == "recognizer_loop:utterance"
    assert msg.data["utterances"] == ["turn on the lights"]


def test_handle_request_no_transcript_surfaces_unknown(monkeypatch):
    monkeypatch.setattr(protocol_module, "wyoming_transcribe", lambda *a, **k: None)
    proto = _make_protocol()
    client = _make_client()

    proto.handle_stt_handle_request(_loud(160), SAMPLE_RATE, SAMPLE_WIDTH,
                                    "en-us", client)

    assert not proto.hm_protocol.handle_inject_agent_msg.called
    assert _rejections(client)


# ── RAW_AUDIO streaming + silence segmentation ─────────────────────────────

def test_raw_audio_buffers_until_silence_then_injects(monkeypatch):
    seen = {}
    def fake(uri, pcm, rate, width, channels, lang=None):
        seen["pcm"] = pcm
        return "what time is it"
    monkeypatch.setattr(protocol_module, "wyoming_transcribe", fake)
    proto = _make_protocol()
    client = _make_client()

    # 500 ms of speech (8000 samples), not yet flushed
    proto.handle_microphone_input(_loud(8000), SAMPLE_RATE, SAMPLE_WIDTH, client)
    assert not proto.hm_protocol.handle_inject_agent_msg.called
    assert client.peer in proto.buffers

    # 900 ms of silence (> SILENCE_FLUSH_MS) triggers the flush
    proto.handle_microphone_input(_quiet(14400), SAMPLE_RATE, SAMPLE_WIDTH, client)

    proto.hm_protocol.handle_inject_agent_msg.assert_called_once()
    msg = proto.hm_protocol.handle_inject_agent_msg.call_args[0][0]
    assert msg.data["utterances"] == ["what time is it"]
    assert client.peer not in proto.buffers  # buffer cleared after flush
    # both loud and quiet frames were sent to ASR
    assert len(seen["pcm"]) == (8000 + 14400) * 2


def test_record_end_flushes_buffer(monkeypatch):
    monkeypatch.setattr(protocol_module, "wyoming_transcribe",
                        lambda *a, **k: "hello")
    proto = _make_protocol()
    client = _make_client(peer="sat::9")
    proto.hm_protocol.clients = {"sat::9": client}

    proto.handle_microphone_input(_loud(4000), SAMPLE_RATE, SAMPLE_WIDTH, client)
    assert not proto.hm_protocol.handle_inject_agent_msg.called

    from ovos_bus_client.message import Message
    proto.handle_record_end(Message("recognizer_loop:record_end",
                                    context={"source": "sat::9"}))

    proto.hm_protocol.handle_inject_agent_msg.assert_called_once()


def test_raw_audio_unsupported_format_refused_once(monkeypatch):
    monkeypatch.setattr(protocol_module, "wyoming_transcribe", lambda *a, **k: "x")
    proto = _make_protocol()
    client = _make_client()

    for _ in range(5):
        proto.handle_microphone_input(_loud(160), 8000, 2, client)

    assert len(_rejections(client)) == 1, "peer refused more than once per stream"
    assert client.peer not in proto.buffers


def test_raw_audio_oversized_stream_is_flushed(monkeypatch):
    monkeypatch.setattr(protocol_module, "wyoming_transcribe", lambda *a, **k: "big")
    proto = _make_protocol()
    client = _make_client()

    # 31 s of continuous speech exceeds the 30 s cap and is force-flushed
    proto.handle_microphone_input(_loud(SAMPLE_RATE * 31), SAMPLE_RATE,
                                  SAMPLE_WIDTH, client)

    proto.hm_protocol.handle_inject_agent_msg.assert_called_once()


# ── TTS ────────────────────────────────────────────────────────────────────

def test_speak_synth_returns_wav_binary(monkeypatch):
    from hivemind_wyoming_binary_protocol.client import WyomingAudio
    monkeypatch.setattr(protocol_module, "wyoming_synthesize",
                        lambda *a, **k: WyomingAudio(b"\x01\x02" * 100, 22050, 2, 1))
    proto = _make_protocol()
    client = _make_client()
    proto.hm_protocol.clients = {"sat::1": client}

    from ovos_bus_client.message import Message
    proto.handle_speak_synth(Message("speak:synth", {"utterance": "hi", "lang": "en-us"},
                                     context={"source": "sat::1"}))

    binaries = [m for m in client.sent if m.msg_type == HiveMessageType.BINARY]
    assert len(binaries) == 1
    assert binaries[0].payload[:4] == b"RIFF"


def test_speak_b64_returns_base64_wav(monkeypatch):
    import base64
    from hivemind_wyoming_binary_protocol.client import WyomingAudio
    monkeypatch.setattr(protocol_module, "wyoming_synthesize",
                        lambda *a, **k: WyomingAudio(b"\x03\x04" * 100, 22050, 2, 1))
    proto = _make_protocol()
    client = _make_client()
    proto.hm_protocol.clients = {"sat::1": client}

    from ovos_bus_client.message import Message
    proto.handle_speak_b64(Message("speak:b64_audio", {"utterance": "hi"},
                                   context={"source": "sat::1"}))

    replies = [m for m in _bus(client)
               if m.payload.msg_type == "speak:b64_audio.response"]
    assert len(replies) == 1
    decoded = base64.b64decode(replies[0].payload.data["audio"])
    assert decoded[:4] == b"RIFF"


def test_speak_synth_tts_failure_surfaces_error(monkeypatch):
    monkeypatch.setattr(protocol_module, "wyoming_synthesize", lambda *a, **k: None)
    proto = _make_protocol()
    client = _make_client()
    proto.hm_protocol.clients = {"sat::1": client}

    from ovos_bus_client.message import Message
    proto.handle_speak_synth(Message("speak:synth", {"utterance": "hi", "lang": "en-us"},
                                     context={"source": "sat::1"}))

    assert not [m for m in client.sent if m.msg_type == HiveMessageType.BINARY]
    errs = [m for m in _bus(client) if m.payload.msg_type == "speak:synth.error"]
    assert errs


# ── base64 STT over the bus ────────────────────────────────────────────────

def test_b64_transcribe_replies_to_satellite(monkeypatch):
    import base64
    monkeypatch.setattr(protocol_module, "wyoming_transcribe",
                        lambda *a, **k: "hello world")
    proto = _make_protocol()
    client = _make_client()
    proto.hm_protocol.clients = {"sat::1": client}

    from ovos_bus_client.message import Message
    b64 = base64.b64encode(pcm_to_wav(_loud(2000), SAMPLE_RATE, SAMPLE_WIDTH, 1)).decode()
    proto.handle_transcribe_b64(Message("recognizer_loop:b64_transcribe",
                                        {"audio": b64, "lang": "en-us"},
                                        context={"source": "sat::1"}))

    replies = [m for m in _bus(client)
               if m.payload.msg_type == "recognizer_loop:b64_transcribe.response"]
    assert len(replies) == 1
    assert replies[0].payload.data["transcriptions"][0][0] == "hello world"


# ── client.py error handling (no live server) ─────────────────────────────

def test_transcribe_helper_connection_error_returns_none():
    from hivemind_wyoming_binary_protocol.client import wyoming_transcribe
    # nothing is listening on this port; must degrade to None, not raise
    assert wyoming_transcribe("tcp://127.0.0.1:1", b"\x00\x01" * 100,
                              SAMPLE_RATE, SAMPLE_WIDTH) is None


def test_transcribe_helper_empty_audio_returns_none():
    from hivemind_wyoming_binary_protocol.client import wyoming_transcribe
    assert wyoming_transcribe("tcp://127.0.0.1:1", b"", SAMPLE_RATE, SAMPLE_WIDTH) is None


def test_synthesize_helper_connection_error_returns_none():
    from hivemind_wyoming_binary_protocol.client import wyoming_synthesize
    assert wyoming_synthesize("tcp://127.0.0.1:1", "hello") is None
