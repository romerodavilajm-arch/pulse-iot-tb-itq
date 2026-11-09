"""Datos para las gráficas SVG de la ficha de resultado (sin librerías JS).

Solo prepara coordenadas; el SVG se dibuja en `templates/result.html`.
"""

from __future__ import annotations

# Tamaño del área de dibujo de la gráfica de la señal (unidades del viewBox).
WIDTH = 600
HEIGHT = 160
PAD = 8
MAX_POINTS = 400

# Las sesiones duran ~5 minutos. En 600 px no caben 300 latidos: se verían
# como una mancha. Si la medición dura más de MAX_FULL_S, se dibuja un
# fragmento de DETAIL_S segundos del centro de la medición, donde la señal
# suele ser más estable que al apoyar o retirar el dedo.
MAX_FULL_S = 30
DETAIL_S = 20


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


def _clock(seconds: float) -> str:
    """Segundos desde el inicio -> "2:30" (o "25 s" si no llega al minuto)."""
    s = int(round(seconds))
    return f"{s // 60}:{s % 60:02d}" if s >= 60 else f"{s} s"


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

    start, end = clean[0][0], clean[-1][0]
    duration_s = (end - start) / 1000
    if duration_s <= 0:
        return None

    t = [p[0] for p in clean]
    ir = [p[1] for p in clean]
    fs = (len(t) - 1) / duration_s
    window = max(3, int(fs * 1.5) | 1)
    baseline = _moving_average(ir, window)
    pulse = [b - v for v, b in zip(ir, baseline)]

    # Fragmento a dibujar: toda la medición si es corta, o DETAIL_S del centro.
    t0, t1 = start, end
    if duration_s > MAX_FULL_S:
        mid = (start + end) / 2
        t0, t1 = mid - DETAIL_S * 500, mid + DETAIL_S * 500
        keep = [i for i, ti in enumerate(t) if t0 <= ti <= t1]
        t = [t[i] for i in keep]
        pulse = [pulse[i] for i in keep]
        if len(t) < 10:
            return None

    step = max(1, len(pulse) // MAX_POINTS)
    t, pulse = t[::step], pulse[::step]

    lo, hi = min(pulse), max(pulse)
    span = (hi - lo) or 1.0
    points = " ".join(
        f"{PAD + (ti - t0) / (t1 - t0) * (WIDTH - 2 * PAD):.1f},"
        f"{HEIGHT - PAD - (v - lo) / span * (HEIGHT - 2 * PAD):.1f}"
        for ti, v in zip(t, pulse)
    )

    # Marcas del eje X cada 5 s (o cada segundo si el tramo es muy corto),
    # rotuladas con el tiempo desde el inicio de la medición.
    shown_s = (t1 - t0) / 1000
    every = 5 if shown_s >= 10 else 1
    offset_s = (t0 - start) / 1000
    first = -(-offset_s // every) * every  # primer múltiplo de `every` dentro del tramo
    ticks = []
    s = first
    while s <= offset_s + shown_s + 0.1:
        x = PAD + (s - offset_s) / shown_s * (WIDTH - 2 * PAD)
        ticks.append({"x": round(x, 1), "label": _clock(s)})
        s += every

    return {
        "width": WIDTH,
        "height": HEIGHT,
        "points": points,
        "ticks": ticks,
        "is_fragment": duration_s > MAX_FULL_S,
        "from": _clock(offset_s),
        "to": _clock(offset_s + shown_s),
        "total": _clock(duration_s),
    }
