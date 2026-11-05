# Grensesnittet mot brettet og verden utenfor

Les dette før du legger til en ekstern kilde eller et spill. Du skal ikke trenge å lese agentløkka.

Det finnes to kontrakter:

- **`Device`** (`backend/device.py`) er eneste vei til brettet.
- **`Plugin`** (`backend/plugin.py`) er alt som legges til assistenten.

En ny kilde eller et nytt spill er én klasse pluss én linje i `get_plugins`. `agent.py` skal ikke endres.

## 1. Lagene

```
brett ⇄ USB-seriell ⇄ Device ⇄ App (main.py) ⇄ Assistant (agent.py) ⇄ Plugin ⇄ verden utenfor
```

Brettet har ikke Wi-Fi i firmwaren. Alt utenfra går derfor gjennom laptopen, og eksterne data når brettet når agenten handler: tale, LED, alarm eller ansikt.

Tre tråder er verdt å kjenne:

| Tråd | Kjører | Regel |
|---|---|---|
| SDK-lesetråd | leser seriellporten, leverer acks | rør den aldri |
| Hovedløkka (`App.run`) | `device.recv`, `on_event`, `handle_event` | må aldri blokkere |
| Arbeidertråden (`App.worker`) | STT, Claude, `call`, `context`, `after_turn`, tale | én jobb om gangen, så en treg jobb holder igjen neste |

## 2. Kontrakten mot brettet

Formatet er én JSON-linje per melding, 115200 baud. Firmwaren er `firmware/project.yaml`.

### Grenser

| Grense | Hva det betyr for deg |
|---|---|
| Linjer er maks 4096 tegn | Tegn utover det forsvinner uten feilmelding. Del opp lange verdier. |
| `value` er alltid tekst | Tall og JSON må tolkes på brettet. |
| Én action per runde i `loop` | Vent på acken før neste. `Device.action` gjør dette for deg. |
| Ack-timeout er 2 s | `action` returnerer `False` ved avvisning eller manglende svar. Sjekk den. |
| `play_pcm` holder låsen hele avspillingen | `action` venter til talen er ferdig. |
| PCM er ca. 3000 base64-tegn per action | `play_pcm` deler opp for deg. |
| Backend må si `hello` innen 60 s | Ellers slutter brettet å lytte. `App.run` sender det hvert 15. sekund. |
| Firmwaren kjenner bare sine egne actions | Andre svarer `ok:false`. |
| `æøå` i verdier til brettet går trolig i stykker | SDK-et sender `ø`, og parseren dropper backslash. Lest fra koden, ikke testet. Hold deg til ASCII til det er testet. |

### Actions til brettet

| Action | Verdi |
|---|---|
| `hello` | epoch, også heartbeat |
| `set_time` | epoch |
| `face` | `idle`, `listening`, `thinking`, `talking`, `sleep` |
| `led_mode` | `off`, `nightlight`, `listening`, `thinking`, `status`, `sunrise:N` |
| `set_alarm` | `HH:MM[/soloppgangsminutter]` eller `off` |
| `stop_alarm` | ingen |
| `ring_alarm` | ingen, ringer med en gang (demo) |
| `set_threshold` | mikrofonterskel |
| `bench_record` | sekunder, 1 til 10 |
| `listen_now` | ingen, åpner mikrofonen én gang i opptil 8 s, med kind `vad` |
| `game_mode` | `1` eller `0`. I spillmodus er knotten morsenøkkel, mikrofonen lytter ikke, og griden tegnes ikke |
| `morse` | `<unit_ms>:<mønster>`, for eksempel `150:.-. . -..`. Krever `game_mode 1` |
| `speaker_play_rtttl`, `speaker_set_volume`, `speaker_stop`, `speaker_play_pcm` | innebygd |
| `led_fill`, `led_pixel`, `led_clear` | innebygd |

### Events fra brettet

| Event | Verdi |
|---|---|
| `presence` | `enter`, bare når backend kobler til |
| `gesture` | `press`, `demo` |
| `listen` | `vad`, `ptt`, `tap`, `bench` |
| `audio_chunk` | `{seq, data}` |
| `audio_end` | `{samples, chunks, rate, reason, kind}` |
| `audio_cancel` | årsak |
| `alarm` | tilstand, f.eks. `stopped` |
| `edge` | `1:<millis>` ved trykk, `0:<millis>` ved slipp. Bare i spillmodus |
| `morse_done` | `1`, når blinkingen er ferdig |
| `diag` | diagnostikk |
| `aht20_temperature`, `aht20_humidity`, `mic_level`, `knob`, `knob_btn` | innebygd |

`kind` forteller om opptaket startet av seg selv (`vad`) eller med knotten (`ptt`, `tap`). Det avgjør om agenten får skrive.

## 3. Kontrakten for en plugin

Arv `Plugin` og overstyr bare det du trenger.

