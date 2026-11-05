"""M1: measure audio over USB. Up: 5 s recording from the board's mic. Down: ~5 s speech to the speaker.

    cd nattbord && ../.venv/bin/python -m backend.bench_audio [--secs 5] [--stt]
"""
from __future__ import annotations

import argparse
import time

import numpy as np

from backend import tts
from backend.audio_in import ChunkAssembler, rms, save_wav
from backend.config import DATA_DIR, load_config
from backend.device import BoardDevice
from backend.log import console, log


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--secs", type=int, default=5)
    p.add_argument("--stt", action="store_true", help="transcribe the recording too")
    p.add_argument("--no-down", action="store_true")
    args = p.parse_args()
    cfg = load_config()  # puts the ElevenLabs key in the environment

    dev = BoardDevice()
    dev.action("hello", int(time.time()))

    # ------------------------------------------------------------- up
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
        log("warn", f"never got audio_end (got {n} chunks)")
        return
    pcm = asm.finish(end_info)
    secs = len(pcm) / 16000
    total = t_end - t_cmd
    console.rule("[bold]M1 · audio UP (board → laptop)")
    console.print(f"  recording:         {secs:.2f} s ({len(pcm)} samples, {end_info.get('chunks')} chunks)")
    console.print(f"  first chunk after: {(t_first - t_cmd) * 1000:.0f} ms")
    console.print(f"  total:             {total:.2f} s  →  delay after recording ≈ {(total - args.secs) * 1000:.0f} ms")
    console.print(f"  rms / peak:        {rms(pcm):.0f} / {int(np.max(np.abs(pcm))) if len(pcm) else 0}")
    save_wav(DATA_DIR / "bench_up.wav", pcm)
    console.print(f"  saved:             {DATA_DIR / 'bench_up.wav'}  (afplay to listen)")
    if args.stt:
        from backend.stt import STT
        STT().transcribe(pcm)

    # ------------------------------------------------------------- down
    if args.no_down:
        return
    text = ("Hi! This is a test of the speaker. Tomorrow you have a lecture at quarter past eight, "
            "and lunch with Ingrid at twelve.")
    down = tts.synthesize(text, cfg.tts_voice, 190)
    dur = len(down) / 32000
    dev.action("face", "talking")
    wall = dev.play_pcm(down)
    dev.action("face", "idle")
    console.rule("[bold]M1 · audio DOWN (laptop → speaker)")
    console.print(f"  audio length: {dur:.2f} s")
    console.print(f"  send time:    {wall:.2f} s  →  factor {wall / dur:.2f} (≈1.0 = real time, >1.1 = stutter)")
    console.print("  Listen: were there stutters or clicks between the chunks?")
    dev.close()


if __name__ == "__main__":
    main()
