"""Morse encode and decode.

    python morse.py --selftest  check it, no hardware
"""
import random
import statistics
import sys

CODE = {
    "A": ".-", "B": "-...", "C": "-.-.", "D": "-..", "E": ".", "F": "..-.", "G": "--.",
    "H": "....", "I": "..", "J": ".---", "K": "-.-", "L": ".-..", "M": "--", "N": "-.",
    "O": "---", "P": ".--.", "Q": "--.-", "R": ".-.", "S": "...", "T": "-", "U": "..-",
    "V": "...-", "W": ".--", "X": "-..-", "Y": "-.--", "Z": "--..",
    "0": "-----", "1": ".----", "2": "..---", "3": "...--", "4": "....-",
    "5": ".....", "6": "-....", "7": "--...", "8": "---..", "9": "----.",
    ".": ".-.-.-", ",": "--..--", "?": "..--..", "/": "-..-.", "=": "-...-",
}
LETTERS = {code: letter for letter, code in CODE.items()}

ASSUMED_WPM = 12
BOUNCE_MS = 15  # wasPressed() has no software debounce, and no dit is this short


def unit_ms(wpm):
    return round(1200 / wpm)


def encode(text):
    """'sos hi' gives '... --- ... / .... ..'. ValueError on a character morse lacks."""
    try:
        return " / ".join(" ".join(CODE[c] for c in word) for word in text.upper().split())
    except KeyError as missing:
        raise ValueError(f"no morse code for {missing}") from None


def decode(edges):
    """Turn (down_ms, up_ms) presses into (text, pattern, wpm). Unknown letters become ?."""
    edges = [(down, up) for down, up in edges if up - down >= BOUNCE_MS]
    if not edges:
        return "", "", 0

    presses = [up - down for down, up in edges]
    low, high = min(presses), max(presses)
    # shortcut: one symbol type has no second cluster, so it uses ASSUMED_WPM. A fast
    # single-letter answer can misread: pass the session's speed if that shows up.
    split = (low + high) / 2 if high >= 2 * low else 2 * unit_ms(ASSUMED_WPM)
    dits = [press for press in presses if press < split]
    unit = statistics.median(dits) if dits else statistics.median(presses) / 3

    pattern = ""
    for i, (down, up) in enumerate(edges):
        gap = down - edges[i - 1][1] if i else 0
        if gap >= 5 * unit:
            pattern += " / "
        elif gap >= 2 * unit:
            pattern += " "
        pattern += "." if up - down < split else "-"

    text = " ".join(
        "".join(LETTERS.get(letter, "?") for letter in word.split())
        for word in pattern.split(" / ")
    )
    return text, pattern, round(1200 / unit)


def key(text, wpm, jitter, rng):
    """Press edges for text, keyed by a human with +-jitter timing spread."""
    unit = unit_ms(wpm)
    spread = lambda units: units * unit * (1 + rng.uniform(-jitter, jitter))
    now, gap, edges = 0, 0, []
    for symbol in encode(text):
        if symbol == " ":
            gap = max(gap, 3)
        elif symbol == "/":
            gap = 7
        else:
            down = now + spread(gap)
            now = down + spread(1 if symbol == "." else 3)
            edges.append((down, now))
            gap = 1
    return edges


def selftest():
    assert unit_ms(12) == 100
    assert encode("sos hi") == "... --- ... / .... .."
    try:
        encode("ø")
    except ValueError:
        pass
    else:
        raise AssertionError("encode must reject a character with no morse code")

    rng = random.Random(0)
    message = "CQ DE THOMAS 73"
    for wpm in (8, 12, 20):
        text, pattern, speed = decode(key(message, wpm, 0.2, rng))
        assert text == message, (wpm, text)
        assert pattern == encode(message), (wpm, pattern)
        assert abs(speed - wpm) <= 0.2 * wpm + 1, (wpm, speed)

    # One symbol type gives no second cluster, so it falls back to the assumed speed.
    assert decode(key("EE", 12, 0, rng))[0] == "EE"
    assert decode(key("TT", 12, 0, rng))[0] == "TT"

    # Six dits are not a letter.
    assert decode([(i * 200, i * 200 + 100) for i in range(6)])[0] == "?"

    # A contact bounce is a press far shorter than any dit.
    edges = key("A", 12, 0, rng)
    edges.insert(1, (edges[0][1] + 20, edges[0][1] + 25))
    assert decode(edges)[0] == "A"

    assert decode([]) == ("", "", 0)
    print("selftest ok")


if __name__ == "__main__":
    selftest() if "--selftest" in sys.argv else print(__doc__)
