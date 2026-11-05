"""Calendar behind one interface. CALENDAR=demo|google.

    python -m backend.calendar_backend --check    # read-only setup check, writes nothing
"""
import json
import sys
import uuid
from datetime import date, datetime, time, timedelta
from time import monotonic
from typing import Protocol
from urllib.parse import quote
from zoneinfo import ZoneInfo

from .config import DATA_DIR, ROOT, WEEKDAYS, Config, load_config
from .plugin import TIMEOUT_S, ExternalError, Plugin, request_json

TZ = ZoneInfo("Europe/Oslo")

GOOGLE_API = "https://www.googleapis.com/calendar/v3/calendars"
GOOGLE_SCOPE = "https://www.googleapis.com/auth/calendar.events"
CACHE_S = 60  # context() reads two days every turn; any write clears the cache


class Calendar(Protocol):
    def get_events(self, day: date) -> list[dict]: ...

    def create_event(self, title: str, start: datetime, end: datetime, location: str = "") -> dict: ...

    def update_event(self, event_id: str, title: str = "", start: datetime | None = None,
                     end: datetime | None = None, location: str = "") -> dict: ...  # start and end together

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
        _ev(today, 10, 15, 12, 0, "TDT4100 Object-Oriented Programming, lecture", "S1, Sentralbygg"),
        _ev(today, 17, 0, 18, 0, "Workout", "SiT Gløshaugen"),
        _ev(today, 20, 0, 21, 30, "Math 1 study group", "Library, Realfagbygget"),
        _ev(tomorrow, 8, 15, 10, 0, "TMA4100 Mathematics 1, lecture", "R1, Realfagbygget"),
        _ev(tomorrow, 12, 0, 12, 45, "Lunch with Ingrid", "Realfagbygget canteen"),
        _ev(tomorrow, 14, 15, 16, 0, "TDT4100 exercise session", "Computer lab, Gamle elektro"),
        _ev(tomorrow, 23, 59, 23, 59, "Deadline: TDT4100 assignment 7", "Blackboard"),
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
        raise ValueError(f"unknown event id {event_id!r}")

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
    """Service account with the calendar shared to it. calendarId must be the calendar's id:
    "primary" would be the service account's own, empty calendar."""

    def __init__(self, cfg: Config, session=None) -> None:
        if not cfg.google_calendar_id:
            raise SystemExit("CALENDAR=google needs GOOGLE_CALENDAR_ID in nattbord/.env.")
        self.tz_name, self.tz = cfg.tz, ZoneInfo(cfg.tz)
        self.url = f"{GOOGLE_API}/{quote(cfg.google_calendar_id, safe='')}/events"
        self.session = session or self._login(cfg)
        self._cache: dict[date, tuple[float, list[dict]]] = {}

    @staticmethod
    def _login(cfg: Config):
        key = ROOT / cfg.google_key_file
        if not key.exists():
            raise SystemExit(f"CALENDAR=google needs the key file {key}. See the README.")
        from google.auth.transport.requests import AuthorizedSession  # only here: demo needs no Google
        from google.oauth2 import service_account

        creds = service_account.Credentials.from_service_account_file(str(key), scopes=[GOOGLE_SCOPE])
        return AuthorizedSession(creds, refresh_timeout=TIMEOUT_S)

    def _when(self, dt: datetime) -> dict:
        return {"dateTime": dt.isoformat(), "timeZone": self.tz_name}

    @staticmethod
    def _from_google(item: dict) -> dict:
        start, end = item["start"], item["end"]
        return {"id": item["id"], "title": item.get("summary") or "(untitled)",
                "location": item.get("location", ""), "all_day": "date" in start,
                "start": start.get("dateTime") or start["date"], "end": end.get("dateTime") or end["date"]}

    def _event_url(self, event_id: str) -> str:
        return f"{self.url}/{quote(event_id, safe='')}"  # the id comes from the model, never raw in the URL

    def get_events(self, day: date) -> list[dict]:
        hit = self._cache.get(day)
        if hit and monotonic() - hit[0] < CACHE_S:
            return hit[1]
        start = datetime.combine(day, time.min, self.tz)
        # shortcut: first page only (50 events per day), use nextPageToken if that gets too small.
        # singleEvents gives instance ids, so move and delete hit one occurrence, not the whole series.
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
            # date: None because PATCH merges, and an all-day event would otherwise get both date and dateTime.
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
        return GoogleCalendar(cfg)  # stops startup on bad setup: demo events must never look real
    return DemoCalendar()


def _hm(iso: str) -> str:
    return datetime.fromisoformat(iso).strftime("%H:%M")


def _fmt_events(events: list[dict]) -> list[dict]:
    return [{"id": e["id"], "title": e["title"], "location": e.get("location", ""),
             "start": "all day" if e.get("all_day") else _hm(e["start"]),
             "end": "" if e.get("all_day") else _hm(e["end"])}
            for e in events]


def _compact(events: list[dict]) -> str:
    def line(e: dict) -> str:
        when = e["start"] if e["end"] == "" else f"{e['start']}–{e['end']}"
        return f"{when} {e['title']}" + (f" ({e['location']})" if e["location"] else "")
    return "; ".join(line(e) for e in events) or "none"


