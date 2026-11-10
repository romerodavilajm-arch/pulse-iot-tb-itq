"""PulsePPG en la Raspberry Pi: captura una sesión y la publica en ThingsBoard.

Flujo de una sesión (una ejecución = una sesión):
  1. Genera un PIN aleatorio de 6 dígitos y lo muestra en el LCD.
  2. Espera a que la persona apoye el dedo en el MAX30102.
  3. Captura 5 minutos de señal (rojo + IR) a 50 muestras/s.
  4. Publica la sesión por MQTT (QoS 1) al dispositivo test-pi, troceada en
     mensajes de ~44 KB porque ThingsBoard no acepta mensajes MQTT grandes:
     primero las partes (`samples_part`) y al final la cabecera con pin,
     sample_count, started_at, finished_at y parts. La web las vuelve a unir.
  5. Deja el PIN en el LCD para que la persona lo teclee en la web.

Uso:
    python pulse.py                  # sesión real de 5 minutos
    python pulse.py --segundos 60    # sesión corta para probar
    python pulse.py --simular        # sin sensor: señal sintética (prueba ThingsBoard + web)
    python pulse.py --sin-enviar     # captura y guarda en sesiones/, no publica
    python pulse.py --reenviar sesiones/123456.json   # vuelve a publicar una sesión guardada

Configuración en .env (ver .env.example).
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import secrets
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent
SESSIONS_DIR = HERE / "sesiones"
TOPIC = "v1/devices/me/telemetry"
SAMPLE_RATE_HZ = 50
PERIOD_MS = 1000 // SAMPLE_RATE_HZ  # 20 ms entre muestras


# --------------------------------------------------------------------------- LCD


class Display:
    """LCD 16x2 por I2C (PCF8574). Si no hay LCD, escribe en la consola."""

    def __init__(self, address: int | None):
        self.lcd = None
        self.last = ("", "")
        if address is None:
            return
        try:
            from RPLCD.i2c import CharLCD

            self.lcd = CharLCD("PCF8574", address, port=1, cols=16, rows=2,
                               charmap="A00", auto_linebreaks=False)
            self.lcd.clear()
        except Exception as exc:  # sin LCD conectado, dirección errónea, etc.
            print(f"[LCD] no disponible ({exc}); sigo solo con la consola")
            self.lcd = None

    def show(self, line1: str, line2: str = "", echo: bool = True) -> None:
        line1, line2 = line1[:16], line2[:16]
        if (line1, line2) == self.last:
            return
        self.last = (line1, line2)
        if echo:
            print(f"[LCD] {line1:<16} | {line2}")
        if self.lcd:
            self.lcd.cursor_pos = (0, 0)
            self.lcd.write_string(line1.ljust(16))
            self.lcd.cursor_pos = (1, 0)
            self.lcd.write_string(line2.ljust(16))


# ------------------------------------------------------------------- captura


def mmss(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"


def wait_for_finger(sensor, display: Display, pin: str, ir_min: int) -> None:
    """Espera a que el IR medio de ~1 s supere `ir_min` (dedo apoyado)."""
    display.show(f"PIN: {pin}", "Pon tu dedo...")
    window: list[int] = []
    while True:
        samples, _ = sensor.read_fifo()
        window.extend(ir for _, ir in samples)
        window = window[-SAMPLE_RATE_HZ:]
        if len(window) == SAMPLE_RATE_HZ and sum(window) / len(window) >= ir_min:
            return
        time.sleep(0.05)


def capture(sensor, display: Display, pin: str, seconds: int) -> tuple[list[dict], int, int]:
    """Captura `seconds` segundos. Devuelve (samples, started_at, finished_at).

    `t` son milisegundos desde el inicio de la sesión, calculados con el
    índice de la muestra (el sensor muestrea a ritmo fijo de 50 Hz), así los
    intervalos son exactos aunque la Pi lea la FIFO a trompicones.
    """
    target = seconds * SAMPLE_RATE_HZ
    samples: list[dict] = []
    lost_total = 0
    sensor.clear_fifo()
    started_at = int(time.time() * 1000)
    start = time.monotonic()
    while len(samples) < target:
        batch, lost = sensor.read_fifo()
        lost_total += lost
        for red, ir in batch:
            if len(samples) >= target:
                break
            samples.append({"t": len(samples) * PERIOD_MS, "ir": ir, "red": red})
        left = seconds - (time.monotonic() - start)
        display.show(f"PIN: {pin}", f"Midiendo {mmss(left)}", echo=int(left) % 30 == 0)
        time.sleep(0.02)
    finished_at = int(time.time() * 1000)
    if lost_total:
        print(f"AVISO: el sensor descartó {lost_total} muestras (FIFO llena); "
              "los tiempos posteriores quedan desplazados")
    return samples, started_at, finished_at


def simulate(seconds: int) -> tuple[list[dict], int, int]:
    """Señal PPG sintética (~72 lpm con algo de variabilidad), sin esperar."""
    rng = random.Random()
    n = seconds * SAMPLE_RATE_HZ
    beats, t_beat = [], 300.0
    while t_beat < seconds * 1000 + 2000:
        beats.append(t_beat)
        t_beat += rng.gauss(830, 45)
    samples, b = [], 0
    for i in range(n):
        t = i * PERIOD_MS
        while b + 1 < len(beats) and beats[b + 1] <= t:
            b += 1
        dt = (t - beats[b]) / 1000
        pulse = math.exp(-((dt - 0.12) / 0.06) ** 2) + 0.3 * math.exp(-((dt - 0.38) / 0.07) ** 2)
        drift = 600 * math.sin(2 * math.pi * t / 9000)
        ir = 110_000 + drift - 900 * pulse + rng.gauss(0, 40)
        red = 90_000 + drift * 0.8 - 600 * pulse + rng.gauss(0, 40)
        samples.append({"t": t, "ir": int(ir), "red": int(red)})
    finished_at = int(time.time() * 1000)
    return samples, finished_at - seconds * 1000, finished_at


# ------------------------------------------------------------------- envío


def build_message(pin: str, samples: list[dict], started_at: int, finished_at: int) -> bytes:
    """Un solo mensaje con ts común. JSON compacto para que pese lo mínimo."""
    compact = (",", ":")
    message = {
        "ts": finished_at,
        "values": {
            "pin": pin,  # string: conserva los ceros a la izquierda
            "samples": json.dumps(samples, separators=compact),
            "sample_count": len(samples),
            "started_at": started_at,
            "finished_at": finished_at,
        },
    }
    return json.dumps(message, separators=compact).encode()


# Muestras por parte. Una muestra ocupa como mucho ~44 bytes dentro del
# mensaje, así que 1000 muestras son ~44 KB: por debajo de los 64 KB que
# ThingsBoard acepta por defecto en un mensaje MQTT.
PART_SAMPLES = 1000


def split_message(payload: bytes) -> list[bytes]:
    """Trocea la sesión en mensajes MQTT pequeños.

    - Parte i (i = 1..N): ts = ts_sesión + i, clave `samples_part` con
      1000 muestras (string JSON).
    - Cabecera, al final: ts = ts_sesión, claves pin, sample_count,
      started_at, finished_at y `parts` = N.
    La cabecera va la última para que la web no encuentre el PIN hasta que
    ThingsBoard haya confirmado todas las partes.
    """
    compact = (",", ":")
    message = json.loads(payload)
    ts, values = message["ts"], message["values"]
    samples = json.loads(values["samples"])
    chunks = [samples[i:i + PART_SAMPLES] for i in range(0, len(samples), PART_SAMPLES)]
    out = [
        json.dumps({"ts": ts + i, "values": {"samples_part": json.dumps(chunk, separators=compact)}},
                   separators=compact).encode()
        for i, chunk in enumerate(chunks, start=1)
    ]
    header = {k: v for k, v in values.items() if k != "samples"}
    header["parts"] = len(chunks)
    out.append(json.dumps({"ts": ts, "values": header}, separators=compact).encode())
    return out


def send_mqtt(host: str, port: int, token: str, messages: list[bytes], timeout: float = 30) -> bool:
    """Publica los mensajes en orden con QoS 1, esperando el PUBACK de cada uno."""
    import paho.mqtt.client as mqtt

    connected = threading.Event()
    lost = threading.Event()  # ThingsBoard cerró la conexión (p. ej. mensaje demasiado grande)
    refused: list[str] = []

    def on_connect(client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            refused.append(str(reason_code))
        connected.set()

    def on_disconnect(client, userdata, flags, reason_code, properties):
        lost.set()

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    client.username_pw_set(token)  # en ThingsBoard el usuario MQTT es el token del dispositivo
    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    try:
        client.connect(host, port, keepalive=60)
    except OSError as exc:
        print(f"MQTT: no se pudo conectar a {host}:{port} ({exc})")
        return False
    client.loop_start()
    try:
        if not connected.wait(15) or refused:
            print(f"MQTT: conexión rechazada ({refused[0] if refused else 'sin respuesta'}); "
                  "revisa TB_HOST y TB_DEVICE_TOKEN")
            return False
        for n, payload in enumerate(messages, start=1):
            info = client.publish(TOPIC, payload, qos=1)
            # Esperamos el PUBACK, pero si ThingsBoard corta la conexión no
            # tiene sentido seguir: paho reintentaría el mensaje una y otra vez.
            deadline = time.monotonic() + timeout
            while not info.is_published() and not lost.is_set() and time.monotonic() < deadline:
                time.sleep(0.05)
            if not info.is_published():
                print(f"MQTT: ThingsBoard no confirmó el mensaje {n}/{len(messages)} "
                      f"({len(payload) / 1024:.0f} KB); cerró la conexión o no respondió.")
                return False
            print(f"MQTT: mensaje {n}/{len(messages)} confirmado", end="\r")
        print()
        return True
    finally:
        client.disconnect()
        client.loop_stop()


def send_http(base_url: str, token: str, payload: bytes) -> bool:
    """Plan B: la API HTTP de dispositivos de ThingsBoard, la sesión entera en
    un solo mensaje (`samples` completo), que la web también entiende."""
    url = f"{base_url.rstrip('/')}/api/v1/{token}/telemetry"
    req = urllib.request.Request(url, data=payload, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return 200 <= resp.status < 300
    except (urllib.error.URLError, OSError) as exc:
        print(f"HTTP: fallo al publicar ({exc})")
        return False


def publish(payload: bytes) -> str | None:
    """Intenta MQTT y, si ThingsBoard no lo acepta, HTTP. Devuelve la vía usada."""
    host = os.environ.get("TB_HOST", "thingsboard.cloud")
    port = int(os.environ.get("TB_MQTT_PORT", "1883"))
    token = os.environ.get("TB_DEVICE_TOKEN", "")
    if not token or token.startswith("pega-aqui"):
        sys.exit("Falta TB_DEVICE_TOKEN en raspberrypi/.env")
    messages = split_message(payload)
    print(f"Sesión: {len(payload) / 1024:.0f} KB en {len(messages)} mensajes MQTT "
          f"(el mayor de {max(map(len, messages)) / 1024:.0f} KB)")
    if send_mqtt(host, port, token, messages):
        return "MQTT"
    if os.environ.get("HTTP_FALLBACK", "1") == "1":
        print("Reintentando por HTTP...")
        if send_http(os.environ.get("TB_HTTP_URL") or f"https://{host}", token, payload):
            return "HTTP"
    return None


def save(pin: str, payload: bytes) -> Path:
    SESSIONS_DIR.mkdir(exist_ok=True)
    path = SESSIONS_DIR / f"{pin}.json"
    path.write_bytes(payload)
    return path


# ------------------------------------------------------------------- main


def main() -> None:
    load_dotenv(HERE / ".env")
    parser = argparse.ArgumentParser(description="Sesión PulsePPG en la Raspberry Pi")
    parser.add_argument("--segundos", type=int,
                        default=int(os.environ.get("SESSION_SECONDS", "300")),
                        help="duración de la captura (por defecto 300 = 5 min)")
    parser.add_argument("--simular", action="store_true", help="sin sensor: señal sintética")
    parser.add_argument("--sin-enviar", action="store_true", help="solo guarda en sesiones/")
    parser.add_argument("--reenviar", metavar="ARCHIVO", help="publica una sesión guardada")
    args = parser.parse_args()

    if args.reenviar:
        via = publish(Path(args.reenviar).read_bytes())
        print(f"Publicado por {via}" if via else "No se pudo publicar")
        sys.exit(0 if via else 1)

    lcd_addr = os.environ.get("LCD_ADDRESS", "0x27")
    display = Display(None if lcd_addr.lower() in ("", "none") else int(lcd_addr, 16))
    pin = f"{secrets.randbelow(1_000_000):06d}"
    print(f"PIN de la sesión: {pin}")

    if args.simular:
        display.show(f"PIN: {pin}", "Simulando...")
        samples, started_at, finished_at = simulate(args.segundos)
    else:
        from max30102 import MAX30102

        sensor = MAX30102(led_current=int(os.environ.get("LED_CURRENT", "0x24"), 16))
        try:
            wait_for_finger(sensor, display, pin, int(os.environ.get("FINGER_IR_MIN", "50000")))
            samples, started_at, finished_at = capture(sensor, display, pin, args.segundos)
        except KeyboardInterrupt:
            display.show(f"PIN: {pin}", "Cancelado")
            sys.exit(1)
        finally:
            sensor.close()

    payload = build_message(pin, samples, started_at, finished_at)
    path = save(pin, payload)
    print(f"{len(samples)} muestras guardadas en {path}")
    if args.sin_enviar:
        display.show(f"PIN: {pin}", "Guardado local")
        return

    display.show(f"PIN: {pin}", "Enviando...")
    via = publish(payload)
    if via:
        print(f"Sesión publicada por {via}. Teclea el PIN {pin} en la web.")
        display.show(f"PIN: {pin}", "Listo! Ve a web")
    else:
        print(f"No se pudo publicar. Reintenta con: python pulse.py --reenviar {path}")
        display.show(f"PIN: {pin}", "Error al enviar")
        sys.exit(1)


if __name__ == "__main__":
    main()
