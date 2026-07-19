#!/usr/bin/env python3
"""Curio, complete: voice-driven robot + voice-asked questions. One program.

Say a drive word:            forward / back / left / right / stop
Say the wake word "question" then ask anything: the answer pages across
the OLED face and is spoken aloud by the Mac.

Usage:
  ./venv/bin/python curio_main.py
  ./venv/bin/python curio_main.py --list-devices
  ./venv/bin/python curio_main.py --device N --no-speak --dry-run

Close voice_drive.py / curio_ask.py / arduino-cli monitor first:
one program owns the serial port at a time.
"""
import argparse
import json
import os
import queue
import socket
import subprocess
import textwrap
import threading
import time

import serial as pyserial
import sounddevice as sd
from vosk import KaldiRecognizer, Model, SetLogLevel

SYSTEM_BASE = """
You are Curio, a warm, playful curiosity companion for children aged 8-12.

Answer the child's question accurately in simple language.
Use one playful metaphor when useful.
Reply in no more than 45 spoken words.
End with one safe observation, prediction, or experiment the child can try.

Never invent a sensor measurement.
Never claim an experiment proves more than it observes.
Never suggest anything involving fire, electricity, chemicals, traffic,
heights, sharp objects, medicine, or eating unknown things.
Do not use emoji, markdown, headings, or lists because the answer is spoken.
"""

MODEL_ID = "claude-haiku-4-5-20251001"

# NOTE: no "go"/"ahead" aliases - "go left" must never half-fire as forward.
COMMANDS = {
    "forward": b"f",
    "back": b"b", "backward": b"b", "reverse": b"b",
    "left": b"l",
    "right": b"r",
    "stop": b"s", "halt": b"s",
    "automatic": b"a",  # hand control to the analog steering pin
    "manual": b"m",     # take control back
}
WAKE_WORD = "question"  # flips into ask-mode ("curio" is not in Vosk's vocab)
EXPERIMENT_WORD = "experiment"
GRAMMAR = json.dumps(list(COMMANDS) + [WAKE_WORD, EXPERIMENT_WORD, "[unk]"])
PREDICT_GRAMMAR = json.dumps(["bright", "dark", "[unk]"])
YESNO_GRAMMAR = json.dumps(["yes", "no", "[unk]"])

COOLDOWN_S = 1.5
MIN_CONF = 0.85
PAGE_CHARS = 63
PAGE_SECS = 2.8


def load_env():
    try:
        with open(os.path.join(os.path.dirname(__file__), ".env")) as f:
            for line in f:
                if "=" in line and not line.startswith("#"):
                    k, v = line.strip().split("=", 1)
                    os.environ.setdefault(k, v)
    except FileNotFoundError:
        pass


def pages_of(answer):
    lines = textwrap.wrap(answer, width=21)
    for i in range(0, len(lines), 3):
        yield "".join(line.ljust(21) for line in lines[i:i + 3]).rstrip()


class TcpLink:
    """Same interface as pyserial, but over WiFi to the robot."""

    def __init__(self, host, port=3333):
        print(f"connecting to {host}:{port} ...")
        self.sock = socket.create_connection((host, port), timeout=8)
        self.sock.settimeout(0.005)  # never stall the audio loop
        print("robot connected over wifi")

    def write(self, data):
        self.sock.sendall(data)

    def read(self, n=200):
        try:
            return self.sock.recv(n)
        except (socket.timeout, BlockingIOError):
            return b""

    def reset_input_buffer(self):
        while self.read(4096):
            pass


