"""Analizador de señal PPG: latidos, pulsaciones y variabilidad (HRV).

Función pura: no accede a red, BD ni variables de entorno. Recibe la lista de
muestras ya parseada y devuelve un dict con la firma fija:

    {"label": str, "score": float, "rules_met": int, "metrics": dict,
     "elapsed_ms": int, "beats": list[float]}

Pasos:
  1. Limpieza: muestras bien formadas, ordenadas por tiempo.
  2. Calidad básica: suficientes muestras y dedo apoyado (IR medio alto).
  3. Detección de latidos en el canal IR: se quita la deriva lenta, se
     suaviza y se buscan los picos del pulso.
  4. Intervalos entre latidos (IBI) y descarte de intervalos imposibles o
     anómalos (movimiento, latidos perdidos).
  5. Métricas: pulsaciones por minuto, RMSSD y SDNN.
  6. Tres reglas de umbral sobre esas métricas -> etiqueta.

Los umbrales son valores de referencia generales para adultos en reposo, no
están calibrados por persona: es una demostración académica, no un
diagnóstico.
"""

from __future__ import annotations

import bisect
import math
import time

MIN_SAMPLES = 30

# Por debajo de esta media de IR asumimos que no había dedo sobre el sensor.
# Valor orientativo para un MAX30102 en configuración típica: ajústalo con
# tus propias capturas.
MIN_IR_MEAN = 50_000

# Latidos plausibles: entre 40 y 180 pulsaciones por minuto.
MIN_IBI_MS = 333
MAX_IBI_MS = 1500
# Un intervalo que se aleja más de un 25 % de la mediana se considera
# artefacto (movimiento, latido no detectado o doble detección).
IBI_OUTLIER_FRAC = 0.25
# Hacen falta al menos estos intervalos válidos, y que sean al menos esta
# fracción del total, para dar un resultado.
MIN_VALID_IBIS = 20
MIN_VALID_FRAC = 0.6

# Reglas de estrés (referencias generales en reposo, ver docstring).
HR_THRESHOLD = 90      # lpm: por encima, pulso acelerado
RMSSD_THRESHOLD = 20   # ms: por debajo, poca variación latido a latido
SDNN_THRESHOLD = 30    # ms: por debajo, poca variabilidad global

# Descripción de los indicadores para la ficha de resultado.
# `bad` indica en qué dirección se cumple la regla de estrés.
INDICATORS = [
    {"key": "hr_bpm", "threshold": HR_THRESHOLD, "bad": "above", "unit": "lpm"},
    {"key": "rmssd_ms", "threshold": RMSSD_THRESHOLD, "bad": "below", "unit": "ms"},
    {"key": "sdnn_ms", "threshold": SDNN_THRESHOLD, "bad": "below", "unit": "ms"},
]

TOTAL_RULES = len(INDICATORS)
STRESS_MIN_RULES = 2


def _mean(values: list[float]) -> float:
    return sum(values) / len(values)


def _std(values: list[float]) -> float:
    m = _mean(values)
    return math.sqrt(sum((v - m) ** 2 for v in values) / len(values))


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    mid = len(ordered) // 2
    return ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2


def _moving_average(values: list[float], window: int) -> list[float]:
    """Media móvil centrada (ventana impar)."""
    half = window // 2
    prefix = [0.0]
    for v in values:
        prefix.append(prefix[-1] + v)
    out = []
    for i in range(len(values)):
        lo, hi = max(0, i - half), min(len(values), i + half + 1)
        out.append((prefix[hi] - prefix[lo]) / (hi - lo))
    return out


def _odd(n: float) -> int:
    return max(1, int(n)) | 1


