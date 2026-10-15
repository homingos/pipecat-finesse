# Pipecat community listing: submission kit

Process from Pipecat's `COMMUNITY_INTEGRATIONS.md` (checked 15 Oct 2026). Our code stays in our
repo; the listing is a PR to https://github.com/pipecat-ai/docs.

## Before opening the PR

- [ ] Public repo is live. **Replace `https://github.com/homingos/pipecat-finesse`** in
      `finesse.mdx` (3 places) if the repo ends up elsewhere.
- [ ] `pipecat-finesse` 0.1.0 is published on PyPI.
- [ ] Demo video (30-60 s) recorded: the example agent speaking, and the user interrupting it
      mid-sentence so it stops and answers the new question.
- [ ] Joined the Pipecat Discord: https://discord.gg/pipecat

## Changes in the fork of pipecat-ai/docs

1. Add `api-reference/server/services/tts/finesse.mdx` (the file next to this one).
2. In `api-reference/server/services/supported-services.mdx`, add this row to the
   **Text-to-Speech** table in alphabetical order (between Deepgram/ElevenLabs/Fish rows and Floe):

   ```
   | [Finesse](/api-reference/server/services/tts/finesse)             | `uv add pipecat-finesse`                                                              | Community  |
   ```

3. In `docs.json`, add `"api-reference/server/services/tts/finesse",` to the TTS navigation group
   (alphabetical, like `"api-reference/server/services/tts/vakyam"`), and a redirect:

   ```json
   {
     "source": "/server/services/tts/finesse",
     "destination": "/api-reference/server/services/tts/finesse"
   }
   ```

4. Run `npx mint broken-links` (their CI runs it too).

## PR description

> **Add Finesse TTS (community integration)**
>
> Adds a docs page and a Supported Services row for
> [pipecat-finesse](https://github.com/homingos/pipecat-finesse), a community-maintained TTS
> integration built and maintained by Flam, the company behind
> [Finesse](https://finesse.flamapp.ai).
>
> - `FinesseTTSService` (`WebsocketTTSService`) over the Finesse realtime WebSocket: one context per
>   LLM turn, server-side cancel on interruption, resampling for 8/16 kHz pipelines.
> - 60 languages, including 22 Indian languages, and cloned voices.
> - Tested with Pipecat v1.12.0.
>
> Demo video: <link>

After opening it, post the PR link in `#community-integrations` on Discord.