| Medlem | Bruk | Regler |
|---|---|---|
| `name` | navn i logg og kontekst | Kort og norsk, det kan bli lest høyt. |
| `tools` | Anthropic-verktøy | `strict: true`, alle felt `required`, unikt navn. Regler for modellen skrives i `description`. |
| `writes` | verktøy som endrer noe utenfor | Må alltid fylles ut riktig, se under. |
| `fast` | verktøy der svaret er ferdig i samme tur | Hopper over runde 2 med Claude, så teksten ved siden av verktøykallet blir stående. |
| `call(tool, args)` | utfører verktøyet | Arbeidertråden, kan ta noen sekunder. Returnerer JSON-tekst. Exception blir `Feil: ...` til modellen. |
| `context()` | én linje i `[Kontekst: ...]` | Kjører hver tur, så den må være rask eller cachet. Exception blir `<name>: utilgjengelig`. |
| `handle_event(key, value)` | se events fra brettet | Hovedløkka. Må aldri blokkere. `True` betyr at ingen andre ser eventet. Exception logges og regnes som `False`. |
| `after_turn(say)` | kjører etter hver jobb, når talen er ferdig | Arbeidertråden, kan ta lang tid, og andre jobber venter imens. `True` betyr at plugin-en snakket med brukeren, så mikrofonen åpnes én gang for svaret. |

### Skriveregelen

Et verktøy i `writes` kjører bare ved en direkte forespørsel: trykk på knotten eller tekstmodus. Alltid-på-mikrofonen og rutinene kan lese, men ikke skrive. Sperren ligger i `Assistant._exec_tool`, så du trenger ikke kode for den.

**Alt som endrer noe utenfor skal stå i `writes`.** Sperren vet ikke om verktøy du glemmer. Bakgrunnsprat og feilhørt tale kan ellers endre ekte data.

### Tekst utenfra er ikke til å stole på

Hvem som helst kan invitere brukeren til en avtale, så en avtaletittel er tekst fra fremmede. Det samme gjelder alt annet som kommer utenfra. Systemprompten sier at slik tekst aldri er instruksjoner, og skriveregelen begrenser skaden, men:

- Sett aldri ekstern tekst rett i en URL. Bruk `urllib.parse.quote`.
- Id-er fra modellen er også ekstern tekst.

### Feil

- Bruk `request_json` for HTTP. Den gir timeout på 3 s, ett nytt forsøk for GET ved 429 og 5xx, og `ExternalError` ved feil.
- Skrivinger prøves aldri på nytt, for en gjentatt POST kan lage duplikat. Timeouts prøves heller ikke på nytt, for da står stemmen stille.
- Svar aldri med tomme data når kilden er nede. Kast en feil, så sier agenten at den ikke vet.
- Stopp oppstarten med `SystemExit` hvis oppsettet er feil. Fall aldri tilbake til demodata som kan se ekte ut.

## 4. Eksempel: ny ekstern kilde

```python
class NotatKilde(Plugin):
    name = "notater"
    writes = {"post_note"}
    tools = [...]  # get_note og post_note, samme form som CalendarPlugin.tools

    def __init__(self, session) -> None:
        self.session = session

    def context(self) -> str:
        return f"Notat: {request_json(self.session, 'GET', URL)['tekst']}."

    def call(self, tool: str, args: dict) -> str:
        if tool == "post_note":
            request_json(self.session, "POST", URL, json={"tekst": args["tekst"]})
        return json.dumps({"ok": True})
```

Legg den til i `get_plugins`, og legg nøkler i `Config` og `.env.example`. Ta `session` som parameter, så kan selvtesten bruke `FakeSession` og teste uten nett. `CalendarPlugin` er det virkelige eksempelet.

## 5. Eksempel: nytt spill

Et spill starter med et verktøy og eier så input fra brettet til det er ferdig. Assistenten reagerer ikke på events spillet tar, så det trengs ingen egen modus.

```python
class MiniSpill(Plugin):
    name = "minispill"
    tools = [...]  # start_game
    EIER = {"knob_btn", "gesture", "listen", "audio_chunk", "audio_end", "audio_cancel"}

    def __init__(self, device) -> None:
        self.device, self.running, self.inbox = device, False, queue.Queue()

    def call(self, tool: str, args: dict) -> str:
        self.running = True
        threading.Thread(target=self._loop, daemon=True).start()
        return json.dumps({"ok": True})

    def handle_event(self, key: str, value) -> bool:
        if self.running and key in self.EIER:
            self.inbox.put((key, value))  # aldri arbeid her
            return True
        return False

    def _loop(self) -> None:
        try:
            ...  # les self.inbox, styr brettet med self.device.action
        finally:
            self.running = False
```

Bruk `get_plugins(cfg, device)` for å gi spillet `device`. Eget trådløp og en kø er samme mønster som `_pump` og `inbox` i `morse/trainer.py`.

Et spill som snakker bør heller kjøre i `after_turn`, slik `backend/morse_game.py` gjør. Da starter det først når agenten har snakket ferdig, og rutinene venter i køen til spillet er over.

### Dette må på plass før neste spill

`backend/morse_game.py` er det eneste spillet så langt. Disse tingene må løses for neste spill:

- **Skjermen trenger en ny action.** `st7735_tft` har ingen actions, og firmwaren tegner bare ansikt, klokke og temperatur. Et spill på skjermen krever en ny action og ny flashing.
- **Spillogikk hører trolig hjemme på brettet.** Hver action venter på en ack og hver linje er maks 4096 tegn, så å strømme bilder fra laptopen blir tregt. La brettet tegne, og send bare tilstand.
- **Timing hører hjemme i firmwaren.** Morse stempler `edge` på brettet fordi jitter i USB og Python ellers ødelegger lengden på prikker og streker.
- **Tale blokkerer brettet.** Agenten snakker ferdig etter at `start_game` er kalt, og `action` venter så lenge.

## 6. Test

```sh
cd nattbord && ../.venv/bin/python -m backend.selftest
```

Selvtesten kjører uten brett, Claude og Google. `FakeSource` og `FakeGame` følger skjelettene over, så legg til en test når du legger til en plugin.
