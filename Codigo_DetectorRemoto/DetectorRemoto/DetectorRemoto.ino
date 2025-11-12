// LIBRERIAS
#include <SPI.h>
#include <Wire.h>
#include <LoRa.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>
#include <TinyGPS++.h>

// CONFIGURACION LORA32
#define LORA_SCK  5
#define LORA_MISO 19
#define LORA_MOSI 27
#define LORA_SS   18
#define LORA_RST  14
#define LORA_DIO0 26

// CONFIGURACION OLED
#define SCREEN_WIDTH 128
#define SCREEN_HEIGHT 64
Adafruit_SSD1306 display(SCREEN_WIDTH, SCREEN_HEIGHT, &Wire, -1);

// CONFIGURACION GEIGER MULLER
#define GEIGER_PIN 34
#define LOG_PERIOD 15000
#define MAX_PERIOD 60000
unsigned long counts = 0, cpm = 0;
unsigned int multiplier = MAX_PERIOD / LOG_PERIOD;
unsigned long previousMillis = 0;

// CONFIGURACION GPS
#define RXD2 12
#define TXD2 13
#define GPS_BAUD 9600
TinyGPSPlus gps;
HardwareSerial gpsSerial(2);



// Contador de envios
unsigned int envioNum = 1;

// Dosis: 151 CPM = 1 uSv/h
const float CPM_PER_uSvPH = 512.0f;
double dosis_uSv = 0.0;
unsigned long lastDoseMillis = 0;


// CONFIGURACION ACK/CRC

static const uint16_t ACK_TIMEOUT_MS = 800;

// CRC16-CCITT (poly 0x1021, init 0xFFFF)
uint16_t crc16_ccitt(const uint8_t* data, size_t len) {
  uint16_t crc = 0xFFFF;
  for (size_t i = 0; i < len; i++) {
    crc ^= (uint16_t)data[i] << 8;
    for (uint8_t j = 0; j < 8; j++) {
      if (crc & 0x8000) crc = (uint16_t)((crc << 1) ^ 0x1021);
      else crc <<= 1;
    }
  }
  return crc;
}

// append ";CRC=XXXX" to payload (CRC over text before CRC)
String append_crc16_ccitt(const String& payload_without_crc) {
  uint16_t c = crc16_ccitt((const uint8_t*)payload_without_crc.c_str(), payload_without_crc.length());
  char buf[16];
  snprintf(buf, sizeof(buf), ";CRC=%04X", c);
  String out = payload_without_crc;
  out += String(buf);
  return out;
}

String twoDigits(uint16_t n){
  char b[4];
  n = n % 100;
  snprintf(b, sizeof(b), "%02u", n);
  return String(b);
}

bool waitForAck(uint16_t seq, uint16_t timeout_ms){
  // drenar paquetes residuales
  int ps;
  while ((ps = LoRa.parsePacket())) { while (LoRa.available()) LoRa.read(); }
  unsigned long t0 = millis();
  LoRa.receive();
  while (millis() - t0 < timeout_ms){
    int packetSize = LoRa.parsePacket();
    if (packetSize){
      String r = "";
      while (LoRa.available()) r += (char)LoRa.read();
      if (r.startsWith("ACK:")){
        int comma = r.indexOf(',');
        String seqStr = (comma > 4) ? r.substring(4, comma) : r.substring(4);
        int rx = seqStr.toInt();
        bool ok = (r.indexOf(",OK") >= 0);
        if (rx == (int)(seq % 100)) return ok;
      }
    }
    delay(1);
  }
  return false;
}


// CONFIGURACION COLA FIFO

static const uint8_t  QUEUE_MAX = 40;
String   q_msg[QUEUE_MAX];
uint16_t q_seq[QUEUE_MAX];
uint8_t  q_head = 0, q_tail = 0, q_size = 0;

bool q_enqueue(const String& msg, uint16_t seq){
  if (q_size >= QUEUE_MAX){
    // drop oldest
    q_head = (q_head + 1) % QUEUE_MAX;
    q_size--;
  }
  q_msg[q_tail] = msg;
  q_seq[q_tail] = seq;
  q_tail = (q_tail + 1) % QUEUE_MAX;
  q_size++;
  return true;
}

bool q_is_empty(){ return q_size == 0; }

void q_front(String& msg, uint16_t& seq){
  msg = q_msg[q_head];
  seq = q_seq[q_head];
}

void q_pop(){
  if (q_size == 0) return;
  q_head = (q_head + 1) % QUEUE_MAX;
  q_size--;
}

// slots de reintento tras un envio normal OK
const uint16_t SLOT_OFFSETS_MS[4] = { 3000, 6000, 9000, 12000 };
bool slot_used[4] = {false,false,false,false};
bool cycle_ok = false;
unsigned long cycle_start_ms = 0;

