"""Analizador de señal PPG (placeholder).

Función pura: no accede a red, BD ni variables de entorno. Recibe la lista de
muestras ya parseada y devuelve un dict con la firma fija:

    {"label": str, "score": float, "rules_met": int, "metrics": dict, "elapsed_ms": int}

IMPORTANTE: el cuerpo actual NO es un análisis fisiológico. Calcula
estadísticos simples de los canales `ir` y `red` y aplica tres reglas de
umbral arbitrarias para producir una etiqueta. Los bloques marcados con
"REEMPLAZAR" indican dónde iría el pipeline real de PPG/HRV.
"""

from __future__ import annotations

import math
import time

MIN_SAMPLES = 30

# Por debajo de esta media de IR asumimos que no había dedo sobre el sensor.
# Valor orientativo para un MAX30102 en configuración típica: ajústalo con
# tus propias capturas.
MIN_IR_MEAN = 50_000

# Umbrales de las reglas placeholder (arbitrarios, solo para la demo).
IR_CV_THRESHOLD = 0.01
RED_CV_THRESHOLD = 0.01
IR_DIFF_THRESHOLD = 0.005

TOTAL_RULES = 3
STRESS_MIN_RULES = 2

# Las sesiones duran ~5 minutos. Calcular la variación sobre los 5 minutos
# enteros mezcla el pulso con la deriva lenta de la señal (respiración,
# presión del dedo), y un pulso tranquilo acaba marcado como estrés. Por eso
# las métricas se calculan en tramos de 30 s (la escala con la que se
# fijaron los umbrales) y se toma la mediana de los tramos.
WINDOW_S = 30


def _mean(values: list[float]) -> float:
    return sum(values) / len(values)


def _variance(values: list[float], mean: float) -> float:
    return sum((v - mean) ** 2 for v in values) / len(values)


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    mid = len(ordered) // 2
    return ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2


def _window_metrics(ir: list[float], red: list[float]) -> tuple[float, float, float]:
    """ir_cv, red_cv e ir_diff de un tramo de la señal."""
    ir_mean = _mean(ir)
    red_mean = _mean(red)
    ir_cv = math.sqrt(_variance(ir, ir_mean)) / ir_mean
    red_cv = math.sqrt(_variance(red, red_mean)) / red_mean if red_mean else 0.0
    ir_diff = _mean([abs(b - a) for a, b in zip(ir, ir[1:])]) / ir_mean
    return ir_cv, red_cv, ir_diff


def _windows(t: list[float], n: int) -> list[tuple[int, int]]:
    """Índices [inicio, fin) de tramos consecutivos de WINDOW_S segundos.

    Si la medición es corta (menos de 1,5 tramos) se usa entera, como antes.
    """
    span_ms = WINDOW_S * 1000
    if t[-1] - t[0] < 1.5 * span_ms:
        return [(0, n)]
    bounds, start = [], 0
    for i in range(1, n):
        if t[i] - t[start] >= span_ms:
            bounds.append((start, i))
            start = i
    # El trozo final se usa solo si dura al menos medio tramo.
    if t[-1] - t[start] >= span_ms / 2:
        bounds.append((start, n))
    return [(a, b) for a, b in bounds if b - a >= MIN_SAMPLES] or [(0, n)]


def _low_quality(start: float, metrics: dict | None = None) -> dict:
    return {
        "label": "low_quality",
        "score": 0.0,
        "rules_met": 0,
        "metrics": metrics or {},
        "elapsed_ms": int((time.perf_counter() - start) * 1000),
    }


def analyze(samples: list[dict]) -> dict:
    start = time.perf_counter()

    # Nos quedamos solo con muestras bien formadas.
    clean = [
        s
        for s in samples or []
        if isinstance(s, dict)
        and isinstance(s.get("t"), (int, float))
        and isinstance(s.get("ir"), (int, float))
        and isinstance(s.get("red"), (int, float))
    ]

    if len(clean) < MIN_SAMPLES:
        return _low_quality(start)

    clean.sort(key=lambda s: s["t"])
    ir = [float(s["ir"]) for s in clean]
    red = [float(s["red"]) for s in clean]
    t = [float(s["t"]) for s in clean]

    ir_mean = _mean(ir)
    if ir_mean < MIN_IR_MEAN:
        # Sin dedo o mal contacto: no tiene sentido calcular nada más.
        return _low_quality(start)

    # ------------------------------------------------------------------
    # REEMPLAZAR (1): preprocesado real.
    # Aquí iría: ordenar por t, remuestrear a frecuencia fija, filtro
    # pasa-banda (~0.5–4 Hz), eliminación de tendencia y de artefactos de
    # movimiento, y un índice de calidad de señal (SQI) que pueda devolver
    # "low_quality" si la señal no es usable.
    # ------------------------------------------------------------------
    windows = _windows(t, len(clean))
    per_window = [_window_metrics(ir[a:b], red[a:b]) for a, b in windows]
    ir_cv = _median([w[0] for w in per_window])
    red_cv = _median([w[1] for w in per_window])
    ir_diff = _median([w[2] for w in per_window])

    duration_ms = max(t) - min(t)

    # ------------------------------------------------------------------
    # REEMPLAZAR (2): extracción de características HRV.
    # Aquí iría: detección de picos sistólicos, intervalos entre latidos
    # (IBI/NN), y métricas como HR media, SDNN, RMSSD, pNN50 y LF/HF.
    # Esas métricas sustituirían a las de abajo en el dict `metrics`.
    # ------------------------------------------------------------------
    # Solo métricas que se pueden explicar en una frase a un no médico:
    #   duration_s  cuánto duró la medición
    #   ir_mean     intensidad media de la señal (contacto del dedo)
    #   ir_cv       cuánto varía la señal IR respecto a su media (regla 1)
    #   red_cv      lo mismo para la luz roja (regla 2)
    #   ir_diff     cambio medio de una muestra a la siguiente (regla 3)
    #   windows     en cuántos tramos de 30 s se dividió la medición
    metrics = {
        "duration_s": round(duration_ms / 1000, 1),
        "ir_mean": round(ir_mean),
        "ir_cv": round(ir_cv, 5),
        "red_cv": round(red_cv, 5),
        "ir_diff": round(ir_diff, 5),
        "windows": len(windows),
    }

    # ------------------------------------------------------------------
    # REEMPLAZAR (3): reglas de decisión.
    # Reglas placeholder sin validez fisiológica. En el pipeline real serían
    # p. ej.: RMSSD bajo, LF/HF alto, HR por encima de la basal.
    # ------------------------------------------------------------------
    rules = [
        ir_cv > IR_CV_THRESHOLD,
        red_cv > RED_CV_THRESHOLD,
        ir_diff > IR_DIFF_THRESHOLD,
    ]
    rules_met = sum(rules)
    score = round(rules_met / TOTAL_RULES, 3)
    label = "stress" if rules_met >= STRESS_MIN_RULES else "no_stress"

    return {
        "label": label,
        "score": score,
        "rules_met": rules_met,
        "metrics": metrics,
        "elapsed_ms": int((time.perf_counter() - start) * 1000),
    }
