# VKS — Curio, a voice- and vision-driven robot

Two independent control programs for an Arduino-based robot cart, plus the
Arduino sketches they talk to. Each program is self-contained; you run one at
a time.

| Subsystem | Runs on | What it does |
|-----------|---------|--------------|
| [cart-motion-code/](cart-motion-code/) | your Mac | Voice-driven driving + spoken Q&A for kids (uses Claude) |
| [unoq-vision-code/](unoq-vision-code/) | Arduino UNO Q | Camera face/person tracking that steers the cart |

---

## 1. Voice control — `cart-motion-code/curio_main.py`

"Curio" is a curiosity companion for children (ages 8–12). It listens on a
microphone (laptop or the robot's own mic over WiFi), drives the robot on
spoken commands, and answers spoken questions using Claude.

**Modes**
- **Drive** — say a single word: `forward`, `back`, `left`, `right`, `stop`
  (plus `automatic` / `manual` to hand steering to/from the analog pin).
  Each maps to a one-byte serial command the Arduino understands.
- **Ask** — say the wake word `question`, then ask anything. The answer is
  spoken aloud (`say` on macOS) and paged across the robot's OLED face.
- **Experiment** — say `experiment` to run a predict → measure → explain
  activity. Light experiment reads the LDR sensor; if that sensor is dead it
  falls back automatically to a mic-based sound experiment. **The code does
  every measurement; Claude only narrates the numbers** — it never invents a
  reading.

Speech recognition is offline via [Vosk](https://alphacephei.com/vosk/) with
constrained grammars, so drive words stay strict and reliable.

### Setup

```bash
cd cart-motion-code
python3 -m venv venv
./venv/bin/pip install pyserial sounddevice vosk anthropic

# Offline speech model (unzip so ./vosk-model-small-en-us-0.15/ exists)
# https://alphacephei.com/vosk/models  ->  vosk-model-small-en-us-0.15

echo "ANTHROPIC_API_KEY=sk-ant-..." > .env
```

`.env` sits next to `curio_main.py` and is loaded at startup.

### Running

```bash
# List microphones, then run with a chosen input device
./venv/bin/python curio_main.py --list-devices
./venv/bin/python curio_main.py --device 2

# Talk to the robot over USB serial (default port shown below)
./venv/bin/python curio_main.py --port /dev/cu.usbmodem2101

# Or over WiFi, using the robot's own microphone
./venv/bin/python curio_main.py --host curio.local

# Try it with no hardware attached
./venv/bin/python curio_main.py --dry-run --no-speak
```

Only one program may own the serial port at a time — close any
`arduino-cli monitor` or other script first.

### Key options

| Flag | Meaning |
|------|---------|
| `--port` | USB serial device (default `/dev/cu.usbmodem2101`) |
| `--host` | Connect over WiFi instead of USB (e.g. `curio.local`) |
| `--local-mic` | Use the laptop mic even in WiFi mode |
| `--device N` | Microphone input device index |
| `--model` | Vosk model folder (default `vosk-model-small-en-us-0.15`) |
| `--dry-run` | Print serial bytes instead of sending them |
| `--no-speak` | Don't speak answers aloud |
| `--list-devices` | Print audio devices and exit |

### Serial protocol (Mac → Arduino)

Single bytes drive the robot: `f` `b` `l` `r` `s` `a` `m`, and `g` requests a
light reading (the board replies with a `LIGHT:<n>` line). Face text is sent as
`t<message>\n` for the OLED.

---

## 2. Vision tracking — `unoq-vision-code/`

An [Arduino App Lab](https://www.arduino.cc/) project that runs on the
**Arduino UNO Q**. A USB camera feeds an on-device object-detection model; the
program locks onto a person/face and steers the cart to keep them centered.

**Files**
- [python/main.py](unoq-vision-code/python/main.py) — the vision + tracking logic (Linux side).
- [sketch/sketch.ino](unoq-vision-code/sketch/sketch.ino) — the microcontroller sketch (steering output).
- [app.yaml](unoq-vision-code/app.yaml) — App Lab bricks (object detection + web UI).
- [sketch/sketch.yaml](unoq-vision-code/sketch/sketch.yaml) — build profile (`arduino:zephyr`).

**How it works**
1. Camera runs at 320×240 @ 10 fps.
2. For the first **10 seconds** it acquires a target, preferring a `face` over
   a `person`, tracking the same one across frames via IoU. If nothing is
   found, it locks the whole frame as a centered fallback.
3. Once locked, it computes the target's horizontal angle from center and emits
   `LEFT` / `RIGHT` / `CENTER` (within ±5° counts as centered).
4. Directions are sent over the Router Bridge to the sketch's
   `set_cart_direction`, which drives PWM on pin `D6`:
   `LEFT` → 0 V, `CENTER` → ~1.65 V, `RIGHT` → 3.3 V. Repeated identical
   commands are suppressed.
5. A background thread saves a JPEG every 2 seconds (and on lock) to
   `saved_faces/` for debugging. Saved images are git-ignored.

### Running

Open the `unoq-vision-code` project in the Arduino App Lab environment on the
UNO Q, connect a USB camera, and run the app. The web UI brick provides a live
preview.

---

## Repository layout

```
cart-motion-code/
  curio_main.py          # voice control + Claude Q&A (runs on your Mac)
unoq-vision-code/
  app.yaml               # App Lab brick config
  python/main.py         # camera tracking (runs on UNO Q)
  sketch/
    sketch.ino           # steering sketch (PWM on D6)
    sketch.yaml          # build profile
```

## Notes

- The two subsystems are separate control strategies for the same robot; pick
  one per session. The vision program does not use Claude; the voice program
  does (model `claude-haiku-4-5`).
- Provide your own `ANTHROPIC_API_KEY` and Vosk model — neither is committed.
- `.jpg` files and `.DS_Store` are ignored by git.
