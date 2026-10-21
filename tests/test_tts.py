"""FinesseTTSService against a fake finesse.v1 server, inside a real Pipecat pipeline."""

import asyncio
import json

import pytest
from pipecat.frames.frames import (
    ErrorFrame,
    InterruptionFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    TTSAudioRawFrame,
)
from pipecat.pipeline.worker import PipelineParams
from pipecat.tests.utils import SleepFrame, run_test
from pipecat.transcriptions.language import Language
from websockets.asyncio.server import serve

from pipecat_finesse import FinesseTTSService, language_to_finesse_language

VOICE = "6a996c5457317abcfd297003"
CHUNK = b"\x01\x00" * 2400  # 0.1 s of 24 kHz s16le


class FakeFinesse:
    """Minimal finesse.v1 server: audio per text frame, task_done on finalize."""

    def __init__(
        self,
        slow: bool = False,
        first_audio_delay: float = 0,
        server_cancels: bool = False,
        drop_connection: bool = False,
        cancel_after_done: bool = False,
    ):
        self.messages: list[dict] = []
        self.headers = None
        self.subprotocol = None
        self.slow = slow  # stream many chunks slowly so a cancel can land mid-turn
        self.first_audio_delay = first_audio_delay  # a slow engine, or a cold GPU
        self.server_cancels = server_cancels  # cancel the context after its first chunk
        self.drop_connection = drop_connection  # close the socket after the first chunk
        self.cancel_after_done = cancel_after_done  # answer task_cancel as if the turn had ended
        self._cancelled: set[str] = set()
        self._audio_tasks: dict[str, list[asyncio.Task]] = {}

    async def handler(self, ws):
        self.headers = ws.request.headers
        self.subprotocol = ws.subprotocol
        async for raw in ws:
            msg = json.loads(raw)
            self.messages.append(msg)
            ctx = msg.get("context_id")
            kind = msg["type"]
            if kind == "task_start":
                await ws.send(
                    json.dumps(
                        {
                            "type": "task_started",
                            "context_id": ctx,
                            "sample_rate": msg["sample_rate"],
                            "audio_format": "s16le",
                        }
                    )
                )
            if kind in ("task_start", "task_continue") and msg.get("text"):
                self._audio_tasks.setdefault(ctx, []).append(
                    asyncio.create_task(self._audio(ws, ctx))
                )
            if kind == "task_continue" and msg.get("finalize"):
                asyncio.create_task(self._done(ws, ctx))
            if kind == "task_cancel" and self.cancel_after_done:
                self._cancelled.add(ctx)
                await ws.send(
                    json.dumps(
                        {
                            "type": "error",
                            "context_id": ctx,
                            "code": "context_not_found",
                            "message": f"no open context '{ctx}'",
                            "fatal": False,
                        }
                    )
                )
            elif kind == "task_cancel":
                self._cancelled.add(ctx)
                await ws.send(
                    json.dumps(
                        {
                            "type": "task_cancelled",
                            "context_id": ctx,
                            "sentences_completed": 0,
                            "sentences_dropped": 1,
                        }
                    )
                )

    async def _done(self, ws, ctx):
        # Like the real server, task_done follows all of the context's audio.
        await asyncio.gather(*self._audio_tasks.get(ctx, []))
        await asyncio.sleep(0.05)
        if ctx not in self._cancelled and not ws.close_code:
            await ws.send(
                json.dumps(
                    {
                        "type": "task_done",
                        "context_id": ctx,
                        "num_sentences": 1,
                        "profile": {"ttfb_ms": 1},
                    }
                )
            )

    async def _audio(self, ws, ctx):
        if self.first_audio_delay:
            await asyncio.sleep(self.first_audio_delay)
        for i in range(20 if self.slow else 1):
            if ctx in self._cancelled:
                return
            await ws.send(
                json.dumps(
                    {
                        "type": "audio_chunk",
                        "context_id": ctx,
                        "sentence_index": 0,
                        "chunk_index": i,
                        "byte_length": len(CHUNK),
                    }
                )
            )
            await ws.send(CHUNK)
            if self.server_cancels:
                await ws.send(json.dumps({"type": "task_cancelled", "context_id": ctx}))
                return
            if self.drop_connection:
                await ws.close(code=1011, reason="upstream_error")
                return
            if self.slow:
                await asyncio.sleep(0.05)

    def of_type(self, kind: str) -> list[dict]:
        return [m for m in self.messages if m["type"] == kind]