class Curio:
    def __init__(self, args):
        self.args = args
        SetLogLevel(-1)
        model = Model(args.model)
        self.rec_cmd = KaldiRecognizer(model, 16000, GRAMMAR)
        self.rec_cmd.SetWords(True)
        self.rec_ask = KaldiRecognizer(model, 16000)
        self.rec_predict = KaldiRecognizer(model, 16000, PREDICT_GRAMMAR)
        self.rec_yesno = KaldiRecognizer(model, 16000, YESNO_GRAMMAR)

        load_env()
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise SystemExit("ANTHROPIC_API_KEY missing - put it in .env")
        import anthropic
        self.claude = anthropic.Anthropic()

        self.ser = None
        if not args.dry_run:
            if args.host:
                self.ser = TcpLink(args.host)
            else:
                self.ser = pyserial.Serial(args.port, 115200, timeout=0.05)
                time.sleep(2.5)
            self.ser.reset_input_buffer()

        self.audio_q = queue.Queue()
        self.last_cmd = 0.0

    def send(self, data):
        if self.ser:
            self.ser.write(data)
        else:
            print(f"  [serial] {data!r}")

    def face_text(self, msg):
        self.send(b"t" + msg.encode(errors="replace") + b"\n")

    def drain_audio(self):
        while not self.audio_q.empty():
            self.audio_q.get_nowait()

    # ---- drive mode ----
    def handle_drive(self, data):
        if not self.rec_cmd.AcceptWaveform(data):
            return
        res = json.loads(self.rec_cmd.Result())
        words = res.get("result", [])
        text = res.get("text", "")
        tokens = text.split()
        if not tokens:
            return
        # wake words are forgiving: any utterance containing them counts
        # ("I have a question" works). Drive words stay strict single-word.
        if WAKE_WORD in tokens:
            self.answer_question()
            return
        if EXPERIMENT_WORD in tokens:
            self.run_experiment()
            return
        if not (len(words) == 1 and words[0].get("conf", 0) >= MIN_CONF):
            print(f"(heard {text!r} - say a drive word alone, "
                  f"or '{WAKE_WORD}' to ask)")
            return
        if text in COMMANDS and time.time() - self.last_cmd > COOLDOWN_S:
            print(f">>> {text}")
            self.send(COMMANDS[text])
            self.last_cmd = time.time()

    # ---- ask mode ----
    def listen_question(self):
        print("  listening for the question...")
        deadline = time.time() + 15
        while time.time() < deadline:
            if self.rec_ask.AcceptWaveform(self.audio_q.get()):
                text = json.loads(self.rec_ask.Result()).get("text", "")
                if text.strip():
                    return text.strip()
        return ""

    def answer_question(self):
        self.face_text("Ask me!")
        self.drain_audio()
        question = self.listen_question()
        if not question:
            print("  (heard nothing)")
            self.face_text(":|")
            return
        print(f"  Q: {question}")
        self.face_text("Hmm...")
        reply = self.claude.messages.create(
            model=MODEL_ID, max_tokens=200, system=SYSTEM_BASE,
            messages=[{"role": "user", "content": question}],
        )
        answer = reply.content[0].text.strip()
        print(f"  A: {answer}")

        speaker = None
        if not self.args.no_speak:
            speaker = subprocess.Popen(["say", "-r", "175", answer])
        for page in pages_of(answer):
            print(f"  [face] {page}")
            self.face_text(page)
            time.sleep(PAGE_SECS)
        if speaker:
            speaker.wait()
        # discard everything the mic heard while Curio was talking,
        # or the robot obeys its own voice
        self.drain_audio()
        self.rec_cmd.Reset()

    def narrate(self, text):
        """Speak a line aloud while paging it across the face."""
        print(f"  Curio: {text}")
        speaker = None
        if not self.args.no_speak:
            speaker = subprocess.Popen(["say", "-r", "175", text])
        for page in pages_of(text):
            self.face_text(page)
            time.sleep(PAGE_SECS)
        if speaker:
            speaker.wait()
        self.drain_audio()

    def read_light(self):
        """Ask the board for one LDR reading; code measures, never the LLM."""
        if not self.ser:
            return 0
        self.ser.reset_input_buffer()
        self.ser.write(b"g")
        deadline = time.time() + 2
        buf = b""
        while time.time() < deadline:
            buf += self.ser.read(100)
            if b"LIGHT:" in buf and b"\n" in buf[buf.index(b"LIGHT:"):]:
                line = buf[buf.index(b"LIGHT:"):].split(b"\n")[0]
                return int(line.split(b":")[1])
        return -1

    def listen_prediction(self):
        self.rec_predict.Reset()
        deadline = time.time() + 12
        while time.time() < deadline:
            if self.rec_predict.AcceptWaveform(self.audio_q.get()):
                text = json.loads(self.rec_predict.Result()).get("text", "")
                if text in ("bright", "dark"):
                    return text
        return ""

    def _capture_rms(self, seconds):
        """Measure sound level from the live mic stream. Code, not the LLM."""
        import math
        import struct
        self.drain_audio()
        data = b""
        deadline = time.time() + seconds
        while time.time() < deadline:
            try:
                data += self.audio_q.get(timeout=1)
            except queue.Empty:
                break
        n = len(data) // 2
        if not n:
            return 0
        samples = struct.unpack(f"<{n}h", data[:n * 2])
        return int(math.sqrt(sum(x * x for x in samples) / n))

    def listen_yesno(self):
        self.rec_yesno.Reset()
        deadline = time.time() + 12
        while time.time() < deadline:
            if self.rec_yesno.AcceptWaveform(self.audio_q.get()):
                text = json.loads(self.rec_yesno.Result()).get("text", "")
                if text in ("yes", "no"):
                    return text
        return ""

    # ---- sound experiment: works with mic only, no extra sensors ----
    def run_sound_experiment(self):
        self.narrate("Sound hunt! Do you think your loudest clap can be ten "
                     "times louder than this quiet room? Say yes or no!")
        prediction = self.listen_yesno()
        if not prediction:
            self.narrate("We can try again later!")
            return
        print(f"  prediction: {prediction}")
        self.narrate("First, everyone quiet! I am listening to the room.")
        quiet = self._capture_rms(2.5)
        print(f"  quiet room level: {quiet}")
        self.narrate("Now clap as loud as you can!")
        loud = self._capture_rms(2.5)
        print(f"  clap level: {loud}")

        ratio = round(loud / max(quiet, 1), 1)
        facts = (
            f"We ran the sound experiment. The child predicted '{prediction}' "
            f"to whether a clap could be ten times louder than the quiet room. "
            f"Measured quiet room level: {quiet}. Measured clap level: {loud}. "
            f"That is {ratio} times louder. Narrate the result honestly, "
            f"celebrate the child's scientific thinking whether or not the "
            f"prediction was right, and explain simply that louder sounds "
            f"are bigger vibrations of the air."
        )
        reply = self.claude.messages.create(
            model=MODEL_ID, max_tokens=200, system=SYSTEM_BASE,
            messages=[{"role": "user", "content": facts}],
        )
        self.face_text(f"{quiet} -> {loud} ({ratio}x)")
        time.sleep(2)
        self.narrate(reply.content[0].text.strip())
        self.rec_cmd.Reset()

    # ---- experiment mode: predict -> measure -> explain ----
    def run_experiment(self):
        # dead light sensor? fall back to the sound experiment automatically
        if self.read_light() < 15:
            print("  (light sensor reads ~0 - running sound experiment)")
            self.run_sound_experiment()
            return
        self.narrate("Light hunt! Will my light sensor see more light "
                     "out here in the open, or under your cupped hands? "
                     "Say bright or dark!")
        prediction = self.listen_prediction()
        if not prediction:
            self.narrate("We can try again later!")
            return
        print(f"  prediction: {prediction} (child thinks covered = {prediction})")

        reading_a = self.read_light()
        print(f"  baseline light: {reading_a}")
        self.narrate("Now cover my light sensor with your hands!")
        for n in ("3", "2", "1"):
            self.face_text(n)
            time.sleep(1)
        reading_b = self.read_light()
        print(f"  covered light: {reading_b}")

        # code computes the result - the LLM only narrates it
        facts = (
            f"We ran the light experiment. The child predicted it would be "
            f"'{prediction}' under their hands. Sensor reading in the open: "
            f"{reading_a}. Sensor reading covered by hands: {reading_b}. "
            f"Higher numbers mean more light. Narrate the result honestly and "
            f"celebrate the child's scientific thinking whether or not the "
            f"prediction was right. Explain simply why hands block light."
        )
        reply = self.claude.messages.create(
            model=MODEL_ID, max_tokens=200, system=SYSTEM_BASE,
            messages=[{"role": "user", "content": facts}],
        )
        self.face_text(f"{reading_a} -> {reading_b}")
        time.sleep(2)
        self.narrate(reply.content[0].text.strip())
        self.rec_cmd.Reset()

    # ---- audio sources ----
    def _robot_mic(self):
        """Pull the robot's mic stream (port 3334) into the audio queue."""
        while True:
            try:
                sock = socket.create_connection((self.args.host, 3334),
                                                timeout=8)
                print("ears: robot mic connected")
                while True:
                    data = sock.recv(8000)
                    if not data:
                        raise ConnectionError("audio stream closed")
                    self.audio_q.put(data)
            except OSError as e:
                print(f"ears: reconnecting ({e})")
                time.sleep(2)

    def _brain_loop(self):
        print(f"Drive: {', '.join(COMMANDS)}.  Ask: say '{WAKE_WORD}' first.")
        last_fb = 0.0
        while True:
            self.handle_drive(self.audio_q.get())
            # if recognition fell behind real time, jump back to live audio
            if self.audio_q.qsize() > 40:
                self.drain_audio()
                self.rec_cmd.Reset()
                print("(lagged - skipped to live audio)")
            # poll robot text feedback sparingly; polling every chunk was
            # what made recognition lag further behind every second
            if self.ser and time.time() - last_fb > 0.3:
                last_fb = time.time()
                fb = self.ser.read(200)
                if fb:
                    print(fb.decode(errors="replace"), end="", flush=True)

    # ---- main loop ----
    def run(self):
        if self.args.host and not self.args.local_mic:
            print("Curio is awake - ears on the ROBOT. Speak to the robot!")
            threading.Thread(target=self._robot_mic, daemon=True).start()
            self._brain_loop()
        else:
            def cb(indata, frames, t, status):
                self.audio_q.put(bytes(indata))

            with sd.RawInputStream(samplerate=16000, blocksize=4000,
                                   dtype="int16", channels=1, callback=cb,
                                   device=self.args.device):
                print("Curio is awake - ears on the laptop.")
                self._brain_loop()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default="/dev/cu.usbmodem2101")
    ap.add_argument("--host", default=None,
                    help="connect over wifi instead of USB, e.g. curio.local")
    ap.add_argument("--local-mic", action="store_true",
                    help="use the laptop mic even in wifi mode")
    ap.add_argument("--device", type=int, default=None)
    ap.add_argument("--model", default="vosk-model-small-en-us-0.15")
    ap.add_argument("--list-devices", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-speak", action="store_true")
    args = ap.parse_args()
    if args.list_devices:
        print(sd.query_devices())
        return
    Curio(args).run()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nbye")
