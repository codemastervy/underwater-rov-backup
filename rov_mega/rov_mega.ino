/*
  ROV BACKUP - Arduino Mega 2560 motor bridge
  -------------------------------------------
  The laptop (rov.py) reads the game controller and sends frames over USB serial.
  This sketch only sets the BTS7960 pins - all the driving logic stays on the laptop.

  Frame (one line, 115200 baud):
      F <enable_pin> <0|1> <pin>:<duty 0-255> <pin>:<duty 0-255> ...\n
  e.g. F 22 1 2:128 3:0 5:0 6:0 7:0 8:0 11:0 12:0

  Arm sound (the motors "sing", like a 3D printer):
      T <enable_pin> <0|1> <freq Hz> <pin>:<duty 0-255> ...\n
  Same as F, but timers 1/3/4 (pins 2,3,5,6,7,8,11,12) run at <freq> so the motors whistle
  at that note. The next F frame (or the 300 ms timeout) puts the timers back to normal.

  SAFETY
   - If no valid frame arrives for 300 ms (laptop crashed, USB unplugged, program closed)
     every motor pin goes to 0 and the enable pin goes LOW.
   - At power-up the enable pin (22) is held LOW until the laptop says otherwise.
     Also fit a 10k resistor from pin 22 to GND so it stays low while the Mega boots.
   - Only real PWM pins are accepted for motors; malformed frames are ignored.

  Replies: "ROV-MEGA READY" at boot, "OK" every 10 frames, "TIMEOUT" when the link drops,
           "ERR ..." for a rejected frame. The built-in LED is on while motors are enabled.
*/

const unsigned long TIMEOUT_MS = 300;
const uint8_t DEFAULT_ENABLE_PIN = 22;

const uint8_t PWM_PINS[] = {2, 3, 5, 6, 7, 8, 9, 10, 11, 12, 44, 45, 46};  // not 4/13 (timer0 = millis)
const uint8_t MAX_PINS = 16;

uint8_t usedPins[MAX_PINS];
uint8_t nUsed = 0;
int enablePin = DEFAULT_ENABLE_PIN;

char buf[160];
uint8_t len = 0;
unsigned long lastFrame = 0;
bool live = false;
unsigned int frameCount = 0;
unsigned int toneFreq = 0;      // 0 = normal motor PWM

// ---- timers 1/3/4: normal (8-bit phase-correct, /8 = 3.9 kHz) or tone (fast PWM, TOP = ICR) ----
void timersNormal() {
  TCCR1A = _BV(WGM10); TCCR1B = _BV(CS11); TCNT1 = 0;
  TCCR3A = _BV(WGM30); TCCR3B = _BV(CS31); TCNT3 = 0;
  TCCR4A = _BV(WGM40); TCCR4B = _BV(CS41); TCNT4 = 0;
}

void timersTone(unsigned int freq) {
  uint16_t top = (uint16_t)(2000000UL / freq - 1);     // 16 MHz / 8 = 2 MHz
  TCCR1A = _BV(WGM11); TCCR1B = _BV(WGM13) | _BV(WGM12) | _BV(CS11); ICR1 = top; TCNT1 = 0;
  TCCR3A = _BV(WGM31); TCCR3B = _BV(WGM33) | _BV(WGM32) | _BV(CS31); ICR3 = top; TCNT3 = 0;
  TCCR4A = _BV(WGM41); TCCR4B = _BV(WGM43) | _BV(WGM42) | _BV(CS41); ICR4 = top; TCNT4 = 0;
}

volatile uint16_t *ocrFor(int p, uint16_t *top) {
  switch (p) {
    case 11: *top = ICR1; return &OCR1A;
    case 12: *top = ICR1; return &OCR1B;
    case 5:  *top = ICR3; return &OCR3A;
    case 2:  *top = ICR3; return &OCR3B;
    case 3:  *top = ICR3; return &OCR3C;
    case 6:  *top = ICR4; return &OCR4A;
    case 7:  *top = ICR4; return &OCR4B;
    case 8:  *top = ICR4; return &OCR4C;
  }
  return 0;
}

// duty 0-255 on pin p, in whichever timer mode is active
void setDuty(int p, int d) {
  if (toneFreq == 0 || d == 0) { analogWrite(p, d); return; }
  uint16_t top;
  volatile uint16_t *ocr = ocrFor(p, &top);
  if (!ocr) { analogWrite(p, 0); return; }    // pin not on timer 1/3/4: stays quiet
  analogWrite(p, 1);                            // connects the pin to its timer output
  *ocr = (uint16_t)((uint32_t)top * d / 255);
}

