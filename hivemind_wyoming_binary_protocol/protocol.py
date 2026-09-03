"""Wyoming-backed binary audio protocol for HiveMind.

This is a drop-in alternative to ``hivemind-audio-binary-protocol``: same
``BinaryDataHandlerProtocol`` contract, same satellite-facing message shapes,
but the audio backend is a `Wyoming <https://github.com/rhasspy/wyoming>`_
ASR/TTS server pair (the protocol Home Assistant voice uses) instead of OVOS
STT/TTS plugins and an OVOS listener. It proves the HiveMind binary/audio layer
is not tied to OVOS, and lets a Home Assistant user point a HiveMind hub at the
``wyoming-faster-whisper`` and ``wyoming-piper`` servers they already run.

A satellite streams RAW_AUDIO to the hub; the hub relays it to the Wyoming ASR
server for transcription and injects the resulting utterance into the agent
exactly as the OVOS plugin does. Replies (``speak``) are synthesized by the
Wyoming TTS server and streamed back as TTS audio.
"""
import io
import wave
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

import pybase64
from hivemind_bus_client.message import (HiveMessage, HiveMessageType,
                                         HiveMindBinaryPayloadType)
from hivemind_plugin_manager.protocols import (BinaryDataHandlerProtocol,
                                               ClientCallbacks)
from ovos_bus_client.message import Message
from ovos_bus_client.util import get_message_lang
from ovos_utils.log import LOG

from hivemind_wyoming_binary_protocol.client import (wyoming_synthesize,
                                                     wyoming_transcribe)

if TYPE_CHECKING:  # hivemind-core is the AGPL host; imported only for typing
    from hivemind_core.protocol import HiveMindClientConnection


# HIVEMIND-AUDIO-1 §2 default stream format: mono signed 16-bit PCM at 16 kHz.
# There is no resampling here, so a payload stating anything else is rejected
# rather than misread. These are also the defaults handed to the Wyoming
# servers when a payload carries no explicit format metadata.
SAMPLE_RATE = 16000
SAMPLE_WIDTH = 2
SAMPLE_CHANNELS = 1

# Silence-based end-of-utterance heuristic for the RAW_AUDIO stream. This node
# runs no OVOS listener and no VAD plugin, so a streamed utterance is segmented
# by amplitude: once speech is seen, a run of low-energy frames flushes the
# buffer to the ASR server. ``recognizer_loop:record_end`` also flushes, so a
# satellite that already does client-side VAD gets a precise cut.
SILENCE_RMS_THRESHOLD = 500  # mean abs amplitude below this counts as silence
SILENCE_FLUSH_MS = 800  # trailing silence that ends an utterance
MAX_UTTERANCE_BYTES = 16000 * 2 * 30  # 30 s cap; a runaway stream is flushed


def _is_supported_audio_format(sample_rate: int, sample_width: int) -> bool:
    return sample_rate == SAMPLE_RATE and sample_width == SAMPLE_WIDTH


def _mean_abs_amplitude(pcm: bytes) -> float:
    """Mean absolute amplitude of signed 16-bit little-endian PCM."""
    if len(pcm) < 2:
        return 0.0
    import array
    samples = array.array("h")
    samples.frombytes(pcm[:len(pcm) - (len(pcm) % 2)])
    if not samples:
        return 0.0
    return sum(abs(s) for s in samples) / len(samples)


