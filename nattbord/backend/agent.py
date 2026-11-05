"""Claude-agent med verktøy for nattbordsassistenten (norsk, kort, muntlig)."""
import json
import re
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import anthropic

from .config import Config
from .log import log

UKEDAGER = ["mandag", "tirsdag", "onsdag", "torsdag", "fredag", "lørdag", "søndag"]
MANEDER = ["januar", "februar", "mars", "april", "mai", "juni", "juli", "august",
           "september", "oktober", "november", "desember"]

FALLBACK_ERROR = "Beklager, jeg får ikke kontakt med hjernen min akkurat nå. Prøv igjen om litt."
FALLBACK_REFUSAL = "Det kan jeg dessverre ikke hjelpe med."

# Statisk systemprompt (dato/tid legges i brukermeldingen, så historikken er append-only).
SYSTEM_PROMPT = """Du er en liten, vennlig nattbordsassistent på hybelen til en student i Trondheim. Alt du skriver blir lest høyt av en høyttaler.

Slik svarer du:
- Alltid norsk bokmål, muntlig og naturlig. Maks to korte setninger, under 30 ord. Ved oppramsing av avtaler: nevn de to eller tre viktigste kort.
- Ingen markdown, lister, emoji, parenteser eller forkortelser. Si klokkeslett slik folk sier dem: «kvart over åtte», «halv tre», «klokka fire». Si «i dag», «i morgen» eller ukedagen i stedet for datoer.
- Varm og litt leken, med litt personlighet, men aldri masete.
- Teksten kommer ofte fra talegjenkjenning og kan ha feil. Tolk velvillig. Er det helt uforståelig, be kort om å gjenta.

Kontekst:
- Siste brukermelding starter med [Nå: …] og [Kontekst: …] med avtalene i dag og i morgen og siste romdata. Svar direkte fra konteksten uten verktøy når den holder. Kall get_events bare for andre datoer. Regn ut «i dag», «i morgen» og ukedager fra [Nå: …].

Verktøy:
- Handlinger (create_event, set_alarm, set_light, show_face): skriv den korte bekreftelsen til brukeren i SAMME svar som verktøykallet, ikke etterpå. Eksempel: «Klart, kollokvie klokka fire i morgen er lagt inn.»
- Ny avtale: kall create_event med tid som YYYY-MM-DDTHH:MM. «Klokka fire» betyr 16:00 når det er naturlig for en student. Er slutt ikke oppgitt, sett én time etter start. Bekreft kort etterpå.
- Spørsmål om temperatur, luft, fukt eller søvnklima: kall get_room.
- Lys og nattlys: set_light. Vekking: set_alarm med 24-timers HH:MM. show_face brukes bare når det passer.
- Når du bruker et verktøy, kan du si en kort setning først. Finnes det ikke et verktøy for det brukeren ber om, si det kort i stedet for å gjette.
- Ikke ta med interne eller system-XML-tagger i svaret."""

TOOLS = [
    {
        "name": "get_events",
        "description": "Henter brukerens kalenderavtaler for én dato. Returnerer tittel, sted, start og slutt (HH:MM).",
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
        "description": "Legger inn en ny avtale i brukerens kalender.",
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "tittel": {"type": "string", "description": "Kort tittel, f.eks. «Kollokvie med gruppa»"},
                "start": {"type": "string", "description": "Starttid som YYYY-MM-DDTHH:MM (lokal tid)"},
                "slutt": {"type": "string", "description": "Sluttid som YYYY-MM-DDTHH:MM. Ukjent: én time etter start."},
            },
            "required": ["tittel", "start", "slutt"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_room",
        "description": "Siste måling av temperatur og luftfuktighet på rommet, med vurdering for søvn (16–19 °C og 40–60 % RH er bra).",
        "strict": True,
        "input_schema": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
    },
    {
        "name": "set_light",
        "description": "Styrer LED-lyset: off (av), nightlight (svakt oransje nattlys), status (farge etter romklima), sunrise (soloppgang over 15 minutter).",
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {"modus": {"type": "string", "enum": ["off", "nightlight", "status", "sunrise"]}},
            "required": ["modus"],
            "additionalProperties": False,
        },
    },
    {
        "name": "set_alarm",
        "description": "Setter vekkerklokka. Soloppgangslyset starter automatisk 15 minutter før.",
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {"tid": {"type": "string", "description": "Klokkeslett som HH:MM (24 timer)"}},
            "required": ["tid"],
            "additionalProperties": False,
        },
    },
    {
        "name": "show_face",
        "description": "Viser et ansiktsuttrykk på den lille skjermen.",
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {"uttrykk": {"type": "string", "enum": ["idle", "listening", "thinking", "talking", "sleep"]}},
            "required": ["uttrykk"],
            "additionalProperties": False,
        },
    },
]


