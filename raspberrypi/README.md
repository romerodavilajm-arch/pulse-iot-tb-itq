# PulsePPG · Raspberry Pi

Código del lado IoT: captura la señal del sensor, genera el PIN, lo muestra en el
LCD y publica la sesión en ThingsBoard (dispositivo `test-pi`) por MQTT con QoS 1.

> **Hardware supuesto (por confirmar):** Raspberry Pi 5 + sensor **MAX30102** +
> LCD **16x2 con adaptador I2C PCF8574**. Conexiones en [CONEXIONES.md](CONEXIONES.md).

```
raspberrypi/
  pulse.py          una sesión: PIN -> dedo -> 5 min de captura -> publicación
  max30102.py       driver mínimo del sensor por I2C (50 muestras/s, rojo + IR)
  .env.example      host de ThingsBoard, token de test-pi y ajustes
  requirements.txt  paho-mqtt, python-dotenv, smbus2, RPLCD
  CONEXIONES.md     cableado y comprobación del I2C
  sesiones/         copia local de cada sesión enviada (se crea sola, no se sube a git)
```

## Instalación (una vez)

```bash
# 1. Activa el I2C y comprueba que se ven el sensor y el LCD (ver CONEXIONES.md)
sudo raspi-config            # Interface Options > I2C > Enable
i2cdetect -y 1               # deben aparecer 57 y 27 (o 3f)

# 2. Entorno de Python (Raspberry Pi OS Bookworm exige un venv)
cd raspberrypi
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 3. Configuración
cp .env.example .env
nano .env                    # pon TB_HOST y TB_DEVICE_TOKEN de test-pi
```

## Una sesión de la demo

```bash
source .venv/bin/activate
python pulse.py
```

1. El LCD muestra `PIN: 123456` y `Pon tu dedo...`.
2. Al apoyar el dedo empieza la cuenta atrás `Midiendo 4:59`. No te muevas.
3. Al terminar, `Enviando...` y luego `Listo! Ve a web`. El PIN sigue en pantalla.
4. En la web, teclea ese PIN y verás el resultado.

Para otra persona, vuelve a ejecutar `python pulse.py` (cada ejecución es una sesión
con un PIN nuevo).

Otras opciones útiles:

| Comando | Para qué |
|---|---|
| `python pulse.py --segundos 30` | sesión corta para probar el montaje |
| `python pulse.py --simular` | sin sensor: genera una señal sintética de 5 min y la publica. Sirve para probar ThingsBoard + web antes de cablear nada |
| `python pulse.py --sin-enviar` | captura y guarda en `sesiones/`, sin publicar |
| `python pulse.py --reenviar sesiones/123456.json` | vuelve a publicar una sesión guardada (p. ej. si falló la red) |

## Qué se publica

Un único mensaje en `v1/devices/me/telemetry`, con un `ts` común para que la web
encuentre todas las claves de la sesión juntas:

```json
{
  "ts": 1731200000000,
  "values": {
    "pin": "012345",
    "samples": "[{\"t\":0,\"ir\":110250,\"red\":90310},{\"t\":20,...}]",
    "sample_count": 15000,
    "started_at": 1731199700000,
    "finished_at": 1731200000000
  }
}
```

- `pin` va como **texto** para no perder los ceros a la izquierda.
- `samples` es un **string JSON**; `t` son milisegundos desde el inicio (0, 20, 40...),
  `ir` y `red` son las lecturas crudas de 18 bits.
- `ts` = `finished_at`.

## Tamaño del mensaje y el límite de ThingsBoard

5 minutos a 50 Hz son 15 000 muestras: el mensaje pesa unos **625 KB** aun con JSON
compacto y `t` relativo (lo mínimo que acepta la web sin cambiarla).

ThingsBoard tiene un límite de tamaño para los mensajes MQTT. En una instalación
propia es `NETTY_MAX_PAYLOAD_SIZE`, que por defecto vale **64 KB**; en ThingsBoard
Cloud no está publicado. Un mensaje de 5 minutos lo supera de sobra, y lo que hace
ThingsBoard entonces es cerrar la conexión sin confirmar el mensaje.

Lo que hace `pulse.py`:

1. Publica por **MQTT con QoS 1** y espera el PUBACK (confirmación de ThingsBoard).
2. Si ThingsBoard no lo confirma, reintenta **una vez** por la **API HTTP de
   dispositivos** (`POST /api/v1/<token>/telemetry`) con exactamente el mismo JSON y
   el mismo `ts`. Para la web es indistinguible. Se puede desactivar con `HTTP_FALLBACK=0`.
   (Las versiones recientes de ThingsBoard admiten mensajes HTTP mucho más grandes que
   por MQTT, pero en tu cuenta no está comprobado: la primera sesión real lo dirá.)
3. Siempre guarda una copia en `sesiones/<PIN>.json` para poder reenviarla.

La consola dice por qué vía salió (`Sesión publicada por MQTT` o `por HTTP`). Una
sesión de 30 s (~62 KB) sí cabe en 64 KB, así que `--segundos 30` siempre sale por MQTT.

Si en tu servidor propio quieres que todo vaya por MQTT, sube el límite:
`NETTY_MAX_PAYLOAD_SIZE=1048576` (1 MB) en la configuración de ThingsBoard.

## Problemas frecuentes

| Síntoma | Causa probable |
|---|---|
| `no responde un MAX30102` / `Errno 121` | I2C desactivado o cable SDA/SCL mal; revisa `i2cdetect -y 1` |
| Se queda en `Pon tu dedo...` con el dedo puesto | IR por debajo de `FINGER_IR_MIN`; sube `LED_CURRENT` (0x30, 0x3F) o baja el umbral |
| `[LCD] no disponible` | dirección equivocada (`LCD_ADDRESS=0x3F`) o LCD sin alimentar |
| LCD encendido pero sin texto | gira el potenciómetro de contraste del adaptador |
| `conexión rechazada` por MQTT | token o `TB_HOST` incorrectos (Cloud EU: `eu.thingsboard.cloud`) |
| La web dice "lectura no válida" | movimiento o poca presión del dedo; repite la sesión quieto |
| Pulso raro en la web | algunos módulos traen los LED rojo/IR invertidos: si `red` es mayor que `ir`, avisa |