// record de ultima medicion para refrescar pantalla
String last_lat = "0.0", last_lon = "0.0", last_alt = "0.0", last_sat = "0";
unsigned long last_cpm = 0;
float last_tasa = 0.0f;

void IRAM_ATTR tube_impulse() { counts++; }

// Escribir datos en pantalla
void draw_screen_line1(bool ok_flag){
  display.clearDisplay();
  display.setCursor(0, 0);
  display.println(String("Envio N. ") + twoDigits(envioNum) + (ok_flag ? " OK " : " BAD ") + "[" + twoDigits((uint16_t)(q_size % 100)) + "]");
  display.println("Lat: " + last_lat);
  display.println("Lon: " + last_lon);
  display.println("Alt: " + last_alt + " m");
  display.println("Sat: " + last_sat);
  display.println("CPM: " + String(last_cpm));
  display.println("TdD: " + String(last_tasa, 4) + " uSv/h");
  display.println("Dos: " + String(dosis_uSv, 4) + " uSv");
  display.display();
}

// Formateo de fecha/hora GPS
String gps_date_str(){
  if (!gps.date.isValid()) return "0000-00-00";
  int y = gps.date.year();
  int m = gps.date.month();
  int d = gps.date.day();
  char b[11];
  snprintf(b, sizeof(b), "%04d-%02d-%02d", y, m, d);
  return String(b);
}

String gps_time_str(){
  if (!gps.time.isValid()) return "00:00:00";
  int hh = gps.time.hour();
  int mm = gps.time.minute();
  int ss = gps.time.second();
  char b[9];
  snprintf(b, sizeof(b), "%02d:%02d:%02d", hh, mm, ss);
  return String(b);
}


// DEBUG DE ARRANQUE
void draw_boot_status(bool ok_gm, bool ok_gps, bool ok_lora){
  display.clearDisplay();
  display.setCursor(0,0);
  display.println("DEBUG INICIAL");
  display.println(ok_gm  ? "GM: OK"      : "GM: esperando...");
  display.println(ok_gps ? "GPS: OK"     : "GPS: cargando...");
  display.println(ok_lora? "LORA: OK"    : "LORA: conectando...");
  display.display();
}

// linea de espera con contador: GPS: Wait(XXX s.)
void draw_boot_status_wait(uint16_t secs, bool ok_gm, bool ok_lora){
  display.clearDisplay();
  display.setCursor(0,0);
  display.println("DEBUG INICIAL");
  display.println(ok_gm  ? "GM: OK"      : "GM: esperando...");
  char line[24];
  snprintf(line, sizeof(line), "GPS: Wait(%3u s.)", (unsigned)secs);
  display.println(String(line));
  display.println(ok_lora? "LORA: OK"    : "LORA: conectando...");
  display.display();
}

bool wait_gm_ok(){
  unsigned long base = counts;
  while (true){
    draw_boot_status((counts - base) > 0, false, false);
    if ((counts - base) > 0) return true;
    delay(200);
  }
}

// 2 min de espera para conectar GPS o continúa igual
bool wait_gps_ok(){
  const unsigned long TMAX_MS = 120000UL;
  unsigned long t0 = millis();
  unsigned long lastLog = 0;

  while (true){
    while (gpsSerial.available()) gps.encode(gpsSerial.read());

    double lat = gps.location.lat();
    double lon = gps.location.lng();
    bool havePosNonZero = !(lat == 0.0 && lon == 0.0);
    if (havePosNonZero){
      draw_boot_status(true, true, false);
      return true;
    }

    unsigned long elapsed = millis() - t0;
    if (elapsed >= TMAX_MS){
      draw_boot_status(true, true, false);
      Serial.println("[GPS] timeout 120 s: continuando sin fix (lat/lon==0.0)");
      return true;
    }

    uint16_t secs = (uint16_t)(elapsed / 1000UL);
    draw_boot_status_wait(secs, true, false);

    if (elapsed - lastLog > 2000){
      lastLog = elapsed;
      Serial.print("[GPS] wait "); Serial.print(secs); Serial.print(" s, chars=");
      Serial.print(gps.charsProcessed());
      Serial.print(" sats=");
      if (gps.satellites.isValid()) Serial.println((int)gps.satellites.value());
      else Serial.println(-1);
    }

    delay(150);
  }
}

// HandShake LoRa
bool wait_lora_handshake_ok(){
  const uint16_t seq0 = 0;
  while (true){
    String hello = "HELLO,SEQ=" + twoDigits(seq0);
    LoRa.idle();
    LoRa.beginPacket();
    LoRa.print(hello);
    LoRa.endPacket();
    bool ok = waitForAck(seq0, 800);
    draw_boot_status(true, true, ok);
    if (ok) return true;
    delay(500);
  }
}

