# Audio Flow

The plugin implements `BinaryDataHandlerProtocol` from `hivemind-plugin-manager`.
When a satellite sends a binary frame, `hivemind-core` dispatches it here based on
the `HiveMindBinaryPayloadType` tag. The speech backend is a pair of
[Wyoming](https://github.com/rhasspy/wyoming) servers reached over TCP.

## Inbound binary types

| Type | Handler | Description |
|---|---|---|
| Microphone audio chunks | `handle_microphone_input()` | Continuous raw PCM; buffered and segmented, then transcribed. |
| STT transcription request | `handle_stt_transcribe_request()` | One-shot; returns `recognizer_loop:transcribe.response`. |
| STT handle request | `handle_stt_handle_request()` | One-shot; injects `recognizer_loop:utterance` into the agent. |

## Outbound flows (triggered by agent bus events)

| Bus event | Handler | Description |
|---|---|---|
| `speak:synth` | `handle_speak_synth()` | Synthesize TTS; send a binary WAV frame to the satellite. |
| `speak:b64_audio` | `handle_speak_b64()` | Synthesize TTS; send base64 WAV as a bus message. |
| `recognizer_loop:b64_audio` | `handle_audio_b64()` | Transcribe base64 audio; emit an utterance on the agent bus. |
| `recognizer_loop:b64_transcribe` | `handle_transcribe_b64()` | Transcribe base64 audio; reply with the transcript. |

## Microphone stream

```
satellite mic → RAW_AUDIO frames → hub
                                     │
                                     └─ per-peer buffer + silence segmentation
                                           │
                                           └─ Wyoming ASR server (Transcript)
                                                 │
                                                 └─ recognizer_loop:utterance
                                                       └─ injected into the agent
```

### Segmentation

A raw microphone stream carries no turn boundaries, and this plugin runs no OVOS
listener and no VAD model. Frames are accumulated per peer and segmented by
amplitude: the mean absolute amplitude of each frame is compared with a silence
threshold; once speech is seen, a run of trailing silence (`SILENCE_FLUSH_MS`,
about 800 ms) ends the utterance and the buffer is sent to the ASR server. A
30-second cap force-flushes a runaway stream. A satellite that runs its own voice
detection can send `recognizer_loop:record_end` to flush immediately, which gives
a precise cut without waiting for the silence timer.

This is a deliberately simple heuristic. It is correct for a push-to-talk or
VAD-gated satellite and adequate for an always-open mic in a quiet room. For
tighter control, do voice detection on the satellite and end each turn with
`record_end`.

### Audio format

The default format is mono signed 16-bit PCM at 16 kHz (HIVEMIND-AUDIO-1 §2).
There is no resampling: a frame whose stated sample rate or width this node
cannot process is rejected with a `recognizer_loop:speech.recognition.unknown`
message carrying `{"error": "unsupported_audio_format", ...}`, not misread into a
wrong transcript. For a continuous stream the refusal is sent once per peer, not
once per chunk. Configure the satellite to match, or set `sample_rate` /
`sample_width` to the format your stream uses.

## The Wyoming exchange

### ASR

```
open AsyncClient.from_uri(asr_uri)
  → Transcribe(language=lang)
  → AudioStart(rate, width, channels)
  → AudioChunk(audio=...)   × N
  → AudioStop()
  ← read events until Transcript  → .text
```

### TTS

```
open AsyncClient.from_uri(tts_uri)
  → Synthesize(text, voice)
  ← AudioStart(rate, width, channels)
  ← AudioChunk(audio=...)   × N   (concatenated)
  ← AudioStop()
```

The concatenated PCM is wrapped in a WAV (RIFF) container before it is returned
to the satellite, so the satellite receives the same audio shape the OVOS-backed
sibling sends. Both exchanges run in short-lived connections wrapped in
`asyncio.run`, so the synchronous HiveMind dispatch path stays synchronous. A
connection failure or a missing Transcript degrades to a logged error and an
"unknown" result — a dead server never crashes the hub.

---
[Home](../README.md) · [Configuration →](configuration.md)
