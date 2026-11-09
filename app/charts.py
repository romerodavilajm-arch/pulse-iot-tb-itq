"""Datos para las gráficas SVG de la ficha de resultado (sin librerías JS).

Solo prepara coordenadas; el SVG se dibuja en `templates/result.html`.
"""

from __future__ import annotations

# Tamaño del área de dibujo de la gráfica de la señal (unidades del viewBox).
WIDTH = 600
HEIGHT = 160
PAD = 8
MAX_POINTS = 400


def _moving_average(values: list[float], window: int) -> list[float]:
    """Media móvil centrada (ventana impar), para quitar la deriva lenta."""
    half = window // 2
    prefix = [0.0]
    for v in values:
        prefix.append(prefix[-1] + v)
    out = []
    for i in range(len(values)):
        lo, hi = max(0, i - half), min(len(values), i + half + 1)
        out.append((prefix[hi] - prefix[lo]) / (hi - lo))
    return out


def ir_signal(samples: list[dict]) -> dict | None:
    """Polilínea de la señal IR a lo largo del tiempo.

    Para que se vean los latidos: se quita la deriva lenta (media móvil de
    ~1,5 s) y se invierte el signo, porque en PPG la luz IR que vuelve al
    sensor *baja* cuando llega el pulso. Así cada pico de la línea es un latido.
    Devuelve None si no hay muestras suficientes para dibujar algo útil.
    """
    clean = sorted(
        (
            (float(s["t"]), float(s["ir"]))
            for s in samples or []
            if isinstance(s, dict)
            and isinstance(s.get("t"), (int, float))
            and isinstance(s.get("ir"), (int, float))
        ),
        key=lambda p: p[0],
    )
    if len(clean) < 10:
        return None

    t0, t1 = clean[0][0], clean[-1][0]
    duration_s = (t1 - t0) / 1000
    if duration_s <= 0:
        return None

    t = [p[0] for p in clean]
    ir = [p[1] for p in clean]
    fs = (len(t) - 1) / duration_s
    window = max(3, int(fs * 1.5) | 1)
    baseline = _moving_average(ir, window)
    pulse = [b - v for v, b in zip(ir, baseline)]

    step = max(1, len(pulse) // MAX_POINTS)
    t, pulse = t[::step], pulse[::step]

    lo, hi = min(pulse), max(pulse)
    span = (hi - lo) or 1.0
    points = " ".join(
        f"{PAD + (ti - t0) / (t1 - t0) * (WIDTH - 2 * PAD):.1f},"
        f"{HEIGHT - PAD - (v - lo) / span * (HEIGHT - 2 * PAD):.1f}"
        for ti, v in zip(t, pulse)
    )

    # Marcas del eje X cada 5 s (o cada segundo si la medición es muy corta).
    every = 5 if duration_s >= 10 else 1
    ticks = [
        {"x": round(PAD + s * 1000 / (t1 - t0) * (WIDTH - 2 * PAD), 1), "label": f"{s} s"}
        for s in range(0, int(duration_s) + 1, every)
    ]

    return {"width": WIDTH, "height": HEIGHT, "points": points, "ticks": ticks}
