# Changelog

## 0.1.0 (unreleased)

- `FinesseTTSService`: Finesse realtime text-to-speech over the `finesse.v1` WebSocket.
  One context per LLM turn, server-side cancel on interruption, 8 kHz telephony audio straight
  from Finesse and resampling for other pipelines below 24 kHz, reconnect after idle timeouts,
  `X-Finesse-Client` attribution.
- Audio that arrives more than 3 s into a turn (a slow first audio, a long first sentence) is
  played instead of dropped: `stop_frame_timeout_s` defaults to 10 s.
- A turn the server cancels, or one cut off by a dropped connection, ends with an error instead of
  going silent.
- Egyptian Arabic (`ar-EG`) maps to `arz` and Gulf Arabic (`ar-AE`, `ar-KW`, `ar-QA`, `ar-BH`,
  `ar-OM`) to `afb`; Finesse codes such as `ars` and `yue` pass through.
- Text is sent as UTF-8 and split below the 1 MiB frame limit.
- Tested with Pipecat 1.12 and 1.13.
