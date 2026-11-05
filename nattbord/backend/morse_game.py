"""One hardcoded morse round for the morning demo: the light blinks RED, the player keys BLUE."""
from __future__ import annotations

import json
import queue
import sys
import time
from pathlib import Path

from .log import log
from .plugin import Plugin

# shortcut: morse/ is not a package, make it one if a third project needs it.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "morse"))
import morse  # noqa: E402

WORD, ANSWER = "RED", "BLUE"
WPM = 8
KEY_DONE_S = 2.5  # a pause this long ends the answer
FIRST_PRESS_S = 20.0


class MorseGame(Plugin):
    name = "morse game"
    fast = {"start_morse_game"}
    tools = [{
        "name": "start_morse_game",
        "description": "Starts the morse game on the light and the knob. Call it when the user agrees to play. "
                       "Write a short lead-in such as \"Watch the light\" in the same reply. Never say the word "
                       "or the answer: the game does that itself.",
        "strict": True,
        "input_schema": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
    }]

    def __init__(self, device) -> None:
        self.device, self.pending, self.result = device, False, ""
        self.inbox: queue.Queue = queue.Queue()

    def call(self, tool: str, args: dict) -> str:
        if not self.device.is_board:
            raise RuntimeError("the morse game needs the board")
        self.pending = True
        return json.dumps({"ok": True})

    def context(self) -> str:
        return self.result

    def handle_event(self, key: str, value) -> bool:
        if key in ("edge", "morse_done"):
            self.inbox.put((key, value))  # never any work here
            return True
        return False

    def after_turn(self, say) -> bool:
        if not self.pending:
            return False
        self.pending = False
        try:
            if not self.device.action("game_mode", "1"):
                raise RuntimeError("the board refused game mode")
            self._blink()
            say("Now key the opposite.")
            keyed = self._keyed()
        except RuntimeError as e:
            log("warn", f"morse game: {e}")
            say("Sorry, the light isn't answering.")
            return True
        finally:
            self.device.action("game_mode", "0")
        self.result = f"Morse game: the answer was {ANSWER} and the user keyed {keyed or 'nothing'}."
        log("game", self.result)
        reveal = f"The light said {WORD.lower()}, so the answer was {ANSWER.lower()}."
        if keyed == ANSWER:
            say(f"Yes, {ANSWER.lower()}! Nicely keyed.")
        else:
            say(f"{'Not quite' if keyed else 'No answer this time'}. {reveal}")
        return True

    def _next(self, deadline: float):
        try:
            return self.inbox.get(timeout=max(0, deadline - time.monotonic()))
        except queue.Empty:
            return None

    def _drain(self) -> None:
        while not self.inbox.empty():
            self.inbox.get_nowait()

    def _blink(self) -> None:
        self._drain()
        unit, pattern = morse.unit_ms(WPM), morse.encode(WORD)
        if not self.device.action("morse", f"{unit}:{pattern}"):
            raise RuntimeError("the board refused the blink")
        units = sum({".": 2, "-": 4, " ": 2, "/": 6}[symbol] for symbol in pattern)
        deadline = time.monotonic() + units * unit / 1000 + 2
        while event := self._next(deadline):
            if event[0] == "morse_done":
                return
        raise RuntimeError("the board never finished blinking")

    def _keyed(self) -> str:
        """Collect presses until the key goes quiet, then decode them on board time."""
        self._drain()  # presses during the speech are not an answer
        presses, down = [], None
        deadline = time.monotonic() + FIRST_PRESS_S
        while event := self._next(deadline):
            if event[0] != "edge":
                continue
            state, at = event[1].split(":")
            if state == "1":
                down = int(at)
            elif down is not None:
                presses.append((down, int(at)))
                down = None
            deadline = time.monotonic() + KEY_DONE_S
        return morse.decode(presses)[0].replace(" ", "")  # a long pause between letters decodes as a word gap
