"""Kalender bak ett grensesnitt. CALENDAR=demo|google.

    python -m backend.calendar_backend --check    # lesesjekk av oppsettet, skriver ingenting
"""
import json
import sys
import uuid
from datetime import date, datetime, time, timedelta
from time import monotonic
from typing import Protocol
from urllib.parse import quote
from zoneinfo import ZoneInfo

from .config import DATA_DIR, ROOT, UKEDAGER, Config, load_config
from .plugin import TIMEOUT_S, ExternalError, Plugin, request_json

TZ = ZoneInfo("Europe/Oslo")

GOOGLE_API = "https://www.googleapis.com/calendar/v3/calendars"
GOOGLE_SCOPE = "https://www.googleapis.com/auth/calendar.events"
CACHE_S = 60  # context() leser to dager hver tur; ved skriving tømmes cachen


class Calendar(Protocol):
    def get_events(self, day: date) -> list[dict]: ...
    def create_event(self, title: str, start: datetime, end: datetime, location: str = "") -> dict: ...

    def update_event(self, event_id: str, title: str = "", start: datetime | None = None,
                     end: datetime | None = None, location: str = "") -> dict: ...  # start og end sammen

    def delete_event(self, event_id: str) -> None: ...


def _aware(start: datetime, end: datetime, tz: ZoneInfo) -> tuple[datetime, datetime]:
    if start.tzinfo is None:
        start = start.replace(tzinfo=tz)
    if end.tzinfo is None:
        end = end.replace(tzinfo=tz)
    return start, end if end > start else start + timedelta(hours=1)


def _ev(day: date, h1: int, m1: int, h2: int, m2: int, title: str, location: str = "") -> dict:
    start = datetime.combine(day, time(h1, m1), TZ)
    end = datetime.combine(day, time(h2, m2), TZ)
    return {"id": uuid.uuid4().hex[:8], "title": title, "location": location,
            "start": start.isoformat(timespec="minutes"), "end": end.isoformat(timespec="minutes")}


def _seed(today: date) -> list[dict]:
    tomorrow = today + timedelta(days=1)
    return [
        _ev(today, 10, 15, 12, 0, "TDT4100 Objektorientert programmering, forelesning", "S1, Sentralbygg"),
        _ev(today, 17, 0, 18, 0, "Trening", "SiT Gløshaugen"),
        _ev(today, 20, 0, 21, 30, "Kollokvie Matte 1", "Biblioteket, Realfagbygget"),
        _ev(tomorrow, 8, 15, 10, 0, "TMA4100 Matematikk 1, forelesning", "R1, Realfagbygget"),
        _ev(tomorrow, 12, 0, 12, 45, "Lunsj med Ingrid", "Realfagskantina"),
        _ev(tomorrow, 14, 15, 16, 0, "Øvingstime TDT4100", "Datasal, Gamle elektro"),
        _ev(tomorrow, 23, 59, 23, 59, "Frist: øving 7 i TDT4100", "Blackboard"),
    ]


class DemoCalendar:
    def __init__(self, path=DATA_DIR / "demo_calendar.json") -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        today = datetime.now(TZ).date()
        data = None
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                data = None
        if not data or data.get("seeded_on") != today.isoformat():
            data = {"seeded_on": today.isoformat(), "events": _seed(today)}
        self.data = data
        self._save()

    def _save(self) -> None:
        self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")

    def _find(self, event_id: str) -> dict:
        for ev in self.data["events"]:
            if ev["id"] == event_id:
                return ev
        raise ValueError(f"ukjent avtale-id {event_id!r}")

    def get_events(self, day: date) -> list[dict]:
        evs = [e for e in self.data["events"] if datetime.fromisoformat(e["start"]).date() == day]
        return sorted(evs, key=lambda e: e["start"])

    def create_event(self, title: str, start: datetime, end: datetime, location: str = "") -> dict:
        start, end = _aware(start, end, TZ)
        ev = {"id": uuid.uuid4().hex[:8], "title": title, "location": location,
              "start": start.isoformat(timespec="minutes"), "end": end.isoformat(timespec="minutes")}
        self.data["events"].append(ev)
        self._save()
        return ev

    def update_event(self, event_id: str, title: str = "", start: datetime | None = None,
                     end: datetime | None = None, location: str = "") -> dict:
        ev = self._find(event_id)
        if title:
            ev["title"] = title
        if location:
            ev["location"] = location
        if start:
            start, end = _aware(start, end or start, TZ)
            ev["start"], ev["end"] = start.isoformat(timespec="minutes"), end.isoformat(timespec="minutes")
        self._save()
        return ev

    def delete_event(self, event_id: str) -> None:
        self.data["events"].remove(self._find(event_id))
        self._save()


