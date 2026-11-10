"""Driver mínimo del sensor MAX30102 por I2C (solo lo que usa PulsePPG).

Configuración fija:
  - Modo SpO2: el sensor enciende el LED rojo y el IR y guarda las dos lecturas.
  - 100 muestras/s internas con promedio de 2 -> 50 muestras/s en la FIFO.
  - Pulso de LED de 411 µs (ADC de 18 bits) y rango de 4096 nA.

El sensor guarda las muestras en una FIFO de 32 posiciones (640 ms a 50 Hz),
así que basta con leerla cada pocos milisegundos para no perder ninguna.
"""

from __future__ import annotations

import time

from smbus2 import SMBus

ADDRESS = 0x57
PART_ID = 0x15

REG_INT_STATUS_1 = 0x00
REG_FIFO_WR_PTR = 0x04
REG_OVF_COUNTER = 0x05
REG_FIFO_RD_PTR = 0x06
REG_FIFO_DATA = 0x07
REG_FIFO_CONFIG = 0x08
REG_MODE_CONFIG = 0x09
REG_SPO2_CONFIG = 0x0A
REG_LED1_PA = 0x0C   # LED rojo
REG_LED2_PA = 0x0D   # LED infrarrojo
REG_PART_ID = 0xFF

SAMPLE_RATE_HZ = 50  # muestras por segundo que salen de la FIFO

FIFO_CONFIG = 0b001_1_0000   # promedio de 2 muestras, rollover activado
MODE_SPO2 = 0x03             # rojo + IR
SPO2_CONFIG = 0b0_01_001_11  # rango 4096 nA, 100 muestras/s, pulso 411 µs
LED_CURRENT = 0x24           # ~7 mA; súbelo si el IR con dedo sale bajo


class MAX30102:
    def __init__(self, bus: int = 1, address: int = ADDRESS, led_current: int = LED_CURRENT):
        self.address = address
        self.bus = SMBus(bus)
        part = self.bus.read_byte_data(address, REG_PART_ID)
        if part != PART_ID:
            raise RuntimeError(
                f"En 0x{address:02x} no responde un MAX30102 (PART_ID=0x{part:02x}, esperado 0x15)"
            )
        self._write(REG_MODE_CONFIG, 0x40)  # reset
        deadline = time.monotonic() + 1
        while self.bus.read_byte_data(address, REG_MODE_CONFIG) & 0x40:
            if time.monotonic() > deadline:
                raise RuntimeError("El MAX30102 no termina de reiniciarse")
            time.sleep(0.01)
        self.bus.read_byte_data(address, REG_INT_STATUS_1)  # limpia el aviso de encendido
        self._write(REG_FIFO_CONFIG, FIFO_CONFIG)
        self._write(REG_SPO2_CONFIG, SPO2_CONFIG)
        self._write(REG_LED1_PA, led_current)
        self._write(REG_LED2_PA, led_current)
        self._write(REG_MODE_CONFIG, MODE_SPO2)
        self.clear_fifo()

    def _write(self, reg: int, value: int) -> None:
        self.bus.write_byte_data(self.address, reg, value)

    def clear_fifo(self) -> None:
        self._write(REG_FIFO_WR_PTR, 0)
        self._write(REG_OVF_COUNTER, 0)
        self._write(REG_FIFO_RD_PTR, 0)

    def read_fifo(self) -> tuple[list[tuple[int, int]], int]:
        """Devuelve ([(red, ir), ...], muestras_perdidas) con lo que haya en la FIFO."""
        lost = self.bus.read_byte_data(self.address, REG_OVF_COUNTER)
        wr = self.bus.read_byte_data(self.address, REG_FIFO_WR_PTR)
        rd = self.bus.read_byte_data(self.address, REG_FIFO_RD_PTR)
        pending = (wr - rd) & 0x1F
        if lost and pending == 0:
            pending = 32  # FIFO llena: wr == rd
        out: list[tuple[int, int]] = []
        while pending:
            n = min(pending, 5)  # 5 muestras x 6 bytes = 30 bytes por lectura I2C
            raw = self.bus.read_i2c_block_data(self.address, REG_FIFO_DATA, n * 6)
            for i in range(n):
                b = raw[i * 6:(i + 1) * 6]
                red = ((b[0] << 16) | (b[1] << 8) | b[2]) & 0x3FFFF
                ir = ((b[3] << 16) | (b[4] << 8) | b[5]) & 0x3FFFF
                out.append((red, ir))
            pending -= n
        return out, lost

    def close(self) -> None:
        try:
            self._write(REG_MODE_CONFIG, 0x80)  # apaga los LED (shutdown)
        finally:
            self.bus.close()
