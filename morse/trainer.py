"""Voice morse trainer: Claude talks through the Atech board and blinks morse at you.

    python trainer.py             run it (flash fw first, keys in the repo-root .env)
    python trainer.py --selftest  check the logic, no hardware or keys
"""
import base64
import json
import os
import queue
import re
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace as NS
from urllib.parse import urlencode

import anthropic
from atech import ActionRejected, Board, NoAcknowledgement
from elevenlabs import ElevenLabs
from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect

import morse

ENV_FILE = Path(__file__).resolve().parent.parent / ".env"

MODEL = "claude-opus-5-5"  # if replies feel slow: claude-haiku-5-5, and drop fallbacks and BETAS
BETAS = ["server-side-fallback-2026-07-01"]
MAX_TRIES = 3

VOICE = "JBFqnCBsd6RMkjVDRZzb"
TTS_MODEL = "eleven_flash_v2_5"
STT_URL = "wss://api.elevenlabs.io/v1/speech-to-text/realtime?" + urlencode({
    "model_id": "scribe_v2_realtime",
    "audio_format": "pcm_16000",
    "commit_strategy": "vad",
    "vad_silence_threshold_secs": 0.8,
    "language_code": "en",
})

VOLUME = 0.5
CHUNK_BYTES = 2250  # 70 ms, the 3000 base64 characters spk_play_pcm takes per action
DRAIN_S = 0.2  # lets the 128 ms speaker DMA buffer empty before the mic opens
SILENCE_S = 60.0  # ElevenLabs bills while the stream is open
KEY_DONE_S = 2.5  # a pause this long ends a keyed answer
MAX_MORSE_CHARS = 40

SYSTEM = """You are a morse code coach talking with Thomas through a small board on a desk: \
a speaker, a microphone, a light and a key.

Everything you write is read aloud, so write short spoken sentences with no markdown, lists \
or symbols.

Use send_morse whenever morse fits: a quiz, a drill, a surprise message, a repeat. Put \
what you want to say before the blink in its say field, for example "Hey Thomas, what am I \
trying to say?", and write no other text in that turn: text beside a tool call may never be \
heard. Never say the answer before Thomas has tried. After send_morse, stop: the next message \
carries the tool result and the answer together.

Thomas's turns arrive tagged [voice] or [morse], and either is a fine way to answer you. A \
[morse] turn gives what was keyed, the dots and dashes, and the speed, so coach on the actual \
mistake. Move from letters to words to phrases as Thomas improves, and slow down after a \
struggle."""

GREETING = "[session start] Greet Thomas in one sentence, then start with something easy."

TOOLS = [{
    "name": "send_morse",
    "description": "Say something aloud, then blink text in morse on the light in front of "
                   "Thomas. Takes letters, digits and . , ? / = only, at most 40 characters.",
    "eager_input_streaming": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "say": {"type": "string", "description": "Spoken aloud before the blink."},
            "text": {"type": "string", "description": "What to blink."},
            "wpm": {"type": "integer", "description": "Speed in words per minute, 5 to 25."},
        },
        "required": ["say", "text", "wpm"],
    },
}]


def split_sentences(buffer):
    """Split finished sentences off streamed text. Returns (sentences, unfinished rest)."""
    parts = re.split(r"(?<=[.?!\n])\s+", buffer)
    return [part.strip() for part in parts[:-1] if part.strip()], parts[-1]


def pcm_chunks(stream):
    """Cut TTS bytes to spk_play_pcm's size. Network chunks can split a sample: keep it even."""
    pending = b""
    for data in stream:
        pending += data
        while len(pending) >= CHUNK_BYTES:
            yield pending[:CHUNK_BYTES]
            pending = pending[CHUNK_BYTES:]
    if len(pending) >= 2:
        yield pending[: len(pending) & ~1]


def check_morse(args):
    """Validate send_morse input. Returns (pattern, unit_ms), or raises ValueError."""
    # Eager input streaming means the API did not validate this.
    say, text, wpm = (args.get(k) if isinstance(args, dict) else None for k in ("say", "text", "wpm"))
    if not isinstance(say, str) or not say.strip():
        raise ValueError("say must hold what to speak before the blink")
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_MORSE_CHARS:
        raise ValueError(f"text must be 1 to {MAX_MORSE_CHARS} characters")
    if not isinstance(wpm, int) or not 5 <= wpm <= 25:
        raise ValueError("wpm must be a whole number from 5 to 25")
    return morse.encode(text), morse.unit_ms(wpm)


