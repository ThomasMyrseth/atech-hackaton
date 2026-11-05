"""Text mode: type instead of talking. Run: cd nattbord && ../.venv/bin/python -m backend.textmode

Commands: /evening (evening routine), /reset (new conversation), /mute (speech on/off), /quit
"""
import time

from .agent import Assistant
from .config import load_config
from .log import console, log
from .plugin import get_plugins
from .room import Room
from .tts import duration_s, play_laptop, synthesize

try:
    from .device import LogDevice
except ImportError:  # device.py is owned by the main thread; simple fallback until it exists
    class LogDevice:  # type: ignore[no-redef]
        def action(self, key: str, value="") -> bool:
            log("action", f"→ {key} {value!r}")
            return True


def speak(text: str, cfg, muted: bool) -> None:
    if muted or not text:
        return
    t0 = time.monotonic()
    pcm = synthesize(text, cfg.tts_voice, cfg.tts_rate)
    log("tts", f"say {time.monotonic() - t0:.2f}s → {duration_s(pcm):.1f}s audio")
    play_laptop(pcm)


def main() -> None:
    cfg = load_config()
    device = LogDevice()
    assistant = Assistant(device, Room.fake(), get_plugins(cfg, device), cfg)
    muted = False
    console.print(f"[bold]Nattbord – text mode[/bold]  model={cfg.model} thinking={cfg.thinking} "
                  f"effort={cfg.effort} voice={cfg.tts_voice}")
    console.print("[dim]Type in English. /evening /reset /mute /quit[/dim]")
    while True:
        try:
            line = console.input("[bold cyan]you> [/bold cyan]").strip()
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
        if line == "/mute":
            muted = not muted
            log("tts", "speech OFF" if muted else "speech ON")
            continue
        t0 = time.monotonic()
        text = assistant.evening_routine() if line == "/evening" else assistant.respond(line)
        t_agent = time.monotonic() - t0
        console.print(f"[bold green]nattbord>[/bold green] {text}")
        speak(text, cfg, muted)
        log("time", f"agent {t_agent:.2f}s, total with speech {time.monotonic() - t0:.2f}s")


if __name__ == "__main__":
    main()
