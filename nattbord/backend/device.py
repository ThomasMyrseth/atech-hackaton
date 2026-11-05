"""The board over USB (Atech SDK), plus a log device for text mode. Device is the only path to the board."""
from __future__ import annotations

import base64
import threading
import time
from typing import Optional, Protocol

from backend.log import log

# Actions that should not spam the log
_QUIET = {"speaker_play_pcm", "hello"}


class Device(Protocol):
    """The boundary around the board. The limits on what can be sent are in docs/INTERFACE.md.

    play_pcm holds the lock for the whole playback, so action waits until the speech is done.
    """

    is_board: bool

    def action(self, key: str, value="") -> bool:
        """Sends one action and waits for the ack. False if the board rejected it or did not answer."""

    def play_pcm(self, pcm: bytes) -> float:
        """Plays 16 kHz mono s16le and returns the seconds used."""

    def recv(self, timeout: float = 0.2):
        """Next event from the board, or None. Only the main loop may call this."""

    def close(self) -> None:
        """Releases the port."""


def _short(v) -> str:
    s = str(v)
    return s if len(s) <= 60 else s[:57] + "..."


class LogDevice:
    """No hardware: only logs what would have been sent."""

    is_board = False

    def action(self, key: str, value="") -> bool:
        if key not in _QUIET:
            log("action", f"(sim) {key} {_short(value)}")
        return True

    def play_pcm(self, pcm: bytes) -> float:
        return 0.0

    def recv(self, timeout: float = 0.2):
        time.sleep(timeout)
        return None

    def close(self) -> None:
        pass


class BoardDevice:
    """The Atech board on USB serial (JSON lines, one ack per action)."""

    is_board = True

    def __init__(self, port: Optional[str] = None):
        from atech import Board  # imported here so text mode does not need pyserial

        self.board = Board.connect(port=port)
        self._lock = threading.Lock()
        log("board", f"connected to the board ({port or 'auto'})")

    def action(self, key: str, value="", timeout: float = 2.0) -> bool:
        from atech import ActionRejected, NoAcknowledgement

        with self._lock:
            try:
                self.board.call(key, value, timeout=timeout)
                if key not in _QUIET:
                    log("action", f"{key} {_short(value)}")
                return True
            except ActionRejected as e:
                log("warn", f"{key} rejected: {e}")
            except NoAcknowledgement:
                log("warn", f"{key}: no ack from the board")
            except Exception as e:  # noqa: BLE001 – the demo must never crash
                log("warn", f"{key}: {e}")
            return False

    def play_pcm(self, pcm: bytes, chunk_bytes: int = 2048) -> float:
        """Sends PCM as speaker_play_pcm chunks. Each chunk blocks on the board until it is
        written to I2S DMA, so we send the next as soon as the ack arrives."""
        from atech import NoAcknowledgement

        t0 = time.monotonic()
        with self._lock:
            for i in range(0, len(pcm) - 1, chunk_bytes):
                part = pcm[i : i + chunk_bytes]
                if len(part) % 2:
                    part = part[:-1]
                b64 = base64.b64encode(part).decode("ascii")
                try:
                    self.board.call("speaker_play_pcm", b64, timeout=2.0)
                except NoAcknowledgement:
                    log("warn", "play_pcm: no ack, aborting playback")
                    break
                except Exception as e:  # noqa: BLE001
                    log("warn", f"play_pcm: {e}")
                    break
        return time.monotonic() - t0

    def recv(self, timeout: float = 0.2):
        return self.board.transport.recv(timeout=timeout)

    def close(self) -> None:
        try:
            self.board.close()
        except Exception:  # noqa: BLE001
            pass
