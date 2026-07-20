// robot_main — motors + face + LDR + command server over USB serial AND WiFi.
// The laptop brain (curio_main.py) connects either via the USB cable or,
// untethered, via TCP to curio.local:3333 on the phone hotspot.
//
// Commands (same protocol on both transports):
//   f=forward burst  b=back burst  l=pivot left  r=pivot right  s=stop
//   +/- = fwd time +/-50ms   [/] = turn time +/-25ms
//   1-9,0 = duty 10..100%    ? = settings   g = LDR light reading
//   t<text>\n = show text on the face for 3 s
//
// Pins: motors A1=22 A2=0 ENA=6 B1=15 B2=14 ENB=9 | OLED SDA=4 SCL=5 | LDR=1
#include <Wire.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>
#include <WiFi.h>
#include <ESPmDNS.h>
#include "ESP_I2S.h"

// ==== FILL THESE IN (phone hotspot; must be 2.4 GHz) ====
#define WIFI_SSID "Kriti’s iPhone"  // iPhone SSIDs use a CURLY apostrophe
#define WIFI_PASS "kreetos2708"

#define PIN_A1  22  // moved off GPIO 1 to free the ADC pin for the LDR
#define PIN_A2  0
#define PIN_ENA 6
#define PIN_B1  15
#define PIN_B2  14
#define PIN_ENB 9
#define PIN_SDA 4
#define PIN_SCL 5
#define PIN_MIC_SCK 17  // T5848 I2S mic
#define PIN_MIC_WS  3
#define PIN_MIC_SD  16
#define PIN_STEER   1   // analog steering input (LDR retired; GPIO2 is not
                        // broken out on the Glyph, and only GPIO 0-6 have
                        // an ADC). Source MUST share GND with us.
                        // right: 2.5-3.3V | forward: 1.2-2.0V | left: 0-0.8V

#define INVERT_A false
#define INVERT_B true
#define A_IS_LEFT true

Adafruit_SSD1306 display(128, 32, &Wire, -1);
WiFiServer server(3333);        // commands
WiFiServer audioServer(3334);   // raw mic stream: 16 kHz 16-bit mono PCM
WiFiClient client;
WiFiClient audioClient;
I2SClass i2s;
bool micOK = false;
Stream *io = &Serial;  // whoever sent the current command gets the replies

int dutyPct = 80;
int runMs   = 400;
int turnMs  = 300;
bool autoSteer = false;
bool calMode = false;   // live steering-voltage readout on the face

unsigned long nextBlink = 0;
unsigned long blinkUntil = 0;
unsigned long textUntil = 0;

// ---------- face ----------
void drawEyes(bool open, int dx, int h) {
  display.clearDisplay();
  int y = 4 + (24 - h) / 2;
  if (open) {
    display.fillRoundRect(30, y, 24, h, min(8, h / 2), SSD1306_WHITE);
    display.fillRoundRect(74, y, 24, h, min(8, h / 2), SSD1306_WHITE);
    if (h > 14) {
      display.fillRect(38 + dx, y + h / 2 - 4, 8, 8, SSD1306_BLACK);
      display.fillRect(82 + dx, y + h / 2 - 4, 8, 8, SSD1306_BLACK);
    }
  } else {
    display.fillRect(30, 14, 24, 4, SSD1306_WHITE);
    display.fillRect(74, 14, 24, 4, SSD1306_WHITE);
  }
  display.display();
}

void faceIdle()   { drawEyes(true, 0, 24); }
void faceLeft()   { drawEyes(true, -8, 24); }
void faceRight()  { drawEyes(true, 8, 24); }
void faceWide()   { drawEyes(true, 0, 28); }
void faceSquint() { drawEyes(true, 0, 12); }

void faceText(const String &msg) {
  display.clearDisplay();
  display.setTextSize(1);
  display.setTextColor(SSD1306_WHITE);
  display.setCursor(0, 8);
  display.println(msg);
  display.display();
  textUntil = millis() + 3000;
}

// ---------- motors ----------
void setSpeed(int pct) {
  ledcWrite(PIN_ENA, pct * 255 / 100);
  ledcWrite(PIN_ENB, pct * 255 / 100);
}

void stopMotors() {
  digitalWrite(PIN_A1, LOW); digitalWrite(PIN_A2, LOW);
  digitalWrite(PIN_B1, LOW); digitalWrite(PIN_B2, LOW);
  setSpeed(0);
}