class GoogleCalendar:
    """Service account med kalenderen delt til seg. calendarId må være kalenderens id:
    "primary" ville vært service accountens egen, tomme kalender."""

    def __init__(self, cfg: Config, session=None) -> None:
        if not cfg.google_calendar_id:
            raise SystemExit("CALENDAR=google krever GOOGLE_CALENDAR_ID i nattbord/.env.")
        self.tz_name, self.tz = cfg.tz, ZoneInfo(cfg.tz)
        self.url = f"{GOOGLE_API}/{quote(cfg.google_calendar_id, safe='')}/events"
        self.session = session or self._login(cfg)
        self._cache: dict[date, tuple[float, list[dict]]] = {}

    @staticmethod
    def _login(cfg: Config):
        key = ROOT / cfg.google_key_file
        if not key.exists():
            raise SystemExit(f"CALENDAR=google krever nøkkelfilen {key}. Se README.")
        from google.auth.transport.requests import AuthorizedSession  # bare her: demo trenger ikke Google
        from google.oauth2 import service_account

        creds = service_account.Credentials.from_service_account_file(str(key), scopes=[GOOGLE_SCOPE])
        return AuthorizedSession(creds, refresh_timeout=TIMEOUT_S)

    def _when(self, dt: datetime) -> dict:
        return {"dateTime": dt.isoformat(), "timeZone": self.tz_name}

    @staticmethod
    def _from_google(item: dict) -> dict:
        start, end = item["start"], item["end"]
        return {"id": item["id"], "title": item.get("summary") or "(uten tittel)",
                "location": item.get("location", ""), "all_day": "date" in start,
                "start": start.get("dateTime") or start["date"], "end": end.get("dateTime") or end["date"]}

    def _event_url(self, event_id: str) -> str:
        return f"{self.url}/{quote(event_id, safe='')}"  # id-en kommer fra modellen, aldri rå i URL-en

    def get_events(self, day: date) -> list[dict]:
        hit = self._cache.get(day)
        if hit and monotonic() - hit[0] < CACHE_S:
            return hit[1]
        start = datetime.combine(day, time.min, self.tz)
        # shortcut: bare første side (50 avtaler per dag), bruk nextPageToken hvis det blir for lite.
        # singleEvents gir instans-id-er, så flytt og slett treffer én forekomst, ikke hele serien.
        params = {"timeMin": start.isoformat(), "timeMax": (start + timedelta(days=1)).isoformat(),
                  "timeZone": self.tz_name, "singleEvents": "true", "orderBy": "startTime", "maxResults": 50}
        data = request_json(self.session, "GET", self.url, params=params)
        events = [self._from_google(item) for item in data.get("items", [])]
        self._cache[day] = (monotonic(), events)
        return events

    def create_event(self, title: str, start: datetime, end: datetime, location: str = "") -> dict:
        start, end = _aware(start, end, self.tz)
        body = {"summary": title, "location": location, "start": self._when(start), "end": self._when(end)}
        ev = self._from_google(request_json(self.session, "POST", self.url, json=body))
        self._cache.clear()
        return ev

    def update_event(self, event_id: str, title: str = "", start: datetime | None = None,
                     end: datetime | None = None, location: str = "") -> dict:
        body: dict = {}
        if title:
            body["summary"] = title
        if location:
            body["location"] = location
        if start:
            start, end = _aware(start, end or start, self.tz)
            # date: None fordi PATCH fletter, og en heldagsavtale ellers ville fått både date og dateTime.
            body["start"] = {**self._when(start), "date": None}
            body["end"] = {**self._when(end), "date": None}
        ev = self._from_google(request_json(self.session, "PATCH", self._event_url(event_id), json=body))
        self._cache.clear()
        return ev

    def delete_event(self, event_id: str) -> None:
        request_json(self.session, "DELETE", self._event_url(event_id))
        self._cache.clear()


