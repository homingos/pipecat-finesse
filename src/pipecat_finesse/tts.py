#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Finesse text-to-speech service for Pipecat.

Streams speech over the Finesse realtime WebSocket (``finesse.v1``). Each LLM
turn is one Finesse context: the turn's first text opens it with
``task_start``, later text is appended with ``task_continue``, the end of the
turn sends ``finalize: true``, and an interruption sends ``task_cancel`` so the
server stops synthesizing that turn without dropping the connection.
"""

import json
import os
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from typing import Any

from loguru import logger
from websockets.protocol import State

from pipecat.frames.frames import ErrorFrame, Frame, TTSAudioRawFrame, TTSStoppedFrame
from pipecat.processors.frame_processor import FrameProcessorSetup
from pipecat.services.settings import TTSSettings
from pipecat.services.tts_service import WebsocketTTSService
from pipecat.transcriptions.language import Language
from pipecat.utils.tracing.service_decorators import traced_tts
from pipecat.utils.types import NOT_GIVEN, NotGiven

from pipecat_finesse._version import __version__

DEFAULT_URL = "wss://fi.production.flamapis.com/flam-voice-svc/api/v1/public/stream"
SUBPROTOCOL = "finesse.v1"
# The engine synthesizes at 24 kHz and can only upsample, so lower pipeline
# rates (16 kHz, 8 kHz telephony) are resampled here.
NATIVE_SAMPLE_RATE = 24000

# Finesse language codes that differ from the base code of Pipecat's Language.
_LANGUAGE_OVERRIDES = {"ar": "arb", "ne": "npi", "no": "nb", "or": "ory"}


def language_to_finesse_language(language: Language | str) -> str:
    """Convert a Pipecat Language (or code) to a Finesse language code.

    Finesse uses base ISO 639 codes (``en``, ``hi``, ``ta``), except where it
    names a macrolanguage member: Arabic ``arb``, Nepali ``npi``, Norwegian
    ``nb`` and Odia ``ory``.
    """
    value = language.value if isinstance(language, Language) else str(language)
    base = value.split("-")[0].lower()
    return _LANGUAGE_OVERRIDES.get(base, base)


@dataclass
class FinesseTTSSettings(TTSSettings):
    """Settings for FinesseTTSService.

    ``voice`` is a Flam voice id (24 hex characters, from the dashboard or
    ``GET /public/voices``). The numeric settings are optional; ``None`` leaves
    the server default.

    Parameters:
        num_step: Quality vs latency, 1-32 (server default 32).
        guidance_scale: 0-10 (server default 2.0).
        length_scale: Speaking-rate control, greater than 0 up to 5 (server
            default 1.0; higher is slower).
    """

    num_step: int | None | NotGiven = field(default_factory=lambda: NOT_GIVEN)
    guidance_scale: float | None | NotGiven = field(default_factory=lambda: NOT_GIVEN)
    length_scale: float | None | NotGiven = field(default_factory=lambda: NOT_GIVEN)


class FinesseTTSService(WebsocketTTSService):
    """Finesse realtime text-to-speech over one WebSocket per pipeline.

    Example::

        tts = FinesseTTSService(
            api_key=os.getenv("FINESSE_API_KEY"),
            settings=FinesseTTSService.Settings(
                voice="6a996c5457317abcfd297003",
                language=Language.EN,
            ),
        )
    """

    Settings = FinesseTTSSettings
    _settings: Settings

    def __init__(
        self,
        *,
        api_key: str | None = None,
        url: str = DEFAULT_URL,
        sample_rate: int | None = None,
        idle_flush_ms: int | None = None,
        client_name: str | None = None,
        settings: Settings | None = None,
        **kwargs,
    ):
        """Initialize the service.

        Args:
            api_key: Finesse API key (``flv_...``). Defaults to the
                ``FINESSE_API_KEY`` environment variable.
            url: Realtime WebSocket URL.
            sample_rate: Output sample rate. Defaults to the pipeline's rate.
            idle_flush_ms: Synthesize a pending partial sentence after this much
                silence (server default 800, clamped 200-5000).
            client_name: Extra ``name/version`` appended to the
                ``X-Finesse-Client`` attribution header, e.g. your app's name.
            settings: Runtime-updatable settings (voice, language, ...).
            **kwargs: Passed to ``WebsocketTTSService``.
        """
        key = api_key or os.getenv("FINESSE_API_KEY")
        if not key:
            raise ValueError("Finesse API key missing: pass api_key or set FINESSE_API_KEY")

        default_settings = self.Settings(
            model=None,
            voice=None,
            language=Language.EN,
            num_step=None,
            guidance_scale=None,
            length_scale=None,
        )
        if settings is not None:
            default_settings.apply_update(settings)

        super().__init__(
            push_text_frames=True,
            push_stop_frames=False,
            push_start_frame=True,
            pause_frame_processing=False,
            sample_rate=sample_rate,
            settings=default_settings,
            **kwargs,
        )

        self._api_key = key
        self._url = url
        self._idle_flush_ms = idle_flush_ms
        self._client = f"pipecat/{__version__}" + (f" {client_name}" if client_name else "")
        self._receive_task = None
        # Contexts opened with task_start on the current connection and not yet
        # done, cancelled or failed. The server forgets them on reconnect.
        self._open_contexts: set[str] = set()
        # The context announced by the last audio_chunk; its audio is the next
        # binary frame.
        self._pending_audio_context: str | None = None

    def can_generate_metrics(self) -> bool:
        """Finesse reports time to first audio and usage metrics."""
        return True

    def language_to_service_language(self, language: Language) -> str | None:
        """Convert a Pipecat Language to a Finesse language code."""
        return language_to_finesse_language(language)

    # ---- connection -------------------------------------------------------

    async def setup(self, setup: FrameProcessorSetup):
        """Set up the service and open the socket early, so the first turn is warm."""
        await super().setup(setup)
        await self._connect()

    async def _connect(self):
        await super()._connect()
        await self._connect_websocket()
        if self._websocket and not self._receive_task:
            self._receive_task = self.create_task(self._receive_task_handler(self._report_error))

    async def _disconnect(self):
        await super()._disconnect()
        if self._receive_task:
            await self.cancel_task(self._receive_task)
            self._receive_task = None
        await self._disconnect_websocket()

    async def _connect_websocket(self):
        try:
            if self._websocket and self._websocket.state is State.OPEN:
                return
            logger.debug(f"{self}: connecting to Finesse")
            self._websocket = await self._websocket_connect(
                self._url,
                subprotocols=[SUBPROTOCOL],
                additional_headers={"X-API-Key": self._api_key, "X-Finesse-Client": self._client},
            )
            self._open_contexts.clear()
            self._pending_audio_context = None
            await self._call_event_handler("on_connected")
        except Exception as e:
            await self.push_error(error_msg=f"Finesse connection error: {_describe(e)}", exception=e)
            self._websocket = None
            await self._call_event_handler("on_connection_error", _describe(e))

    async def _disconnect_websocket(self):
        try:
            await self.stop_all_metrics()
            if self._websocket:
                logger.debug(f"{self}: disconnecting from Finesse")
                await self._websocket.close()
        except Exception as e:
            await self.push_error(error_msg=f"Finesse error closing websocket: {e}", exception=e)
        finally:
            await self.remove_active_audio_context()
            self._websocket = None
            self._open_contexts.clear()
            await self._call_event_handler("on_disconnected")

    def _get_websocket(self):
        if self._websocket:
            return self._websocket
        raise Exception("Websocket not connected")

    async def _send(self, msg: dict[str, Any]):
        await self._get_websocket().send(json.dumps(msg))

    # ---- turn lifecycle ---------------------------------------------------

    def _output_sample_rate(self) -> int:
        """The rate requested from Finesse: the pipeline's, or 24 kHz to resample down."""
        return max(self.sample_rate, NATIVE_SAMPLE_RATE)

    def _task_start(self, text: str, context_id: str) -> dict[str, Any]:
        language = self._settings.language
        msg: dict[str, Any] = {
            "type": "task_start",
            "context_id": context_id,
            "voice_id": self._settings.voice,
            "text": text,
            "finalize": False,
            "language": language_to_finesse_language(language) if language else "en",
            "output_format": "s16le",
            "sample_rate": self._output_sample_rate(),
        }
        for name in ("num_step", "guidance_scale", "length_scale"):
            value = getattr(self._settings, name)
            if value not in (None, NOT_GIVEN):
                msg[name] = value
        if self._idle_flush_ms is not None:
            msg["idle_flush_ms"] = self._idle_flush_ms
        return msg

    @traced_tts
    async def run_tts(self, text: str, context_id: str) -> AsyncGenerator[Frame | None, None]:
        """Send text for a turn: the turn's first text opens the context, later text appends.

        Args:
            text: Text to synthesize.
            context_id: The turn's context id (reused for every sentence of a turn).

        Yields:
            None: audio arrives on the receive task.
        """
        if not self._settings.voice:
            yield ErrorFrame(error="Finesse voice not set: pass settings=FinesseTTSService.Settings(voice=...)")
            return
        try:
            if not self._websocket or self._websocket.state is State.CLOSED:
                await self._connect()
            try:
                if context_id in self._open_contexts:
                    await self._send({"type": "task_continue", "context_id": context_id, "text": text, "finalize": False})
                else:
                    await self._send(self._task_start(text, context_id))
                    self._open_contexts.add(context_id)
                await self.start_tts_usage_metrics(text)
            except Exception as e:
                yield ErrorFrame(error=f"Finesse send error: {e}")
                yield TTSStoppedFrame(context_id=context_id)
                await self._disconnect()
                await self._connect()
                return
            yield None
        except Exception as e:
            yield ErrorFrame(error=f"Finesse error: {e}")

    async def flush_audio(self, context_id: str | None = None):
        """End the turn: synthesize what's buffered and close the context."""
        flush_id = context_id or self.get_active_audio_context_id()
        if not flush_id or flush_id not in self._open_contexts or not self._websocket:
            return
        logger.trace(f"{self}: finalizing context {flush_id}")
        await self._send({"type": "task_continue", "context_id": flush_id, "text": "", "finalize": True})

    async def on_audio_context_interrupted(self, context_id: str):
        """Cancel the interrupted turn on the server; the connection stays open."""
        await self.stop_all_metrics()
        if context_id in self._open_contexts:
            self._open_contexts.discard(context_id)
            try:
                await self._send({"type": "task_cancel", "context_id": context_id})
            except Exception as e:
                logger.debug(f"{self}: task_cancel not sent: {e}")
        await super().on_audio_context_interrupted(context_id)

    async def _update_settings(self, delta: TTSSettings) -> dict[str, Any]:
        """Apply a settings delta. Voice and language are fixed per context, so a
        change takes effect from the next turn."""
        return await super()._update_settings(delta)

    # ---- receiving --------------------------------------------------------

    async def _receive_messages(self):
        while True:
            await self._process_messages()
            # The server closes an idle session after 600 s (session_ended,
            # reason idle); reopen so the next turn doesn't pay for a dial.
            logger.debug(f"{self}: Finesse connection closed, reconnecting")
            await self._connect_websocket()

    async def _process_messages(self):
        async for message in self._get_websocket():
            if isinstance(message, (bytes, bytearray)):
                await self._handle_audio(bytes(message))
            else:
                await self._handle_event(json.loads(message))

    async def _handle_audio(self, audio: bytes):
        ctx_id, self._pending_audio_context = self._pending_audio_context, None
        # Audio for a turn that was interrupted (or already removed) is dropped.
        if not ctx_id or not self.audio_context_available(ctx_id):
            return
        rate = self._output_sample_rate()
        if rate != self.sample_rate:
            audio = await self._resampler.resample(audio, rate, self.sample_rate)
        frame = TTSAudioRawFrame(audio=audio, sample_rate=self.sample_rate, num_channels=1, context_id=ctx_id)
        await self.append_to_audio_context(ctx_id, frame)

    async def _handle_event(self, msg: dict[str, Any]):
        kind = msg.get("type")
        ctx_id = msg.get("context_id")
        if kind == "audio_chunk":
            await self.stop_ttfb_metrics()
            self._pending_audio_context = ctx_id
        elif kind in ("task_started", "sentence_done"):
            pass
        elif kind == "task_done":
            self._open_contexts.discard(ctx_id)
            if ctx_id and self.audio_context_available(ctx_id):
                await self.append_to_audio_context(ctx_id, TTSStoppedFrame(context_id=ctx_id))
                await self.remove_audio_context(ctx_id)
        elif kind == "task_cancelled":
            self._open_contexts.discard(ctx_id)
        elif kind == "task_failed":
            self._open_contexts.discard(ctx_id)
            await self._end_context_with_error(ctx_id, f"Finesse task failed: {msg.get('code')}")
        elif kind == "error":
            code, text = msg.get("code"), msg.get("message", "")
            if ctx_id:
                self._open_contexts.discard(ctx_id)
                await self._end_context_with_error(ctx_id, f"Finesse error {code}: {text}")
            else:
                await self.push_error(error_msg=f"Finesse error {code}: {text}")
            if msg.get("fatal"):
                logger.warning(f"{self}: fatal Finesse error {code}; the session will close")
        elif kind == "session_ended":
            logger.debug(f"{self}: Finesse session ended ({msg.get('reason')})")
        else:
            logger.debug(f"{self}: ignoring Finesse message type {kind!r}")

    async def _end_context_with_error(self, ctx_id: str | None, error: str):
        await self.stop_all_metrics()
        await self.push_error(error_msg=error)
        if ctx_id and self.audio_context_available(ctx_id):
            await self.append_to_audio_context(ctx_id, TTSStoppedFrame(context_id=ctx_id))
            await self.remove_audio_context(ctx_id)


def _describe(e: Exception) -> str:
    """A handshake refusal's HTTP status and error code, else the exception text."""
    response = getattr(e, "response", None)
    if response is None:
        return str(e)
    status = getattr(response, "status_code", "?")
    try:
        body = json.loads(bytes(response.body or b"").decode())
        code = body.get("code") or body.get("error")
        retry_after = response.headers.get("Retry-After")
        detail = f"{code}" + (f", retry after {retry_after}s" if retry_after else "")
    except Exception:
        detail = "no detail"
    return f"HTTP {status} ({detail})"