bool isPwmPin(int p) {
  for (uint8_t i = 0; i < sizeof(PWM_PINS); i++)
    if (PWM_PINS[i] == p) return true;
  return false;
}

void rememberPin(uint8_t p) {
  for (uint8_t i = 0; i < nUsed; i++)
    if (usedPins[i] == p) return;
  if (nUsed < MAX_PINS) {
    usedPins[nUsed++] = p;
    pinMode(p, OUTPUT);
    analogWrite(p, 0);
  }
}

void allOff() {
  if (enablePin >= 0) digitalWrite(enablePin, LOW);   // cut the drivers first
  for (uint8_t i = 0; i < nUsed; i++) analogWrite(usedPins[i], 0);
  if (toneFreq) { toneFreq = 0; timersNormal(); }
  digitalWrite(LED_BUILTIN, LOW);
}

void handleFrame(char *line) {
  if (strcmp(line, "PING") == 0) { Serial.println("PONG"); return; }
  if (line[0] != 'F' && line[0] != 'T') return;
  bool isTone = line[0] == 'T';

  // pass 1: parse + validate everything before touching any pin
  int pins[MAX_PINS], duties[MAX_PINS], n = 0;
  char *tok = strtok(line + 1, " ");
  if (!tok) { Serial.println("ERR no enable pin"); return; }
  int en = atoi(tok);
  tok = strtok(NULL, " ");
  if (!tok) { Serial.println("ERR no enable value"); return; }
  int enVal = atoi(tok);
  if (en < 22 || en > 53 || (enVal != 0 && enVal != 1)) { Serial.println("ERR bad enable"); return; }
  long freq = 0;
  if (isTone) {
    tok = strtok(NULL, " ");
    freq = tok ? atol(tok) : 0;
    if (freq < 40 || freq > 10000) { Serial.println("ERR bad tone freq"); return; }
  }

  while ((tok = strtok(NULL, " ")) != NULL) {
    char *colon = strchr(tok, ':');
    if (!colon || n >= MAX_PINS) { Serial.println("ERR bad pin field"); return; }
    *colon = 0;
    int p = atoi(tok), d = atoi(colon + 1);
    if (!isPwmPin(p) || d < 0 || d > 255) { Serial.println("ERR bad pin/duty"); return; }
    pins[n] = p; duties[n] = d; n++;
  }

  // pass 2: apply
  if (en != enablePin) {          // laptop uses a different enable pin than the default
    digitalWrite(enablePin, LOW);
    enablePin = en;
    pinMode(enablePin, OUTPUT);
  }
  if (!enVal) digitalWrite(enablePin, LOW);           // disabling: enable off first
  unsigned int want = isTone ? (unsigned int)freq : 0;
  if (want != toneFreq) {                              // switch timer mode with every pin at 0
    for (uint8_t i = 0; i < nUsed; i++) analogWrite(usedPins[i], 0);
    toneFreq = want;
    if (want) timersTone(want); else timersNormal();
  }
  for (int i = 0; i < n; i++) {
    rememberPin(pins[i]);
    setDuty(pins[i], duties[i]);
  }
  if (enVal) digitalWrite(enablePin, HIGH);           // enabling: duties set first
  digitalWrite(LED_BUILTIN, enVal ? HIGH : LOW);

  lastFrame = millis();
  live = true;
  if (++frameCount % 10 == 0) Serial.println("OK");
}

void setup() {
  pinMode(DEFAULT_ENABLE_PIN, OUTPUT);
  digitalWrite(DEFAULT_ENABLE_PIN, LOW);
  pinMode(LED_BUILTIN, OUTPUT);
  digitalWrite(LED_BUILTIN, LOW);

  // Raise PWM on timers 1, 3, 4 (pins 2,3,5,6,7,8,11,12) from ~490 Hz to ~3.9 kHz: quieter motors.
  timersNormal();

  Serial.begin(115200);
  Serial.println("ROV-MEGA READY");
}

void loop() {
  while (Serial.available()) {
    char ch = Serial.read();
    if (ch == '\n' || ch == '\r') {
      if (len > 0) {
        buf[len] = 0;
        handleFrame(buf);
        len = 0;
      }
    } else if (len < sizeof(buf) - 1) {
      buf[len++] = ch;
    } else {
      len = 0;  // line too long: drop it
    }
  }
  if (live && millis() - lastFrame > TIMEOUT_MS) {
    allOff();
    live = false;
    Serial.println("TIMEOUT");
  }
}
