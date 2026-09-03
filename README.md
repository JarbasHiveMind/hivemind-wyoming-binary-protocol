# hivemind-wyoming-binary-protocol

Binary audio plugin for [hivemind-core](https://github.com/JarbasHiveMind/HiveMind-core) whose speech-to-text and text-to-speech run on [Wyoming](https://github.com/rhasspy/wyoming) servers — the protocol Home Assistant voice uses.

A lightweight HiveMind satellite streams raw microphone audio to the hub. The hub relays that audio to a Wyoming ASR server for transcription, hands the transcript to the agent, and synthesizes the spoken reply through a Wyoming TTS server. The satellite runs no speech models.

This is a drop-in alternative to [hivemind-audio-binary-protocol](https://github.com/JarbasHiveMind/hivemind-audio-binary-protocol): the same `hivemind.binary.protocol` contract and the same satellite-facing message shapes, with the OVOS STT and TTS plugins swapped for Wyoming servers. It shows the HiveMind audio and agent layers are not tied to OVOS, and it lets a Home Assistant user point a HiveMind hub straight at the `wyoming-faster-whisper` and `wyoming-piper` servers they already run.

## Requirements

A running Wyoming ASR server and a running Wyoming TTS server. The plugin is a client of both — it does not bundle any speech model. Any Wyoming server works: the `wyoming-faster-whisper` / `wyoming-piper` Home Assistant add-ons, standalone containers, or the `docker-compose.yml` in this repo.

## Install

```bash
pip install hivemind-wyoming-binary-protocol
```

The plugin registers under the `hivemind.binary.protocol` entry-point group as `hivemind-wyoming-binary-protocol-plugin`.

## Quickstart

Start two Wyoming servers. If you already run them for Home Assistant, use those addresses. Otherwise the bundled compose stack brings up faster-whisper and Piper:

```bash
docker compose up -d
# wyoming-faster-whisper -> tcp://127.0.0.1:10300
# wyoming-piper          -> tcp://127.0.0.1:10200
```

Add the `binary_protocol` block to `~/.config/hivemind-core/server.json`:

```json
{
  "binary_protocol": {
    "module": "hivemind-wyoming-binary-protocol-plugin",
    "hivemind-wyoming-binary-protocol-plugin": {
      "asr_uri": "tcp://127.0.0.1:10300",
      "tts_uri": "tcp://127.0.0.1:10200",
      "tts_voice": "en_US-lessac-medium"
    }
  }
}
```

Start the hub:

```bash
hivemind-core listen
```

Point a [mic satellite](https://github.com/JarbasHiveMind/hivemind-mic-satellite) at it. The satellite streams audio; the hub transcribes it on faster-whisper, the agent answers, and Piper speaks the reply back down to the satellite.

## Pointing at existing Home Assistant Wyoming add-ons

If Home Assistant already runs the Wyoming Protocol add-ons, use their host and port instead of the compose stack. Host and port work as an alternative to a full URI:

```json
{
  "asr_host": "homeassistant.local",
  "asr_port": 10300,
  "tts_host": "homeassistant.local",
  "tts_port": 10200,
  "tts_voice": "en_US-lessac-medium"
}
```

## Configuration

| Key | Default | Description |
|---|---|---|
| `asr_uri` | — | Wyoming ASR server, e.g. `tcp://host:10300`. |
| `asr_host` / `asr_port` | — / `10300` | Alternative to `asr_uri`. |
| `tts_uri` | — | Wyoming TTS server, e.g. `tcp://host:10200`. |
| `tts_host` / `tts_port` | — / `10200` | Alternative to `tts_uri`. |
| `tts_voice` | server default | Voice name passed to the TTS server (e.g. `en_US-lessac-medium`). |
| `sample_rate` | `16000` | Sample rate sent to the ASR server. |
| `sample_width` | `2` | Sample width in bytes (16-bit PCM). |
| `channels` | `1` | Channel count (mono). |

Incoming stream metadata is honored: a payload states its own sample rate and width, and a format this node cannot process is rejected rather than misread.

## Audio streaming modes

| Mode | Satellite sends | Hub returns | Use case |
|---|---|---|---|
| Microphone stream | Raw PCM audio chunks | `recognizer_loop:utterance` on the agent bus | Mic satellite. The hub segments, transcribes, and handles the utterance. |
| STT transcription | Raw PCM audio | `recognizer_loop:transcribe.response` | The satellite wants a transcript without triggering the agent. |
| STT handle | Raw PCM audio | Injects `recognizer_loop:utterance` | The satellite wants the hub to act on the utterance. |
| TTS reply | `speak:synth` / `speak:b64_audio` | WAV binary frame / base64 WAV | The agent speaks back to the satellite. |

### Utterance segmentation

The microphone stream carries no explicit turn boundaries, and this plugin runs no OVOS listener or VAD model. It segments by amplitude: once speech is detected, a run of trailing silence (about 800 ms) ends the utterance and flushes the buffer to the ASR server. A 30-second cap flushes a runaway stream. A satellite that already does client-side voice detection can send `recognizer_loop:record_end` for a precise cut. See [docs/audio_flow.md](docs/audio_flow.md).

## Related projects

- [JarbasHiveMind/HiveMind-core](https://github.com/JarbasHiveMind/HiveMind-core) — the hub this plugin extends
- [JarbasHiveMind/hivemind-plugin-manager](https://github.com/JarbasHiveMind/hivemind-plugin-manager) — loads this plugin by entry-point
- [JarbasHiveMind/hivemind-audio-binary-protocol](https://github.com/JarbasHiveMind/hivemind-audio-binary-protocol) — the OVOS-backed sibling this mirrors
- [JarbasHiveMind/hivemind-mic-satellite](https://github.com/JarbasHiveMind/hivemind-mic-satellite) — reference satellite client
- [rhasspy/wyoming](https://github.com/rhasspy/wyoming) — the Wyoming protocol

## License

Apache License 2.0. See [LICENSE](LICENSE).

## Docs

- [docs/audio_flow.md](docs/audio_flow.md): STT and TTS flow, segmentation, the Wyoming event exchange
- [docs/configuration.md](docs/configuration.md): full configuration reference
- [docs/operations.md](docs/operations.md): running Wyoming servers, satellite setup, authoring a binary plugin