class Speaker:
    """Says sentences through the board, on its own thread so Claude can keep streaming."""

    def __init__(self, board, tts):
        self.board, self.tts = board, tts
        self.sentences = queue.Queue()
        threading.Thread(target=self._run, daemon=True).start()

    def say(self, sentence):
        print(f"agent: {sentence}")
        self.sentences.put(sentence)

    def join(self):
        """Wait until everything is said. Board calls from other threads must wait for this."""
        self.sentences.join()
        time.sleep(DRAIN_S)

    def _run(self):
        while True:
            sentence = self.sentences.get()
            try:
                for chunk in pcm_chunks(self.tts(sentence)):
                    # The ack is the flow control: it comes once the chunk is in the DMA buffer.
                    self.board.call("spk_play_pcm", base64.b64encode(chunk).decode(), timeout=1)
            except Exception as error:  # one lost sentence must not end the voice
                print(f"could not say {sentence!r}: {error}")
            finally:
                self.sentences.task_done()


class Trainer:
    def __init__(self, board, claude, tts, open_stt):
        self.board, self.claude, self.open_stt = board, claude, open_stt
        self.speaker = Speaker(board, tts)
        self.inbox = queue.Queue()
        self.messages = []
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        for event in self.board.events(keys=["audio", "edge", "morse_done"]):
            self.inbox.put((event.key, event.value))

    def _next(self, deadline):
        """Next inbox event, or None once deadline has passed."""
        try:
            return self.inbox.get(timeout=max(0, deadline - time.monotonic()))
        except queue.Empty:
            return None

    def _drain(self):
        while not self.inbox.empty():
            self.inbox.get_nowait()

    def converse(self):
        pending = self.respond([{"type": "text", "text": GREETING}])
        while True:
            heard = self.listen()
            print(f"you: {heard}")
            pending = self.respond(pending + [{"type": "text", "text": heard}])

    def listen(self):
        """Wait for Thomas's turn, by voice or by key. Returns it tagged [voice] or [morse]."""
        while True:
            self._drain()  # presses during the agent's turn were never read
            heard = self._voice_or_key()
            if heard:
                return heard
            if heard is None:
                print(f"Paused after {SILENCE_S:.0f} s of silence. Press the key to wake me.")
                while not self._is_press(self.inbox.get()):
                    pass

    @staticmethod
    def _is_press(event):
        return event[0] == "edge" and event[1].startswith("1:")

    def _voice_or_key(self):
        """One listening session. None means silence, an empty string an unreadable keying."""
        ws = self.open_stt()
        threading.Thread(target=self._read_stt, args=(ws,), daemon=True).start()
        self.board.call("mic_on", "1")
        deadline = time.monotonic() + SILENCE_S
        press = None
        try:
            while event := self._next(deadline):
                kind, value = event
                if kind == "audio":
                    ws.send(json.dumps({"message_type": "input_audio_chunk", "audio_base_64": value,
                                        "commit": False, "sample_rate": 16000}))
                elif kind == "partial_transcript" and value:
                    deadline = time.monotonic() + SILENCE_S
                elif kind == "committed_transcript" and value.strip():
                    return f"[voice] {value.strip()}"
                elif self._is_press(event):
                    press = value
                    break
        except ConnectionClosed as error:
            print(f"speech-to-text closed: {error}")
        finally:
            self.board.call("mic_on", "0")
            ws.close()
        return self._keyed(press) if press else None

    def _read_stt(self, ws):
        try:
            for raw in ws:
                message = json.loads(raw)
                if message["message_type"] in ("partial_transcript", "committed_transcript"):
                    self.inbox.put((message["message_type"], message.get("text", "")))
                else:
                    print(f"speech-to-text: {message}")
        except ConnectionClosed:
            pass  # the main thread gets this too, on its next send

    def _keyed(self, first_press):
        """Collect presses until the key goes quiet, then decode them on board time."""
        presses, down = [], int(first_press.split(":")[1])
        deadline = time.monotonic() + KEY_DONE_S
        while event := self._next(deadline):
            if event[0] != "edge":
                continue  # leftover audio from before the mic closed
            state, at = event[1].split(":")
            if state == "1":
                down = int(at)
            elif down is not None:
                presses.append((down, int(at)))
                down = None
            deadline = time.monotonic() + KEY_DONE_S
        text, pattern, wpm = morse.decode(presses)
        return f'[morse] "{text}" pattern {pattern} at {wpm} wpm' if text else ""

    def respond(self, content):
        """One agent turn. Returns tool results to send with the next human turn, if any."""
        self.messages.append({"role": "user", "content": content})
        for _ in range(MAX_TRIES):
            try:
                response = self._reply()
            except ValueError:  # tool JSON the SDK could not parse: no block to answer, re-ask
                continue
            self.speaker.join()
            if response.stop_reason not in ("end_turn", "tool_use"):
                # A refusal or cut-off can hold half a tool call, which cannot be replayed.
                print(f"no usable reply: {response.stop_reason}")
                return []
            if response.content:
                self.messages.append({"role": "assistant", "content": response.content})
            uses = [block for block in response.content if block.type == "tool_use"]
            if not uses:
                return []
            results = [self._run_tool(block) for block in uses]
            if not any(result.get("is_error") for result in results):
                # No request now: the mic opens at once, and the answer goes with these.
                return results
            self.messages.append({"role": "user", "content": results})
        print("gave up on this turn")
        return []

    def _reply(self):
        """Stream one Claude reply, sending finished sentences to the speaker as they arrive."""
        unfinished = ""
        with self.claude.beta.messages.stream(
            model=MODEL,
            max_tokens=4096,
            system=SYSTEM,
            tools=TOOLS,
            messages=self.messages,
            output_config={"effort": "low"},
            betas=BETAS,
            fallbacks="default",
        ) as stream:
            for event in stream:
                if event.type == "text":
                    sentences, unfinished = split_sentences(unfinished + event.text)
                    for sentence in sentences:
                        self.speaker.say(sentence)
            response = stream.get_final_message()
        if unfinished.strip():
            self.speaker.say(unfinished.strip())
        return response

    def _run_tool(self, block):
        result = {"type": "tool_result", "tool_use_id": block.id}
        try:
            pattern, unit = check_morse(block.input)
            self.speaker.say(block.input["say"].strip())
            self.speaker.join()
            print(f"morse: {block.input['text']} at {block.input['wpm']} wpm")
            self._blink(pattern, unit)
        except (ValueError, TimeoutError, ActionRejected, NoAcknowledgement) as error:
            return {**result, "is_error": True, "content": str(error)}
        return {**result, "content": "Blinked."}

    def _blink(self, pattern, unit):
        self.board.call("morse", f"{unit}:{pattern}")
        units = sum({".": 2, "-": 4, " ": 2, "/": 6}[symbol] for symbol in pattern)
        deadline = time.monotonic() + units * unit / 1000 + 2
        while event := self._next(deadline):
            if event[0] == "morse_done":
                return
        raise TimeoutError("the board never finished blinking")


