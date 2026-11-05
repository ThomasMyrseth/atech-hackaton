# Atech SDK — guide for coding agents

You are helping someone make an **Atech board** do something. The board is an
ESP32-S3 with numbered ports; modules (button, light grid, sensors, motors, a robot
arm…) slide into the ports. You write one small file, `project.yaml`. The SDK turns
it into firmware, compiles it, flashes it, and then lets you talk to the board from
the laptop.

This text ships inside the package: `atech guide` prints it, and `atech new` puts a
copy in every project as `AGENTS.md` and `CLAUDE.md`.

## The whole workflow

```bash
atech list-boards                  # ask the user which board they are holding
atech list-modules                 # what exists. Never invent a module
atech info <module>                # its C++ API, events, actions and cautions
atech new <name> --board 14port    # project.yaml + this guide
#   ...edit project.yaml...
atech describe <dir>               # what you placed, the API, the events and actions
atech view <dir>                   # the board in 3D in the browser, redrawn as project.yaml changes
atech validate <dir>               # placement check
atech build <dir>                  # generate + compile
atech upload <dir>                 # flash the board on USB
atech check                        # reboot it: which modules answered, and why not
atech monitor                      # events coming from the board
atech send <key> [value]           # one action, confirmed by the board (value: r=255 g=0, a number, or JSON)
```

Every command exits non-zero when it fails and says why on stderr (`error: …`, or
the list of placement issues). Cautions are printed on stdout with the result.
`list-boards`, `list-modules`, `info` and `monitor` take `--json`.

## project.yaml

```yaml
name: my_project
board: 14port                        # 8port | 10port | 14port
modules:
  - {id: button,   instance: btn, port: 1}
  - {id: neopixel, instance: led, port: 2}
  - {id: speaker,  instance: spk, ports: [13, 14]}   # 2-port module: an adjacent pair
code:
  globals: |
    // optional, file scope: #include lines, helper functions
  setup: |
    // optional, runs once after every module is initialised
  loop: |
    // runs forever
    if (btn.wasPressed()) {
      led.setAll(255, 0, 0);
      led.show();
    }
```

- `instance` is the name of the C++ object you call in `code:`. Its methods are in
  `atech info <module>` and `atech describe`.
- Ports: `atech list-boards` shows which ports take modules and which of them have
  UART. A 2-port module needs two neighbouring ports; a UART module (the robot arm)
  needs a UART port. `atech validate` says what is wrong and which ports would work.
- **Ask the user which port each module is in.** You cannot see the board.
  `atech view <dir>` can: it opens a tab in the user's browser with the board in
  3D, as studio draws it, each module in its port and a list of port numbers and
  module names, and redraws whenever you change `project.yaml`. Ask the user to
  compare it with the board in their hand.
- **Every time you add, remove or move a module, show the picture.** Run
  `atech view <dir>` in the background; do not ask first. The first run opens the
  tab and keeps serving it. A later run for the same project starts nothing: it
  returns at once, and opens the tab again only if the user closed it. So run it
  after every change to `modules:`, not only the first, and end that reply with a
  line the user cannot miss, for example: "The board is open in your browser:
  check that each module is in the port shown." When it prints a URL and opens no
  tab (no display, or `--no-open`), give the user that URL.
- Power modules (USB-C, power jack) are not in the catalog and need no entry in
  `project.yaml`: they carry no firmware. Leave the ports they sit in unused.
- There are three code blocks: `globals` (file scope: `#include` lines, helper
  functions), `setup` and `loop`. Keep state in `static` variables inside
  `loop`.
- `atech validate` checks placement only. Mistakes in your C++ show up in `atech build`.
- **Default feedback.** With no `code: loop:`, the firmware shows every input
  (button, knob, motion sensor, microphone, distance, temperature, CO2) on every
  output: a row per input on the screen, the first sensor as a bar on the light
  grid, the whole grid and the button's light on a press, a beep on the speaker. A DC or stepper motor follows a button or knob, never a sensor; the
  robot arm is never moved. Writing `loop` replaces it; `feedback: false` turns it
  off, `feedback: true` keeps it next to your `loop`. It does not use
  `wasPressed()` and the other edge calls.
- **Inputs and no output?** `atech validate` prints a TIP: suggest a light grid
  (`neopixel`) or a screen (`st7735_tft`) to the user, so they can see what the
  sensors read.

