"""Tale til tekst med faster-whisper (lokalt, norsk)."""
from __future__ import annotations

import os
import time
from typing import Optional

import numpy as np

from backend.audio_in import normalize
from backend.log import log

PROMPT = "Kalender, forelesning, kollokvie, øving, innlevering, vekkerklokke, nattlys, i morgen."


class STT:
    def __init__(self, model: str = "", compute_type: str = "int8"):
        from faster_whisper import WhisperModel

        model = model or os.getenv("WHISPER_MODEL", "small")
        t0 = time.monotonic()
        self.model = WhisperModel(model, device="cpu", compute_type=compute_type)
        # oppvarming så første ekte forespørsel ikke er treg
        self.model.transcribe(np.zeros(16000, dtype=np.float32), language="no", beam_size=1)
        log("stt", f"whisper '{model}' klar ({time.monotonic() - t0:.1f} s)")

    def transcribe(self, pcm: np.ndarray) -> Optional[str]:
        """Returnerer tekst, eller None hvis det trolig ikke var tale."""
        t0 = time.monotonic()
        audio = normalize(pcm)
        segments, _info = self.model.transcribe(
            audio,
            language="no",
            beam_size=1,
            vad_filter=True,
            condition_on_previous_text=False,
            initial_prompt=PROMPT,
        )
        segs = list(segments)
        dt = time.monotonic() - t0
        text = " ".join(s.text.strip() for s in segs).strip()
        if not segs or not text:
            log("stt", f"[dim]ingen tale ({dt:.2f} s)[/dim]")
            return None
        nsp = max(s.no_speech_prob for s in segs)
        lp = sum(s.avg_logprob for s in segs) / len(segs)
        words = len(text.split())
        if words < 2 or nsp > 0.6 or lp < -1.0:
            log("stt", f"[dim]forkastet «{text}» (ord={words}, no_speech={nsp:.2f}, logprob={lp:.2f}, {dt:.2f} s)[/dim]")
            return None
        log("stt", f"«{text}»  [dim]({dt:.2f} s)[/dim]")
        return text
