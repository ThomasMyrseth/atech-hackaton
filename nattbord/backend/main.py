"""Nattbordsassistenten: stemmeløkke mot brettet (eller laptop-reserve).

    cd nattbord && ../.venv/bin/python -m backend.main                 # brett på USB
    ../.venv/bin/python -m backend.main --no-board                     # bare laptop (mik + høyttaler)
    AUDIO_IN=laptop AUDIO_OUT=laptop ../.venv/bin/python -m backend.main   # brett for sensorer, lyd på laptop
"""
from __future__ import annotations

import argparse
import os
import queue
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from backend import tts
from backend.agent import Assistant
from backend.audio_in import ChunkAssembler, LaptopListener, rms, save_wav
from backend.config import DATA_DIR, load_config
from backend.device import BoardDevice, LogDevice
from backend.log import console, log
from backend.plugin import get_plugins, route_event
from backend.room import Room

IGNORE_KEYS = {"mic_level", "dist_min_distance", "btn"}


class App:
    def __init__(self, args):
        self.cfg = load_config()
        self.args = args
        self.tz = ZoneInfo(getattr(self.cfg, "tz", "Europe/Oslo"))
        if args.no_board:
            self.device = LogDevice()
            self.room = Room.fake()
        else:
            self.device = BoardDevice(port=os.getenv("SERIAL_PORT") or None)
            self.room = Room()
        self.audio_in = "laptop" if args.no_board else self.cfg.audio_in
        self.audio_out = "laptop" if args.no_board else self.cfg.audio_out
        self.plugins = get_plugins(self.cfg, self.device)
        self.assistant = Assistant(self.device, self.room, self.plugins, self.cfg)
        from backend.stt import STT  # tar noen sekunder (laster modellen)
        self.stt = STT()
        self.assembler = ChunkAssembler()
        self.present = args.no_board
        self.speaking = False
        self.absent_since = time.monotonic() - 3600
        self.evening_done_day = None
        self.jobs: "queue.Queue[tuple]" = queue.Queue()
        self.laptop = None
        if self.audio_in == "laptop":
            self.laptop = LaptopListener(
                threshold=float(os.getenv("LAPTOP_THRESHOLD", "600")),
                enabled=lambda: self.present and not self.speaking,
            )

    # ---------------------------------------------------------------- tale ut
    def say(self, text: str) -> None:
        text = tts.clean_for_speech(text)
        if not text:
            self.device.action("face", "idle")
            return
        t0 = time.monotonic()
        pcm = tts.synthesize(text, self.cfg.tts_voice, self.cfg.tts_rate)
        t_tts = time.monotonic() - t0
        dur = len(pcm) / 32000
        self.speaking = True
        try:
            self.device.action("face", "talking")
            if self.audio_out == "device" and self.device.is_board:
                wall = self.device.play_pcm(tts.prepare_for_speaker(pcm))
                log("tts", f"{dur:.1f} s tale på brettet, sendt på {wall:.1f} s (say {t_tts:.2f} s)")
            else:
                log("tts", f"{dur:.1f} s tale på laptop (say {t_tts:.2f} s)")
                tts.play_laptop(pcm)
        finally:
            self.device.action("face", "idle")
            time.sleep(0.3)
            self.speaking = False

    # ------------------------------------------------------------- en ytring
    def handle_utterance(self, pcm, t_end: float, source: str) -> None:
        dur = len(pcm) / 16000
        log("audio", f"{source}: {dur:.1f} s lyd, rms {rms(pcm):.0f}")
        if self.args.save_audio:
            save_wav(DATA_DIR / "last_utterance.wav", pcm)
        self.device.action("face", "thinking")
        text = self.stt.transcribe(pcm)
        if not text:
            self.device.action("face", "idle")
            return
        t1 = time.monotonic()
        reply = self.assistant.respond(text, ambient=(source in ("vad", "laptop")))
        t2 = time.monotonic()
        if not reply:
            self.device.action("face", "idle")
            return
        console.print(f"[bold green]   🤖 {reply}[/bold green]")
        self.say(reply)
        log("time", f"slutt på tale → svar starter ≈ {t2 - t_end + 0.3:.1f} s "
                    f"(STT+agent {t2 - t_end:.1f} s, agent {t2 - t1:.1f} s)")

    def evening(self, why: str) -> None:
        log("routine", f"kveldsrutine ({why})")
        self.device.action("face", "thinking")
        text = self.assistant.evening_routine()
        console.print(f"[bold magenta]   🌙 {text}[/bold magenta]")
        self.say(text)
        self.evening_done_day = datetime.now(self.tz).date()

    # ---------------------------------------------------------------- events
    def on_event(self, ev) -> None:
        key, val = ev.key, ev.value
        if route_event(self.plugins, key, val):  # før IGNORE_KEYS: spill trenger btn og mic_level
            return
        if key in IGNORE_KEYS:
            return
        if self.room.update(key, val):
            return
        if key == "presence":
            if (val == "enter") == self.present:
                return  # ingen endring (f.eks. synk etter hello)
            log("presence", f"{'👋 til stede' if val == 'enter' else 'borte'}")
            if val == "enter":
                away = time.monotonic() - self.absent_since
                self.present = True
                if away > 120:
                    self.assistant.reset()
                    if self.args.greet:
                        self.jobs.put(("greet",))
            else:
                self.present = False
                self.absent_since = time.monotonic()
        elif key == "listen":
            log("audio", f"🎙  lytter ({val})")
            self.assembler.start(str(val))
        elif key == "audio_chunk":
            self.assembler.add(val)
        elif key == "audio_cancel":
            log("audio", f"[dim]opptak forkastet ({val})[/dim]")
            self.assembler.reset()
        elif key == "audio_end":
            kind = (val.get("kind") if isinstance(val, dict) else None) or self.assembler.kind or "vad"
            pcm = self.assembler.finish(val if isinstance(val, dict) else None)
            log("audio", f"audio_end ({kind}) {val}")
            if self.speaking or not self.jobs.empty():
                log("audio", "[dim]opptatt, forkaster opptaket[/dim]")
            else:
                self.jobs.put(("utterance", pcm, time.monotonic(), kind))
        elif key == "gesture":
            log("button", f"knapp: {val}")
            if val == "demo":
                self.jobs.put(("evening", "demo-knapp"))
            elif val == "press" and self.laptop:
                self.laptop.force.set()
        elif key == "alarm":
            log("routine", f"⏰ alarm: {val}")
            if val == "stopped":
                self.jobs.put(("morning",))
        elif key == "diag":
            if self.args.diag:
                log("board", f"[dim]diag {val}[/dim]")
        else:
            log("board", f"[dim]{ev.type}/{key} = {str(val)[:80]}[/dim]")

    def check_schedule(self) -> None:
        now = datetime.now(self.tz)
        try:
            hh, mm = map(int, self.cfg.evening_time.split(":"))
        except ValueError:
            return
        if (self.present and self.evening_done_day != now.date()
                and (now.hour, now.minute) >= (hh, mm)):
            self.jobs.put(("evening", f"planlagt {self.cfg.evening_time}"))
            self.evening_done_day = now.date()

    # ------------------------------------------------------------------ løkke
    def worker(self) -> None:
        """Tunge jobber (STT, Claude, tale) i én tråd, så event-lesingen aldri stopper."""
        while True:
            job = self.jobs.get()
            try:
                if job[0] == "utterance":
                    self.handle_utterance(job[1], job[2], job[3])
                elif job[0] == "evening":
                    self.evening(job[1])
                elif job[0] == "morning":
                    self.device.action("face", "thinking")
                    text = self.assistant.morning_routine()
                    console.print(f"[bold yellow]   ☀️  {text}[/bold yellow]")
                    self.say(text)
                elif job[0] == "greet":
                    hour = datetime.now(self.tz).hour
                    hils = "God morgen" if hour < 10 else "Hei" if hour < 18 else "God kveld"
                    self.say(f"{hils}! Si ifra hvis du lurer på noe.")
            except Exception as e:  # noqa: BLE001 – demoen skal overleve alt
                log("warn", f"jobb {job[0]} feilet: {e!r}")
                self.device.action("face", "idle")

    def run(self) -> None:
        epoch = int(time.time())
        self.device.action("hello", epoch)
        thr = self.cfg.mic_threshold if self.audio_in == "device" else 32767
        self.device.action("set_threshold", thr)
        self.device.action("speaker_set_volume", float(os.getenv("SPEAKER_VOLUME", "0.85")))
        self.device.action("face", "idle")
        threading.Thread(target=self.worker, daemon=True).start()
        if self.laptop:
            self.laptop.start()
        engine = "elevenlabs" if os.getenv("ELEVENLABS_API_KEY") and os.getenv("TTS_ENGINE", "elevenlabs") != "say" else "say"
        log("board", f"klar · lyd inn={self.audio_in} ut={self.audio_out} · tale={engine} · kalender={self.cfg.calendar}")
        last_sched = 0.0
        while True:
            ev = self.device.recv(0.1)
            if ev is not None:
                self.on_event(ev)
            if self.laptop:
                try:
                    pcm, forced = self.laptop.utterances.get_nowait()
                    self.jobs.put(("utterance", pcm, time.monotonic(), "tap" if forced else "laptop"))
                except queue.Empty:
                    pass
            if time.monotonic() - last_sched > 15:
                last_sched = time.monotonic()
                self.check_schedule()
                if not self.speaking:  # heartbeat: brettet strømmer bare lyd når vi lever
                    self.device.action("hello", int(time.time()))


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--no-board", action="store_true", help="kjør uten brett (laptop-mik og høyttaler)")
    p.add_argument("--greet", action="store_true", help="hils når noen kommer etter >2 min borte")
    p.add_argument("--save-audio", action="store_true", help="lagre siste ytring i data/")
    p.add_argument("--diag", action="store_true", help="vis avstand og mikrofonnivå hvert 2. s (kalibrering)")
    app = App(p.parse_args())
    try:
        app.run()
    except KeyboardInterrupt:
        log("board", "avslutter")
    finally:
        app.device.close()


if __name__ == "__main__":
    main()