def pcm_to_wav(pcm: bytes, rate: int, width: int, channels: int) -> bytes:
    """Wrap raw PCM in a WAV (RIFF) container for satellite playback."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(width)
        wav.setframerate(rate)
        wav.writeframes(pcm)
    return buf.getvalue()


@dataclass
class _StreamBuffer:
    """Per-peer accumulator for a RAW_AUDIO utterance."""
    frames: bytearray = field(default_factory=bytearray)
    sample_rate: int = SAMPLE_RATE
    sample_width: int = SAMPLE_WIDTH
    speech_seen: bool = False
    silence_ms: float = 0.0


@dataclass
class WyomingBinaryProtocol(BinaryDataHandlerProtocol):
    """Binary data handler whose STT/TTS backend is a Wyoming server pair.

    Mirrors ``AudioBinaryProtocol`` method-for-method (``handle_microphone_input``,
    ``handle_stt_transcribe_request``, ``handle_stt_handle_request``, the
    ``speak:*`` / ``recognizer_loop:b64_*`` bus handlers) so it is a drop-in
    replacement, but replaces the OVOS STT plugin with a Wyoming ASR server and
    the OVOS TTS plugin with a Wyoming TTS server.
    """
    config: Dict[str, Any] = field(default_factory=dict)
    hm_protocol: Optional[Any] = None
    agent_protocol: Optional[Any] = None
    callbacks: Optional[ClientCallbacks] = None
    asr_uri: Optional[str] = None
    tts_uri: Optional[str] = None
    tts_voice: Optional[str] = None
    sample_rate: int = SAMPLE_RATE
    sample_width: int = SAMPLE_WIDTH
    sample_channels: int = SAMPLE_CHANNELS
    # peers streaming RAW_AUDIO in a format this node can not process; told once,
    # not once per chunk (a raw stream is continuous — a per-chunk refusal floods)
    refused_streams: set = field(default_factory=set)
    buffers: Dict[str, _StreamBuffer] = field(default_factory=dict)

    def __post_init__(self):
        super().__post_init__()

        def _uri(kind: str) -> Optional[str]:
            uri = self.config.get(f"{kind}_uri")
            if uri:
                return uri
            host = self.config.get(f"{kind}_host")
            if host:
                port = self.config.get(f"{kind}_port",
                                       10300 if kind == "asr" else 10200)
                return f"tcp://{host}:{port}"
            return None

        self.asr_uri = self.asr_uri or _uri("asr")
        self.tts_uri = self.tts_uri or _uri("tts")
        self.tts_voice = self.tts_voice or self.config.get("tts_voice")
        self.sample_rate = self.config.get("sample_rate", self.sample_rate)
        self.sample_width = self.config.get("sample_width", self.sample_width)
        self.sample_channels = self.config.get("channels", self.sample_channels)

        if not self.asr_uri:
            LOG.warning("No Wyoming ASR server configured (asr_uri / asr_host); "
                        "speech-to-text is disabled")
        if not self.tts_uri:
            LOG.warning("No Wyoming TTS server configured (tts_uri / tts_host); "
                        "text-to-speech is disabled")

        # clear per-peer state on disconnect, mirroring the OVOS plugin
        if not self.callbacks:
            self.callbacks = ClientCallbacks(on_disconnect=self._on_disconnect)
        else:
            original = self.callbacks.on_disconnect

            def wrapper(c):
                try:
                    original(c)
                finally:
                    self._on_disconnect(c)

            self.callbacks.on_disconnect = wrapper

        # bus-driven audio flows, same event names as the OVOS plugin
        bus = self.agent_protocol.bus
        bus.on("recognizer_loop:b64_audio", self.handle_audio_b64)
        bus.on("recognizer_loop:b64_transcribe", self.handle_transcribe_b64)
        bus.on("speak:b64_audio", self.handle_speak_b64)
        bus.on("speak:synth", self.handle_speak_synth)
        # a satellite doing client-side VAD ends the utterance explicitly
        bus.on("recognizer_loop:record_end", self.handle_record_end)

    def _on_disconnect(self, client: "HiveMindClientConnection") -> None:
        self.refused_streams.discard(client.peer)
        self.buffers.pop(client.peer, None)

    # ── Wyoming backends ──────────────────────────────────────────────────
    def transcribe(self, pcm: bytes, sample_rate: int, sample_width: int,
                   lang: Optional[str]) -> Optional[str]:
        """Transcribe raw PCM via the Wyoming ASR server. None on failure."""
        if not self.asr_uri:
            LOG.error("Wyoming ASR request with no asr_uri configured")
            return None
        return wyoming_transcribe(self.asr_uri, pcm, sample_rate, sample_width,
                                  self.sample_channels, lang)

    def synthesize(self, utterance: str) -> Optional[bytes]:
        """Synthesize ``utterance`` via the Wyoming TTS server as WAV bytes."""
        if not self.tts_uri:
            LOG.error("Wyoming TTS request with no tts_uri configured")
            return None
        audio = wyoming_synthesize(self.tts_uri, utterance, self.tts_voice)
        if audio is None:
            return None
        return pcm_to_wav(audio.audio, audio.rate, audio.width, audio.channels)

    # ── RAW_AUDIO microphone stream ───────────────────────────────────────
    def handle_microphone_input(self, bin_data: bytes, sample_rate: int,
                                sample_width: int,
                                client: "HiveMindClientConnection") -> None:
        """Buffer a RAW_AUDIO chunk; flush to Wyoming ASR at end-of-utterance.

        Segmentation is amplitude-based (see ``SILENCE_*``): the buffer is
        flushed after a run of trailing silence, when the 30 s cap is hit, or on
        an explicit ``recognizer_loop:record_end``.
        """
        if not _is_supported_audio_format(sample_rate, sample_width):
            if client.peer not in self.refused_streams:
                self.refused_streams.add(client.peer)
                self._reject_audio_format(sample_rate, sample_width, client)
            return
        self.refused_streams.discard(client.peer)

        buf = self.buffers.get(client.peer)
        if buf is None:
            buf = self.buffers[client.peer] = _StreamBuffer(
                sample_rate=sample_rate, sample_width=sample_width)
        buf.frames.extend(bin_data)

        frame_ms = 1000.0 * (len(bin_data) / sample_width) / sample_rate
        if _mean_abs_amplitude(bin_data) >= SILENCE_RMS_THRESHOLD:
            buf.speech_seen = True
            buf.silence_ms = 0.0
        elif buf.speech_seen:
            buf.silence_ms += frame_ms

        if (buf.speech_seen and buf.silence_ms >= SILENCE_FLUSH_MS) \
                or len(buf.frames) >= MAX_UTTERANCE_BYTES:
            self._flush_stream(client)

    def handle_record_end(self, message: Message) -> None:
        """Flush the streaming buffer when a satellite signals end-of-speech."""
        peer = message.context.get("source")
        client = (self.hm_protocol.clients or {}).get(peer) if self.hm_protocol else None
        if client is not None:
            self._flush_stream(client)

    def _flush_stream(self, client: "HiveMindClientConnection") -> None:
        buf = self.buffers.pop(client.peer, None)
        if not buf or not buf.frames:
            return
        lang = getattr(getattr(client, "sess", None), "lang", None)
        text = self.transcribe(bytes(buf.frames), buf.sample_rate,
                               buf.sample_width, lang)
        if text:
            m = Message("recognizer_loop:utterance",
                        {"utterances": [text.strip(" '\"")], "lang": lang})
            self.hm_protocol.handle_inject_agent_msg(m, client)
        else:
            client.send(HiveMessage(
                HiveMessageType.BUS,
                payload=Message("recognizer_loop:speech.recognition.unknown")))

    def _reject_audio_format(self, sample_rate: int, sample_width: int,
                             client: "HiveMindClientConnection") -> None:
        """Refuse a payload whose stated format this node can not process.

        HIVEMIND-AUDIO-1 §2: reject rather than misread the bytes — a wrong
        sample rate handed to ASR yields a plausible but wrong transcript.
        """
        LOG.error(f"Rejecting audio from {client.peer}: unsupported format "
                  f"({sample_rate}, {sample_width}), "
                  f"expected: ({SAMPLE_RATE}, {SAMPLE_WIDTH})")
        client.send(HiveMessage(
            HiveMessageType.BUS,
            payload=Message("recognizer_loop:speech.recognition.unknown",
                            {"error": "unsupported_audio_format",
                             "sample_rate": SAMPLE_RATE,
                             "sample_width": SAMPLE_WIDTH})))

    # ── one-shot STT ──────────────────────────────────────────────────────
    def handle_stt_transcribe_request(self, bin_data: bytes, sample_rate: int,
                                      sample_width: int, lang: str,
                                      client: "HiveMindClientConnection") -> None:
        """Transcribe one payload and return ``transcribe.response``."""
        LOG.debug(f"Received binary STT input: {len(bin_data)} bytes")
        if not _is_supported_audio_format(sample_rate, sample_width):
            self._reject_audio_format(sample_rate, sample_width, client)
            return
        text = self.transcribe(bin_data, sample_rate, sample_width, lang)
        transcriptions = [(text.strip(" '\""), 1.0)] if text else []
        m = Message("recognizer_loop:transcribe.response",
                    {"transcriptions": transcriptions, "lang": lang})
        client.send(HiveMessage(HiveMessageType.BUS, payload=m))

    def handle_stt_handle_request(self, bin_data: bytes, sample_rate: int,
                                  sample_width: int, lang: str,
                                  client: "HiveMindClientConnection") -> None:
        """Transcribe one payload and inject the utterance into the agent."""
        LOG.debug(f"Received binary STT input: {len(bin_data)} bytes")
        if not _is_supported_audio_format(sample_rate, sample_width):
            self._reject_audio_format(sample_rate, sample_width, client)
            return
        text = self.transcribe(bin_data, sample_rate, sample_width, lang)
        if text:
            m = Message("recognizer_loop:utterance",
                        {"utterances": [text.strip(" '\"")], "lang": lang})
            self.hm_protocol.handle_inject_agent_msg(m, client)
        else:
            LOG.info(f"STT transcription error for client: {client.peer}")
            client.send(HiveMessage(
                HiveMessageType.BUS,
                payload=Message("recognizer_loop:speech.recognition.unknown")))

    # ── base64 STT/TTS over the OVOS bus ──────────────────────────────────
    def transcribe_b64_audio(self, message: Message) -> List[Tuple[str, float]]:
        b64audio = message.data["audio"]
        lang = message.data.get("lang")
        sample_rate = message.data.get("sample_rate", SAMPLE_RATE)
        sample_width = message.data.get("sample_width", SAMPLE_WIDTH)
        pcm = pybase64.b64decode(b64audio)
        text = self.transcribe(pcm, sample_rate, sample_width, lang)
        return [(text.strip(" '\""), 1.0)] if text else []

    def get_b64_tts(self, message: Message) -> Optional[str]:
        wav = self.synthesize(message.data["utterance"])
        if wav is None:
            return None
        return pybase64.b64encode(wav).decode("utf-8")

    def handle_audio_b64(self, message: Message):
        lang = get_message_lang(message)
        transcriptions = self.transcribe_b64_audio(message)
        msg = message.forward("recognizer_loop:utterance",
                              {"utterances": [u[0] for u in transcriptions],
                               "lang": lang})
        self.hm_protocol.agent_protocol.bus.emit(msg)

    def handle_transcribe_b64(self, message: Message):
        lang = get_message_lang(message)
        client = self.hm_protocol.clients[message.context["source"]]
        msg = message.reply("recognizer_loop:b64_transcribe.response", {"lang": lang})
        msg.data["transcriptions"] = self.transcribe_b64_audio(message)
        if msg.context.get("destination") is None:
            msg.context["destination"] = "skills"
        client.send(HiveMessage(HiveMessageType.BUS, msg))

    def handle_speak_b64(self, message: Message):
        client = self.hm_protocol.clients[message.context["source"]]
        msg = message.reply("speak:b64_audio.response", message.data)
        msg.data["audio"] = self.get_b64_tts(message)
        if msg.context.get("destination") is None:
            msg.context["destination"] = "audio"
        client.send(HiveMessage(HiveMessageType.BUS, msg))

    def handle_speak_synth(self, message: Message):
        client = self.hm_protocol.clients[message.context["source"]]
        lang = get_message_lang(message)
        utterance = message.data["utterance"]
        wav = self.synthesize(utterance)
        if wav is None:
            client.send(HiveMessage(
                HiveMessageType.BUS,
                payload=Message("speak:synth.error", {"utterance": utterance})))
            return
        payload = HiveMessage(HiveMessageType.BINARY,
                              payload=wav,
                              metadata={"lang": lang,
                                        "file_name": "tts.wav",
                                        "utterance": utterance},
                              bin_type=HiveMindBinaryPayloadType.TTS_AUDIO)
        client.send(payload)
