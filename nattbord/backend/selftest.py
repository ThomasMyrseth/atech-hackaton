"""Offline self-test of the plugin interface. Run: cd nattbord && ../.venv/bin/python -m backend.selftest

Touches neither the board, Claude nor Google. Exit 0 means everything held.
"""
import base64
import json
import random
import tempfile
import threading
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from . import morse_game
from . import plugin as plugin_module
from .agent import FALLBACK_TOOL_ERROR, NEEDS_BUTTON, Assistant
from .calendar_backend import CalendarPlugin, DemoCalendar, GoogleCalendar
from .config import Config
from .device import LogDevice
from .plugin import ExternalError, Plugin, request_json, route_event
from .room import Room
from .stt import STT

plugin_module.RETRY_WAIT_S = 0  # the self-test should not sleep
morse_game.KEY_DONE_S, morse_game.FIRST_PRESS_S = 0.1, 0.3


class FakeResponse:
    def __init__(self, status_code=200, body=None):
        self.status_code, self.body = status_code, body
        self.content = b"" if body is None else b"x"
        self.text = json.dumps(body)

    def json(self):
        return self.body


class FakeSession:
    """Plays back replies in order and remembers every call."""

    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def request(self, method, url, **kw):
        self.calls.append(SimpleNamespace(method=method, url=url, **kw))
        return self.responses.pop(0)


def google(*responses) -> tuple[GoogleCalendar, FakeSession]:
    session = FakeSession(*responses)
    return GoogleCalendar(Config(google_calendar_id="me@gmail.com"), session=session), session


def item(eid="e1", summary="Lunch"):
    return {"id": eid, "summary": summary, "start": {"dateTime": "2026-11-05T12:00:00+01:00"},
            "end": {"dateTime": "2026-11-05T13:00:00+01:00"}}


def test_request_json() -> None:
    s = FakeSession(FakeResponse(429), FakeResponse(200, {"ok": 1}))
    assert request_json(s, "GET", "u") == {"ok": 1} and len(s.calls) == 2, "GET retries on 429"
    assert s.calls[0].timeout == plugin_module.TIMEOUT_S, "a timeout is always set"

    s = FakeSession(FakeResponse(503), FakeResponse(503))
    try:
        request_json(s, "GET", "u")
        raise AssertionError("two 503s must raise")
    except ExternalError as e:
        assert e.status == 503 and len(s.calls) == 2

    s = FakeSession(FakeResponse(503), FakeResponse(200, {}))
    try:
        request_json(s, "POST", "u")
        raise AssertionError("POST must not retry")
    except ExternalError:
        assert len(s.calls) == 1, "a repeated POST can create a duplicate"

    assert request_json(FakeSession(FakeResponse(204)), "DELETE", "u") == {}, "an empty body is fine"

    class Timeouts:
        calls = 0

        def request(self, *a, **k):
            Timeouts.calls += 1
            raise TimeoutError("slow")
    try:
        request_json(Timeouts(), "GET", "u")
        raise AssertionError("a timeout must raise")
    except ExternalError:
        assert Timeouts.calls == 1, "timeouts are never retried"


def test_google_read() -> None:
    all_day = {"id": "e2", "start": {"date": "2026-11-05"}, "end": {"date": "2026-11-06"}}
    cal, s = google(FakeResponse(200, {"items": [item(), all_day]}))
    day = date(2026, 11, 5)
    evs = cal.get_events(day)
    assert evs[0]["title"] == "Lunch" and evs[0]["all_day"] is False
    assert evs[1]["title"] == "(untitled)" and evs[1]["all_day"] is True
    call = s.calls[0]
    assert "/me%40gmail.com/events" in call.url, "the calendar id is URL-encoded, never primary"
    assert call.params["timeMin"] == "2026-11-05T00:00:00+01:00"
    assert call.params["timeMax"] == "2026-11-06T00:00:00+01:00"
    assert call.params["singleEvents"] == "true", "instance ids, so move and delete hit one occurrence"

    cal.get_events(day)
    assert len(s.calls) == 1, "the second read comes from the cache"


def test_google_dst_window() -> None:
    cal, s = google(FakeResponse(200, {"items": []}))
    cal.get_events(date(2026, 10, 25))  # the clocks go back this night
    assert s.calls[0].params["timeMin"].endswith("+02:00")
    assert s.calls[0].params["timeMax"] == "2026-10-26T00:00:00+01:00"


