"""Latest temperature and humidity from the AHT20, plus an assessment for sleep."""
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
        """Takes board events. Returns True if the key was a room value."""
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
            return "No room data yet."
        t, rh = self.temp, self.humidity
        parts = []
        if t < 16:
            parts.append(f"{t:.1f} degrees is cold for sleep, turn the heat up a little")
        elif t <= 19:
            parts.append(f"{t:.1f} degrees is perfect for sleep")
        elif t <= 21:
            parts.append(f"{t:.1f} degrees is a bit warm for sleep, open the window for a while")
        else:
            parts.append(f"{t:.1f} degrees is too warm for good sleep, air the room and turn the heater down")
        if rh < 40:
            parts.append(f"humidity at {rh:.0f} percent is dry, a glass of water on the nightstand helps")
        elif rh <= 60:
            parts.append(f"humidity at {rh:.0f} percent is good")
        else:
            parts.append(f"humidity at {rh:.0f} percent is high, air the room for ten minutes")
        return "; ".join(parts) + ". (Good for sleep: 16–19 °C and 40–60 % RH.)"
