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
| `recognizer_loop:b64_audio` | `handle_audio_b64()` | Transcribe base64 PCM; emit an utterance on the agent bus. |
| `recognizer_loop:b64_transcribe` | `handle_transcribe_b64()` | Transcribe base64 PCM; reply with the transcript. |

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

### The base64 audio field

The `audio` field of `recognizer_loop:b64_audio` and
`recognizer_loop:b64_transcribe` carries base64 of **headerless PCM**, not of a
WAV file. The format is the HIVEMIND-AUDIO-1 §2 default, mono signed 16-bit at
16 kHz, and the `sample_rate` and `sample_width` fields of the same message
override it. A container states its own format, so those two fields are the
proof that this field is PCM.

A payload that starts with a container header is refused: the transcription
result is empty and the reason is logged at error level. It is not read,
because a 44-byte WAV header would count as audio and put a click in front of
the utterance.

The refusal is not particular to this field. It is in `transcribe()`, the one
method every audio surface of this node reaches the ASR server through, so the
`RAW_AUDIO` stream and both binary STT tags refuse a container as well. §2
names them in one sentence, and they all carry headerless PCM.

The detected containers are RIFF and RF64 (any form id, so AVI as well as
WAVE), Ogg, FLAC, and MP3 with an ID3 tag. An MP3 with no ID3 tag is NOT
detected, on purpose: its frame sync is two bytes, `ff fb`, and read as a
little-endian 16-bit sample that is the ordinary value -1025, so matching it
would refuse real speech. A refused utterance costs more than an undetected
container that no sender in the fleet produces.

Nothing reads a format OUT of a header. HIVEMIND-AUDIO-1 §7 forbids it: "treat
the payload bytes as self-describing — the tag and metadata are the only
description". The rate and the width always come from the tag metadata or the
message.

The other direction is not the same. `speak:b64_audio.response` and the
`TTS_AUDIO` binary frame both carry a complete WAV file, because the satellite
plays that audio and needs its format in it.

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