def test_google_write() -> None:
    day = date(2026, 11, 5)
    empty = FakeResponse(200, {"items": []})
    cal, s = google(empty, FakeResponse(200, item()), empty, FakeResponse(200, item()), empty,
                    FakeResponse(204), empty)
    cal.get_events(day)

    cal.create_event("Lunch", datetime(2026, 11, 5, 12), datetime(2026, 11, 5, 12))
    body = s.calls[1].json
    assert body["start"] == {"dateTime": "2026-11-05T12:00:00+01:00", "timeZone": "Europe/Oslo"}
    assert body["end"]["dateTime"] == "2026-11-05T13:00:00+01:00", "end before start becomes one hour"
    cal.get_events(day)
    assert len(s.calls) == 3, "create clears the cache"

    cal.update_event("a/b", title="New")
    assert s.calls[3].method == "PATCH" and s.calls[3].json == {"summary": "New"}, "only what changes"
    assert s.calls[3].url.endswith("/events/a%2Fb"), "the id from the model is encoded in the URL"
    cal.get_events(day)
    assert len(s.calls) == 5, "update clears the cache"

    cal.delete_event("e1")
    assert s.calls[5].method == "DELETE"
    cal.get_events(day)
    assert len(s.calls) == 7, "delete clears the cache"


def test_demo_and_calendar_plugin() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        cal = DemoCalendar(Path(tmp) / "cal.json")
        p = CalendarPlugin(cal, "Europe/Oslo")
        made = json.loads(p.call("create_event", {"title": "Test", "start": "2026-11-20T16:00",
                                                  "end": "2026-11-20T17:00", "location": "Here"}))
        assert made["start"] == "16:00" and made["id"], "create returns the id so a move needs no lookup"
        found = json.loads(p.call("get_events", {"date": "2026-11-20"}))["events"]
        assert found[0]["id"] and found[0]["location"] == "Here", "the model must see the id to move and delete"

        moved = {"id": found[0]["id"], "title": "", "location": "", "start": "2026-11-20T18:00", "end": ""}
        try:
            p.call("update_event", moved)
            raise AssertionError("a move without an end must be rejected")
        except ValueError:
            pass
        moved["end"] = "2026-11-20T19:00"
        assert json.loads(p.call("update_event", moved))["start"] == "18:00"

        p.call("delete_event", {"id": found[0]["id"]})
        assert json.loads(p.call("get_events", {"date": "2026-11-20"}))["events"] == []
        assert "Today:" in p.context() and found[0]["id"] not in p.context(), "no ids in the context"

    all_day = {"id": "x", "title": "Holiday", "start": "2026-11-05", "end": "2026-11-06", "all_day": True}

    class OneDay:
        def get_events(self, day):
            return [all_day]
    assert "all day Holiday" in CalendarPlugin(OneDay(), "Europe/Oslo").context()


class FakeSocket:
    """Answers like ElevenLabs after the last chunk is committed."""

    def __init__(self, *replies):
        self.replies, self.sent, self.closed = list(replies), [], False

    def send(self, raw):
        self.sent.append(json.loads(raw))

    def recv(self, timeout=None):
        assert self.sent and self.sent[-1]["commit"], "do not wait for a reply before the audio is committed"
        return json.dumps(self.replies.pop(0))

    def close(self):
        self.closed = True


def test_stt() -> None:
    speech = (np.sin(np.arange(32000) / 8) * 8000).astype(np.int16)  # 2 s
    done = {"message_type": "committed_transcript", "text": " What do I have tomorrow? "}

    ws = FakeSocket({"message_type": "warning"}, {"message_type": "partial_transcript", "text": "What"}, done)
    assert STT(lambda: ws).transcribe(speech) == "What do I have tomorrow?"
    assert [c["commit"] for c in ws.sent] == [False] * (len(ws.sent) - 1) + [True], "only the last chunk commits"
    sent = b"".join(base64.b64decode(c["audio_base_64"]) for c in ws.sent)
    assert len(sent) == 64000 and ws.sent[0]["sample_rate"] == 16000, "16 kHz int16, nothing lost"
    assert ws.closed

    ws = FakeSocket({"message_type": "committed_transcript", "text": "Hm."})
    assert STT(lambda: ws).transcribe(speech) is None, "one word does not count as speech"
    ws = FakeSocket({"message_type": "committed_transcript", "text": "Yes."})
    assert STT(lambda: ws).transcribe(speech) == "Yes.", "except a short answer to a question"
    assert STT(lambda: FakeSocket({"message_type": "insufficient_audio_activity"})).transcribe(speech) is None
    assert STT(lambda: FakeSocket()).transcribe(np.zeros(0, dtype=np.int16)) is None, "an empty recording is not sent"

    ws = FakeSocket({"message_type": "quota_exceeded", "error": "empty"})
    try:
        STT(lambda: ws).transcribe(speech)
        raise AssertionError("errors from the service must raise")
    except ExternalError as e:
        assert "quota_exceeded" in str(e) and ws.closed, "the socket also closes on errors"

    def down():
        raise OSError("network down")
    try:
        STT(down).transcribe(speech)
        raise AssertionError("a network error must become ExternalError")
    except ExternalError:
        pass


