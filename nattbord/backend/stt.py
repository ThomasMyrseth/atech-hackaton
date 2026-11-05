"""Speech to text with ElevenLabs Scribe, the same streaming API as morse/trainer.py.

The board records the whole utterance before we get it, so we send it all at once and commit manually.
All transcribed speech leaves the laptop.
"""
from __future__ import annotations

import base64
import json
import os
import time
from typing import Callable, Optional
from urllib.parse import urlencode

import numpy as np

from backend.audio_in import normalize
from backend.log import log
from backend.plugin import ExternalError

STT_URL = "wss://api.elevenlabs.io/v1/speech-to-text/realtime?" + urlencode({
    "model_id": "scribe_v2_realtime",
    "audio_format": "pcm_16000",
    "commit_strategy": "manual",
    "language_code": "en",
    "keyterms": ["calendar", "lecture", "study group", "assignment", "deadline", "alarm clock", "night light"],
}, doseq=True)
CHUNK_BYTES = 8000  # 250 ms
TIMEOUT_S = 5.0     # the voice loop waits on this
SHORT_ANSWERS = {"yes", "yeah", "yep", "sure", "okay", "ok", "no", "nope"}  # single words that still answer a question


def open_socket():
    from websockets.sync.client import connect

    ws = connect(STT_URL, additional_headers={"xi-api-key": os.environ["ELEVENLABS_API_KEY"]},
                 open_timeout=TIMEOUT_S, close_timeout=1, legacy=True)
    started = json.loads(ws.recv(timeout=TIMEOUT_S))  # audio sent before this can be lost
    if started.get("message_type") != "session_started":
        ws.close()
        raise ExternalError(f"refused: {started.get('message_type')}")
    return ws


class STT:
    def __init__(self, connect: Optional[Callable] = None) -> None:
        if connect is None and not os.getenv("ELEVENLABS_API_KEY"):
            raise SystemExit("Speech to text needs ELEVENLABS_API_KEY in nattbord/.env.")
        self.connect = connect or open_socket

    def _stream(self, audio: bytes) -> str:
        ws = self.connect()
        try:
            for i in range(0, len(audio), CHUNK_BYTES):
                ws.send(json.dumps({"message_type": "input_audio_chunk", "sample_rate": 16000,
                                    "audio_base_64": base64.b64encode(audio[i:i + CHUNK_BYTES]).decode(),
                                    "commit": i + CHUNK_BYTES >= len(audio)}))
            deadline = time.monotonic() + TIMEOUT_S
            while True:
                message = json.loads(ws.recv(timeout=max(0.0, deadline - time.monotonic())))
                kind = message.get("message_type")
                if kind == "committed_transcript":
                    return message.get("text", "")
                if kind == "insufficient_audio_activity":
                    return ""
                if kind not in ("partial_transcript", "warning"):
                    raise ExternalError(f"{kind}: {message.get('error', '')}")
        finally:
            ws.close()

    def transcribe(self, pcm: np.ndarray) -> Optional[str]:
        """Returns text, or None if it was probably not speech. ExternalError if the service fails."""
        if not len(pcm):
            return None
        t0 = time.monotonic()
        audio = (normalize(pcm) * 32767).astype("<i2").tobytes()
        try:
            text = self._stream(audio).strip()
        except ExternalError:
            raise
        except Exception as e:  # noqa: BLE001 – network, timeout or closed socket
            raise ExternalError(f"{type(e).__name__}: {e}") from e
        dt = time.monotonic() - t0
        words = len(text.split())
        if words < 2 and text.lower().strip(" .,!?") not in SHORT_ANSWERS:
            log("stt", f"[dim]discarded \"{text}\" (words={words}, {dt:.2f} s)[/dim]")
            return None
        log("stt", f"\"{text}\"  [dim]({dt:.2f} s)[/dim]")
        return text
