"""Cliente mínimo de la API REST de ThingsBoard (solo lectura de telemetría).

La configuración se lee de variables de entorno:
    TB_URL        p. ej. https://thingsboard.cloud
    TB_API_KEY    API Key, se envía como "X-Authorization: ApiKey <key>"
    TB_DEVICE_ID  UUID del dispositivo, o varios separados por comas
                  (p. ej. "id-test-pi,id-pi-simulada")
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time

import httpx

# Una sesión de 5 minutos trae miles de muestras (cientos de KB de JSON):
# damos margen para que ThingsBoard Cloud la sirva aunque vaya lento.
TIMEOUT_S = 30.0

SESSION_KEYS = ["pin", "samples", "sample_count", "started_at", "finished_at", "parts"]
# Para listar sesiones en /admin no pedimos `samples` (es el campo pesado).
LIST_KEYS = ["pin", "sample_count", "started_at", "finished_at"]


log = logging.getLogger(__name__)


class ThingsBoardError(Exception):
    """Cualquier fallo al hablar con ThingsBoard (red, timeout, HTTP, JSON)."""


def _config() -> tuple[str, str]:
    url = os.environ.get("TB_URL", "https://thingsboard.cloud").rstrip("/")
    api_key = os.environ.get("TB_API_KEY", "")
    if not api_key or not device_ids():
        raise ThingsBoardError("TB_API_KEY o TB_DEVICE_ID no configurados")
    return url, api_key


def device_ids() -> list[str]:
    """IDs de TB_DEVICE_ID (uno o varios separados por comas), sin repetir."""
    raw = os.environ.get("TB_DEVICE_ID", "")
    return list(dict.fromkeys(d.strip() for d in raw.split(",") if d.strip()))


async def _get(path: str, params: dict | None = None):
    url, api_key = _config()
    headers = {"X-Authorization": f"ApiKey {api_key}"}
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
            resp = await client.get(f"{url}{path}", params=params, headers=headers)
            resp.raise_for_status()
            return resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise ThingsBoardError(str(exc)) from exc


async def device_names() -> dict[str, str]:
    """{device_id: nombre en ThingsBoard}. Si no se puede leer, el ID corto."""
    async def one(device_id: str) -> str:
        try:
            data = await _get(f"/api/device/{device_id}")
            return str(data.get("name") or device_id[:8])
        except (ThingsBoardError, AttributeError):
            return device_id[:8]

    ids = device_ids()
    return dict(zip(ids, await asyncio.gather(*(one(d) for d in ids))))


async def _get_timeseries(device_id: str, params: dict) -> dict:
    """Llama a /values/timeseries y devuelve el JSON crudo.

    Formato esperado de ThingsBoard:
        {"pin": [{"ts": 1731000000000, "value": "123456"}, ...], ...}
    Las claves sin datos simplemente no aparecen.
    """
    data = await _get(f"/api/plugins/telemetry/DEVICE/{device_id}/values/timeseries", params)
    if not isinstance(data, dict):
        raise ThingsBoardError("respuesta inesperada de ThingsBoard")
    return data


def _group_by_ts(data: dict) -> list[dict]:
    """Agrupa {key: [{ts, value}]} en filas {"ts": ts, key: value, ...}.

    Asume que la Pi publica todas las claves de una sesión en un único
    mensaje MQTT, de modo que comparten el mismo `ts`.
    """
    rows: dict[int, dict] = {}
    for key, points in data.items():
        if not isinstance(points, list):
            continue
        for point in points:
            ts = point.get("ts")
            if ts is None:
                continue
            rows.setdefault(ts, {"ts": ts})[key] = point.get("value")
    return sorted(rows.values(), key=lambda r: r["ts"], reverse=True)


async def list_sessions(limit: int = 50) -> list[dict]:
    """Histórico de sesiones (sin `samples`) de todos los dispositivos,
    más recientes primero. Cada fila lleva `device_id`."""
    params = {
        "keys": ",".join(LIST_KEYS),
        "startTs": 0,
        "endTs": int(time.time() * 1000),
        "limit": limit,
        "orderBy": "DESC",
    }
    _config()  # falla pronto si falta configuración
    ids = device_ids()
    results = await asyncio.gather(*(_get_timeseries(d, params) for d in ids),
                                   return_exceptions=True)
    # Si un dispositivo falla (p. ej. ID mal copiado) seguimos con los demás.
    if all(isinstance(r, Exception) for r in results):
        raise results[0] if isinstance(results[0], ThingsBoardError) else ThingsBoardError(str(results[0]))
    rows = []
    for device_id, data in zip(ids, results):
        if isinstance(data, Exception):
            log.warning("no se pudo leer el dispositivo %s: %s", device_id, data)
            continue
        for row in _group_by_ts(data):
            row["device_id"] = device_id
            rows.append(row)
    rows.sort(key=lambda r: r["ts"], reverse=True)
    return rows


def normalize_pin(value) -> str:
    """Normaliza el PIN tal como lo devuelve ThingsBoard.

    Si la Pi publicó el PIN como número (123456 en vez de "123456"),
    ThingsBoard puede devolverlo como "123456", "123456.0" o sin los ceros
    a la izquierda ("12345" para 012345). Lo dejamos siempre en 6 dígitos.
    """
    text = str(value if value is not None else "").strip().strip('"')
    if text.endswith(".0"):
        text = text[:-2]
    if text.isdigit() and len(text) < 6:
        text = text.zfill(6)
    return text


async def find_session_by_pin(pin: str, limit: int = 100) -> dict | None:
    """Busca la sesión más reciente cuyo PIN coincide y la devuelve con `samples`.

    Recorre las últimas `limit` sesiones de cada dispositivo (sin `samples`,
    para que sea ligero) y solo descarga las muestras de la que coincide.
    """
    for row in await list_sessions(limit):
        if normalize_pin(row.get("pin")) == pin:
            return await get_session_at(row["device_id"], row["ts"])
    return None


# Margen para cuando la Pi publica las claves en mensajes separados y no
# comparten exactamente el mismo ts.
SESSION_WINDOW_MS = 60_000


async def get_session_at(device_id: str, ts: int) -> dict | None:
    """Sesión completa (con `samples`) publicada en `ts` por `device_id`.

    Pide una ventana de ±60 s alrededor de `ts` y, para cada clave, se queda
    con el valor más cercano a `ts`. Si la Pi publica todo en un único
    mensaje (lo normal), todas las claves tienen exactamente ese ts.
    """
    params = {
        "keys": ",".join(SESSION_KEYS),
        "startTs": ts - SESSION_WINDOW_MS,
        "endTs": ts + SESSION_WINDOW_MS + 1,
        "limit": 10,
    }
    data = await _get_timeseries(device_id, params)
    session: dict = {}
    for key, points in data.items():
        if not isinstance(points, list):
            continue
        valid = [p for p in points if isinstance(p, dict) and p.get("ts") is not None]
        if valid:
            closest = min(valid, key=lambda p: abs(p["ts"] - ts))
            session[key] = closest.get("value")
    if not session:
        return None
    session["ts"] = ts
    session["device_id"] = device_id
    if not session.get("samples") and session.get("parts"):
        session["samples"] = await _get_parts(device_id, ts, session["parts"])
    return session


async def _get_parts(device_id: str, ts: int, parts) -> list[dict] | None:
    """Une las muestras de una sesión enviada por MQTT en varias partes.

    Una sesión de 5 minutos no cabe en un solo mensaje MQTT de ThingsBoard,
    así que la Pi la manda troceada: la parte i va en `samples_part` con
    ts = ts_de_la_sesión + i (i = 1..parts) y la cabecera (pin, parts, ...)
    va al final con el ts de la sesión. Devuelve None si falta alguna parte.
    """
    try:
        parts = int(float(parts))
    except (TypeError, ValueError):
        return None
    data = await _get_timeseries(device_id, {
        "keys": "samples_part",
        "startTs": ts + 1,
        "endTs": ts + parts + 1,
        "limit": parts,
        "orderBy": "ASC",
    })
    points = sorted(
        (p for p in data.get("samples_part", []) if isinstance(p, dict) and p.get("ts") is not None),
        key=lambda p: p["ts"],
    )
    if len(points) != parts:
        log.warning("sesión ts=%s incompleta: %d de %d partes", ts, len(points), parts)
        return None
    samples: list[dict] = []
    for point in points:
        try:
            chunk = json.loads(point.get("value") or "")
        except ValueError:
            log.warning("sesión ts=%s: parte ts=%s no es JSON", ts, point["ts"])
            return None
        if not isinstance(chunk, list):
            return None
        samples.extend(chunk)
    return samples