def get_calendar(cfg: Config) -> Calendar:
    if cfg.calendar == "google":
        return GoogleCalendar(cfg)  # stopper oppstarten ved feil oppsett: demo-avtaler må aldri se ekte ut
    return DemoCalendar()


def _hm(iso: str) -> str:
    return datetime.fromisoformat(iso).strftime("%H:%M")


def _fmt_events(events: list[dict]) -> list[dict]:
    return [{"id": e["id"], "tittel": e["title"], "sted": e.get("location", ""),
             "start": "hele dagen" if e.get("all_day") else _hm(e["start"]),
             "slutt": "" if e.get("all_day") else _hm(e["end"])}
            for e in events]


def _compact(events: list[dict]) -> str:
    def line(e: dict) -> str:
        when = e["start"] if e["slutt"] == "" else f"{e['start']}–{e['slutt']}"
        return f"{when} {e['tittel']}" + (f" ({e['sted']})" if e["sted"] else "")
    return "; ".join(line(e) for e in events) or "ingen"


class CalendarPlugin(Plugin):
    name = "kalender"
    writes = {"create_event", "update_event", "delete_event"}
    tools = [
        {
            "name": "get_events",
            "description": ("Henter brukerens avtaler for én dato, med id, tittel, sted, start og slutt (HH:MM). "
                            "Kall den bare for andre datoer enn i dag og i morgen, eller når du trenger id-en "
                            "for å flytte eller slette."),
            "strict": True,
            "input_schema": {
                "type": "object",
                "properties": {"dato": {"type": "string", "description": "Dato som YYYY-MM-DD"}},
                "required": ["dato"],
                "additionalProperties": False,
            },
        },
        {
            "name": "create_event",
            "description": ("Legger inn en ny avtale i brukerens kalender. «Klokka fire» betyr 16:00 når det er "
                            "naturlig for en student. Er slutt ikke oppgitt, sett én time etter start. Ikke spør "
                            "etter sted eller varighet: legg inn med det du vet."),
            "strict": True,
            "input_schema": {
                "type": "object",
                "properties": {
                    "tittel": {"type": "string", "description": "Kort tittel, f.eks. «Kollokvie med gruppa»"},
                    "start": {"type": "string", "description": "Starttid som YYYY-MM-DDTHH:MM (lokal tid)"},
                    "slutt": {"type": "string", "description": "Sluttid som YYYY-MM-DDTHH:MM. Ukjent: én time etter start."},
                    "sted": {"type": "string", "description": "Sted, tom tekst hvis ukjent"},
                },
                "required": ["tittel", "start", "slutt", "sted"],
                "additionalProperties": False,
            },
        },
        {
            "name": "update_event",
            "description": ("Endrer eller flytter én avtale. Mangler du id-en, kall get_events selv uten å spørre "
                            "brukeren. Tom tekst betyr "
                            "uendret. Ved flytting må både start og slutt fylles ut, med samme varighet som "
                            "før hvis ikke annet er sagt. Gjentakende avtaler endres bare for denne ene gangen."),
            "strict": True,
            "input_schema": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "Avtalens id fra get_events"},
                    "tittel": {"type": "string", "description": "Ny tittel, eller tom tekst"},
                    "start": {"type": "string", "description": "Ny start som YYYY-MM-DDTHH:MM, eller tom tekst"},
                    "slutt": {"type": "string", "description": "Ny slutt som YYYY-MM-DDTHH:MM, eller tom tekst"},
                    "sted": {"type": "string", "description": "Nytt sted, eller tom tekst"},
                },
                "required": ["id", "tittel", "start", "slutt", "sted"],
                "additionalProperties": False,
            },
        },
        {
            "name": "delete_event",
            "description": ("Sletter én avtale. Mangler du id-en, kall get_events selv uten å spørre brukeren. "
                            "Slett bare avtalen brukeren "
                            "tydelig ba om. Gjentakende avtaler slettes bare for denne ene gangen."),
            "strict": True,
            "input_schema": {
                "type": "object",
                "properties": {"id": {"type": "string", "description": "Avtalens id fra get_events"}},
                "required": ["id"],
                "additionalProperties": False,
            },
        },
    ]

    def __init__(self, calendar: Calendar, tz: str) -> None:
        self.calendar, self.tz = calendar, ZoneInfo(tz)

    def context(self) -> str:
        now = datetime.now(self.tz)
        today = _fmt_events(self.calendar.get_events(now.date()))
        tomorrow = _fmt_events(self.calendar.get_events((now + timedelta(days=1)).date()))
        return f"I dag: {_compact(today)}. I morgen: {_compact(tomorrow)}."

    def call(self, tool: str, args: dict) -> str:
        if tool == "get_events":
            day = date.fromisoformat(args["dato"])
            evs = _fmt_events(self.calendar.get_events(day))
            return json.dumps({"dato": day.isoformat(), "ukedag": UKEDAGER[day.weekday()], "avtaler": evs},
                              ensure_ascii=False)
        if tool == "create_event":
            start = datetime.fromisoformat(args["start"])
            try:
                end = datetime.fromisoformat(args["slutt"])
            except (KeyError, ValueError):
                end = start + timedelta(hours=1)
            return self._done(self.calendar.create_event(args["tittel"].strip(), start, end, args.get("sted", "").strip()))
        if tool == "update_event":
            if bool(args["start"]) != bool(args["slutt"]):
                raise ValueError("oppgi både start og slutt for å flytte avtalen")
            if not (args["tittel"] or args["start"] or args["sted"]):
                raise ValueError("ingenting å endre")
            start = datetime.fromisoformat(args["start"]) if args["start"] else None
            end = datetime.fromisoformat(args["slutt"]) if args["slutt"] else None
            return self._done(self.calendar.update_event(args["id"], args["tittel"].strip(), start, end, args["sted"].strip()))
        if tool == "delete_event":
            self.calendar.delete_event(args["id"])
            return json.dumps({"ok": True})
        raise ValueError(f"ukjent verktøy {tool}")

    @staticmethod
    def _done(ev: dict) -> str:
        f = _fmt_events([ev])[0]
        return json.dumps({"ok": True, "id": f["id"], "tittel": f["tittel"], "dato": ev["start"][:10],
                           "start": f["start"], "slutt": f["slutt"]}, ensure_ascii=False)


def _check() -> int:
    """Sjekker oppsettet med én lesing. Skriver ingenting til kalenderen."""
    cfg = load_config()
    calendar = get_calendar(cfg)
    print(f"Kalender: {type(calendar).__name__}")
    try:
        for ev in _fmt_events(calendar.get_events(datetime.now(ZoneInfo(cfg.tz)).date())):
            print(f"  {ev['start']} {ev['tittel']}")
    except ExternalError as e:
        print(f"Feil: {e}")
        if e.status == 404:
            print("Fant ikke kalenderen. Del den med service accounten og sjekk GOOGLE_CALENDAR_ID.")
        return 1
    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(_check() if "--check" in sys.argv else "bruk: python -m backend.calendar_backend --check")
