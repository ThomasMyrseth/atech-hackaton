"""M1: mål lyd gjennom USB. Opp: 5 s opptak fra brettets mik. Ned: ~5 s tale til høyttaleren.

    cd nattbord && ../.venv/bin/python -m backend.bench_audio [--secs 5] [--stt]
"""
from __future__ import annotations

import argparse
import time

import numpy as np

from backend import tts
from backend.audio_in import ChunkAssembler, rms, save_wav
from backend.config import DATA_DIR
from backend.device import BoardDevice
from backend.log import console, log


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--secs", type=int, default=5)
    p.add_argument("--stt", action="store_true", help="transkriber opptaket også")
    p.add_argument("--no-down", action="store_true")
    args = p.parse_args()

    dev = BoardDevice()
    dev.action("hello", int(time.time()))

    # ------------------------------------------------------------- opp
    asm = ChunkAssembler()
    t_cmd = time.monotonic()
    dev.action("bench_record", args.secs)
    t_first = None
    end_info = None
    n = 0
    deadline = time.monotonic() + args.secs + 10
    while time.monotonic() < deadline:
        ev = dev.recv(0.2)
        if ev is None:
            continue
        if ev.key == "listen":
            asm.start(str(ev.value))
        elif ev.key == "audio_chunk":
            if t_first is None:
                t_first = time.monotonic()
            asm.add(ev.value)
            n += 1
        elif ev.key == "audio_end":
            end_info = ev.value
            break
    t_end = time.monotonic()
    if end_info is None:
        log("warn", f"fikk aldri audio_end (fikk {n} biter)")
        return
    pcm = asm.finish(end_info)
    secs = len(pcm) / 16000
    total = t_end - t_cmd
    console.rule("[bold]M1 · lyd OPP (brett → laptop)")
    console.print(f"  opptak:            {secs:.2f} s ({len(pcm)} samples, {end_info.get('chunks')} biter)")
    console.print(f"  første bit etter:  {(t_first - t_cmd) * 1000:.0f} ms")
    console.print(f"  totalt:            {total:.2f} s  →  forsinkelse etter opptak ≈ {(total - args.secs) * 1000:.0f} ms")
    console.print(f"  rms / topp:        {rms(pcm):.0f} / {int(np.max(np.abs(pcm))) if len(pcm) else 0}")
    save_wav(DATA_DIR / "bench_up.wav", pcm)
    console.print(f"  lagret:            {DATA_DIR / 'bench_up.wav'}  (afplay for å høre)")
    if args.stt:
        from backend.stt import STT
        STT().transcribe(pcm)

    # ------------------------------------------------------------- ned
    if args.no_down:
        return
    text = ("Hei! Dette er en test av høyttaleren. I morgen har du forelesning klokka kvart over åtte, "
            "og lunsj med Ingrid klokka tolv.")
    down = tts.synthesize(text, "Nora", 190)
    dur = len(down) / 32000
    dev.action("face", "talking")
    wall = dev.play_pcm(down)
    dev.action("face", "idle")
    console.rule("[bold]M1 · lyd NED (laptop → høyttaler)")
    console.print(f"  lydlengde:   {dur:.2f} s")
    console.print(f"  sendetid:    {wall:.2f} s  →  faktor {wall / dur:.2f} (≈1,0 = sanntid, >1,1 = hakking)")
    console.print("  Lytt: var det hakk eller klikk mellom bitene?")
    dev.close()


if __name__ == "__main__":
    main()