def _hm(iso: str) -> str:
    return datetime.fromisoformat(iso).strftime("%H:%M")


def _fmt_events(events: list[dict]) -> list[dict]:
    return [{"tittel": e["title"], "sted": e.get("location", ""), "start": _hm(e["start"]), "slutt": _hm(e["end"])}
            for e in events]


class Assistant:
    MAX_ROUNDS = 5
    FAST_TOOLS = {"create_event", "set_alarm", "set_light", "show_face"}

    def __init__(self, device, room, calendar, cfg: Config) -> None:
        self.device, self.room, self.calendar, self.cfg = device, room, calendar, cfg
        self.tz = ZoneInfo(cfg.tz)
        kw = {"timeout": 20.0, "max_retries": 1}
        self.client = anthropic.Anthropic(api_key=cfg.anthropic_api_key, **kw) if cfg.anthropic_api_key \
            else anthropic.Anthropic(**kw)
        self.history: list[dict] = []

    # ------------------------------------------------------------------ public
    def reset(self) -> None:
        self.history = []
        log("agent", "samtalehistorikk nullstilt")

    def _context_line(self) -> str:
        now = datetime.now(self.tz)
        try:
            today = _fmt_events(self.calendar.get_events(now.date()))
            tomorrow = _fmt_events(self.calendar.get_events((now + timedelta(days=1)).date()))
        except Exception as e:  # noqa: BLE001
            log("warn", f"kalender utilgjengelig: {e}")
            today = tomorrow = []
        compact = lambda evs: "; ".join(f"{e['start']}–{e['slutt']} {e['tittel']}" + (f" ({e['sted']})" if e["sted"] else "")
                                        for e in evs) or "ingen"
        snap = self.room.snapshot()
        room = (f"{snap.get('temp')} °C, {snap.get('humidity')} % RH – {self.room.assess()}"
                if snap.get("temp") is not None else "ingen måling ennå")
        return f"[Kontekst: I dag: {compact(today)}. I morgen: {compact(tomorrow)}. Rom: {room}]"

    def _user_msg(self, text: str) -> dict:
        return {"role": "user", "content": [{"type": "text", "text": text}]}

    def _with_history(self, msg: dict) -> list[dict]:
        """Hurtigsporet kan la historikken slutte med tool_result; slå da sammen med ny brukermelding."""
        if self.history and self.history[-1]["role"] == "user":
            last = self.history[-1]
            merged = {"role": "user", "content": list(last["content"]) + list(msg["content"])}
            return self.history[:-1] + [merged]
        return self.history + [msg]

    def respond(self, user_text: str) -> str:
        msg = self._user_msg(f"{self._now_line()}\n{self._context_line()}\n{user_text}")
        text, msgs = self._run(self._with_history(msg))
        if msgs is not None:
            self.history = msgs
            self._trim()
        return text

    def evening_routine(self) -> str:
        now = datetime.now(self.tz)
        tomorrow = (now + timedelta(days=1)).date()
        snap = self.room.snapshot()
        events = _fmt_events(self.calendar.get_events(tomorrow))
        prompt = (
            f"{self._now_line()}\n"
            "Det er kveld, brukeren er til stede.\n"
            f"Romdata: {json.dumps(snap, ensure_ascii=False)}. Vurdering: {self.room.assess()}\n"
            f"Kalender i morgen ({UKEDAGER[tomorrow.weekday()]}): {json.dumps(events, ensure_ascii=False)}\n"
            "Bestem selv hva du sier, maks to til tre korte setninger: minn om det viktigste i morgen, "
            "gi råd om lufting eller temperatur hvis rommet trenger det, og sett gjerne vekking med set_alarm "
            "omtrent en time før første avtale i morgen og si hva du satte den til. "
            "Slå på nattlyset med set_light nightlight. Skriv hele beskjeden i samme svar som verktøykallene."
        )
        log("routine", "kveldsrutine startet")
        self._nightlight_set = False
        text, msgs = self._run(self._with_history(self._user_msg(prompt)))
        if not self._nightlight_set:
            log("routine", "modellen slo ikke på nattlys selv, gjør det nå")
            self.device.action("led_mode", "nightlight")
        if msgs is not None:
            self.history = msgs
            self._trim()
        return text

    # ---------------------------------------------------------------- internals
    def _now_line(self) -> str:
        n = datetime.now(self.tz)
        return f"[Nå: {UKEDAGER[n.weekday()]} {n.day}. {MANEDER[n.month - 1]} {n.year}, klokka {n:%H:%M} ({n:%Y-%m-%d})]"

    def _request_kwargs(self) -> dict:
        kw = {
            "model": self.cfg.model,
            "max_tokens": 1024,  # tak, ikke mål: prompten holder svarene korte
            "system": SYSTEM_PROMPT,
            "tools": TOOLS,
        }
        if self.cfg.thinking == "off":
            kw["thinking"] = {"type": "disabled"}
            kw["output_config"] = {"effort": self.cfg.effort if self.cfg.effort in ("low", "medium", "high") else "low"}
        else:
            kw["thinking"] = {"type": "adaptive"}
            kw["output_config"] = {"effort": self.cfg.effort}
        return kw

    def _run(self, messages: list[dict]) -> tuple[str, list[dict] | None]:
        """Verktøyløkke. Returnerer (tekst, ny historikk) – historikk None betyr: ikke lagre turen."""
        messages = list(messages)
        t_total = time.monotonic()
        text = ""
        for _ in range(self.MAX_ROUNDS):
            t0 = time.monotonic()
            try:
                resp = self.client.messages.create(messages=messages, **self._request_kwargs())
            except anthropic.AuthenticationError:
                log("warn", "Anthropic: ugyldig eller manglende API-nøkkel (ANTHROPIC_API_KEY i nattbord/.env)")
                return FALLBACK_ERROR, None
            except anthropic.RateLimitError:
                log("warn", "Anthropic: rate limit")
                return FALLBACK_ERROR, None
            except anthropic.APIStatusError as e:
                log("warn", f"Anthropic API-feil {e.status_code}: {e.message}")
                return FALLBACK_ERROR, None
            except anthropic.APIConnectionError:
                log("warn", "Anthropic: ingen nettforbindelse / timeout")
                return FALLBACK_ERROR, None
            dt = time.monotonic() - t0
            log("agent", f"Claude svarte på {dt:.2f}s (stop={resp.stop_reason}, "
                         f"in={resp.usage.input_tokens} out={resp.usage.output_tokens})")

            if resp.stop_reason == "refusal":
                log("warn", "Claude avslo forespørselen")
                return FALLBACK_REFUSAL, None

            text = " ".join(b.text for b in resp.content if b.type == "text").strip()
            messages.append({"role": "assistant", "content": resp.content})

            if resp.stop_reason != "tool_use":
                break

            results = []
            any_error = False
            names = set()
            for block in resp.content:
                if block.type != "tool_use":
                    continue
                names.add(block.name)
                result, is_error = self._exec_tool(block.name, block.input)
                any_error |= is_error
                entry = {"type": "tool_result", "tool_use_id": block.id, "content": result}
                if is_error:
                    entry["is_error"] = True
                results.append(entry)
            messages.append({"role": "user", "content": results})
            # Hurtigspor: rene handlinger som lyktes, og bekreftelsen er allerede skrevet.
            if text and not any_error and names <= self.FAST_TOOLS:
                log("agent", "[dim]hurtigspor: hopper over runde 2[/dim]")
                break
        else:
            log("warn", f"nådde maks {self.MAX_ROUNDS} verktøyrunder")

        log("time", f"agent totalt {time.monotonic() - t_total:.2f}s")
        if not text:
            text = "Ok, det er gjort."
        log("agent", f"svar: {text}")
        return text, messages

    def _exec_tool(self, name: str, args: dict) -> tuple[str, bool]:
        t0 = time.monotonic()
        try:
            result = self._dispatch(name, args)
            is_error = False
        except Exception as e:  # verktøyfeil sendes tilbake til modellen
            result, is_error = f"Feil: {e}", True
        short = result if len(result) <= 160 else result[:157] + "..."
        log("tool", f"{name}({json.dumps(args, ensure_ascii=False)}) → {short} "
                    f"[{(time.monotonic() - t0) * 1000:.0f} ms]", "bold red" if is_error else "")
        return result, is_error

    def _dispatch(self, name: str, a: dict) -> str:
        if name == "get_events":
            day = date.fromisoformat(a["dato"])
            evs = _fmt_events(self.calendar.get_events(day))
            return json.dumps({"dato": day.isoformat(), "ukedag": UKEDAGER[day.weekday()], "avtaler": evs},
                              ensure_ascii=False)
        if name == "create_event":
            start = datetime.fromisoformat(a["start"])
            try:
                end = datetime.fromisoformat(a["slutt"])
            except (KeyError, ValueError):
                end = start + timedelta(hours=1)
            ev = self.calendar.create_event(a["tittel"].strip(), start, end)
            return json.dumps({"ok": True, "tittel": ev["title"], "dato": ev["start"][:10],
                               "start": _hm(ev["start"]), "slutt": _hm(ev["end"])}, ensure_ascii=False)
        if name == "get_room":
            return json.dumps({**self.room.snapshot(), "vurdering": self.room.assess()}, ensure_ascii=False)
        if name == "set_light":
            modus = a["modus"]
            value = "sunrise:15" if modus == "sunrise" else modus
            ok = self.device.action("led_mode", value)
            if modus == "nightlight":
                self._nightlight_set = True
            return json.dumps({"ok": bool(ok), "modus": modus})
        if name == "set_alarm":
            tid = a["tid"].strip()
            if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", tid):
                raise ValueError(f"ugyldig tid {tid!r}, bruk HH:MM")
            ok = self.device.action("set_alarm", tid)
            return json.dumps({"ok": bool(ok), "tid": tid, "soloppgang_fra": _minus(tid, 15)})
        if name == "show_face":
            ok = self.device.action("face", a["uttrykk"])
            return json.dumps({"ok": bool(ok)})
        raise ValueError(f"ukjent verktøy {name}")

    def _trim(self) -> None:
        if self.cfg.thinking != "off":
            # Med thinking må historikken være append-only (ellers 400): nullstill heller enn å klippe.
            if len(self.history) > 40:
                self.history = []
            return
        max_msgs = 12
        if len(self.history) <= max_msgs:
            return
        def clean_user_turn(m: dict) -> bool:  # brukermelding uten tool_result = trygt kuttpunkt
            if m["role"] != "user":
                return False
            if isinstance(m["content"], str):
                return True
            return not any(isinstance(b, dict) and b.get("type") == "tool_result" for b in m["content"])

        for i in range(len(self.history) - max_msgs, len(self.history)):
            m = self.history[i]
            if clean_user_turn(m):
                self.history = self.history[i:]
                return
        self.history = []

    _nightlight_set = False


def _minus(hhmm: str, minutes: int) -> str:
    h, m = map(int, hhmm.split(":"))
    t = (h * 60 + m - minutes) % (24 * 60)
    return f"{t // 60:02d}:{t % 60:02d}"
