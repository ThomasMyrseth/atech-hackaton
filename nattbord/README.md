# Nattbordsassistent

En nattbordsassistent for studenter på hybel: et Atech ESP32-S3-brett med høyttaler, mikrofon, skjerm, temp/fukt, avstand, LED-grid og knapp. Den snakker norsk om kalenderen din, følger med på romklimaet, har nattlys og vekking med soloppgang.

```
brett (firmware/project.yaml) ⇄ USB-seriell (JSON-linjer) ⇄ backend/ på laptop ⇄ Claude
```

Atech-SDK-et snakker med brettet **bare over USB-seriell**. Wi-Fi-gatewayen brukes derfor ikke, og brettet står koblet til laptopen.

## Porter (14-port-brett)

| Port | Modul |
|---|---|
| 1–2 | Høyttaler |
| 3–4 | Mikrofon |
| 5–6 | Skjerm 160x80 |
| 7 | Temp/fukt (AHT20) |
| 9 | Avstand (VL53L5CX) |
| 10 | LED-grid 3x3 |
| 11 | Knapp |

## Oppsett (én gang)

```sh
uv venv --python 3.12 .venv            # i repo-roten
VIRTUAL_ENV=.venv uv pip install -r nattbord/requirements.txt
cp nattbord/.env.example nattbord/.env # legg inn ANTHROPIC_API_KEY og ELEVENLABS_API_KEY
```

### Google Kalender (valgfritt, ca. 5 minutter)

Uten dette brukes en lokal demokalender.

1. Lag et prosjekt i [Google Cloud Console](https://console.cloud.google.com/) og slå på Google Calendar API.
2. Lag en service account, legg til en JSON-nøkkel, og lagre den som `nattbord/service-account.json`. Filen er i `.gitignore`.
3. I innstillingene i Google Kalender: del kalenderen med e-postadressen til service accounten, og velg «Gjøre endringer i aktiviteter».
4. Sett `CALENDAR=google` og `GOOGLE_CALENDAR_ID=<gmail-adressen din>` i `nattbord/.env`.
5. Sjekk oppsettet: `../.venv/bin/python -m backend.calendar_backend --check` (fra `nattbord/`). Den skriver ingenting til kalenderen.

Assistenten kan lese, legge inn, flytte og slette. Gjentakende avtaler endres bare for én gang, og deltakere varsles ikke.

## Kjøring (fra `nattbord/`)

```sh
../.venv/bin/atech upload firmware             # flash brettet (USB-C datakabel)
../.venv/bin/atech check                       # hvilke moduler svarte
./run.sh                                       # full stemmeløkke mot brettet (samme som linja under)
../.venv/bin/python -m backend.main            # full stemmeløkke mot brettet
../.venv/bin/python -m backend.main --no-board # bare laptop (mik + høyttaler)
../.venv/bin/python -m backend.textmode        # skriv i stedet for å snakke
../.venv/bin/python -m backend.bench_audio     # M1: mål lyd opp og ned
../.venv/bin/python -m backend.selftest        # test uten brett, Claude og Google
```

Reserve: sett `AUDIO_IN=laptop` og/eller `AUDIO_OUT=laptop` i `.env`.

## Bruk

- **Gå bort til brettet.** Avstandssensoren merker deg (under ca. 500 mm), og da lytter den når du snakker.
- **Kort trykk** starter lytting med en gang, eller stopper alarmen.
- **Hold knappen** for å snakke. **Stille hold over 3 s** starter kveldsrutinen (demo).
- **Tale sendes til ElevenLabs.** Alt mikrofonen tar opp, også bakgrunnsprat nær brettet, forlater laptopen for å bli gjort om til tekst.
- **Endre kalenderen krever knapp.** Det assistenten fanger opp av seg selv kan lese, men ikke legge inn, flytte eller slette.
- **Alarm og soloppgang** går lokalt på brettet, også uten backend. Klokka settes fra laptopen ved oppstart.

## Utvide assistenten

Nye eksterne kilder og spill legges til som plugins. Se [docs/INTERFACE.md](docs/INTERFACE.md), som også har den fullstendige protokollen og grensene mot brettet.

## Protokoll (egendefinert)

**Events fra brettet:**
- `presence` (enter/leave)
- `gesture` (press/demo)
- `listen`
- `audio_chunk` {seq, data}
- `audio_end` {samples, chunks, reason}
- `audio_cancel`
- `alarm`

I tillegg kommer de innebygde `aht20_temperature`, `aht20_humidity`, `dist_min_distance`, `mic_level` og `btn`.

**Actions til brettet:**
- `hello` (epoch)
- `set_time` (epoch)
- `face` (idle|listening|thinking|talking|sleep)
- `led_mode` (off|nightlight|listening|thinking|status|sunrise:N)
- `set_alarm` (HH:MM[/soloppgangsminutter] | off)
- `stop_alarm`
- `set_threshold`
- `set_presence`
- `dist_grid`
- `bench_record`

I tillegg kommer de innebygde `speaker_play_pcm` og `led_fill`.