def open_stt():
    ws = connect(STT_URL, additional_headers={"xi-api-key": os.environ["ELEVENLABS_API_KEY"]},
                 close_timeout=1, legacy=True)
    started = json.loads(ws.recv(timeout=5))  # audio sent before this can be lost
    if started.get("message_type") != "session_started":
        raise RuntimeError(f"speech-to-text refused: {started}")
    return ws


def load_env():
    """Read the repo-root .env into the environment. A real variable wins."""
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            name, separator, value = line.strip().partition("=")
            if separator and not name.startswith("#"):
                os.environ.setdefault(name.strip(), value.strip().strip("\"'"))


def run():
    load_env()
    missing = [name for name in ("ANTHROPIC_API_KEY", "ELEVENLABS_API_KEY") if not os.environ.get(name)]
    if missing:
        sys.exit(f"add {' and '.join(missing)} to {ENV_FILE}")

    eleven = ElevenLabs(api_key=os.environ["ELEVENLABS_API_KEY"])  # its default reads at import

    def tts(sentence):
        return eleven.text_to_speech.stream(voice_id=VOICE, text=sentence, model_id=TTS_MODEL,
                                            output_format="pcm_16000")

    with Board.connect() as board:
        board.call("spk_set_volume", VOLUME)
        try:
            Trainer(board, anthropic.Anthropic(), tts, open_stt).converse()
        except KeyboardInterrupt:
            pass
        finally:
            board.call("mic_on", "0")


# Selftest: the board, the speech services and Claude are faked, the logic is real.