# Same skeleton as docs/INTERFACE.md, sections 4 and 5.
class FakeSource(Plugin):
    name = "source"
    writes = {"post_note"}
    tools = [{"name": "get_note", "description": "x", "strict": True,
              "input_schema": {"type": "object", "properties": {}, "required": [], "additionalProperties": False}},
             {"name": "post_note", "description": "x", "strict": True,
              "input_schema": {"type": "object", "properties": {}, "required": [], "additionalProperties": False}}]

    def __init__(self, down: bool = False) -> None:
        self.down, self.posted = down, 0

    def context(self) -> str:
        if self.down:
            raise ExternalError("down")
        return "Note: hi."

    def call(self, tool: str, args: dict) -> str:
        if tool == "post_note":
            self.posted += 1
        return json.dumps({"ok": True})


class FakeGame(Plugin):
    name = "game"

    def __init__(self) -> None:
        self.running, self.seen = False, []

    def handle_event(self, key: str, value) -> bool:
        if self.running and key in ("knob_btn", "gesture", "listen", "audio_chunk", "audio_end"):
            self.seen.append(key)
            return True
        return False


class Broken(Plugin):
    name = "broken"

    def handle_event(self, key: str, value) -> bool:
        raise RuntimeError("boom")


class ScriptedClient:
    """Answers like Claude: first the tool call, then the text we ask for."""

    def __init__(self, *rounds):
        self.rounds = list(rounds)
        self.messages = self

    def create(self, **kw):
        blocks = self.rounds.pop(0)
        stop = "tool_use" if any(b.type == "tool_use" for b in blocks) else "end_turn"
        return SimpleNamespace(content=blocks, stop_reason=stop, usage=SimpleNamespace(input_tokens=0, output_tokens=0))


def text(t: str):
    return SimpleNamespace(type="text", text=t)


def tool_use(name: str):
    return SimpleNamespace(type="tool_use", id="t1", name=name, input={})


def assistant(*plugins, rounds=()) -> Assistant:
    a = Assistant(LogDevice(), Room.fake(), list(plugins), Config(anthropic_api_key="test"))
    a.client = ScriptedClient(*rounds)
    return a


def test_source_plugs_in_without_touching_agent() -> None:
    src = FakeSource()
    a = assistant(src)
    assert {"get_note", "post_note"} <= {t["name"] for t in a.tools}
    assert "Note: hi." in a._context_line()

    a._can_write = True
    assert a._exec_tool("post_note", {}) == ('{"ok": true}', False) and src.posted == 1
    assert a._exec_tool("nope", {})[1] is True, "an unknown tool becomes an error to the model"


def test_write_gate() -> None:
    src = FakeSource()
    a = assistant(src, rounds=[[text("Done!"), tool_use("post_note")], [text("Press the button first.")]])
    assert a.respond("post this", ambient=True) == "Press the button first."
    assert src.posted == 0, "the always-on mic must never reach the plugin"

    a = assistant(src, rounds=[[text("Done!"), tool_use("post_note")], []])
    assert a.respond("post this", ambient=True) == FALLBACK_TOOL_ERROR, "never \"okay\" after a blocked write"

    a = assistant(src, rounds=[[text("Done!"), tool_use("post_note")]])
    assert a.respond("post this") == "Done!" and src.posted == 1, "a direct request can write"

    for routine in ("evening_routine", "morning_routine"):
        posted = src.posted
        a = assistant(src, rounds=[[tool_use("post_note")], [text("Good night.")]])
        a._can_write = True  # left over from an earlier direct request
        getattr(a, routine)()
        assert src.posted == posted, f"{routine} must not be able to write"

    a = assistant(src)
    assert a._exec_tool("post_note", {}) == (f"Error: {NEEDS_BUTTON}", True), "the default is no writing"


