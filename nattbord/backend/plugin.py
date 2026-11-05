"""Grensesnittet mellom assistenten og alt utenfor: eksterne kilder og spill.

Reglene (tråder, feil, skriving) står i docs/INTERFACE.md. Les den før du lager en plugin.
"""
from __future__ import annotations

import time

from .log import log

TIMEOUT_S = 3.0  # stemmeløkka venter på dette, så hold det lavt
RETRY_WAIT_S = 0.5


class ExternalError(Exception):
    """En ekstern tjeneste feilet. Teksten går til modellen og loggen, så hold den kort."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class Plugin:
    """Grunnklasse med trygge standardverdier: overstyr bare det du trenger.

    En kilde bruker tools, writes, call og context.
    Et spill bruker tools for å starte og handle_event for å eie input fra brettet.
    """

    name = ""
    tools: list[dict] = []    # Anthropic-verktøy, strict, samme form som agent.TOOLS
    writes: set[str] = set()  # verktøy som endrer noe utenfor; krever knappetrykk

    def call(self, tool: str, args: dict) -> str:
        """Kjører på arbeidertråden. Returnerer JSON-tekst til modellen. Exception = feil til modellen."""
        raise NotImplementedError(tool)

    def context(self) -> str:
        """Kjører hver tur, så den må være rask eller cachet. Én kort linje, "" for ingenting."""
        return ""

    def handle_event(self, key: str, value) -> bool:
        """Kjører på tråden som leser fra brettet, og må aldri blokkere. True = ingen andre ser eventet."""
        return False


def route_event(plugins: list[Plugin], key: str, value) -> bool:
    """Tilbyr et event til hver plugin i rekkefølge. True hvis en av dem tok det."""
    for plugin in plugins:
        try:
            if plugin.handle_event(key, value):
                return True
        except Exception as e:  # noqa: BLE001 – et ødelagt spill skal ikke velte hovedløkka
            log("warn", f"{plugin.name}: handle_event({key}) feilet: {e!r}")
    return False


def request_json(session, method: str, url: str, **kw) -> dict:
    """Ett kall med timeout. Bare GET prøves på nytt, for en gjentatt POST kan lage duplikat.
    Timeout prøves aldri på nytt, for da står stemmen stille for lenge."""
    kw.setdefault("timeout", TIMEOUT_S)
    for attempt in (1, 2):
        try:
            resp = session.request(method, url, **kw)
        except Exception as e:  # noqa: BLE001 – nett, timeout eller innlogging
            raise ExternalError(f"{method} feilet: {e}") from e
        status = resp.status_code
        if method == "GET" and attempt == 1 and (status == 429 or status >= 500):
            time.sleep(RETRY_WAIT_S)
            continue
        if status >= 400:
            raise ExternalError(f"HTTP {status}: {resp.text[:200]}", status)
        return resp.json() if resp.content else {}
    raise AssertionError("uoppnåelig: andre forsøk returnerer eller kaster")


def get_plugins(cfg, device) -> list[Plugin]:
    """Registeret: én linje per plugin. device er med så et spill kan styre brettet."""
    from .calendar_backend import CalendarPlugin, get_calendar  # her for å unngå importsirkel

    return [CalendarPlugin(get_calendar(cfg), cfg.tz)]
