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
    # headerless PCM: the b64 STT field is not a container (HIVEMIND-AUDIO-1 §2).
    # This test used to send a WAV here, and passed only because the ASR call is
    # mocked, so the 44-byte header it prepended was never measured.
    b64 = base64.b64encode(_loud(2000)).decode()
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


# ── the base64 STT field is headerless PCM, not a container ───────────────
def test_b64_transcribe_rejects_a_wav_container(monkeypatch):
    """A WAV in the b64 audio field is refused, not transcribed.

    HIVEMIND-AUDIO-1 §2: the audio inside the STT tags carries uncompressed
    PCM, and a receiver that can not process the bytes must reject them rather
    than misinterpret them. Before the fix the 44-byte RIFF header was handed
    to the ASR server as audio.
    """
    import base64

    seen = []
    monkeypatch.setattr(protocol_module, "wyoming_transcribe",
                        lambda uri, pcm, *a, **k: seen.append(pcm) or "hello")
    proto = _make_protocol()

    from ovos_bus_client.message import Message
    wav = pcm_to_wav(_loud(1600), SAMPLE_RATE, SAMPLE_WIDTH, 1)
    assert wav[:4] == b"RIFF"
    msg = Message("recognizer_loop:b64_transcribe",
                  {"audio": base64.b64encode(wav).decode("utf-8"),
                   "lang": "en-us"})

    assert proto.transcribe_b64_audio(msg) == []
    assert seen == []  # the container never reached the ASR server


def test_b64_transcribe_accepts_headerless_pcm(monkeypatch):
    """The control: the same samples without the header still transcribe."""
    import base64

    seen = []
    monkeypatch.setattr(protocol_module, "wyoming_transcribe",
                        lambda uri, pcm, *a, **k: seen.append(pcm) or "hello")
    proto = _make_protocol()

    from ovos_bus_client.message import Message
    pcm = _loud(1600)
    msg = Message("recognizer_loop:b64_transcribe",
                  {"audio": base64.b64encode(pcm).decode("utf-8"),
                   "lang": "en-us"})

    assert proto.transcribe_b64_audio(msg) == [("hello", 1.0)]
    assert seen == [pcm]


# ── every audio surface refuses a container, not just the base64 field ────
def _recorder(monkeypatch, text="hello"):
    """Substitute the ASR client and record exactly what reaches it."""
    seen = []

    def _fake(uri, pcm, *a, **k):
        seen.append(pcm)
        return text

    monkeypatch.setattr(protocol_module, "wyoming_transcribe", _fake)
    return seen


def _wav():
    """3244 bytes: a 44-byte RIFF/WAVE header in front of 3200 of samples."""
    wav = pcm_to_wav(_loud(1600), SAMPLE_RATE, SAMPLE_WIDTH, 1)
    assert wav[:4] == b"RIFF" and len(wav) == 3244
    return wav


def test_binary_transcribe_request_refuses_a_container(monkeypatch):
    """STT_AUDIO_TRANSCRIBE. The header used to reach the ASR as audio."""
    seen = _recorder(monkeypatch)
    proto = _make_protocol()
    client = _make_client()

    proto.handle_stt_transcribe_request(_wav(), SAMPLE_RATE, SAMPLE_WIDTH,
                                        "en-us", client)

    assert seen == []
    replies = [m for m in _bus(client)
               if m.payload.msg_type == "recognizer_loop:transcribe.response"]
    assert len(replies) == 1
    assert replies[0].payload.data["transcriptions"] == []


def test_binary_handle_request_refuses_a_container(monkeypatch):
    """STT_AUDIO_HANDLE. No utterance may be injected from a header."""
    seen = _recorder(monkeypatch)
    proto = _make_protocol()
    client = _make_client()

    proto.handle_stt_handle_request(_wav(), SAMPLE_RATE, SAMPLE_WIDTH,
                                    "en-us", client)

    assert seen == []
    proto.hm_protocol.handle_inject_agent_msg.assert_not_called()
    assert [m for m in _bus(client)
            if m.payload.msg_type == "recognizer_loop:speech.recognition.unknown"]


