# Conexiones (Raspberry Pi 5)

> **Supuesto por confirmar:** el sensor es un **MAX30102** (da las señales `ir` y `red`)
> y el LCD es un **16x2 con adaptador I2C PCF8574** (la placa negra de 4 pines soldada
> detrás del LCD). Si tu hardware es otro, el código del sensor (`max30102.py`) o del
> LCD (clase `Display` en `pulse.py`) tiene que cambiar.

Los dos módulos van al **mismo bus I2C** (comparten SDA y SCL). No chocan porque
tienen direcciones distintas: MAX30102 en `0x57`, LCD en `0x27` (o `0x3F`).

## Tabla de cables

| Módulo   | Pin del módulo | Pin físico de la Pi | Nombre en la Pi |
|----------|----------------|---------------------|-----------------|
| MAX30102 | VIN            | 1                   | 3V3             |
| MAX30102 | GND            | 9                   | GND             |
| MAX30102 | SDA            | 3                   | GPIO2 (SDA1)    |
| MAX30102 | SCL            | 5                   | GPIO3 (SCL1)    |
| MAX30102 | INT            | (sin conectar)      |                 |
| LCD I2C  | VCC            | 2                   | 5V (ver aviso)  |
| LCD I2C  | GND            | 6                   | GND             |
| LCD I2C  | SDA            | 3                   | GPIO2 (SDA1)    |
| LCD I2C  | SCL            | 5                   | GPIO3 (SCL1)    |

Los pines 3 y 5 se comparten: usa una protoboard o cables en Y.

```
   Raspberry Pi 5 (cabecera de 40 pines, vista desde arriba, USB hacia abajo)

        3V3  (1) (2)  5V   ──── LCD VCC
   ┌─── SDA  (3) (4)  5V
   │┌── SCL  (5) (6)  GND  ──── LCD GND
   ││        (7) (8)
   ││   GND  (9) (10)
   ││    │
   ││    └──────────────────── MAX30102 GND
   │└── MAX30102 SCL + LCD SCL
   └─── MAX30102 SDA + LCD SDA
   pin 1 (3V3) ─────────────── MAX30102 VIN
```

## Aviso sobre el LCD a 5 V

El LCD necesita 5 V para verse bien, pero los GPIO de la Pi son de **3,3 V**. Muchos
adaptadores PCF8574 llevan resistencias pull-up a VCC, lo que pone algo más de 3,3 V
en SDA/SCL. En la práctica suele funcionar, pero lo seguro es una de estas opciones:

1. Probar primero el LCD a **3,3 V** (pin 1). Si el texto se ve (ajusta el
   potenciómetro azul de contraste), quédate así.
2. Si a 3,3 V no se ve, aliméntalo a 5 V y pon un **conversor de nivel I2C**
   bidireccional entre la Pi y el LCD (o retira las dos resistencias pull-up del adaptador).

## Activar y comprobar el I2C

```bash
sudo raspi-config          # Interface Options > I2C > Enable, y reinicia
sudo apt install -y i2c-tools
i2cdetect -y 1
```

Debes ver `57` (MAX30102) y `27` o `3f` (LCD). Si el LCD sale en `3f`, pon
`LCD_ADDRESS=0x3F` en `.env`.

## Colocar el dedo

Apoya la yema del dedo índice sobre el sensor con presión ligera y constante, sin
aplastarlo, y no te muevas durante los 5 minutos. Una cinta o pinza ayuda mucho:
el movimiento es la causa principal de lecturas "no válidas" en la web.