def detect_beats(t: list[float], ir: list[float]) -> list[float]:
    """Instantes (ms) de los latidos detectados en la señal IR.

    - Se resta la deriva lenta (media móvil de ~1,5 s) y se invierte el signo:
      cuando llega el pulso, la luz IR que vuelve al sensor baja.
    - Se suaviza con una media móvil de ~0,1 s para quitar ruido.
    - Se buscan máximos locales separados al menos MIN_IBI_MS, empezando por
      los más altos, y se descartan los que no llegan al 40 % de la altura
      típica (ondas pequeñas, no latidos).
    - El instante de cada latido es el centro de masas de la parte alta del
      pulso, para no quedar limitado al periodo de muestreo.

    La precisión del RMSSD depende de la frecuencia de muestreo: con 25 Hz y
    algo de ruido sale inflado unos 10 ms; con 100 Hz el error es de pocos ms.
    """
    n = len(t)
    fs = (n - 1) / ((t[-1] - t[0]) / 1000)
    baseline = _moving_average(ir, _odd(fs * 1.5))
    pulse = [b - v for v, b in zip(ir, baseline)]
    if fs >= 15:
        pulse = _moving_average(pulse, _odd(fs * 0.1))

    candidates = [
        i for i in range(1, n - 1)
        if pulse[i] > 0 and pulse[i] >= pulse[i - 1] and pulse[i] > pulse[i + 1]
    ]
    candidates.sort(key=lambda i: pulse[i], reverse=True)
    taken: list[int] = []  # índices aceptados, ordenados por tiempo
    for i in candidates:
        pos = bisect.bisect_left(taken, i)
        too_close = (
            (pos > 0 and t[i] - t[taken[pos - 1]] < MIN_IBI_MS)
            or (pos < len(taken) and t[taken[pos]] - t[i] < MIN_IBI_MS)
        )
        if not too_close:
            taken.insert(pos, i)
    if not taken:
        return []
    typical = _median([pulse[i] for i in taken])
    accepted = [i for i in taken if pulse[i] >= 0.4 * typical]


    beats = []
    for i in accepted:
        # Centro de masas de la parte alta del pulso (por encima de la mitad
        # del pico): más estable que el máximo cuando hay pocas muestras por
        # latido.
        half = pulse[i] / 2
        lo, hi = i, i
        while lo > 0 and pulse[lo - 1] > half and t[i] - t[lo - 1] < MIN_IBI_MS / 2:
            lo -= 1
        while hi < n - 1 and pulse[hi + 1] > half and t[hi + 1] - t[i] < MIN_IBI_MS / 2:
            hi += 1
        weights = [pulse[k] - half for k in range(lo, hi + 1)]
        total = sum(weights)
        beats.append(
            sum(w * t[k] for w, k in zip(weights, range(lo, hi + 1))) / total
            if total > 0 else t[i]
        )
    return beats


def _valid_ibis(beats: list[float]) -> tuple[list[float], list[tuple[float, float]], int]:
    """Intervalos válidos, pares de intervalos consecutivos válidos y total."""
    ibis = [b - a for a, b in zip(beats, beats[1:])]
    plausible = [x for x in ibis if MIN_IBI_MS <= x <= MAX_IBI_MS]
    if not plausible:
        return [], [], len(ibis)
    med = _median(plausible)
    ok = [
        MIN_IBI_MS <= x <= MAX_IBI_MS and abs(x - med) <= IBI_OUTLIER_FRAC * med
        for x in ibis
    ]
    valid = [x for x, good in zip(ibis, ok) if good]
    pairs = [
        (ibis[k], ibis[k + 1]) for k in range(len(ibis) - 1) if ok[k] and ok[k + 1]
    ]
    return valid, pairs, len(ibis)


def _low_quality(start: float, metrics: dict | None = None, beats=None) -> dict:
    return {
        "label": "low_quality",
        "score": 0.0,
        "rules_met": 0,
        "metrics": metrics or {},
        "elapsed_ms": int((time.perf_counter() - start) * 1000),
        "beats": beats or [],
    }


def analyze(samples: list[dict]) -> dict:
    start = time.perf_counter()

    clean = sorted(
        (
            s
            for s in samples or []
            if isinstance(s, dict)
            and isinstance(s.get("t"), (int, float))
            and isinstance(s.get("ir"), (int, float))
        ),
        key=lambda s: s["t"],
    )
    if len(clean) < MIN_SAMPLES:
        return _low_quality(start)

    t = [float(s["t"]) for s in clean]
    ir = [float(s["ir"]) for s in clean]
    duration_ms = t[-1] - t[0]
    if duration_ms <= 0:
        return _low_quality(start)

    ir_mean = _mean(ir)
    base = {"duration_s": round(duration_ms / 1000, 1), "ir_mean": round(ir_mean)}
    if ir_mean < MIN_IR_MEAN:
        # Sin dedo o mal contacto: no tiene sentido calcular nada más.
        return _low_quality(start, base)

    beats = detect_beats(t, ir)
    valid, pairs, total = _valid_ibis(beats)
    base.update({
        "beats": len(beats),
        "valid_pct": round(100 * len(valid) / total) if total else 0,
    })
    if len(valid) < MIN_VALID_IBIS or len(valid) < MIN_VALID_FRAC * total or not pairs:
        # La señal no deja ver latidos regulares: movimiento o mal contacto.
        return _low_quality(start, base, beats)

    hr = 60_000 / _mean(valid)
    rmssd = math.sqrt(_mean([(b - a) ** 2 for a, b in pairs]))
    sdnn = _std(valid)
    metrics = {
        **base,
        "hr_bpm": round(hr, 1),
        "rmssd_ms": round(rmssd, 1),
        "sdnn_ms": round(sdnn, 1),
    }

    rules_met = sum(
        (metrics[ind["key"]] > ind["threshold"]) if ind["bad"] == "above"
        else (metrics[ind["key"]] < ind["threshold"])
        for ind in INDICATORS
    )
    score = round(rules_met / TOTAL_RULES, 3)
    label = "stress" if rules_met >= STRESS_MIN_RULES else "no_stress"

    return {
        "label": label,
        "score": score,
        "rules_met": rules_met,
        "metrics": metrics,
        "elapsed_ms": int((time.perf_counter() - start) * 1000),
        "beats": beats,
    }