## Rules for the `code:` block

- **Never block.** Use `millis()` for timing; no `delay()` beyond a few ms.
- **Edge calls are yours.** `btn.wasPressed()`, `knob.wasRotatedCW()`,
  `imu.wasShaken()` and `btn.getGesture()` (tap, double tap, triple tap, long
  press) return each edge once and then clear.
  The firmware's own events to the laptop do not use them, so your code and the
  laptop both see every press. Call each edge function in one place only.
- **Do not read `Serial` yourself.** The generated firmware reads it to receive
  actions from the laptop. Reading it in your code steals those bytes.
- **Do not hardcode GPIO numbers.** Pins come from the port.
- **Do not edit `build/`.** It is regenerated on every `atech build`.
- To send your own event to the laptop, print one JSON line:

  ```cpp
  Serial.printf("{\"type\":\"event\",\"payload\":{\"event_type\":\"sensor\",\"key\":\"level\",\"value\":%d}}\n", level);
  ```

- To accept your own action from the laptop, test `atech_action` and confirm it:

  ```cpp
  if (atech_action == "buzz") { spk.playTone(880, 100); atechDone(); }
  ```

  `atech_value` holds the value as a `String`. An action nobody handles is
  answered with `ok:false`, so the laptop knows.

## Talking to the board from the laptop

`atech describe` lists the exact event and action names of a project. They are
built from the instance name, for example `btn`, `th_temperature`, `led_fill`.

```bash
atech monitor --key btn                          # only the button's events
atech send led_fill r=255 g=0 b=0                # prints ok only if the board did it
```

```python
from atech import Board, ActionRejected, NoAcknowledgement, NoBoardFoundError

with Board.connect() as board:                   # or Board.connect(port="/dev/cu.usbmodem1101")
    board.call("led_fill", {"r": 255, "g": 0, "b": 0})         # do something
    joints = board.query("arm_read", reply="arm_positions")   # ask, get the answer back
    last = board.latest("btn")                   # last event seen for a key, or None
    for event in board.events(keys=["btn"]):     # blocks, yields events forever
        print(event.key, event.value)
```

- `board.call(key, value="", timeout=2.0)` returns once the board confirmed. It
  raises `ActionRejected` when the board could not do it (no such action, bad value,
  a servo that does not answer) and `NoAcknowledgement` when nothing came back.
- `board.query(key, value="", reply=<event key>)` is `call` plus the value of the
  event the board answers with. Use it for every read-back.
- `board.send()` only writes to the port and tells you nothing. Use `call`.
- `Board.connect()` takes the first board on USB and raises `NoBoardFoundError` when
  there is none. With several boards attached, pass `port=` (`atech ports` lists them).
- Wait for each action before sending the next: a flood of unconfirmed ones can
  overflow the board's buffer and some get lost.
- **One program per board at a time.** A second program opening the same port gets
  `PortUnavailableError`. Stop `atech monitor` before `atech send` or `atech upload`,
  or listen and send from one Python script as above.

## Things that are true and save time

- **The first build downloads the ESP32 compiler** (about 2.4 GB on disk, once).
  After that a build takes seconds. Do the first build on good wifi. A few modules
  (the light grid, for one) also download a small library the first time a project
  uses them, so a new project can still need the network once.
- **`atech upload` always regenerates and rebuilds from `project.yaml`.** There is no
  stale build to worry about.
