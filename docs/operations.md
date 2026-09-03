# Operations

## Running the Wyoming servers

The plugin needs a Wyoming ASR server and a Wyoming TTS server. A Home Assistant
user already runs these as the Wyoming Protocol add-ons and can point the hub
straight at them. To run them standalone, the repo ships a compose stack:

```bash
docker compose up -d
docker compose logs -f          # first start downloads the model and voice
```

| Server | Image | Port |
|---|---|---|
| faster-whisper (ASR) | `rhasspy/wyoming-whisper` | `10300` |
| Piper (TTS) | `rhasspy/wyoming-piper` | `10200` |

Change the model or voice in `docker-compose.yml`: `--model base-int8` for
whisper (use `tiny-int8` on constrained hardware, `small-int8` for better
accuracy) and `--voice en_US-lessac-medium` for Piper.

Confirm a server is up:

```bash
nc -z 127.0.0.1 10300 && echo ASR up
nc -z 127.0.0.1 10200 && echo TTS up
```

## Satellite setup

Any HiveMind satellite that streams RAW_AUDIO works, for example
[hivemind-mic-satellite](https://github.com/JarbasHiveMind/hivemind-mic-satellite):

```bash
pip install hivemind-mic-satellite
hivemind-mic-satellite --host ws://hub-address:5678 \
                       --key your-api-key \
                       --name my-satellite
```

The satellite captures audio and, ideally, does client-side voice detection so it
can end each turn with `recognizer_loop:record_end`. All transcription and
synthesis happen on the hub's Wyoming servers.

Provision the satellite's key on the hub and make sure its `allowed_types`
whitelist includes `recognizer_loop:utterance` and the audio message types it
sends:

```bash
hivemind-core add-client --name my-satellite
```

## Live round-trip test

The `tests/e2e` suite is skipped by default and by the reachability check. Run it
against the compose stack to prove the backend end to end:

```bash
docker compose up -d
WYOMING_ASR_URI=tcp://127.0.0.1:10300 \
WYOMING_TTS_URI=tcp://127.0.0.1:10200 \
    pytest tests/e2e -v
```

It synthesizes a phrase with Piper, feeds the audio into faster-whisper, and
checks the transcript — a real audio round trip, no HiveMind topology needed.

## Resource considerations

- One short-lived Wyoming connection is opened per utterance for ASR and per
  reply for TTS. There is no per-satellite model in this process; the models live
  in the Wyoming servers.
- Segmentation buffers audio per peer in memory until end-of-utterance, capped at
  30 seconds. A satellite that ends turns with `record_end` keeps buffers small.
- Scale by scaling the Wyoming servers. Several hubs and satellites can share one
  faster-whisper / Piper pair.

## Authoring a binary protocol plugin

Implement `BinaryDataHandlerProtocol` from `hivemind_plugin_manager.protocols` and
register it under the `hivemind.binary.protocol` entry-point group:

```toml
[project.entry-points."hivemind.binary.protocol"]
"my-binary-plugin" = "my_package:MyBinaryProtocol"
```

The three inbound handlers to implement are `handle_microphone_input`,
`handle_stt_transcribe_request`, and `handle_stt_handle_request`; TTS is driven by
subscribing to `speak:synth` / `speak:b64_audio` on the agent bus. This package is
a worked example that backs all of them with Wyoming servers.

---
[← Configuration](configuration.md) · [Home](../README.md)
