#!/usr/bin/env python3
# Interfaz_Lora_MQTT_ACK_CRC_v4.py
# Receptor con verificacion CRC16-CCITT y ACK:SEQ,OK/ERR
# - Compatible con paquete: todo:lat,lon,cpm,alt,sat,DATE=YYYY-MM-DD,TIME=HH:MM:SS,SEQ=NN;CRC=XXXX
# - Handshake de arranque: responde a "HELLO,SEQ=00" con "ACK:00,OK" (sin exigir CRC)
# - Publica a MQTT solo si CRC valido y prefijo "todo:"

import RPi.GPIO as GPIO
print("[DEBUG] Limpiando configuracion previa de GPIO...")
GPIO.cleanup()

from SX127x.LoRa import LoRa
from SX127x.board_config import BOARD
from SX127x.constants import MODE, BW
import time, re
import paho.mqtt.client as mqtt

# === MQTT ===
BROKER = "localhost"
TOPIC_DATOS = "dispositivos/ESP/datos"

client = mqtt.Client()
try:
    client.connect(BROKER, 1883, 60)
    print(f"[DEBUG] Conectado a MQTT broker en {BROKER}")
except Exception as e:
    print(f"[ERROR] No se pudo conectar a MQTT: {e}")

BOARD.setup()

contador_paquetes = 0

# ---- CRC16-CCITT helpers ----
def crc16_ccitt(data: bytes, poly: int = 0x1021, init_val: int = 0xFFFF) -> int:
    crc = init_val
    for b in data:
        crc ^= (b << 8) & 0xFFFF
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ poly) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc

def parse_seq_and_crc(payload_str: str):
    m_seq = re.search(r'(?:SEQ\s*[:=]\s*)([A-Fa-f0-9]+)', payload_str)
    seq = m_seq.group(1) if m_seq else None
    m_crc = re.search(r'(?:CRC\s*[:=]\s*)([A-Fa-f0-9]{4})', payload_str)
    rx_crc = m_crc.group(1).upper() if m_crc else None
    core = payload_str[:m_crc.start()].rstrip('; ') if m_crc else payload_str
    calc = f"{crc16_ccitt(core.encode('ascii', errors='ignore')):04X}"
    ok = (rx_crc is not None and rx_crc == calc)
    return core, seq, rx_crc, calc, ok

def build_ack(seq: str | None, ok: bool) -> str:
    s = seq if seq is not None else "NA"
    return f"ACK:{s},{'OK' if ok else 'ERR'}"

def parse_kv_pairs(text: str) -> dict:
    kv = {}
    for m in re.finditer(r'([A-Z]+)\s*=\s*([^,;]+)', text):
        kv[m.group(1).upper()] = m.group(2)
    return kv

class MyLoRa(LoRa):
    def __init__(self):
        super(MyLoRa, self).__init__()
        self.set_mode(MODE.SLEEP)
        self.set_dio_mapping([0,0,0,0,0,0])  # DIO0=RxDone

    def on_rx_done(self):
        global contador_paquetes
        contador_paquetes += 1
        self.set_mode(MODE.STDBY)
        self.clear_irq_flags(RxDone=1, ValidHeader=1, PayloadCrcError=1)

        payload = self.read_payload(nocheck=True)
        mensaje = bytes(payload).decode('utf-8', errors='ignore').strip()
        print(f"[LoRa] Mensaje recibido: {mensaje}")

        # Handshake HELLO sin CRC
        if mensaje.startswith("HELLO"):
            m_seq = re.search(r'(?:SEQ\s*[:=]\s*)([A-Fa-f0-9]+)', mensaje)
            seq = m_seq.group(1) if m_seq else "NA"
            ack = build_ack(seq, True)
            print(f"[DEBUG] Handshake HELLO detectado -> Enviando ACK -> {ack}")
            self._tx_ack_and_listen(ack)
            return

        # CRC
        core, seq, rx_crc, calc, ok = parse_seq_and_crc(mensaje)
        ack = build_ack(seq, ok)
        print(f"[DEBUG] CRC rx={rx_crc} calc={calc} -> {'OK' if ok else 'ERR'}")
        print(f"[DEBUG] Enviando ACK -> {ack}")
        self._tx_ack_and_listen(ack)

        # MQTT publish
        if ok and mensaje.startswith("todo:"):
            contenido = core.replace("todo:", "", 1)
            try:
                client.publish(TOPIC_DATOS, contenido)
                print(f"[MQTT] Publicado en {TOPIC_DATOS} : {contenido}")
            except Exception as e:
                print(f"[WARN] MQTT fallo publicando: {e}")

            partes = contenido.split(",")
            try:
                lat, lon, cpm, alt, sat = partes[:5]
            except Exception:
                lat = lon = cpm = alt = sat = "NA"

            kv = parse_kv_pairs(contenido)
            num = str(contador_paquetes).zfill(3)

            print(f"\n========== DATOS RECIBIDOS ({num}) ==========")
            print(f" Latitud     : {lat}")
            print(f" Longitud    : {lon}")
            print(f" CPM         : {cpm}")
            print(f" Altitud     : {alt} m")
            print(f" Satelites   : {sat}")
            if 'DATE' in kv: print(f" Fecha       : {kv.get('DATE')}")
            if 'TIME' in kv: print(f" Hora        : {kv.get('TIME')}")
            if 'SEQ'  in kv: print(f" SEQ         : {kv.get('SEQ')}")
            print("=============================================\n")
        else:
            if not ok:
                print("[WARN] CRC invalido, no se publica a MQTT")
            else:
                print("[WARN] Mensaje no reconocido, ignorado")

    def _tx_ack_and_listen(self, ack_str: str):
        self.set_dio_mapping([1,0,0,0,0,0])  # DIO0=TxDone
        self.clear_irq_flags(TxDone=1)
        self.write_payload(list(bytearray(ack_str, 'utf-8')))
        self.set_mode(MODE.TX)

    def on_tx_done(self):
        print("[DEBUG] ACK TX done")
        self.clear_irq_flags(TxDone=1)
        self.set_dio_mapping([0,0,0,0,0,0])  # DIO0=RxDone
        self.set_mode(MODE.RXCONT)

# Radio
lora = MyLoRa()
lora.set_mode(MODE.STDBY)
lora.set_freq(915.0)
lora.set_pa_config(pa_select=1, max_power=7, output_power=15)
lora.set_rx_crc(True)
lora.set_spreading_factor(7)
lora.set_coding_rate(5)
lora.set_bw(BW.BW125)
lora.set_mode(MODE.RXCONT)

print("[OK] Receptor LoRa SX127x iniciado y escuchando...")

try:
    while True:
        time.sleep(0.2)
except KeyboardInterrupt:
    print("\n[EXIT] KeyboardInterrupt")
finally:
    BOARD.teardown()
    print("[DEBUG] GPIO y SPI liberados correctamente")
