"""Offline selvtest av plugin-grensesnittet. Kjør: cd nattbord && ../.venv/bin/python -m backend.selftest

Rører verken brettet, Claude eller Google. Exit 0 betyr at alt holdt.
"""
import base64
import json
import tempfile
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from . import plugin as plugin_module
from .agent import FALLBACK_TOOL_ERROR, NEEDS_BUTTON, Assistant
from .calendar_backend import CalendarPlugin, DemoCalendar, GoogleCalendar
from .config import Config
from .device import LogDevice
from .plugin import ExternalError, Plugin, request_json, route_event
from .room import Room
from .stt import STT

plugin_module.RETRY_WAIT_S = 0  # selvtesten skal ikke sove


class FakeResponse:
    def __init__(self, status_code=200, body=None):
        self.status_code, self.body = status_code, body
        self.content = b"" if body is None else b"x"
        self.text = json.dumps(body)

    def json(self):
        return self.body


class FakeSession:
    """Spiller av svar i rekkefølge og husker hvert kall."""

    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def request(self, method, url, **kw):
        self.calls.append(SimpleNamespace(method=method, url=url, **kw))
        return self.responses.pop(0)


def google(*responses) -> tuple[GoogleCalendar, FakeSession]:
    session = FakeSession(*responses)
    return GoogleCalendar(Config(google_calendar_id="meg@gmail.com"), session=session), session


def item(eid="e1", summary="Lunsj"):
    return {"id": eid, "summary": summary, "start": {"dateTime": "2026-11-05T12:00:00+01:00"},
            "end": {"dateTime": "2026-11-05T13:00:00+01:00"}}


def test_request_json() -> None:
    s = FakeSession(FakeResponse(429), FakeResponse(200, {"ok": 1}))
    assert request_json(s, "GET", "u") == {"ok": 1} and len(s.calls) == 2, "GET prøves på nytt ved 429"
    assert s.calls[0].timeout == plugin_module.TIMEOUT_S, "timeout settes alltid"

    s = FakeSession(FakeResponse(503), FakeResponse(503))
    try:
        request_json(s, "GET", "u")
        raise AssertionError("to 503 skal kaste")
    except ExternalError as e:
        assert e.status == 503 and len(s.calls) == 2

    s = FakeSession(FakeResponse(503), FakeResponse(200, {}))
    try:
        request_json(s, "POST", "u")
        raise AssertionError("POST skal ikke prøves på nytt")
    except ExternalError:
        assert len(s.calls) == 1, "en gjentatt POST kan lage duplikat"

    assert request_json(FakeSession(FakeResponse(204)), "DELETE", "u") == {}, "tom body er ok"

    class Timeouts:
        calls = 0

        def request(self, *a, **k):
            Timeouts.calls += 1
            raise TimeoutError("treg")
    try:
        request_json(Timeouts(), "GET", "u")
        raise AssertionError("timeout skal kaste")
    except ExternalError:
        assert Timeouts.calls == 1, "timeout prøves aldri på nytt"


def test_google_read() -> None:
    all_day = {"id": "e2", "start": {"date": "2026-11-05"}, "end": {"date": "2026-11-06"}}
    cal, s = google(FakeResponse(200, {"items": [item(), all_day]}))
    day = date(2026, 11, 5)
    evs = cal.get_events(day)
    assert evs[0]["title"] == "Lunsj" and evs[0]["all_day"] is False
    assert evs[1]["title"] == "(uten tittel)" and evs[1]["all_day"] is True
    call = s.calls[0]
    assert "/meg%40gmail.com/events" in call.url, "kalender-id-en er URL-kodet, aldri primary"
    assert call.params["timeMin"] == "2026-11-05T00:00:00+01:00"
    assert call.params["timeMax"] == "2026-11-06T00:00:00+01:00"
    assert call.params["singleEvents"] == "true", "instans-id-er, så flytt og slett treffer én forekomst"

    cal.get_events(day)
    assert len(s.calls) == 1, "andre lesing kommer fra cachen"


def test_google_dst_window() -> None:
    cal, s = google(FakeResponse(200, {"items": []}))
    cal.get_events(date(2026, 10, 25))  # klokka stilles tilbake denne natta
    assert s.calls[0].params["timeMin"].endswith("+02:00")
    assert s.calls[0].params["timeMax"] == "2026-10-26T00:00:00+01:00"


