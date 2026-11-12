/*
  ROV BACKUP - Arduino Mega 2560 motor bridge
  -------------------------------------------
  The laptop (rov.py) reads the game controller and sends frames over USB serial.
  This sketch only sets the BTS7960 pins - all the driving logic stays on the laptop.

  Frame (one line, 115200 baud):
      F <enable_pin> <0|1> <pin>:<duty 0-255> <pin>:<duty 0-255> ...\n
  e.g. F 22 1 2:128 3:0 5:0 6:0 7:0 8:0 11:0 12:0

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
  digitalWrite(LED_BUILTIN, LOW);
}

void handleFrame(char *line) {
  if (strcmp(line, "PING") == 0) { Serial.println("PONG"); return; }
  if (line[0] != 'F') return;

  // pass 1: parse + validate everything before touching any pin
  int pins[MAX_PINS], duties[MAX_PINS], n = 0;
  char *tok = strtok(line + 1, " ");
  if (!tok) { Serial.println("ERR no enable pin"); return; }
  int en = atoi(tok);
  tok = strtok(NULL, " ");
  if (!tok) { Serial.println("ERR no enable value"); return; }
  int enVal = atoi(tok);
  if (en < 22 || en > 53 || (enVal != 0 && enVal != 1)) { Serial.println("ERR bad enable"); return; }

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
  for (int i = 0; i < n; i++) {
    rememberPin(pins[i]);
    analogWrite(pins[i], duties[i]);
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
  TCCR1B = (TCCR1B & 0b11111000) | 0x02;
  TCCR3B = (TCCR3B & 0b11111000) | 0x02;
  TCCR4B = (TCCR4B & 0b11111000) | 0x02;

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