// Bucle de configuraciones
void setup() {
  Serial.begin(115200);
  gpsSerial.begin(GPS_BAUD, SERIAL_8N1, RXD2, TXD2);
  pinMode(GEIGER_PIN, INPUT);
  attachInterrupt(GEIGER_PIN, tube_impulse, FALLING);

  if (!display.begin(SSD1306_SWITCHCAPVCC, 0x3C)) { while (1); }
  display.clearDisplay();
  display.setTextSize(1);
  display.setTextColor(WHITE);
  display.setCursor(0, 0);
  display.println("Iniciando LoRa...");
  display.display();

  SPI.begin(LORA_SCK, LORA_MISO, LORA_MOSI, LORA_SS);
  LoRa.setPins(LORA_SS, LORA_RST, LORA_DIO0);

  if (!LoRa.begin(915E6)) {
    display.clearDisplay();
    display.setCursor(0, 0);
    display.println("Fallo LoRa");
    display.display();
    while (1);
  }
  LoRa.setTxPower(20, PA_OUTPUT_PA_BOOST_PIN);
  LoRa.enableCrc();

  // debug inicial bloqueante: GM -> GPS -> LoRa
  draw_boot_status(false, false, false);
  wait_gm_ok();
  wait_gps_ok();
  wait_lora_handshake_ok();

  display.clearDisplay();
  display.setCursor(0, 0);
  display.println("LoRa listo!");
  display.display();

  lastDoseMillis = millis();
  previousMillis = millis();
}

// Envio de datos encolados
void try_send_queue_slots(){
  if (!cycle_ok || q_is_empty()) return;
  unsigned long now = millis();
  for (uint8_t i = 0; i < 4; i++){
    if (slot_used[i]) continue;
    unsigned long slot_time = cycle_start_ms + SLOT_OFFSETS_MS[i];
    if (now >= slot_time){
      if (q_is_empty()) { slot_used[i] = true; continue; }

      String msg; uint16_t seq;
      q_front(msg, seq);

      LoRa.idle();
      LoRa.beginPacket();
      LoRa.print(msg);
      LoRa.endPacket();

      bool ok = waitForAck(seq, 600);
      if (ok){
        q_pop();
        draw_screen_line1(true);
      }
      slot_used[i] = true;
    }
  }
}

void loop() {
  while (gpsSerial.available()) { gps.encode(gpsSerial.read()); }

  // reintentos programados en cola
  try_send_queue_slots();

  unsigned long currentMillis = millis();
  if (currentMillis - previousMillis > LOG_PERIOD) {
    unsigned long dt_ms = currentMillis - lastDoseMillis;
    lastDoseMillis = currentMillis;

    previousMillis = currentMillis;
    cpm = counts * multiplier;
    counts = 0;

    String lat = "0.0";
    String lon = "0.0";
    String alt = "0.0";
    String sat = "0";
    bool gps_ok = false;

    if (gps.location.isValid()) {
      lat = String(gps.location.lat(), 6);
      lon = String(gps.location.lng(), 6);
      alt = String(gps.altitude.meters());
      sat = String(gps.satellites.value());
      // gps_ok si lat/lon no son cero
      gps_ok = !(lat == "0.000000" && lon == "0.000000");
      if (lat == "0.0" && lon == "0.0") gps_ok = false;
    }

    float tasa_uSv_h = (float)cpm / CPM_PER_uSvPH;
    double dt_h = (double)dt_ms / 3600000.0;
    dosis_uSv += (double)tasa_uSv_h * dt_h;

    // guardar ultimos para pantalla
    last_lat = lat; last_lon = lon; last_alt = alt; last_sat = sat;
    last_cpm = cpm; last_tasa = tasa_uSv_h;

    // fecha y hora GPS
    String fstr = gps_date_str();
    String tstr = gps_time_str();

    // payload con DATE/TIME antes de SEQ y CRC
    String core = "todo:" + lat + "," + lon + "," + String(cpm) + "," + alt + "," + sat;
    core += ",DATE=" + fstr + ",TIME=" + tstr;
    core += ",SEQ=" + twoDigits(envioNum);
    String mensaje = append_crc16_ccitt(core);

    // enviar
    LoRa.idle();
    LoRa.beginPacket();
    LoRa.print(mensaje);
    LoRa.endPacket();

    bool ok = waitForAck(envioNum, ACK_TIMEOUT_MS);
    if (!ok) {
      // encolar solo si hay posicion valida (no 0.0)
      if (gps_ok) {
        q_enqueue(mensaje, envioNum);
      }
    }

    // preparar slots de reintento si hubo OK
    if (ok){
      cycle_ok = true;
      cycle_start_ms = currentMillis;
      for (uint8_t i=0;i<4;i++) slot_used[i] = false;
    } else {
      cycle_ok = false;
    }

    // pantalla
    draw_screen_line1(ok);

    Serial.println("Enviado: " + mensaje);
    envioNum++;
    if (envioNum > 99) envioNum = 1;
  }
}