def test_google_write() -> None:
    day = date(2026, 11, 5)
    empty = FakeResponse(200, {"items": []})
    cal, s = google(empty, FakeResponse(200, item()), empty, FakeResponse(200, item()), empty,
                    FakeResponse(204), empty)
    cal.get_events(day)

    cal.create_event("Lunsj", datetime(2026, 11, 5, 12), datetime(2026, 11, 5, 12))
    body = s.calls[1].json
    assert body["start"] == {"dateTime": "2026-11-05T12:00:00+01:00", "timeZone": "Europe/Oslo"}
    assert body["end"]["dateTime"] == "2026-11-05T13:00:00+01:00", "slutt før start blir én time"
    cal.get_events(day)
    assert len(s.calls) == 3, "innlegging tømmer cachen"

    cal.update_event("a/b", title="Ny")
    assert s.calls[3].method == "PATCH" and s.calls[3].json == {"summary": "Ny"}, "bare det som endres"
    assert s.calls[3].url.endswith("/events/a%2Fb"), "id-en fra modellen kodes i URL-en"
    cal.get_events(day)
    assert len(s.calls) == 5, "endring tømmer cachen"

    cal.delete_event("e1")
    assert s.calls[5].method == "DELETE"
    cal.get_events(day)
    assert len(s.calls) == 7, "sletting tømmer cachen"


def test_demo_and_calendar_plugin() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        cal = DemoCalendar(Path(tmp) / "kal.json")
        p = CalendarPlugin(cal, "Europe/Oslo")
        made = json.loads(p.call("create_event", {"tittel": "Test", "start": "2026-11-20T16:00",
                                                  "slutt": "2026-11-20T17:00", "sted": "Her"}))
        assert made["start"] == "16:00"
        found = json.loads(p.call("get_events", {"dato": "2026-11-20"}))["avtaler"]
        assert found[0]["id"] and found[0]["sted"] == "Her", "modellen må se id for å flytte og slette"

        moved = {"id": found[0]["id"], "tittel": "", "sted": "", "start": "2026-11-20T18:00", "slutt": ""}
        try:
            p.call("update_event", moved)
            raise AssertionError("flytting uten slutt skal avvises")
        except ValueError:
            pass
        moved["slutt"] = "2026-11-20T19:00"
        assert json.loads(p.call("update_event", moved))["start"] == "18:00"

        p.call("delete_event", {"id": found[0]["id"]})
        assert json.loads(p.call("get_events", {"dato": "2026-11-20"}))["avtaler"] == []
        assert "I dag:" in p.context() and found[0]["id"] not in p.context(), "ingen id-er i konteksten"

    all_day = {"id": "x", "title": "Ferie", "start": "2026-11-05", "end": "2026-11-06", "all_day": True}
    class OneDay:
        def get_events(self, day):
            return [all_day]
    assert "hele dagen Ferie" in CalendarPlugin(OneDay(), "Europe/Oslo").context()


class FakeSocket:
    """Svarer som ElevenLabs etter at siste bit er committet."""

    def __init__(self, *replies):
        self.replies, self.sent, self.closed = list(replies), [], False

    def send(self, raw):
        self.sent.append(json.loads(raw))

    def recv(self, timeout=None):
        assert self.sent and self.sent[-1]["commit"], "ikke vent på svar før lyden er committet"
        return json.dumps(self.replies.pop(0))

    def close(self):
        self.closed = True


def test_stt() -> None:
    speech = (np.sin(np.arange(32000) / 8) * 8000).astype(np.int16)  # 2 s
    done = {"message_type": "committed_transcript", "text": " Hva har jeg i morgen? "}

    ws = FakeSocket({"message_type": "warning"}, {"message_type": "partial_transcript", "text": "Hva"}, done)
    assert STT(lambda: ws).transcribe(speech) == "Hva har jeg i morgen?"
    assert [c["commit"] for c in ws.sent] == [False] * (len(ws.sent) - 1) + [True], "bare siste bit committer"
    sent = b"".join(base64.b64decode(c["audio_base_64"]) for c in ws.sent)
    assert len(sent) == 64000 and ws.sent[0]["sample_rate"] == 16000, "16 kHz int16, ingenting mistet"
    assert ws.closed

    ws = FakeSocket({"message_type": "committed_transcript", "text": "Hm."})
    assert STT(lambda: ws).transcribe(speech) is None, "ett ord regnes ikke som tale"
    assert STT(lambda: FakeSocket({"message_type": "insufficient_audio_activity"})).transcribe(speech) is None
    assert STT(lambda: FakeSocket()).transcribe(np.zeros(0, dtype=np.int16)) is None, "tomt opptak sendes ikke"

    ws = FakeSocket({"message_type": "quota_exceeded", "error": "tomt"})
    try:
        STT(lambda: ws).transcribe(speech)
        raise AssertionError("feil fra tjenesten skal kastes")
    except ExternalError as e:
        assert "quota_exceeded" in str(e) and ws.closed, "socketen lukkes også ved feil"

    def down():
        raise OSError("nett nede")
    try:
        STT(down).transcribe(speech)
        raise AssertionError("nettfeil skal bli ExternalError")
    except ExternalError:
        pass