def fake_board():
    from atech import Board
    from atech.runtime.transport import MockTransport

    class Firmware(MockTransport):
        """Acks every action and finishes morse at once."""

        def send(self, action):
            super().send(action)
            self.push_wire(json.dumps({"type": "ack", "payload": {"action": action.key, "ok": True}}))
            if action.key == "morse":
                done = {"event_type": "state", "key": "morse_done", "value": 1}
                self.push_wire(json.dumps({"type": "event", "payload": done}))

    return Board(Firmware())


class FakeWs:
    """An STT session that delivers its script, then stays open until closed."""

    def __init__(self, *script):
        self.script, self.sent = script, []
        self.closed = threading.Event()

    def __iter__(self):
        for message_type, text in self.script:
            yield json.dumps({"message_type": message_type, "text": text})
        self.closed.wait()

    def send(self, raw):
        self.sent.append(json.loads(raw))

    def close(self):
        self.closed.set()


class FakeClaude:
    """Plays back scripted replies and records what each request sent."""

    def __init__(self, *replies):
        self.replies, self.requests = list(replies), []
        self.beta = NS(messages=NS(stream=self.stream))

    def stream(self, **request):
        self.requests.append({**request, "messages": list(request["messages"])})
        return FakeStream(self.replies.pop(0))


class FakeStream:
    def __init__(self, reply):
        self.reply = reply

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        return (NS(type="text", text=b.text) for b in self.reply.content if b.type == "text")

    def get_final_message(self):
        return self.reply


def reply(stop_reason, *content):
    return NS(stop_reason=stop_reason, content=list(content))


def text(value):
    return NS(type="text", text=value)


def tool_use(block_id, **args):
    return NS(type="tool_use", id=block_id, name="send_morse", input=args)


def sent(board, key):
    return [a.value for a in board.transport.sent if a.key == key]


def trainer_with(claude=None, sessions=()):
    board = fake_board()
    spoken = []
    sessions = list(sessions)

    def tts(sentence):
        spoken.append(sentence)
        return [b"\x01\x02" * 10]

    return Trainer(board, claude or FakeClaude(), tts, lambda: sessions.pop(0)), board, spoken


def selftest_text():
    assert split_sentences("Hello there. How are y") == (["Hello there."], "How are y")
    assert split_sentences("Ready? Go! Now") == (["Ready?", "Go!"], "Now")
    assert split_sentences("It runs at 3.5 wpm") == ([], "It runs at 3.5 wpm")

    assert [len(c) for c in pcm_chunks([b"a" * 3001, b"b" * 2000])] == [2250, 2250, 500]
    assert b"".join(pcm_chunks([b"ab", b"cd"])) == b"abcd"


def selftest_tool_input():
    good = {"say": "Watch this.", "text": "SOS", "wpm": 12}
    assert check_morse(good) == ("... --- ...", 100)
    for bad in ({**good, "text": "ø"}, {**good, "wpm": 40}, {**good, "wpm": True},
                {**good, "text": "A" * 41}, {**good, "text": "  "}, {**good, "say": " "},
                {"say": "Hi", "wpm": 12}, {"text": "SOS", "wpm": 12}, "SOS"):
        try:
            check_morse(bad)
        except ValueError:
            continue
        raise AssertionError(f"accepted {bad!r}")


def selftest_speaker():
    board = fake_board()
    calls = []

    def tts(sentence):
        calls.append(sentence)
        if sentence == "broken":
            raise RuntimeError("tts down")
        return [b"\x01\x02" * 2000]

    speaker = Speaker(board, tts)
    speaker.say("broken")  # one failed sentence must not end the voice
    speaker.say("Hello.")
    speaker.join()
    assert calls == ["broken", "Hello."]
    chunks = sent(board, "spk_play_pcm")
    assert all(len(c) <= 3000 for c in chunks), max(len(c) for c in chunks)
    assert sum(len(base64.b64decode(c)) for c in chunks) == 4000


