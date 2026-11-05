"""Brettet over USB (Atech SDK), pluss en logg-enhet for tekstmodus.

Alle enheter har samme grensesnitt:
    action(key, value="") -> bool      # sender én action, venter på ack
    play_pcm(pcm: bytes) -> float      # spiller 16 kHz mono s16le, returnerer sekunder brukt
    recv(timeout) -> Event | None      # neste event fra brettet
"""
from __future__ import annotations

import base64
import threading
import time
from typing import Optional

from backend.log import log

# Actions som ikke skal spamme loggen
_QUIET = {"speaker_play_pcm", "hello"}


def _short(v) -> str:
    s = str(v)
    return s if len(s) <= 60 else s[:57] + "..."


class LogDevice:
    """Ingen hardware: logger bare hva som ville blitt sendt."""

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
    """Atech-brettet på USB-seriell (JSON-linjer, én ack per action)."""

    is_board = True

    def __init__(self, port: Optional[str] = None):
        from atech import Board  # importeres her så tekstmodus ikke trenger pyserial

        self.board = Board.connect(port=port)
        self._lock = threading.Lock()
        log("board", f"koblet til brettet ({port or 'auto'})")

    def action(self, key: str, value="", timeout: float = 2.0) -> bool:
        from atech import ActionRejected, NoAcknowledgement

        with self._lock:
            try:
                self.board.call(key, value, timeout=timeout)
                if key not in _QUIET:
                    log("action", f"{key} {_short(value)}")
                return True
            except ActionRejected as e:
                log("warn", f"{key} avvist: {e}")
            except NoAcknowledgement:
                log("warn", f"{key}: ingen ack fra brettet")
            except Exception as e:  # noqa: BLE001 – demo skal aldri krasje
                log("warn", f"{key}: {e}")
            return False

    def play_pcm(self, pcm: bytes, chunk_bytes: int = 2048) -> float:
        """Send PCM som speaker_play_pcm-biter. Hver bit blokkerer på brettet til den er
        skrevet til I2S-DMA, så vi sender neste så snart acken kommer."""
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
                    log("warn", "play_pcm: ingen ack, avbryter avspilling")
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
