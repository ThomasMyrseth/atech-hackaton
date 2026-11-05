"""Tekst til tale → 16 kHz mono s16le PCM.

ElevenLabs (hvis ELEVENLABS_API_KEY er satt og TTS_ENGINE ikke er "say"), ellers macOS `say`.
"""
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


# Standardstemme: premade "Charlotte" (skandinavisk preg). Bytt med ELEVENLABS_VOICE_ID (f.eks. en norsk
# stemme fra Voice Library, lagt til på kontoen).
_EL_DEFAULT_VOICE = "XB0fDUnXU5powFXDhCwa"
_el_cache: dict[str, bytes] = {}


def _elevenlabs(text: str) -> bytes:
    import httpx

    key = os.getenv("ELEVENLABS_API_KEY", "")
    voice_id = os.getenv("ELEVENLABS_VOICE_ID", "") or _EL_DEFAULT_VOICE
    model = os.getenv("ELEVENLABS_MODEL", "eleven_flash_v2_5")
    body = {"text": text, "model_id": model,
            "voice_settings": {"stability": 0.5, "similarity_boost": 0.75, "speed": 1.0}}
    if "v2_5" in model:
        body["language_code"] = "no"
    r = httpx.post(
        f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
        params={"output_format": "pcm_16000"},
        headers={"xi-api-key": key, "Content-Type": "application/json"},
        json=body,
        timeout=15.0,
    )
    if r.status_code != 200:
        raise RuntimeError(f"ElevenLabs {r.status_code}: {r.text[:200]}")
    pcm = r.content
    return pcm[: len(pcm) // 2 * 2]


def synthesize(text: str, voice: str = "Nora", rate: int = 190) -> bytes:
    """Returnerer rå PCM (s16le, mono, 16 kHz)."""
    text = clean_for_speech(text)
    if not text:
        return b""
    if os.getenv("ELEVENLABS_API_KEY") and os.getenv("TTS_ENGINE", "elevenlabs") != "say":
        if text in _el_cache:
            return _el_cache[text]
        try:
            pcm = _elevenlabs(text)
            if len(_el_cache) < 50 and len(text) < 80:
                _el_cache[text] = pcm
            return pcm
        except Exception as e:  # noqa: BLE001 – faller tilbake til say
            try:
                from backend.log import log
                log("warn", f"ElevenLabs feilet, bruker say: {e}")
            except Exception:  # noqa: BLE001
                pass
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


def prepare_for_speaker(pcm: bytes, cutoff_hz: float = 220.0, peak: float = 0.92) -> bytes:
    """For den lille høyttaleren: fjern bass (forvrenger) og normaliser til nesten full styrke."""
    import numpy as np

    x = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
    if not len(x):
        return pcm
    # 2. ordens høypass (to kaskaderte 1. ordens)
    rc = 1.0 / (2 * np.pi * cutoff_hz)
    a = rc / (rc + 1.0 / SAMPLE_RATE)
    for _ in range(2):
        y = np.empty_like(x)
        prev_x = prev_y = 0.0
        for i in range(len(x)):  # ~100k samples: ok (< 0,1 s)
            prev_y = a * (prev_y + x[i] - prev_x)
            prev_x = x[i]
            y[i] = prev_y
        x = y
    m = float(np.max(np.abs(x)))
    if m > 1e-4:
        x = x * (peak / m)
    # 10 ms inn-/uttoning mot klikk
    f = min(160, len(x) // 2)
    if f:
        ramp = np.linspace(0, 1, f, dtype=np.float32)
        x[:f] *= ramp
        x[-f:] *= ramp[::-1]
    return (np.clip(x, -1, 1) * 32767).astype(np.int16).tobytes()


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


if __name__ == "__main__":
    # Rask stemmetest:  ../.venv/bin/python -m backend.tts "Hei, jeg er nattbordet ditt."  [--board]
    import sys
    import time

    from backend.config import load_config

    cfg = load_config()
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    text = " ".join(args) or "Hei! I morgen har du forelesning klokka kvart over åtte, og lunsj med Ingrid klokka tolv."
    t0 = time.monotonic()
    pcm = synthesize(text, cfg.tts_voice, cfg.tts_rate)
    engine = "elevenlabs" if os.getenv("ELEVENLABS_API_KEY") and os.getenv("TTS_ENGINE", "elevenlabs") != "say" else "say"
    print(f"{engine}: {time.monotonic() - t0:.2f} s for {len(pcm) / 32000:.1f} s lyd")
    if "--board" in sys.argv:
        from backend.device import BoardDevice
        d = BoardDevice()
        d.action("speaker_set_volume", float(os.getenv("SPEAKER_VOLUME", "0.85")))
        d.play_pcm(prepare_for_speaker(pcm))
        d.close()
    else:
        play_laptop(pcm)