void setMotorA(bool fwd) {
  bool a = fwd ^ INVERT_A;
  digitalWrite(PIN_A1, a ? HIGH : LOW);
  digitalWrite(PIN_A2, a ? LOW : HIGH);
}

void setMotorB(bool fwd) {
  bool b = fwd ^ INVERT_B;
  digitalWrite(PIN_B1, b ? HIGH : LOW);
  digitalWrite(PIN_B2, b ? LOW : HIGH);
}

void burst(bool forward) {
  if (forward) faceWide(); else faceSquint();
  setMotorA(forward); setMotorB(forward);
  setSpeed(dutyPct);
  delay(runMs);
  stopMotors();
  faceIdle();
  io->printf("burst done: %d ms at %d%%\n", runMs, dutyPct);
}

// ---- auto-steer: analog voltage on PIN_STEER drives micro-moves ----
void steerStep(bool left) {
  bool aFwd = A_IS_LEFT ? !left : left;
  setMotorA(aFwd); setMotorB(!aFwd);
  setSpeed(dutyPct);
  delay(max(10, turnMs / 12));  // 7.5 degrees = 1/12 of the 90-degree time
  stopMotors();
}

void forwardStep() {
  setMotorA(true); setMotorB(true);
  setSpeed(dutyPct);
  delay(80);
  stopMotors();
}

void autoSteerTick() {
  int mv = analogReadMilliVolts(PIN_STEER);  // factory-calibrated millivolts
  if (mv >= 2500)                steerStep(false);  // right: 2.5-3.3V
  else if (mv <= 800)            steerStep(true);   // left:  0-0.8V
  else if (mv >= 1200 && mv <= 2000) forwardStep(); // forward: 1.2-2.0V
  else                           stopMotors();      // between bands: hold
}

void turnBurst(bool left) {
  if (left) faceLeft(); else faceRight();
  bool aFwd = A_IS_LEFT ? !left : left;
  setMotorA(aFwd); setMotorB(!aFwd);
  setSpeed(dutyPct);
  delay(turnMs);
  stopMotors();
  faceIdle();
  io->printf("turn done: %d ms\n", turnMs);
}

void printSettings() {
  io->printf("runMs=%d turnMs=%d dutyPct=%d\n", runMs, turnMs, dutyPct);
}

// ---------- command handling (shared by serial + wifi) ----------
void handleChar(char c, Stream &src) {
  io = &src;
  switch (c) {
    case 'f': burst(true);  break;
    case 'b': burst(false); break;
    case 'l': turnBurst(true);  break;
    case 'r': turnBurst(false); break;
    case 's': stopMotors(); faceIdle(); io->println("stopped"); break;
    case '+': runMs += 50; printSettings(); break;
    case '-': runMs = max(50, runMs - 50); printSettings(); break;
    case '[': turnMs = max(50, turnMs - 25); printSettings(); break;
    case ']': turnMs += 25; printSettings(); break;
    case '?': printSettings(); break;
    case 'g': io->println("LIGHT:0"); break;  // LDR retired; GPIO1 = steering
    case 'a': autoSteer = true;  faceWide();
              io->println("auto-steer ON");  break;
    case 'm': autoSteer = false; stopMotors(); faceIdle();
              io->println("auto-steer OFF"); break;
    case 'v': io->printf("STEER:%dmV\n", analogReadMilliVolts(PIN_STEER)); break;
    case 'c': calMode = !calMode;
              if (!calMode) faceIdle();
              io->printf("cal mode %s\n", calMode ? "ON" : "OFF"); break;
    case 't': {
      String msg = src.readStringUntil('\n');
      faceText(msg);
      break;
    }
    default:
      if (c >= '1' && c <= '9') { dutyPct = (c - '0') * 10; printSettings(); }
      else if (c == '0')        { dutyPct = 100;            printSettings(); }
  }
}

