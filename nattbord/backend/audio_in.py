"""Lyd inn: sett sammen audio_chunk-events fra brettet, eller ta opp fra laptop-mikrofonen."""
from __future__ import annotations

import base64
import queue
import threading
import time
import wave
from pathlib import Path
from typing import Callable, Optional

import numpy as np

from backend.log import log

RATE = 16000


class ChunkAssembler:
    """Samler audio_chunk {seq, data} til én int16-array."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.chunks: dict[int, bytes] = {}
        self.t_start: Optional[float] = None
        self.t_last: Optional[float] = None
        self.kind = ""

    def start(self, kind: str = "") -> None:
        self.reset()
        self.kind = kind
        self.t_start = time.monotonic()

    def add(self, value: dict) -> None:
        if self.t_start is None:
            self.t_start = time.monotonic()
        try:
            self.chunks[int(value["seq"])] = base64.b64decode(value["data"])
        except Exception as e:  # noqa: BLE001
            log("warn", f"ødelagt audio_chunk: {e}")
        self.t_last = time.monotonic()

    def finish(self, info: Optional[dict] = None) -> np.ndarray:
        expected = int((info or {}).get("chunks", len(self.chunks)))
        missing = [i for i in range(expected) if i not in self.chunks]
        if missing:
            log("warn", f"mangler {len(missing)}/{expected} lydbiter: {missing[:10]}")
        data = b"".join(self.chunks[i] for i in sorted(self.chunks))
        pcm = np.frombuffer(data[: len(data) // 2 * 2], dtype=np.int16).copy()
        self.reset()
        return pcm


def normalize(pcm: np.ndarray, target_peak: float = 0.9) -> np.ndarray:
    """int16 -> float32 [-1, 1], med forsterkning så stille mikrofoner blir hørbare for whisper."""
    x = pcm.astype(np.float32) / 32768.0
    x = x - float(np.mean(x)) if len(x) else x
    peak = float(np.max(np.abs(x))) if len(x) else 0.0
    if peak > 1e-4:
        x = x * min(target_peak / peak, 30.0)
    return x


def rms(pcm: np.ndarray) -> float:
    if not len(pcm):
        return 0.0
    return float(np.sqrt(np.mean(pcm.astype(np.float64) ** 2)))


def save_wav(path: Path, pcm: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm.astype(np.int16).tobytes())


class LaptopListener:
    """Reserve: laptop-mikrofonen med enkel energi-VAD i egen tråd.

    `enabled()` avgjør om vi skal lytte (presence og ikke snakker). Ferdige ytringer
    havner i `self.utterances` som int16-arrays.
    """

    def __init__(self, threshold: float = 600.0, enabled: Callable[[], bool] = lambda: True,
                 silence_s: float = 1.0, max_s: float = 6.0, min_s: float = 0.7):
        self.threshold = threshold
        self.enabled = enabled
        self.silence_s, self.max_s, self.min_s = silence_s, max_s, min_s
        self.utterances: "queue.Queue[np.ndarray]" = queue.Queue()
        self.force = threading.Event()   # knapp: start opptak nå
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="laptop-mic")

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        import sounddevice as sd

        block = 512
        preroll: list[np.ndarray] = []
        rec: list[np.ndarray] = []
        recording = False
        t_start = t_loud = 0.0
        with sd.InputStream(samplerate=RATE, channels=1, dtype="int16", blocksize=block) as stream:
            log("audio", "laptop-mikrofon åpen")
            while not self._stop.is_set():
                data, _ = stream.read(block)
                x = data[:, 0].copy()
                now = time.monotonic()
                level = rms(x)
                if not recording:
                    preroll.append(x)
                    preroll = preroll[-10:]  # ~320 ms
                    if self.enabled() and (level > self.threshold or self.force.is_set()):
                        self.force.clear()
                        recording = True
                        rec = list(preroll)
                        t_start = t_loud = now
                        log("audio", f"laptop: opptak starter (nivå {level:.0f})")
                    continue
                rec.append(x)
                if level > self.threshold * 0.6:
                    t_loud = now
                if now - t_loud > self.silence_s or now - t_start > self.max_s or not self.enabled():
                    recording = False
                    pcm = np.concatenate(rec)
                    dur = len(pcm) / RATE
                    if dur >= self.min_s:
                        log("audio", f"laptop: ytring {dur:.1f} s")
                        self.utterances.put(pcm)
                    preroll = []
