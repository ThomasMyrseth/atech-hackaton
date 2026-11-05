"""Chirp sonar test for the Atech board: speaker on ports 1-2, mic on ports 13-14.

    python sonar.py             live distance bar (flash sonar_fw first)
    python sonar.py --selftest  check the signal processing, no hardware
"""
import base64
import sys
import time

import matplotlib.pyplot as plt
import numpy as np
from atech import Board

# Chirp constants must match project.yaml.
FS = 16000
F0, F1 = 1000.0, 7000.0
CHIRP_LEN = 160
REC_LEN = 2000
CHUNKS = 10

C = 343.0  # speed of sound, m/s at about 20 C
VOLUME = 0.5
SPACING_M = 0.10  # speaker-to-mic distance: measure it on the board
BLIND_M = 0.25  # echoes closer than this are masked by the direct sound
MAX_RANGE_M = 4.0
PINGS = 5
GAP_S = 0.15  # lets the room go quiet between pings
CLIP = 32000

# Chart colors: dataviz reference palette, light mode.
SURFACE, INK, MUTED, RULE = "#fcfcfb", "#0b0b0b", "#52514e", "#e6e5e1"
BAR_COLOR = "#2a78d6"


def make_chirp():
    t = np.arange(CHIRP_LEN) / FS
    phase = 2 * np.pi * (F0 * t + (F1 - F0) * t**2 / (2 * CHIRP_LEN / FS))
    return np.hanning(CHIRP_LEN) * np.exp(1j * phase)


REF = make_chirp()
DIST = (C * np.arange(int(np.ceil((2 * MAX_RANGE_M - SPACING_M) / C * FS))) / FS + SPACING_M) / 2


def ping(board):
    """Fire one chirp. Returns the recording, or None if chunks were lost."""
    board.call("ping")
    chunks = []
    deadline = time.monotonic() + 1.5
    # mic_level arrives every 250 ms, so the deadline still fires if chunks stop.
    for event in board.events(keys=["sonar", "mic_level"]):
        if event.key == "sonar":
            seq, data = event.value.split(":", 1)
            if int(seq) == 0:
                chunks = []
            if int(seq) == len(chunks):  # skips a stale tail, and a gap never completes
                chunks.append(data)
            if len(chunks) == CHUNKS:
                break
        if time.monotonic() > deadline:
            return None
    time.sleep(GAP_S)
    rec = np.frombuffer(b"".join(base64.b64decode(c) for c in chunks), dtype="<i2").astype(float)
    if np.abs(rec).max() >= CLIP:
        print("warning: mic clipped, lower VOLUME")
    return rec


def compress(rec):
    """Matched filter. Index k is a chirp starting at sample k; magnitude is the envelope."""
    return np.correlate(rec - rec.mean(), REF, mode="valid")


def align(comp):
    """Cut from the direct sound and scale to it, or None if the window is too short."""
    t0 = int(np.argmax(np.abs(comp)))
    if t0 + len(DIST) > len(comp):
        return None
    return comp[t0 : t0 + len(DIST)] / comp[t0]


def averaged(board, count):
    """Average count pings. Chirp start jitters by DMA slot, so align each one first."""
    good = []
    for _ in range(count * 2 + 5):
        rec = ping(board)
        trace = None if rec is None else align(compress(rec))
        if trace is not None:
            good.append(trace)
        if len(good) == count:
            return np.mean(good, axis=0)
    raise RuntimeError("too many failed pings: check the board is flashed with sonar_fw")


def to_db(trace):
    return 20 * np.log10(np.abs(trace) + 1e-6)


def peak(trace):
    """Strongest echo beyond the blind zone, as (distance in m, dB)."""
    start = np.searchsorted(DIST, BLIND_M)
    i = start + int(np.argmax(np.abs(trace[start:])))
    return DIST[i], to_db(trace)[i]


def make_plot():
    plt.rcParams.update({
        "figure.facecolor": SURFACE, "axes.facecolor": SURFACE,
        "text.color": INK, "axes.labelcolor": MUTED, "ytick.color": MUTED,
        "axes.edgecolor": RULE, "grid.color": RULE, "grid.linewidth": 0.8,
    })
    plt.ion()
    fig, ax = plt.subplots(figsize=(3.5, 6), layout="constrained")
    ax.spines[["top", "right"]].set_visible(False)
    ax.grid(True, axis="y")
    ax.set_axisbelow(True)
    ax.set_xlim(-0.5, 0.5)
    ax.set_xticks([])
    ax.set_ylim(0, MAX_RANGE_M + 0.4)  # room for the label on a full bar
    ax.set_title("Distance now", loc="left")
    ax.set_ylabel("Distance (m)")
    ax.axhspan(0, BLIND_M, color=RULE, alpha=0.6, linewidth=0)
    ax.text(-0.46, BLIND_M / 2, "blind", va="center", color=MUTED, fontsize=9)
    bar = ax.bar([0], [0], width=0.4, color=BAR_COLOR)[0]
    label = ax.text(0, 0, "", ha="center", va="bottom", fontsize=16)
    return fig, bar, label


def run():
    with Board.connect() as board:
        board.call("spk_set_volume", VOLUME)
        print("Close the plot window to stop.")
        fig, bar, label = make_plot()
        while plt.fignum_exists(fig.number):
            distance, db = peak(averaged(board, PINGS))
            bar.set_height(distance)
            label.set_position((0, distance + 0.05))
            label.set_text(f"{distance:.2f} m")
            print(f"{distance:.2f} m  {db:.0f} dB")
            plt.pause(0.01)


def selftest():
    """Synthetic recordings with echoes at known distances, no hardware."""
    wave = 30000 * REF.imag
    rng = np.random.default_rng(0)

    def fake(echoes, start):
        rec = rng.normal(0, 3, REC_LEN)
        rec[start : start + CHIRP_LEN] += wave
        for distance, gain in echoes:
            at = start + round((2 * distance - SPACING_M) / C * FS)
            rec[at : at + CHIRP_LEN] += gain * wave
        return rec

    wall = (1.50, 0.01)
    # Different start samples: the chirp start jitters, the distance must not.
    for start in (300, 420):
        trace = align(compress(fake([wall], start)))
        assert abs(peak(trace)[0] - wall[0]) < 0.03, peak(trace)
    print("selftest ok")


if __name__ == "__main__":
    selftest() if "--selftest" in sys.argv else run()
