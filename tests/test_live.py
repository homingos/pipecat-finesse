"""Live checks against a real Finesse server. Opt in with FINESSE_LIVE=1.

Needs FINESSE_API_KEY and FINESSE_VOICE_ID; FINESSE_URL defaults to production.
"""

import os

import pytest
from pipecat.frames.frames import (
    InterruptionFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    TTSAudioRawFrame,
)
from pipecat.pipeline.worker import PipelineParams
from pipecat.tests.utils import SleepFrame, run_test

from pipecat_finesse import FinesseTTSService
from pipecat_finesse.tts import DEFAULT_URL

pytestmark = pytest.mark.skipif(
    os.getenv("FINESSE_LIVE") != "1", reason="set FINESSE_LIVE=1 to run"
)


def service() -> FinesseTTSService:
    return FinesseTTSService(
        url=os.getenv("FINESSE_URL", DEFAULT_URL),
        settings=FinesseTTSService.Settings(voice=os.environ["FINESSE_VOICE_ID"]),
    )


@pytest.mark.asyncio
async def test_live_turn_streams_audio():
    down, _ = await run_test(
        service(),
        frames_to_send=[
            LLMFullResponseStartFrame(),
            LLMTextFrame("Hi, welcome to Nova Burgers. "),
            LLMTextFrame("What can I get started for you today?"),
            LLMFullResponseEndFrame(),
            SleepFrame(8),
        ],
        start_timeout=90,
    )
    audio = [f for f in down if isinstance(f, TTSAudioRawFrame)]
    seconds = sum(len(f.audio) for f in audio) / 2 / 24000
    assert seconds > 1.5, f"only {seconds:.2f}s of audio"


@pytest.mark.asyncio
async def test_live_phone_rate_and_interruption():
    down, up = await run_test(
        service(),
        frames_to_send=[
            # Warm-up turn: a scaled-down engine (dev) can take seconds to first audio.
            LLMFullResponseStartFrame(),
            LLMTextFrame("Warming up. "),
            LLMTextFrame("One moment."),
            LLMFullResponseEndFrame(),
            SleepFrame(8),
            LLMFullResponseStartFrame(),
            LLMTextFrame("This is a long answer that keeps going so the caller can talk over it. "),
            LLMTextFrame("It has a second sentence, and then a third one after that."),
            SleepFrame(2),
            InterruptionFrame(),
            SleepFrame(2),
        ],
        pipeline_params=PipelineParams(audio_out_sample_rate=8000),
        start_timeout=90,
    )
    audio = [f for f in down if isinstance(f, TTSAudioRawFrame)]
    assert audio and all(f.sample_rate == 8000 for f in audio)
    errors = [f for f in up if f.__class__.__name__ == "ErrorFrame"]
    assert not errors, errors


@pytest.mark.asyncio
async def test_live_16k_pipeline_is_resampled():
    # Finesse has no 16 kHz output: the service asks for 24 kHz and resamples down itself.
    down, up = await run_test(
        service(),
        frames_to_send=[
            LLMFullResponseStartFrame(),
            LLMTextFrame("Thanks for waiting. "),
            LLMTextFrame("Your table for two is booked for seven tonight."),
            LLMFullResponseEndFrame(),
            SleepFrame(8),
        ],
        pipeline_params=PipelineParams(audio_out_sample_rate=16000),
        start_timeout=90,
    )
    audio = [f for f in down if isinstance(f, TTSAudioRawFrame)]
    assert audio and all(f.sample_rate == 16000 for f in audio)
    seconds = sum(len(f.audio) for f in audio) / 2 / 16000
    assert seconds > 1.5, f"only {seconds:.2f}s of audio"
    errors = [f for f in up if f.__class__.__name__ == "ErrorFrame"]
    assert not errors, errors