async def run_turn(
    fake: FakeFinesse, frames, out_rate: int = 24000, upstream: bool = False, **kwargs
):
    async with serve(fake.handler, "127.0.0.1", 0, subprotocols=["finesse.v1"]) as server:
        port = server.sockets[0].getsockname()[1]
        tts = FinesseTTSService(
            api_key="flv_test",
            url=f"ws://127.0.0.1:{port}",
            settings=FinesseTTSService.Settings(voice=VOICE, language=Language.HI),
            **kwargs,
        )
        down, up = await run_test(
            tts,
            frames_to_send=frames,
            pipeline_params=PipelineParams(audio_out_sample_rate=out_rate),
        )
        return (down, up) if upstream else down


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
async def test_phone_rate_pipeline_requests_8k_without_resampling():
    fake = FakeFinesse()
    down = await run_turn(fake, one_turn(), out_rate=8000)
    assert fake.of_type("task_start")[0]["sample_rate"] == 8000
    audio = [f for f in down if isinstance(f, TTSAudioRawFrame)]
    assert audio and all(f.sample_rate == 8000 for f in audio)
    # Finesse produces 8 kHz itself, so its audio is passed through untouched.
    assert all(f.audio == CHUNK for f in audio)


@pytest.mark.asyncio
async def test_16k_pipeline_requests_24k_and_resamples_down():
    fake = FakeFinesse()
    down = await run_turn(fake, one_turn(), out_rate=16000)
    assert fake.of_type("task_start")[0]["sample_rate"] == 24000
    audio = [f for f in down if isinstance(f, TTSAudioRawFrame)]
    assert audio and all(f.sample_rate == 16000 for f in audio)
    # 16 kHz is two thirds of the samples of the 24 kHz chunks the server sent.
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


@pytest.mark.asyncio
async def test_a_cancel_that_crosses_the_end_of_the_turn_is_not_an_error():
    # The caller talks just as the turn finishes: the server has already closed the context.
    fake = FakeFinesse(slow=True, cancel_after_done=True)
    _, up = await run_turn(
        fake,
        [
            LLMFullResponseStartFrame(),
            LLMTextFrame("This is a long answer that the caller will talk over. "),
            LLMTextFrame("It keeps going"),
            SleepFrame(0.3),
            InterruptionFrame(),
            SleepFrame(0.3),
        ],
        upstream=True,
    )
    assert fake.of_type("task_cancel")
    assert not [f for f in up if isinstance(f, ErrorFrame)]


@pytest.mark.asyncio
async def test_first_audio_slower_than_pipecats_default_idle_timeout_is_played():
    # The LLM finishes quickly, but the first audio takes longer than Pipecat's default
    # 3 s idle timeout for an audio context.
    fake = FakeFinesse(first_audio_delay=3.5)
    frames = one_turn()
    frames[-1] = SleepFrame(4.5)
    down = await run_turn(fake, frames)
    assert [f for f in down if isinstance(f, TTSAudioRawFrame)]


@pytest.mark.asyncio
async def test_a_turn_the_server_cancels_is_reported():
    fake = FakeFinesse(server_cancels=True)
    _, up = await run_turn(fake, one_turn(), upstream=True)
    errors = [f for f in up if isinstance(f, ErrorFrame)]
    assert any("cancelled" in f.error for f in errors)


@pytest.mark.asyncio
async def test_a_connection_dropped_mid_turn_is_reported():
    fake = FakeFinesse(drop_connection=True)
    _, up = await run_turn(fake, one_turn(), upstream=True)
    errors = [f for f in up if isinstance(f, ErrorFrame)]
    assert any("connection" in f.error.lower() for f in errors)


def test_language_codes():
    assert language_to_finesse_language(Language.EN_US) == "en"
    assert language_to_finesse_language(Language.HI) == "hi"
    assert language_to_finesse_language(Language.TA_IN) == "ta"
    assert language_to_finesse_language(Language.OR) == "ory"
    assert language_to_finesse_language(Language.NE) == "npi"
    assert language_to_finesse_language("ar") == "arb"
    assert language_to_finesse_language("ar-SA") == "arb"


@pytest.mark.parametrize(
    ("language", "expected"),
    [
        ("ar-EG", "arz"),  # Egyptian Arabic
        ("ar-AE", "afb"),  # Gulf Arabic
        ("ar-KW", "afb"),
        ("ar-QA", "afb"),
        ("ar-BH", "afb"),
        ("ar-OM", "afb"),
        ("arz", "arz"),
        ("ars", "ars"),
        ("acw", "acw"),
        ("yue", "yue"),
        ("mni", "mni"),
    ],
)
def test_finesse_dialect_codes(language, expected):
    assert language_to_finesse_language(language) == expected


def test_missing_key_is_a_clear_error(monkeypatch):
    monkeypatch.delenv("FINESSE_API_KEY", raising=False)
    with pytest.raises(ValueError, match="FINESSE_API_KEY"):
        FinesseTTSService(settings=FinesseTTSService.Settings(voice=VOICE))
