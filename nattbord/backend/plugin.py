"""The interface between the assistant and everything outside: external sources and games.

The rules (threads, errors, writes) are in docs/INTERFACE.md. Read it before you make a plugin.
"""
from __future__ import annotations

import time

from .log import log

TIMEOUT_S = 3.0  # the voice loop waits on this, so keep it low
RETRY_WAIT_S = 0.5


class ExternalError(Exception):
    """An external service failed. The text goes to the model and the log, so keep it short."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class Plugin:
    """Base class with safe defaults: override only what you need.

    A source uses tools, writes, call and context.
    A game uses tools to start and handle_event to own input from the board.
    """

    name = ""
    tools: list[dict] = []    # Anthropic tools, strict, same shape as agent.TOOLS
    writes: set[str] = set()  # tools that change something outside; need a button press
    fast: set[str] = set()    # tools whose reply is finished in the same turn: skips the second Claude round

    def call(self, tool: str, args: dict) -> str:
        """Runs on the worker thread. Returns JSON text for the model. An exception = an error to the model."""
        raise NotImplementedError(tool)

    def context(self) -> str:
        """Runs every turn, so it must be fast or cached. One short line, "" for nothing."""
        return ""

    def handle_event(self, key: str, value) -> bool:
        """Runs on the thread that reads from the board, and must never block. True = nobody else sees the event."""
        return False

    def after_turn(self, say) -> bool:
        """Runs on the worker thread after each job, once speech is done. Can take long: other jobs wait.
        True = the plugin talked to the user, so the mic opens once for their reply."""
        return False


def route_event(plugins: list[Plugin], key: str, value) -> bool:
    """Offers an event to each plugin in order. True if one of them took it."""
    for plugin in plugins:
        try:
            if plugin.handle_event(key, value):
                return True
        except Exception as e:  # noqa: BLE001 – a broken game must not bring down the main loop
            log("warn", f"{plugin.name}: handle_event({key}) failed: {e!r}")
    return False


def request_json(session, method: str, url: str, **kw) -> dict:
    """One call with a timeout. Only GET is retried, because a repeated POST can create a duplicate.
    Timeouts are never retried, because the voice would stall too long."""
    kw.setdefault("timeout", TIMEOUT_S)
    for attempt in (1, 2):
        try:
            resp = session.request(method, url, **kw)
        except Exception as e:  # noqa: BLE001 – network, timeout or login
            raise ExternalError(f"{method} failed: {e}") from e
        status = resp.status_code
        if method == "GET" and attempt == 1 and (status == 429 or status >= 500):
            time.sleep(RETRY_WAIT_S)
            continue
        if status >= 400:
            raise ExternalError(f"HTTP {status}: {resp.text[:200]}", status)
        return resp.json() if resp.content else {}
    raise AssertionError("unreachable: the second attempt returns or raises")


def get_plugins(cfg, device) -> list[Plugin]:
    """The registry: one line per plugin. device is passed so a game can drive the board."""
    from .calendar_backend import CalendarPlugin, get_calendar  # here to avoid an import cycle
    from .morse_game import MorseGame

    return [CalendarPlugin(get_calendar(cfg), cfg.tz), MorseGame(device)]
