"""Cliente mínimo de la API REST de ThingsBoard (solo lectura de telemetría).

La configuración se lee de variables de entorno:
    TB_URL        p. ej. https://thingsboard.cloud
    TB_API_KEY    API Key, se envía como "X-Authorization: ApiKey <key>"
    TB_DEVICE_ID  UUID del dispositivo test-pi
"""

from __future__ import annotations

import os
import time

import httpx

# Una sesión de 5 minutos trae miles de muestras (cientos de KB de JSON):
# damos margen para que ThingsBoard Cloud la sirva aunque vaya lento.
TIMEOUT_S = 30.0

SESSION_KEYS = ["pin", "samples", "sample_count", "started_at", "finished_at"]
# Para listar sesiones en /admin no pedimos `samples` (es el campo pesado).
LIST_KEYS = ["pin", "sample_count", "started_at", "finished_at"]


class ThingsBoardError(Exception):
    """Cualquier fallo al hablar con ThingsBoard (red, timeout, HTTP, JSON)."""


def _config() -> tuple[str, str, str]:
    url = os.environ.get("TB_URL", "https://thingsboard.cloud").rstrip("/")
    api_key = os.environ.get("TB_API_KEY", "")
    device_id = os.environ.get("TB_DEVICE_ID", "")
    if not api_key or not device_id:
        raise ThingsBoardError("TB_API_KEY o TB_DEVICE_ID no configurados")
    return url, api_key, device_id


async def _get_timeseries(params: dict) -> dict:
    """Llama a /values/timeseries y devuelve el JSON crudo.

    Formato esperado de ThingsBoard:
        {"pin": [{"ts": 1731000000000, "value": "123456"}, ...], ...}
    Las claves sin datos simplemente no aparecen.
    """
    url, api_key, device_id = _config()
    endpoint = f"{url}/api/plugins/telemetry/DEVICE/{device_id}/values/timeseries"
    headers = {"X-Authorization": f"ApiKey {api_key}"}
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
            resp = await client.get(endpoint, params=params, headers=headers)
            resp.raise_for_status()
            data = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise ThingsBoardError(str(exc)) from exc
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
    """Histórico de sesiones (sin `samples`), más recientes primero."""
    params = {
        "keys": ",".join(LIST_KEYS),
        "startTs": 0,
        "endTs": int(time.time() * 1000),
        "limit": limit,
        "orderBy": "DESC",
    }
    return _group_by_ts(await _get_timeseries(params))


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

    Recorre las últimas `limit` sesiones (sin `samples`, para que sea ligero)
    y solo descarga las muestras de la sesión que coincide.
    """
    for row in await list_sessions(limit):
        if normalize_pin(row.get("pin")) == pin:
            return await get_session_at(row["ts"])
    return None


# Margen para cuando la Pi publica las claves en mensajes separados y no
# comparten exactamente el mismo ts.
SESSION_WINDOW_MS = 60_000


async def get_session_at(ts: int) -> dict | None:
    """Sesión completa (con `samples`) publicada en `ts`.

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
    data = await _get_timeseries(params)
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
    return session