def test_raw_audio_stream_refuses_a_container(monkeypatch):
    """RAW_AUDIO, the path the module docstring calls the primary one.

    A satellite that streams a whole WAV and then ends the turn had its
    header transcribed with the samples.
    """
    from ovos_bus_client.message import Message

    seen = _recorder(monkeypatch)
    proto = _make_protocol()
    client = _make_client(peer="sat::9")
    proto.hm_protocol.clients = {"sat::9": client}

    proto.handle_microphone_input(_wav(), SAMPLE_RATE, SAMPLE_WIDTH, client)
    proto.handle_record_end(Message("recognizer_loop:record_end",
                                    context={"source": "sat::9"}))

    assert seen == []
    proto.hm_protocol.handle_inject_agent_msg.assert_not_called()


def test_the_same_samples_headerless_still_reach_the_asr(monkeypatch):
    """The control for all three, on the same bytes minus the header."""
    from ovos_bus_client.message import Message

    pcm = _loud(1600)
    assert len(pcm) == 3200

    seen = _recorder(monkeypatch)
    proto = _make_protocol()
    client = _make_client()
    proto.handle_stt_transcribe_request(pcm, SAMPLE_RATE, SAMPLE_WIDTH,
                                        "en-us", client)

    seen2 = _recorder(monkeypatch)
    proto2 = _make_protocol()
    client2 = _make_client()
    proto2.handle_stt_handle_request(pcm, SAMPLE_RATE, SAMPLE_WIDTH,
                                     "en-us", client2)

    seen3 = _recorder(monkeypatch)
    proto3 = _make_protocol()
    client3 = _make_client(peer="sat::9")
    proto3.hm_protocol.clients = {"sat::9": client3}
    proto3.handle_microphone_input(pcm, SAMPLE_RATE, SAMPLE_WIDTH, client3)
    proto3.handle_record_end(Message("recognizer_loop:record_end",
                                     context={"source": "sat::9"}))

    assert seen == [pcm]
    assert seen2 == [pcm]
    assert seen3 == [pcm]
    proto2.hm_protocol.handle_inject_agent_msg.assert_called_once()
    proto3.hm_protocol.handle_inject_agent_msg.assert_called_once()


def test_the_other_containers_are_refused_too(monkeypatch):
    """§2 asks for a refusal of what can not be processed, not of WAV alone.

    Each of these was transcribed as PCM before. No fleet sender is known to
    produce them in these fields, so this is completeness against the clause
    rather than a second measured defect.
    """
    samples = _loud(1000)
    containers = {
        "RIFF/WAVE": pcm_to_wav(samples, SAMPLE_RATE, SAMPLE_WIDTH, 1),
        "RIFF/AVI": b"RIFF" + b"\x00" * 4 + b"AVI " + samples,
        "RF64": b"RF64" + b"\xff" * 4 + b"WAVE" + samples,
        "Ogg": b"OggS" + b"\x00" * 8 + samples,
        "FLAC": b"fLaC" + b"\x00" * 8 + samples,
        "MP3 with an ID3 tag": b"ID3" + b"\x04\x00\x00" + samples,
    }
    for label, payload in containers.items():
        seen = _recorder(monkeypatch)
        proto = _make_protocol()
        client = _make_client()
        proto.handle_stt_transcribe_request(payload, SAMPLE_RATE,
                                            SAMPLE_WIDTH, "en-us", client)
        assert seen == [], f"{label} reached the ASR server"


def test_a_short_payload_is_not_a_container(monkeypatch):
    """The negative result, kept as a test.

    Eleven bytes can not hold a RIFF header and samples, so there is no
    container to misread and nothing to refuse. The worst case is a few
    bytes of junk handed to the ASR, which a sender achieves just as well
    with bare PCM.
    """
    seen = _recorder(monkeypatch)
    proto = _make_protocol()
    client = _make_client()
    for n in (4, 8, 11):
        proto.handle_stt_transcribe_request(_wav()[:n], SAMPLE_RATE,
                                            SAMPLE_WIDTH, "en-us", client)
    assert [len(p) for p in seen] == [4, 8, 11]