# Samme skjelett som docs/INTERFACE.md, avsnitt 4 og 5.
class FakeSource(Plugin):
    name = "kilde"
    writes = {"post_note"}
    tools = [{"name": "get_note", "description": "x", "strict": True,
              "input_schema": {"type": "object", "properties": {}, "required": [], "additionalProperties": False}},
             {"name": "post_note", "description": "x", "strict": True,
              "input_schema": {"type": "object", "properties": {}, "required": [], "additionalProperties": False}}]

    def __init__(self, down: bool = False) -> None:
        self.down, self.posted = down, 0

    def context(self) -> str:
        if self.down:
            raise ExternalError("nede")
        return "Notat: hei."

    def call(self, tool: str, args: dict) -> str:
        if tool == "post_note":
            self.posted += 1
        return json.dumps({"ok": True})


class FakeGame(Plugin):
    name = "spill"

    def __init__(self) -> None:
        self.running, self.seen = False, []

    def handle_event(self, key: str, value) -> bool:
        if self.running and key in ("btn", "gesture", "listen", "audio_chunk", "audio_end"):
            self.seen.append(key)
            return True
        return False


class Broken(Plugin):
    name = "ødelagt"

    def handle_event(self, key: str, value) -> bool:
        raise RuntimeError("boom")


class ScriptedClient:
    """Svarer som Claude: først verktøykallet, så teksten vi ber om."""

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
    assert "Notat: hei." in a._context_line()

    a._can_write = True
    assert a._exec_tool("post_note", {}) == ('{"ok": true}', False) and src.posted == 1
    assert a._exec_tool("nope", {})[1] is True, "ukjent verktøy blir feil til modellen"


def test_write_gate() -> None:
    src = FakeSource()
    a = assistant(src, rounds=[[text("Klart!"), tool_use("post_note")], [text("Trykk på knappen først.")]])
    assert a.respond("post dette", ambient=True) == "Trykk på knappen først."
    assert src.posted == 0, "alltid-på-mikrofonen skal aldri nå pluginen"

    a = assistant(src, rounds=[[text("Klart!"), tool_use("post_note")], []])
    assert a.respond("post dette", ambient=True) == FALLBACK_TOOL_ERROR, "aldri «ok» etter en blokkert skriving"

    a = assistant(src, rounds=[[text("Klart!"), tool_use("post_note")]])
    assert a.respond("post dette") == "Klart!" and src.posted == 1, "direkte forespørsel kan skrive"

    for routine in ("evening_routine", "morning_routine"):
        posted = src.posted
        a = assistant(src, rounds=[[tool_use("post_note")], [text("God natt.")]])
        a._can_write = True  # rest fra en tidligere direkte forespørsel
        getattr(a, routine)()
        assert src.posted == posted, f"{routine} skal ikke kunne skrive"

    a = assistant(src)
    assert a._exec_tool("post_note", {}) == (f"Feil: {NEEDS_BUTTON}", True), "standard er ingen skriving"


def test_interface_guards() -> None:
    for broken in ([FakeSource(), FakeSource()], [type("Typo", (FakeSource,), {"writes": {"post_nota"}})()]):
        try:
            assistant(*broken)
            raise AssertionError("ugyldige plugins skal stoppe oppstarten")
        except ValueError:
            pass


def test_source_down() -> None:
    line = assistant(FakeSource(down=True))._context_line()
    assert "kilde: utilgjengelig." in line and "Notat" not in line
    a = assistant(FakeSource(down=True), rounds=[[text("Kalenderen er nede.")]])
    assert a.evening_routine() == "Kalenderen er nede.", "rutinen overlever en nede kilde"


def test_game_routing() -> None:
    game = FakeGame()
    assert route_event([game], "btn", 1) is False, "hviler spillet, ser assistenten alt"
    game.running = True
    assert route_event([Broken(), game], "btn", 1) is True, "en plugin som krasjer stopper ikke de andre"
    assert route_event([game], "audio_end", {}) is True and game.seen == ["btn", "audio_end"]
    assert route_event([game], "aht20_temperature", 20.0) is False, "rommet oppdateres under spill"
    game.running = False
    assert route_event([game], "btn", 1) is False


def main() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"{len(tests)} tester ok")


if __name__ == "__main__":
    main()
