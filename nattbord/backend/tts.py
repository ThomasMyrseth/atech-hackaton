"""Tekst til tale med macOS `say` → 16 kHz mono s16le PCM."""
import base64
import os
import re
import subprocess
import tempfile
import wave

SAMPLE_RATE = 16000

_EMOJI = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF‍️]", re.UNICODE
)


def clean_for_speech(text: str) -> str:
    t = re.sub(r"```.*?```", " ", text, flags=re.S)
    t = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", t)        # [lenke](url) → lenke
    t = re.sub(r"[*_`#>|~]+", "", t)                      # markdown-tegn
    t = re.sub(r"^\s*[-•]\s+", "", t, flags=re.M)         # punktlister
    t = _EMOJI.sub("", t)
    return re.sub(r"\s+", " ", t).strip()


def _voice_exists(voice: str) -> bool:
    try:
        out = subprocess.run(["say", "-v", "?"], capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return False
    return any(line.startswith(voice + " ") or line.split("  ")[0].strip() == voice for line in out.splitlines())


_voice_cache: dict[str, bool] = {}


def synthesize(text: str, voice: str = "Nora", rate: int = 190) -> bytes:
    """Returnerer rå PCM (s16le, mono, 16 kHz)."""
    text = clean_for_speech(text)
    if not text:
        return b""
    if voice not in _voice_cache:
        _voice_cache[voice] = _voice_exists(voice)
    fd, path = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    try:
        cmd = ["say"]
        if _voice_cache[voice]:
            cmd += ["-v", voice]
        cmd += ["-r", str(rate), "-o", path, "--data-format=LEI16@16000", text]
        subprocess.run(cmd, check=True, capture_output=True, timeout=30)
        with wave.open(path, "rb") as w:
            assert w.getsampwidth() == 2 and w.getnchannels() == 1, "uventet WAV-format fra say"
            return w.readframes(w.getnframes())
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def write_wav(pcm: bytes, path: str, rate: int = SAMPLE_RATE) -> None:
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)


def play_laptop(pcm: bytes) -> None:
    """Spiller PCM på laptopen (blokkerer til ferdig)."""
    if not pcm:
        return
    fd, path = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    try:
        write_wav(pcm, path)
        subprocess.run(["afplay", path], check=False)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def chunk_b64(pcm: bytes, max_chars: int = 3000) -> list[str]:
    """Deler PCM i base64-biter på ≤ max_chars tegn, alltid på hele samples."""
    raw_per_chunk = (max_chars // 4) * 3          # base64: 3 byte → 4 tegn
    raw_per_chunk -= raw_per_chunk % 6            # delelig med 2 (sample) og 3 (ingen padding midt i)
    return [base64.b64encode(pcm[i:i + raw_per_chunk]).decode("ascii")
            for i in range(0, len(pcm), raw_per_chunk)]


def duration_s(pcm: bytes) -> float:
    return len(pcm) / (2 * SAMPLE_RATE)