def test_interface_guards() -> None:
    for broken in ([FakeSource(), FakeSource()], [type("Typo", (FakeSource,), {"writes": {"post_nota"}})()]):
        try:
            assistant(*broken)
            raise AssertionError("invalid plugins must stop startup")
        except ValueError:
            pass


def test_source_down() -> None:
    line = assistant(FakeSource(down=True))._context_line()
    assert "source: unavailable." in line and "Note" not in line
    a = assistant(FakeSource(down=True), rounds=[[text("The calendar is down.")]])
    assert a.evening_routine() == "The calendar is down.", "the routine survives a source being down"


def test_game_routing() -> None:
    game = FakeGame()
    assert route_event([game], "knob_btn", 1) is False, "while the game rests, the assistant sees everything"
    game.running = True
    assert route_event([Broken(), game], "knob_btn", 1) is True, "a crashing plugin does not stop the others"
    assert route_event([game], "audio_end", {}) is True and game.seen == ["knob_btn", "audio_end"]
    assert route_event([game], "aht20_temperature", 20.0) is False, "the room still updates during a game"
    game.running = False
    assert route_event([game], "knob_btn", 1) is False


class GameBoard:
    """Acks like the board and finishes a blink at once."""

    is_board = True

    def __init__(self, reject=()) -> None:
        self.sent, self.reject, self.game = [], set(reject), None

    def action(self, key: str, value="") -> bool:
        self.sent.append((key, value))
        if key in self.reject:
            return False
        if key == "morse":
            self.game.handle_event("morse_done", 1)
        return True


def edges(presses) -> list[str]:
    return [e for down, up in presses for e in (f"1:{int(down)}", f"0:{int(up)}")]


def play_round(keyed=(), reject=()) -> tuple[GameBoard, morse_game.MorseGame, list[str]]:
    board = GameBoard(reject)
    game = board.game = morse_game.MorseGame(board)
    said = []

    def say(line: str) -> None:
        said.append(line)
        if line.startswith("Now key"):  # the game drains the key after this line, so press a moment later
            threading.Timer(0.02, lambda: [game.handle_event("edge", e) for e in edges(keyed)]).start()

    game.call("start_morse_game", {})
    assert game.after_turn(say) is True and game.after_turn(say) is False, "one round per start"
    return board, game, said


def test_morse_game() -> None:
    rng = random.Random(0)
    key = morse_game.morse.key

    board, game, said = play_round(key("BLUE", 12, 0.1, rng))
    assert said[-1].startswith("Yes, blue"), said
    assert board.sent[0] == ("game_mode", "1") and board.sent[-1] == ("game_mode", "0")
    assert ("morse", "150:.-. . -..") in board.sent, "RED at 8 WPM"
    assert "keyed BLUE" in game.context(), "the model must know the result to chat about it"
    assert not any("red" in line.lower() for line in said[:-1]), "never reveal the word before the attempt"

    late = key("BL", 12, 0, rng) + [(down + 9000, up + 9000) for down, up in key("UE", 12, 0, rng)]
    assert play_round(late)[2][-1].startswith("Yes"), "a long pause between letters still counts"

    board, game, said = play_round(key("RED", 12, 0.1, rng))
    assert "answer was blue" in said[-1] and "keyed RED" in game.context()

    board, game, said = play_round()
    assert said[-1].startswith("No answer") and "keyed nothing" in game.context()

    board, game, said = play_round(reject=["morse"])
    assert board.sent[-1] == ("game_mode", "0"), "the button must always come back"
    assert "isn't answering" in said[-1] and game.context() == ""

    idle = morse_game.MorseGame(GameBoard())
    assert idle.context() == "" and idle.after_turn(print) is False, "no game without a start"
    assert route_event([idle], "edge", "1:0") is True and route_event([idle], "knob_btn", 1) is False


def test_morse_game_start() -> None:
    board = GameBoard()
    game = board.game = morse_game.MorseGame(board)
    a = assistant(game, rounds=[[text("Watch the light."), tool_use("start_morse_game")]])
    assert a.respond("Yes.", ambient=True) == "Watch the light.", "a mic-picked yes starts it, with no second round"
    assert game.pending

    no_board = morse_game.MorseGame(LogDevice())
    a = assistant(no_board, rounds=[[tool_use("start_morse_game")], [text("I need the board for that.")]])
    assert a.respond("play morse") == "I need the board for that." and not no_board.pending


def main() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"{len(tests)} tests ok")


if __name__ == "__main__":
    main()
