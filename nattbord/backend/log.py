"""Felles logging til terminalen (vises på storskjerm under demo)."""
from datetime import datetime

from rich.console import Console

console = Console(highlight=False)

_STYLES = {
    "board": "cyan", "presence": "bold cyan", "button": "cyan",
    "audio": "magenta", "stt": "bold yellow", "agent": "bold green",
    "tool": "green", "tts": "blue", "time": "bold white", "warn": "bold red",
    "room": "cyan", "routine": "bold magenta", "action": "dim cyan",
}


def log(tag: str, msg: str, style: str = "") -> None:
    ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    st = style or _STYLES.get(tag, "white")
    console.print(f"[dim]{ts}[/dim] [{st}]{tag:>8}[/{st}]  {msg}")
