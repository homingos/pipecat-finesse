# Changelog

## 0.1.0 (unreleased)

- `FinesseTTSService`: Finesse realtime text-to-speech over the `finesse.v1` WebSocket.
  One context per LLM turn, server-side cancel on interruption, resampling for pipelines below
  24 kHz, reconnect after idle timeouts, `X-Finesse-Client` attribution.