- **Port in use?** Only one program can hold the board's port. Close the other one
  (`atech monitor` in another terminal, an editor's serial monitor), or `atech free-port`.
- **Linux, "no permission":** `sudo usermod -aG dialout $USER`, then log in again.
- **No board found?** It must be plugged in with a USB data cable. `atech ports`
  shows what the SDK sees. The SDK never falls back to some other serial port.
- **Flashing fails, or the board does not show up?** `atech guide flash`: how the
  board looks on USB (`lsusb`, `/dev/ttyACM0`, `/dev/cu.usbmodem…`, `COM<n>`), the
  BOOT/RESET bootloader by hand, and Linux, macOS and Windows specifics.
- **`atech check` cannot see output-only modules.** A light grid or a display
  reports "initialized" even when nothing is plugged in. Only eyes confirm output.
- **A module does nothing?** Do not re-flash the same firmware. Print a line
  before and after the suspect call and watch `atech monitor --json`; swap the
  module into another port; keep one known-good module in the same build.
- **Several I2C modules in one project:** the temperature, air-quality and motion
  sensors each get their own software I2C bus on their own port, so any number of
  them work together, two of the same kind included. The distance sensor uses the
  chip's one hardware bus, so a project takes **one** distance sensor; `atech
  validate` refuses a second.
- **`atech view` is a picture of `project.yaml`, not of the board.** It never talks
  to the hardware (so it can run beside `monitor` and `upload`), and it cannot tell
  you a module is plugged in. Modules with no CAD model yet (stepper, robot arm, the
  8-port board) are grey boxes; on the 10-port board, which socket carries which
  number is not confirmed, and the page says so. An entry that does not place is
  listed with the reason `atech validate` gives, and the rest are still drawn.
- **Simulation:** `atech sim-assets` writes USD, MuJoCo and URDF files for modules
  and boards (Isaac Sim, MuJoCo, ROS). `atech guide sim` explains them.
- **Helpers in every project** (`atech_helpers.h`, `atech_ui.h`, from studio):
  `EveryTimer t; if (t.ready(500)) {…}`, `RisingEdge`, `FallingEdge`,
  `Debouncer`, `atech::mapRangeF`, `atech::lerp`, `atech::wrap`; for the display
  `atech::renderEvery`, `drawHeader`, `drawValueRow`, `drawCard`,
  `drawProgressBar`, `drawCenteredText`, and for menus and text `drawTitleBar`,
  `drawStatusBar`, `StatusMessage`, `ListView` + `drawList`, `drawCsvTable`,
  `drawWrappedText`, `fitText`; the colours `ATECH_BG`, `ATECH_FG`,
  `ATECH_ACCENT`, `ATECH_OK`, `ATECH_WARN`, `ATECH_ALERT`, `ATECH_PANEL`. Do not
  name your own things `EveryTimer`, `Debouncer`, `ListView` or `StatusMessage`.
- **Displays:** `display()` pushes the whole frame from a buffer: 160x80 on the
  Screen (`st7735_tft`), 320x240 on the Big Screen (`st7789_tft`, the same calls),
  swapped after `setRotation(0)` or `(2)` (portrait). Lay out from `width()` and
  `height()`. For animation, call `setDirectMode(true)` once in `setup`: draws then
  go straight to the screen, so erase the old spot with a small `fillRect` and draw
  the new one. The Big Screen's frame takes about 31 ms to push: redraw every
  33-50 ms, and use a smooth font (`setFont`), since the built-in one is tiny there.
- **SPI: two hardware hosts.** A microSD card needs one, and the generated `setup()`
  reserves it. The screens take what is left in `project.yaml` order; a screen that
  finds none runs software SPI (same drawing, slower frames, `isSoftwareSpi()`). So
  with a card and two screens, list the Big Screen first. One microSD card per
  project.
- **microSD card:** writes block for tens of ms. For anything periodic call
  `beginLogger(path)` once in `setup` and `logLine()` in `loop`. FAT16/FAT32 only:
  an exFAT card (most of 64 GB and up) does not mount until it is reformatted.
  With `instance: sd`, `atech send sd_list /`, `sd_read`, `sd_write` reach the card
  from the laptop.

### Robot arm: power

The Robot Arm module passes the board's high-voltage rail (labelled 12 V) straight
to the servos. That rail is fed by a power module (the power jack module, or the
USB-C PD power module) and carries whatever that module is given. Nothing regulates
it, and the standard USB-C module does not feed it. SO-101 arms come in a 12 V and
a 5 V version, and a 12 V supply on a 5 V arm damages the servos.

Building is harmless. Before you **flash, or tell the user to connect or power the
arm**, ask which arm they have and what voltage is printed on the supply in the
jack, and tell them: 12 V supply for a 12 V arm, 5 V supply for a 5 V arm, never
more than 12 V. `atech validate`, `build` and `upload` print this as a `CAUTION`;
relay it. With nothing in the power jack the servos have no power, so a silent arm
on USB-C alone is expected.

The arm goes stiff when the firmware starts (torque is switched on in setup) and
moves as soon as something tells it to. Have the user clear its reach first.

### Robot arm: no servo answers

If the arm has power and `atech check` still reports it not responding: on a
Robot Arm V1.0 module with six servos, the servos only answered when the board
transmitted on the port's line B. The module transmits on line A by default. Swap
the two lines in `project.yaml` and upload again:

```yaml
  - {id: robot_arm, instance: arm, port: 3, swap_lines: true}
```

Tell the user which of the two worked.

## WiFi, Bluetooth, the browser and the rest of the ESP32

The SDK talks to the board over USB serial only. The chip (ESP32-S3, Arduino core
2.0) can do more, and your `code:` can use it; the SDK generates nothing for it, and
`atech send` / `Board` reach none of it. Everything below compiles in a project.
None of it has been checked on a board by the SDK; check it yourself there.

- **Headers and libraries:** `#include` lines go in `code: globals:`. A library
  that is not part of the Arduino core goes in `libraries:` at the top of
  `project.yaml` (PlatformIO names, e.g. `libraries: [knolleary/PubSubClient@^2.8]`);
  the first build downloads it.
- **WiFi:** `#include <WiFi.h>`. `WiFi.begin(ssid, pass)` returns at once; check
  `WiFi.status() == WL_CONNECTED` in `loop` instead of waiting in a `while`. Ask the
  user for the network name and password; do not invent them. HTTP, MQTT (with a
  library), NTP time (`configTime`) and ESP-NOW (`<esp_now.h>`) build on it.
- **Bluetooth is BLE only.** `<BLEDevice.h>`, `<BLEServer.h>`, `<BLE2902.h>`. The
  ESP32-S3 has no classic Bluetooth: `BluetoothSerial`, A2DP audio and pairing with
  a classic Bluetooth speaker do not exist on it. WiFi and BLE together fit in flash.
- **Web Serial (Chrome, Edge):** a web page can open the board's USB port and
  speak the same JSON lines as the SDK (`atech guide protocol`). The SDK ships no
  JavaScript client. The browser tab holds the port like any other program: close
  it before `atech upload` or `atech monitor`.
- **Web Bluetooth (Chrome, Edge; not Safari or Firefox):** a web page can connect
  to a BLE service your firmware advertises. You write that service yourself; there
  is no built-in one, and the event/action protocol does not run over it.
- **Analog reads and WiFi:** GPIOs 11–20 are on ADC2, which cannot read while
  WiFi is on. `atech describe` shows each placed module's GPIOs. No catalog module
  reads analog; this is about your own `analogRead`.
- **Remembering things across power-off:** `<Preferences.h>` (small key/value
  values) or `<LittleFS.h>` (files; `LittleFS.begin(true)` formats it the first
  time). The SDK has no command to copy files onto the board.
- **Work that has to wait:** a FreeRTOS task (`xTaskCreatePinnedToCore`) may block
  without stalling `loop`. Do not touch module instances from it unless `loop` stays
  off them, and print whole lines so they do not interleave with the firmware's.
- **USB stays a serial port.** The firmware runs the chip's USB in its built-in
  serial/JTAG mode, which the SDK needs. USB keyboard, mouse or MIDI code compiles
  but needs the other USB mode; expect the computer not to see it. Deep sleep
  switches USB off: the board leaves `atech monitor` until it wakes.
- **Not on the chip:** LoRa, GPS, cellular, CAN and RS-485 need a module with that
  hardware, and none is in the catalog: do not promise them. Zigbee and Thread
  (so Matter over Thread) cannot be done on the ESP32-S3 at all; that radio is on
  other ESP32 chips. A UART LoRa module would fit a UART port as your own module
  (see below).

## A module the catalog does not have

```
my_project/
├── project.yaml          # add:  modules_path: ./my_modules
└── my_modules/
    └── <id>/
        ├── module.yaml   # same shape as a bundled one
        ├── <id>.h
        └── <id>.cpp
```

Start from a bundled module: `python -c "import atech.catalog, pathlib;
print(pathlib.Path(atech.catalog.__file__).parent / 'data' / 'modules')"` prints
where they live. A module with the same id as a bundled one replaces it. In
`module.yaml`, every event you declare must be a key the `loop` template really
prints, and every action needs a `handler:` (C++ run when the action arrives;
the value is the `String value`).

## Never

- Invent a module, a method, an event or an action. `atech info` is the truth.
- Skip `atech validate`.
- Tell the user something worked because a command exited 0. Flashing succeeding
  says nothing about the hardware; check with `atech check`, `atech monitor`, or
  ask them what they see.
- Use `delay(1000)` in `loop`.
- Connect or power a robot arm before the supply question above is answered.
