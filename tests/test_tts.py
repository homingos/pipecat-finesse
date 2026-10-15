"""FinesseTTSService against a fake finesse.v1 server, inside a real Pipecat pipeline."""

import asyncio
import json

import pytest
from websockets.asyncio.server import serve

from pipecat.frames.frames import (
    InterruptionFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    TTSAudioRawFrame,
)
from pipecat.pipeline.worker import PipelineParams
from pipecat.tests.utils import SleepFrame, run_test
from pipecat.transcriptions.language import Language

from pipecat_finesse import FinesseTTSService, language_to_finesse_language

VOICE = "6a996c5457317abcfd297003"
CHUNK = b"\x01\x00" * 2400  # 0.1 s of 24 kHz s16le


class FakeFinesse:
    """Minimal finesse.v1 server: audio per text frame, task_done on finalize."""

    def __init__(self, slow: bool = False):
        self.messages: list[dict] = []
        self.headers = None
        self.subprotocol = None
        self.slow = slow  # stream many chunks slowly so a cancel can land mid-turn
        self._cancelled: set[str] = set()

    async def handler(self, ws):
        self.headers = ws.request.headers
        self.subprotocol = ws.subprotocol
        async for raw in ws:
            msg = json.loads(raw)
            self.messages.append(msg)
            ctx = msg.get("context_id")
            kind = msg["type"]
            if kind == "task_start":
                await ws.send(json.dumps({"type": "task_started", "context_id": ctx, "sample_rate": msg["sample_rate"], "audio_format": "s16le"}))
            if kind in ("task_start", "task_continue") and msg.get("text"):
                asyncio.create_task(self._audio(ws, ctx))
            if kind == "task_continue" and msg.get("finalize"):
                await asyncio.sleep(0.05)
                await ws.send(json.dumps({"type": "task_done", "context_id": ctx, "num_sentences": 1, "profile": {"ttfb_ms": 1}}))
            if kind == "task_cancel":
                self._cancelled.add(ctx)
                await ws.send(json.dumps({"type": "task_cancelled", "context_id": ctx, "sentences_completed": 0, "sentences_dropped": 1}))

    async def _audio(self, ws, ctx):
        for i in range(20 if self.slow else 1):
            if ctx in self._cancelled:
                return
            await ws.send(json.dumps({"type": "audio_chunk", "context_id": ctx, "sentence_index": 0, "chunk_index": i, "byte_length": len(CHUNK)}))
            await ws.send(CHUNK)
            if self.slow:
                await asyncio.sleep(0.05)

    def of_type(self, kind: str) -> list[dict]:
        return [m for m in self.messages if m["type"] == kind]


async def run_turn(fake: FakeFinesse, frames, out_rate: int = 24000):
    async with serve(fake.handler, "127.0.0.1", 0, subprotocols=["finesse.v1"]) as server:
        port = server.sockets[0].getsockname()[1]
        tts = FinesseTTSService(
            api_key="flv_test",
            url=f"ws://127.0.0.1:{port}",
            settings=FinesseTTSService.Settings(voice=VOICE, language=Language.HI),
        )
        down, _ = await run_test(
            tts,
            frames_to_send=frames,
            pipeline_params=PipelineParams(audio_out_sample_rate=out_rate),
        )
        return down


def one_turn():
    return [
        LLMFullResponseStartFrame(),
        LLMTextFrame("Hello there. "),
        LLMTextFrame("How can I help you today?"),
        LLMFullResponseEndFrame(),
        SleepFrame(0.5),
    ]


@pytest.mark.asyncio
async def test_turn_opens_continues_and_finalizes_one_context():
    fake = FakeFinesse()
    down = await run_turn(fake, one_turn())

    starts, continues = fake.of_type("task_start"), fake.of_type("task_continue")
    assert len(starts) == 1
    start = starts[0]
    assert start["voice_id"] == VOICE
    assert start["language"] == "hi"
    assert start["output_format"] == "s16le"
    assert start["sample_rate"] == 24000
    assert "Hello there." in start["text"]
    # Every later message targets the same context, and the turn ends with finalize.
    assert {m["context_id"] for m in continues} == {start["context_id"]}
    assert continues[-1]["finalize"] is True and continues[-1]["text"] == ""
    assert any("How can I help" in m["text"] for m in continues if not m["finalize"])

    audio = [f for f in down if isinstance(f, TTSAudioRawFrame)]
    assert audio and all(f.sample_rate == 24000 for f in audio)


@pytest.mark.asyncio
async def test_handshake_sends_key_attribution_and_subprotocol():
    fake = FakeFinesse()
    await run_turn(fake, one_turn())
    assert fake.subprotocol == "finesse.v1"
    assert fake.headers["X-API-Key"] == "flv_test"
    assert fake.headers["X-Finesse-Client"].startswith("pipecat/")


@pytest.mark.asyncio
async def test_phone_rate_pipeline_requests_24k_and_resamples_down():
    fake = FakeFinesse()
    down = await run_turn(fake, one_turn(), out_rate=8000)
    assert fake.of_type("task_start")[0]["sample_rate"] == 24000
    audio = [f for f in down if isinstance(f, TTSAudioRawFrame)]
    assert audio and all(f.sample_rate == 8000 for f in audio)
    # 8 kHz is a third of the samples of the 24 kHz chunks the server sent.
    assert sum(len(f.audio) for f in audio) < len(CHUNK) * len(audio)


@pytest.mark.asyncio
async def test_interruption_cancels_the_turn_on_the_server():
    fake = FakeFinesse(slow=True)
    await run_turn(
        fake,
        [
            LLMFullResponseStartFrame(),
            LLMTextFrame("This is a long answer that the caller will talk over. "),
            # The sentence aggregator releases a sentence once the next text arrives.
            LLMTextFrame("It keeps going"),
            SleepFrame(0.3),
            InterruptionFrame(),
            SleepFrame(0.3),
        ],
    )
    starts, cancels = fake.of_type("task_start"), fake.of_type("task_cancel")
    assert len(cancels) == 1
    assert cancels[0]["context_id"] == starts[0]["context_id"]


def test_language_codes():
    assert language_to_finesse_language(Language.EN_US) == "en"
    assert language_to_finesse_language(Language.HI) == "hi"
    assert language_to_finesse_language(Language.TA_IN) == "ta"
    assert language_to_finesse_language(Language.OR) == "ory"
    assert language_to_finesse_language(Language.NE) == "npi"
    assert language_to_finesse_language("ar-EG") == "arb"


def test_missing_key_is_a_clear_error(monkeypatch):
    monkeypatch.delenv("FINESSE_API_KEY", raising=False)
    with pytest.raises(ValueError, match="FINESSE_API_KEY"):
        FinesseTTSService(settings=FinesseTTSService.Settings(voice=VOICE))
