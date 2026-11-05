"""Siste temp/fukt fra AHT20 + vurdering for søvn."""
from datetime import datetime
from typing import Optional


class Room:
    def __init__(self) -> None:
        self.temp: Optional[float] = None
        self.humidity: Optional[float] = None
        self.updated_at: Optional[datetime] = None

    @classmethod
    def fake(cls) -> "Room":
        r = cls()
        r.temp, r.humidity, r.updated_at = 22.4, 34.0, datetime.now()
        return r

    def update(self, key: str, value) -> bool:
        """Tar imot board-events. Returnerer True hvis nøkkelen var en romverdi."""
        try:
            v = float(value)
        except (TypeError, ValueError):
            return False
        if key == "aht20_temperature":
            self.temp = v
        elif key == "aht20_humidity":
            self.humidity = v
        else:
            return False
        self.updated_at = datetime.now()
        return True

    def snapshot(self) -> dict:
        return {
            "temp": None if self.temp is None else round(self.temp, 1),
            "humidity": None if self.humidity is None else round(self.humidity),
            "updated_at": self.updated_at.isoformat(timespec="seconds") if self.updated_at else None,
        }

    def assess(self) -> str:
        if self.temp is None or self.humidity is None:
            return "Ingen romdata ennå."
        t, rh = self.temp, self.humidity
        parts = []
        if t < 16:
            parts.append(f"{t:.1f} grader er kaldt for søvn, skru opp varmen litt")
        elif t <= 19:
            parts.append(f"{t:.1f} grader er perfekt for søvn")
        elif t <= 21:
            parts.append(f"{t:.1f} grader er litt varmt for søvn, åpne vinduet en stund")
        else:
            parts.append(f"{t:.1f} grader er for varmt for god søvn, luft ut og skru ned ovnen")
        if rh < 40:
            parts.append(f"luftfuktigheten på {rh:.0f} prosent er tørr, et glass vann på nattbordet hjelper")
        elif rh <= 60:
            parts.append(f"luftfuktigheten på {rh:.0f} prosent er bra")
        else:
            parts.append(f"luftfuktigheten på {rh:.0f} prosent er høy, luft ut ti minutter")
        return "; ".join(parts) + ". (Bra for søvn: 16–19 °C og 40–60 % RH.)"
