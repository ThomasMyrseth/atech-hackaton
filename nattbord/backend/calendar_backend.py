"""Kalender bak ett grensesnitt. CALENDAR=demo|google."""
import json
import uuid
from datetime import date, datetime, time, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from .config import DATA_DIR, Config

TZ = ZoneInfo("Europe/Oslo")


class Calendar(Protocol):
    def get_events(self, day: date) -> list[dict]: ...
    def create_event(self, title: str, start: datetime, end: datetime) -> dict: ...


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

    def get_events(self, day: date) -> list[dict]:
        evs = [e for e in self.data["events"] if datetime.fromisoformat(e["start"]).date() == day]
        return sorted(evs, key=lambda e: e["start"])

    def create_event(self, title: str, start: datetime, end: datetime) -> dict:
        if start.tzinfo is None:
            start = start.replace(tzinfo=TZ)
        if end.tzinfo is None:
            end = end.replace(tzinfo=TZ)
        if end <= start:
            end = start + timedelta(hours=1)
        ev = {"id": uuid.uuid4().hex[:8], "title": title, "location": "",
              "start": start.isoformat(timespec="minutes"), "end": end.isoformat(timespec="minutes")}
        self.data["events"].append(ev)
        self._save()
        return ev


class GoogleCalendar:
    def __init__(self, cfg: Config) -> None:
        raise NotImplementedError(
            "GoogleCalendar er ikke implementert ennå (stretch etter M3). "
            "Bruk CALENDAR=demo, eller legg inn service account + delt kalender."
        )


def get_calendar(cfg: Config) -> Calendar:
    if cfg.calendar == "google":
        return GoogleCalendar(cfg)
    return DemoCalendar()