def selftest_listen():
    global KEY_DONE_S, SILENCE_S
    KEY_DONE_S, SILENCE_S = 0.05, 0.1

    # Voice, with a stale key press from before the turn: it must not start keying.
    session = FakeWs(("partial_transcript", "hel"), ("committed_transcript", "hello"))
    trainer, board, _ = trainer_with(sessions=[session])
    trainer.inbox.put(("edge", "1:5"))
    trainer.inbox.put(("audio", "c3RhbGU="))
    assert trainer.listen() == "[voice] hello"
    assert sent(board, "mic_on") == ["1", "0"]
    assert session.closed.is_set()
    assert session.sent == []  # stale audio belongs to the agent's own turn

    # Audio passes through unchanged while listening.
    session = FakeWs(("committed_transcript", "ok"))
    trainer, _, _ = trainer_with(sessions=[session])
    original = trainer._read_stt
    trainer._read_stt = lambda ws: (trainer.inbox.put(("audio", "QUJD")), time.sleep(0.02), original(ws))
    assert trainer.listen() == "[voice] ok"
    assert session.sent[0] == {"message_type": "input_audio_chunk", "audio_base_64": "QUJD",
                               "commit": False, "sample_rate": 16000}

    # A key press takes the turn: mic off, presses collected on board time, decoded.
    session = FakeWs()
    trainer, board, _ = trainer_with(sessions=[session])
    original = trainer._read_stt

    def keyed(ws):
        for edge in ("1:1000", "0:1100", "1:1200", "0:1500"):
            trainer.inbox.put(("edge", edge))
        original(ws)

    trainer._read_stt = keyed
    assert trainer.listen() == '[morse] "A" pattern .- at 12 wpm'
    assert sent(board, "mic_on") == ["1", "0"]
    assert session.closed.is_set()

    # Silence pauses. The waking press is not morse, and listening resumes.
    first, second = FakeWs(), FakeWs(("committed_transcript", "back"))
    trainer, board, _ = trainer_with(sessions=[first, second])
    threading.Timer(0.4, lambda: trainer.inbox.put(("edge", "1:9000"))).start()
    started = time.monotonic()
    assert trainer.listen() == "[voice] back"
    assert time.monotonic() - started >= 0.4, "listening resumed before the press"
    assert sent(board, "mic_on") == ["1", "0", "1", "0"]
    assert first.closed.is_set()


def selftest_agent():
    # The lead-in lives in the tool input: Opus 5.5 can turn text beside a tool call into a
    # thinking block, which would blink at Thomas in silence. Speech comes first, then the blink.
    claude = FakeClaude(reply("tool_use", NS(type="thinking", thinking=""),
                              tool_use("t1", say="Hey Thomas, what am I saying?", text="SOS", wpm=12)),
                        reply("end_turn", text("Well done.")))
    trainer, board, spoken = trainer_with(claude)
    pending = trainer.respond([{"type": "text", "text": "start"}])
    assert spoken == ["Hey Thomas, what am I saying?"]
    assert sent(board, "morse") == ["100:... --- ..."]
    keys = [action.key for action in board.transport.sent]
    assert keys.index("spk_play_pcm") < keys.index("morse")

    # No second request: the mic must open at once.
    assert len(claude.requests) == 1
    assert [r["type"] for r in pending] == ["tool_result"] and not pending[0].get("is_error")

    # The result travels with the human answer, tool_result first.
    assert trainer.respond(pending + [{"type": "text", "text": "[voice] SOS"}]) == []
    assert spoken[-1] == "Well done."
    content = claude.requests[1]["messages"][-1]["content"]
    assert [c["type"] for c in content] == ["tool_result", "text"]
    assert content[0]["tool_use_id"] == "t1"
    assert [b.type for b in claude.requests[1]["messages"][-2]["content"]] == ["thinking", "tool_use"]

    # Bad tool input goes straight back to Claude, not to the board.
    claude = FakeClaude(reply("tool_use", tool_use("t2", say="Watch.", text="ø", wpm=12)),
                        reply("end_turn", text("Sorry, plain letters only.")))
    trainer, board, _ = trainer_with(claude)
    assert trainer.respond([{"type": "text", "text": "start"}]) == []
    assert sent(board, "morse") == []
    assert claude.requests[1]["messages"][-1]["content"][0]["is_error"] is True

    # A cut-off tool call must not run, or be replayed in the history.
    claude = FakeClaude(reply("max_tokens", tool_use("t3", say="Watch.", text="SO", wpm=12)),
                        reply("end_turn", text("Next.")))
    trainer, board, _ = trainer_with(claude)
    assert trainer.respond([{"type": "text", "text": "one"}]) == []
    assert sent(board, "morse") == []
    trainer.respond([{"type": "text", "text": "two"}])
    assert all(m["role"] == "user" for m in claude.requests[1]["messages"])


def selftest():
    selftest_text()
    selftest_tool_input()
    selftest_speaker()
    selftest_listen()
    selftest_agent()
    print("selftest ok")


if __name__ == "__main__":
    selftest() if "--selftest" in sys.argv else run()
