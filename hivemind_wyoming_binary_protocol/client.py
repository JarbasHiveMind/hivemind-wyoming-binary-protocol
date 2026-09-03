"""Synchronous Wyoming ASR/TTS helpers.

The Wyoming protocol client is async; the HiveMind binary handler is called
from synchronous bus/dispatch code. These helpers wrap a short-lived Wyoming
connection in :func:`asyncio.run`, so the rest of the plugin stays synchronous
and mirrors the OVOS ``stt.transcribe`` / ``tts.synth`` call shape.

A Wyoming server speaks a small event protocol over a socket:

  ASR: ``Transcribe`` (optional) → ``AudioStart`` → ``AudioChunk``* →
       ``AudioStop`` → read events until ``Transcript`` (``.text``).
  TTS: ``Synthesize`` → read ``AudioStart`` then ``AudioChunk``* until
       ``AudioStop``, concatenating the chunk audio.
"""
import asyncio
from dataclasses import dataclass
from typing import Optional

from ovos_utils.log import LOG
from wyoming.asr import Transcribe, Transcript
from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.client import AsyncClient
from wyoming.tts import Synthesize, SynthesizeVoice


@dataclass
class WyomingAudio:
    """Raw PCM returned by a Wyoming TTS server, with its stated format."""
    audio: bytes
    rate: int
    width: int
    channels: int


def _chunked(data: bytes, size: int):
    for i in range(0, len(data), size):
        yield data[i:i + size]


async def _async_transcribe(uri: str, pcm: bytes, rate: int, width: int,
                            channels: int, lang: Optional[str],
                            chunk_size: int) -> Optional[str]:
    async with AsyncClient.from_uri(uri) as client:
        # Transcribe primes the server (and picks the language) before audio.
        await client.write_event(Transcribe(language=lang).event())
        await client.write_event(
            AudioStart(rate=rate, width=width, channels=channels).event())
        for chunk in _chunked(pcm, chunk_size):
            await client.write_event(
                AudioChunk(rate=rate, width=width, channels=channels,
                           audio=chunk).event())
        await client.write_event(AudioStop().event())

        while True:
            event = await client.read_event()
            if event is None:  # server closed without a Transcript
                return None
            if Transcript.is_type(event.type):
                return Transcript.from_event(event).text
    return None


async def _async_synthesize(uri: str, text: str, voice: Optional[str]
                            ) -> Optional[WyomingAudio]:
    synth_voice = SynthesizeVoice(name=voice) if voice else None
    async with AsyncClient.from_uri(uri) as client:
        await client.write_event(Synthesize(text=text, voice=synth_voice).event())

        audio = bytearray()
        rate = width = channels = None
        while True:
            event = await client.read_event()
            if event is None:
                break
            if AudioStart.is_type(event.type):
                start = AudioStart.from_event(event)
                rate, width, channels = start.rate, start.width, start.channels
            elif AudioChunk.is_type(event.type):
                chunk = AudioChunk.from_event(event)
                audio.extend(chunk.audio)
                if rate is None:  # some servers omit AudioStart
                    rate, width, channels = chunk.rate, chunk.width, chunk.channels
            elif AudioStop.is_type(event.type):
                break

        if not audio or rate is None:
            return None
        return WyomingAudio(bytes(audio), rate, width, channels)


def wyoming_transcribe(uri: str, pcm: bytes, rate: int, width: int,
                       channels: int = 1, lang: Optional[str] = None,
                       chunk_size: int = 4096) -> Optional[str]:
    """Transcribe raw PCM via a Wyoming ASR server.

    Returns the transcript text, or ``None`` when the server produced no
    Transcript or the connection failed. Errors are logged, never raised, so a
    dead ASR server degrades to "unknown" instead of crashing the hub.
    """
    if not pcm:
        LOG.warning("Empty audio buffer, skipping Wyoming ASR request")
        return None
    try:
        return asyncio.run(
            _async_transcribe(uri, pcm, rate, width, channels, lang, chunk_size))
    except Exception as e:
        LOG.error(f"Wyoming ASR request to {uri} failed: {e}")
        return None


def wyoming_synthesize(uri: str, text: str, voice: Optional[str] = None
                       ) -> Optional[WyomingAudio]:
    """Synthesize ``text`` via a Wyoming TTS server.

    Returns a :class:`WyomingAudio` (raw PCM + format), or ``None`` when the
    server returned no audio or the connection failed.
    """
    if not text:
        return None
    try:
        return asyncio.run(_async_synthesize(uri, text, voice))
    except Exception as e:
        LOG.error(f"Wyoming TTS request to {uri} failed: {e}")
        return None
