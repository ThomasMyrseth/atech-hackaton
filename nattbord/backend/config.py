"""Konfig fra nattbord/.env (aldri commit .env)."""
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent  # nattbord/
DATA_DIR = ROOT / "data"


def _env(name: str, default: str) -> str:
    val = os.environ.get(name, default)
    # tål inline-kommentarer og mellomrom fra .env.example
    return val.split(" #")[0].strip() or default


@dataclass
class Config:
    anthropic_api_key: str = ""
    model: str = "claude-haiku-5-5"
    thinking: str = "off"          # off | adaptive  (off = raskest, trygt å trimme historikk)
    effort: str = "low"            # low | medium | high
    calendar: str = "demo"         # demo | google
    tts_voice: str = "Nora"
    tts_rate: int = 190
    audio_in: str = "device"       # device | laptop
    audio_out: str = "device"      # device | laptop
    evening_time: str = "22:30"
    tz: str = "Europe/Oslo"
    mic_threshold: int = 1500


def load_config() -> Config:
    load_dotenv(ROOT / ".env")
    return Config(
        anthropic_api_key=_env("ANTHROPIC_API_KEY", ""),
        model=_env("MODEL", "claude-haiku-5-5"),
        thinking=_env("THINKING", "off"),
        effort=_env("EFFORT", "low"),
        calendar=_env("CALENDAR", "demo"),
        tts_voice=_env("TTS_VOICE", "Nora"),
        tts_rate=int(_env("TTS_RATE", "190")),
        audio_in=_env("AUDIO_IN", "device"),
        audio_out=_env("AUDIO_OUT", "device"),
        evening_time=_env("EVENING_TIME", "22:30"),
        tz=_env("TZ_NAME", "Europe/Oslo"),
        mic_threshold=int(_env("MIC_THRESHOLD", "1500")),
    )