class CalendarPlugin(Plugin):
    name = "calendar"
    writes = {"create_event", "update_event", "delete_event"}
    tools = [
        {
            "name": "get_events",
            "description": ("Gets the user's events for one date, with id, title, location, start and end (HH:MM). "
                            "Only call it for dates other than today and tomorrow, or when you need the id "
                            "to move or delete."),
            "strict": True,
            "input_schema": {
                "type": "object",
                "properties": {"date": {"type": "string", "description": "Date as YYYY-MM-DD"}},
                "required": ["date"],
                "additionalProperties": False,
            },
        },
        {
            "name": "create_event",
            "description": ("Adds a new event to the user's calendar. \"Four o'clock\" means 16:00 when that is "
                            "natural for a student. If no end is given, set it one hour after the start. Do not "
                            "ask for a location or duration: add it with what you know."),
            "strict": True,
            "input_schema": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Short title, e.g. \"Study group\""},
                    "start": {"type": "string", "description": "Start time as YYYY-MM-DDTHH:MM (local time)"},
                    "end": {"type": "string", "description": "End time as YYYY-MM-DDTHH:MM. Unknown: one hour after start."},
                    "location": {"type": "string", "description": "Location, empty text if unknown"},
                },
                "required": ["title", "start", "end", "location"],
                "additionalProperties": False,
            },
        },
        {
            "name": "update_event",
            "description": ("Changes or moves one event. If you do not have the id, call get_events yourself "
                            "without asking the user. Empty text means unchanged. When moving, fill in both "
                            "start and end, keeping the same duration unless told otherwise. Recurring events "
                            "only change for this one occurrence."),
            "strict": True,
            "input_schema": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "The event's id from get_events"},
                    "title": {"type": "string", "description": "New title, or empty text"},
                    "start": {"type": "string", "description": "New start as YYYY-MM-DDTHH:MM, or empty text"},
                    "end": {"type": "string", "description": "New end as YYYY-MM-DDTHH:MM, or empty text"},
                    "location": {"type": "string", "description": "New location, or empty text"},
                },
                "required": ["id", "title", "start", "end", "location"],
                "additionalProperties": False,
            },
        },
        {
            "name": "delete_event",
            "description": ("Deletes one event. If you do not have the id, call get_events yourself without "
                            "asking the user. Only delete the event the user clearly asked for. Recurring "
                            "events are only deleted for this one occurrence."),
            "strict": True,
            "input_schema": {
                "type": "object",
                "properties": {"id": {"type": "string", "description": "The event's id from get_events"}},
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
        return f"Today: {_compact(today)}. Tomorrow: {_compact(tomorrow)}."

    def call(self, tool: str, args: dict) -> str:
        if tool == "get_events":
            day = date.fromisoformat(args["date"])
            evs = _fmt_events(self.calendar.get_events(day))
            return json.dumps({"date": day.isoformat(), "weekday": WEEKDAYS[day.weekday()], "events": evs},
                              ensure_ascii=False)
        if tool == "create_event":
            start = datetime.fromisoformat(args["start"])
            try:
                end = datetime.fromisoformat(args["end"])
            except (KeyError, ValueError):
                end = start + timedelta(hours=1)
            return self._done(self.calendar.create_event(args["title"].strip(), start, end,
                                                         args.get("location", "").strip()))
        if tool == "update_event":
            if bool(args["start"]) != bool(args["end"]):
                raise ValueError("give both start and end to move the event")
            if not (args["title"] or args["start"] or args["location"]):
                raise ValueError("nothing to change")
            start = datetime.fromisoformat(args["start"]) if args["start"] else None
            end = datetime.fromisoformat(args["end"]) if args["end"] else None
            return self._done(self.calendar.update_event(args["id"], args["title"].strip(), start, end,
                                                         args["location"].strip()))
        if tool == "delete_event":
            self.calendar.delete_event(args["id"])
            return json.dumps({"ok": True})
        raise ValueError(f"unknown tool {tool}")

    @staticmethod
    def _done(ev: dict) -> str:
        f = _fmt_events([ev])[0]
        return json.dumps({"ok": True, "id": f["id"], "title": f["title"], "date": ev["start"][:10],
                           "start": f["start"], "end": f["end"]}, ensure_ascii=False)


def _check() -> int:
    """Checks the setup with one read. Writes nothing to the calendar."""
    cfg = load_config()
    calendar = get_calendar(cfg)
    print(f"Calendar: {type(calendar).__name__}")
    try:
        for ev in _fmt_events(calendar.get_events(datetime.now(ZoneInfo(cfg.tz)).date())):
            print(f"  {ev['start']} {ev['title']}")
    except ExternalError as e:
        print(f"Error: {e}")
        if e.status == 404:
            print("Calendar not found. Share it with the service account and check GOOGLE_CALENDAR_ID.")
        return 1
    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(_check() if "--check" in sys.argv else "usage: python -m backend.calendar_backend --check")
