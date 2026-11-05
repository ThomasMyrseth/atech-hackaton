# Morse trainer

Talk with a Claude morse coach through the Atech board. It speaks through the speaker, listens through the microphone, and decides on its own when to blink morse at you on the LED Grid. Answer by voice, or key morse on the button.

The agent runs on the laptop. The board does the sound, the light and the timing over USB.

## Hardware

14-port board: speaker in ports 1-2, microphone in 3-4, button in 11, LED Grid in 10.

## Setup

From the repo root:

```bash
venv/bin/pip install -r morse/requirements.txt
venv/bin/atech upload morse/fw      # replaces whatever firmware is on the board
```

Add both keys to the repo-root `.env`, which git ignores:

```
ANTHROPIC_API_KEY=...
ELEVENLABS_API_KEY=...
```

## Run

```bash
morse/run.sh              # talk to it
morse/run.sh --selftest   # check the logic, no hardware or keys
```

Only one program can hold the board's port, so close `atech monitor` first. Ctrl-C stops it.

## Using it

- The dim blue light means it is listening. It only listens between its own turns, so wait for the light.
- To answer in morse, start keying. It takes over from the microphone, and a 2.5 s pause sends your answer.
- After 60 s of silence the microphone closes and ElevenLabs stops billing. Press the key once to wake it.
- Speed comes from your own keying, so a single-symbol answer such as `E` or `T` is judged against 12 WPM.

## Tuning

- Slow replies: change `MODEL` in `trainer.py`. The comment there says what else changes.
- Quiet or clipped speech recognition: change `MIC_GAIN` in `fw/project.yaml`, then upload again.
