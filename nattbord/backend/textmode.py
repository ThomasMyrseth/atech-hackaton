"""Tekstmodus: skriv i stedet for å snakke. Kjør: cd nattbord && ../.venv/bin/python -m backend.textmode

Kommandoer: /kveld (kveldsrutine), /reset (ny samtale), /stille (tale av/på), /quit
"""
import time

from .agent import Assistant
from .calendar_backend import get_calendar
from .config import load_config
from .log import console, log
from .room import Room
from .tts import duration_s, play_laptop, synthesize

try:
    from .device import LogDevice
except ImportError:  # device.py eies av hovedtråden; enkel reserve til den finnes
    class LogDevice:  # type: ignore[no-redef]
        def action(self, key: str, value="") -> bool:
            log("action", f"→ {key} {value!r}")
            return True


def speak(text: str, cfg, muted: bool) -> None:
    if muted or not text:
        return
    t0 = time.monotonic()
    pcm = synthesize(text, cfg.tts_voice, cfg.tts_rate)
    log("tts", f"say {time.monotonic() - t0:.2f}s → {duration_s(pcm):.1f}s lyd")
    play_laptop(pcm)


def main() -> None:
    cfg = load_config()
    device = LogDevice()
    assistant = Assistant(device, Room.fake(), get_calendar(cfg), cfg)
    muted = False
    console.print(f"[bold]Nattbord – tekstmodus[/bold]  modell={cfg.model} thinking={cfg.thinking} "
                  f"effort={cfg.effort} stemme={cfg.tts_voice}")
    console.print("[dim]Skriv på norsk. /kveld /reset /stille /quit[/dim]")
    while True:
        try:
            line = console.input("[bold cyan]du> [/bold cyan]").strip()
        except (EOFError, KeyboardInterrupt):
            console.print()
            break
        if not line:
            continue
        if line in ("/quit", "/exit", "/q"):
            break
        if line == "/reset":
            assistant.reset()
            continue
        if line == "/stille":
            muted = not muted
            log("tts", "tale AV" if muted else "tale PÅ")
            continue
        t0 = time.monotonic()
        text = assistant.evening_routine() if line == "/kveld" else assistant.respond(line)
        t_agent = time.monotonic() - t0
        console.print(f"[bold green]nattbord>[/bold green] {text}")
        speak(text, cfg, muted)
        log("time", f"agent {t_agent:.2f}s, totalt med tale {time.monotonic() - t0:.2f}s")


if __name__ == "__main__":
    main()
