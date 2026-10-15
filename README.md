# pipecat-finesse

[Finesse](https://finesse.flamapp.ai) text-to-speech for [Pipecat](https://github.com/pipecat-ai/pipecat).

Finesse streams speech in 60 languages, 22 of them Indian, from your own cloned voices or the
Finesse catalog. This package adds `FinesseTTSService`, which speaks over the Finesse realtime
WebSocket:

- **Low latency.** About 0.4 s to first audio on our production API (measured October 2026).
- **One context per turn.** All sentences of an LLM turn share one Finesse context, so prosody
  carries across them.
- **Interruptions.** When the user talks over the bot, the turn is cancelled on the server and its
  remaining audio is dropped; the connection stays open for the next turn.
- **Any pipeline rate.** Finesse synthesizes at 24 kHz; 16 kHz and 8 kHz (telephony) pipelines are
  resampled for you.

## Install

```bash
pip install pipecat-finesse
```

## Use

```python
from pipecat_finesse import FinesseTTSService

tts = FinesseTTSService(
    api_key=os.getenv("FINESSE_API_KEY"),  # optional: read from FINESSE_API_KEY by default
    settings=FinesseTTSService.Settings(
        voice="6a996c5457317abcfd297003",  # a Flam voice id
        language=Language.EN,
    ),
)
```

Put it in a pipeline where any other TTS service goes. `examples/voice_agent.py` is a complete
voice agent (Deepgram, OpenAI, Finesse) that runs in your browser:

```bash
cp .env.example .env   # fill in the keys
uv run --with "pipecat-ai[deepgram,openai,runner,silero,webrtc]" examples/voice_agent.py
```

### Getting a key and a voice

Create an API key in the [Finesse dashboard](https://finesse.flamapp.ai/dashboard) under
**Settings → API keys**. Pick a voice from the dashboard's voice library, or list them with
`GET https://fi.production.flamapis.com/flam-voice-svc/api/v1/public/voices`; use the voice's `id`.

### Settings

| Setting | Default | Notes |
|---|---|---|
| `voice` | required | Flam voice id |
| `language` | `Language.EN` | Pipecat `Language`; mapped to Finesse codes (Odia `ory`, Nepali `npi`, ...) |
| `num_step` | server default (32) | 1-32, quality vs latency |
| `guidance_scale` | server default (2.0) | 0-10 |
| `length_scale` | server default (1.0) | speaking rate, higher is slower |

Constructor options: `api_key`, `url` (realtime WebSocket URL), `sample_rate`, `idle_flush_ms`
(how long the server waits before speaking a partial sentence, 200-5000 ms) and `client_name`
(added to the `X-Finesse-Client` header so you can tell your apps apart in Finesse analytics).

Voice and language apply per turn: a change takes effect from the next turn.

## Errors and limits

- A refused connection reports the HTTP status and Finesse error code, e.g.
  `HTTP 429 (concurrency_limited, retry after 1s)` when you are at your plan's concurrent-stream
  limit, or `HTTP 503 (upstream_busy, ...)` when the service is at capacity. The service retries
  with backoff.
- Each open connection holds one of your plan's concurrent streams until it closes.
- Errors for one turn (for example `voice_not_found`) end that turn and leave the connection open.

API reference: [Realtime WebSocket](https://docs.finesse.flamapp.ai/voice-api/realtime-websocket).

## Development

```bash
uv sync
uv run pytest                    # unit tests against a fake server
FINESSE_LIVE=1 FINESSE_API_KEY=... FINESSE_VOICE_ID=... \
  FINESSE_URL=<non-production realtime URL> \
  uv run pytest tests/test_live.py
```

## License

BSD 2-Clause, the same as Pipecat.
