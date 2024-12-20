from __future__ import annotations

import os
import numpy as np
from dataclasses import dataclass
from typing import Any, Optional

import aiohttp
from livekit.agents import (
    DEFAULT_API_CONNECT_OPTIONS,
    APIConnectOptions,
    tts,
    utils
)

from .log import logger
from .models import TTSLanguages, TTSModels, TTSVoices, TTSEncoding

API_BASE_URL = "https://waves-api.smallest.ai/api/v1"
NUM_CHANNELS = 1

@dataclass
class _TTSOptions:
    model: TTSModels
    encoding: TTSEncoding
    sample_rate: int
    voice: TTSVoices
    api_key: str
    language: TTSLanguages
    add_wav_header: bool


class TTS(tts.TTS):
    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: TTSModels = "lightning",
        sample_rate: int = 24000,
        language: TTSLanguages = "en",
        voice: TTSVoices = "emily",
        transliterate: Optional[bool] = False,
        encoding: TTSEncoding = "pcm_s16le",
        http_session: aiohttp.ClientSession | None = None,
    ) -> None:
        """
        Create a new instance of smallest.ai Waves TTS.
        Args:
            model (TTSModels, optional): The Waves TTS model to use. Defaults to "lightning".
            language (TTSLanguages, optional): The language code for synthesis. Defaults to "en".
            encoding (TTSEncoding, optional): The audio encoding format. Defaults to "pcm_s16le".
            voice (VoiceSettings, optional): The voice settings to use. Defaults to "emily".
            sample_rate (int, optional): The audio sample rate in Hz. Defaults to 24000.
            api_key (str, optional): The smallest.ai API key. If not provided, it will be read from the SMALLEST_API_KEY environment variable.
            add_wav_header (bool, optional): If True, includes a WAV header in the audio output; otherwise, only raw audio data is returned.
            http_session (aiohttp.ClientSession | None, optional): An existing aiohttp ClientSession to use. If not provided, a new session will be created.
        """

        super().__init__(
            capabilities=tts.TTSCapabilities(streaming=False),
            sample_rate=sample_rate,
            num_channels=NUM_CHANNELS,
        )

        api_key = api_key or os.environ.get("SMALLEST_API_KEY")
        if not api_key:
            raise ValueError("SMALLEST_API_KEY must be set")

        self._opts = _TTSOptions(
            model=model,
            language=language,
            encoding=encoding,
            sample_rate=sample_rate,
            voice=voice,
            api_key=api_key,
            add_wav_header=False
        )
        self._session = http_session

    def _ensure_session(self) -> aiohttp.ClientSession:
        if not self._session:
            self._session = utils.http_context.http_session()

        return self._session

    def synthesize(
        self,
        text: str,
        *,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
    ) -> "ChunkedStream":
        return ChunkedStream(
            tts=self,
            text=text,
            conn_options=conn_options,
            opts=self._opts,
            session=self._ensure_session(),
        )


class ChunkedStream(tts.ChunkedStream):
    """Synthesize chunked text using the Waves API endpoint"""

    def __init__(
        self,
        tts: TTS,
        text: str,
        opts: _TTSOptions,
        conn_options: APIConnectOptions,
        session: aiohttp.ClientSession,
    ) -> None:
        super().__init__(tts=tts, input_text=text, conn_options=conn_options)
        self._text, self._opts, self._session = text, opts, session

    @utils.log_exceptions(logger=logger)
    async def _run(self):
        bstream = utils.audio.AudioByteStream(
            sample_rate=self._opts.sample_rate, num_channels=NUM_CHANNELS
        )
        request_id, segment_id = utils.shortuuid(), utils.shortuuid()

        data = _to_smallest_options(self._opts)
        data["text"] = self._text

        url = f"{API_BASE_URL}/{self._opts.model}/get_speech"
        headers = {
            "Authorization": f"Bearer {self._opts.api_key}",
            "Content-Type": "application/json",
        }

        async with self._session.post(url, headers=headers, json=data) as resp:
            if resp.status != 200:
                error_text = await resp.text()
                raise Exception(f"smallest.ai API error: {resp.status} - {error_text}")
            
            async for data, _ in resp.content.iter_chunks():
                for frame in bstream.write(data):
                    self._event_ch.send_nowait(
                        tts.SynthesizedAudio(
                            request_id=request_id, segment_id=segment_id, frame=frame
                        )
                    )

            for frame in bstream.flush():
                self._event_ch.send_nowait(
                    tts.SynthesizedAudio(
                        request_id=request_id, segment_id=segment_id, frame=frame
                    )
                )


def _to_smallest_options(opts: _TTSOptions) -> dict[str, Any]:
    return {
        "voice_id": opts.voice,
        "sample_rate": opts.sample_rate,
        "add_wav_header": opts.add_wav_header,
    }