// ---------- setup / loop ----------
void setup() {
  pinMode(PIN_A1, OUTPUT); pinMode(PIN_A2, OUTPUT);
  pinMode(PIN_B1, OUTPUT); pinMode(PIN_B2, OUTPUT);
  ledcAttach(PIN_ENA, 1000, 8);
  ledcAttach(PIN_ENB, 1000, 8);
  stopMotors();

  Serial.begin(115200);
  Wire.begin(PIN_SDA, PIN_SCL);
  Wire.setClock(400000);  // 4x faster screen redraws = shorter mic stalls
  bool oledOK = display.begin(SSD1306_SWITCHCAPVCC, 0x3C);
  delay(1000);

  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  if (oledOK) faceText("wifi...");
  unsigned long t0 = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - t0 < 12000) delay(250);

  // (mic LR is hard-wired to GND; stereo capture tolerates either channel)
  // PIN_STEER stays a plain ADC input - no pulls, they would distort the
  // steering source's voltage
  i2s.setPins(PIN_MIC_SCK, PIN_MIC_WS, -1, PIN_MIC_SD);
  // STEREO on purpose: we grab both slots and keep whichever channel the
  // mic actually answers on, so the L/R pin's wiring can't silence us.
  micOK = i2s.begin(I2S_MODE_STD, 16000, I2S_DATA_BIT_WIDTH_32BIT,
                    I2S_SLOT_MODE_STEREO);
  Serial.println(micOK ? "mic OK" : "mic init FAILED");

  if (WiFi.status() == WL_CONNECTED) {
    server.begin();
    audioServer.begin();
    MDNS.begin("curio");  // laptop connects to curio.local:3333
    String ip = WiFi.localIP().toString();
    Serial.printf("wifi up: %s (curio.local:3333)\n", ip.c_str());
    if (oledOK) { faceText("curio.local " + ip); delay(3000); }
  } else {
    Serial.println("wifi FAILED - serial only (check SSID/pass, 2.4 GHz)");
    if (oledOK) { faceText("no wifi :("); delay(1500); }
  }
  Serial.println("f/b/l/r/s  +/-  [/]  1-9,0  ?  g  t<text>");
  if (oledOK) faceIdle();
}

void loop() {
  // non-blocking blink: never stall the loop, the mic stream can't gap
  unsigned long now = millis();
  if (textUntil && now > textUntil) { textUntil = 0; faceIdle(); }
  if (!textUntil) {
    if (blinkUntil && now > blinkUntil) {
      faceIdle();
      blinkUntil = 0;
      nextBlink = now + 2200;
    } else if (!blinkUntil && now > nextBlink) {
      drawEyes(false, 0, 24);
      blinkUntil = now + 100;
    }
  }

  if (!client || !client.connected()) {
    WiFiClient next = server.available();
    if (next) { client = next; Serial.println("brain connected over wifi"); }
  }

  if (client && client.connected() && client.available()) {
    handleChar(client.read(), client);
  } else if (Serial.available()) {
    handleChar(Serial.read(), Serial);
  }

  // ---- auto-steer ----
  static unsigned long nextSteer = 0;
  if (autoSteer && now > nextSteer) {
    autoSteerTick();
    nextSteer = millis() + 30;
  }

  // ---- calibration: live mV + band on the face, 5x per second ----
  static unsigned long nextCal = 0;
  if (calMode && now > nextCal) {
    int mv = analogReadMilliVolts(PIN_STEER);
    const char *band = (mv >= 2500) ? "RIGHT"
                     : (mv >= 1200 && mv <= 2000) ? "FWD"
                     : (mv <= 800) ? "LEFT" : "hold";
    display.clearDisplay();
    display.setTextSize(2);
    display.setTextColor(SSD1306_WHITE);
    display.setCursor(0, 8);
    display.printf("%4dmV %s", mv, band);
    display.display();
    io->printf("STEER:%dmV %s\n", mv, band);
    nextCal = now + 200;
    textUntil = 0;  // suppress blink/idle repaint while calibrating
    nextBlink = now + 1000;
  }

  // ---- mic relay: ship audio to the laptop brain ----
  if (!audioClient || !audioClient.connected()) {
    WiFiClient next = audioServer.available();
    if (next) { audioClient = next; Serial.println("ears connected over wifi"); }
  }
  if (micOK && audioClient && audioClient.connected()) {
    static int32_t raw[256];   // stereo frames: L,R,L,R...
    static int16_t pcm[128];
    size_t n = i2s.readBytes((char *)raw, sizeof(raw));  // ~8 ms of audio
    int frames = n / 8;
    for (int i = 0; i < frames; i++) {
      // 24-bit sample in the top of the 32-bit slot; >>12 adds ~16x gain.
      // Keep whichever channel carries the signal.
      int32_t a = raw[2 * i] >> 12;
      int32_t b = raw[2 * i + 1] >> 12;
      int32_t s = (abs(a) >= abs(b)) ? a : b;
      if (s > 32767) s = 32767;
      if (s < -32768) s = -32768;
      pcm[i] = (int16_t)s;
    }
    if (frames > 0) audioClient.write((uint8_t *)pcm, frames * 2);
  }
}
