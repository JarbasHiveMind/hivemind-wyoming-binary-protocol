# Configuration Reference

The plugin is configured under the `binary_protocol` key in
`~/.config/hivemind-core/server.json`.

```json
{
  "binary_protocol": {
    "module": "hivemind-wyoming-binary-protocol-plugin",
    "hivemind-wyoming-binary-protocol-plugin": {
      "asr_uri": "tcp://127.0.0.1:10300",
      "tts_uri": "tcp://127.0.0.1:10200",
      "tts_voice": "en_US-lessac-medium",
      "sample_rate": 16000,
      "sample_width": 2,
      "channels": 1
    }
  }
}
```

## Keys

| Key | Default | Description |
|---|---|---|
| `asr_uri` | — | Wyoming ASR server URI, e.g. `tcp://host:10300`. |
| `asr_host` | — | ASR host, used when `asr_uri` is absent. |
| `asr_port` | `10300` | ASR port, paired with `asr_host`. |
| `tts_uri` | — | Wyoming TTS server URI, e.g. `tcp://host:10200`. |
| `tts_host` | — | TTS host, used when `tts_uri` is absent. |
| `tts_port` | `10200` | TTS port, paired with `tts_host`. |
| `tts_voice` | server default | Voice name passed to the TTS server. |
| `sample_rate` | `16000` | Sample rate sent to the ASR server. |
| `sample_width` | `2` | Sample width in bytes (16-bit PCM). |
| `channels` | `1` | Channel count (mono). |

Give each server either a full `*_uri` or a `*_host` (the port then defaults to
the standard Wyoming port). A URI takes precedence over host and port. With no
ASR server configured, speech-to-text is disabled and logged; with no TTS server,
text-to-speech is disabled and logged.

## Choosing a voice

`tts_voice` is passed straight to the Wyoming TTS server as the voice name. For
`wyoming-piper` this is a Piper voice such as `en_US-lessac-medium` or
`en_GB-alba-medium`. Omit it to use the server's own default voice.

## Format

The default stream format is mono signed 16-bit PCM at 16 kHz. A payload states
its own sample rate and width; one this node cannot process is rejected rather
than misread. Set `sample_rate` / `sample_width` / `channels` only if your
satellites stream a different format and your ASR server accepts it.

---
[← Audio flow](audio_flow.md) · [Home](../README.md) · [Operations →](operations.md)
