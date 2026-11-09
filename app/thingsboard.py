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

TIMEOUT_S = 10.0

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


async def get_latest_session() -> dict | None:
    """Últimos valores de cada clave (sin startTs/endTs = latest)."""
    data = await _get_timeseries({"keys": ",".join(SESSION_KEYS)})
    rows = _group_by_ts(data)
    if not rows:
        return None
    # Si por algún motivo las claves tienen ts distintos, tomamos el último
    # valor de cada una (comportamiento "latest" de ThingsBoard).
    latest: dict = {}
    for row in reversed(rows):  # de más antiguo a más reciente
        latest.update(row)
    return latest


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


async def get_session_at(ts: int) -> dict | None:
    """Sesión completa (con `samples`) publicada exactamente en `ts`."""
    params = {
        "keys": ",".join(SESSION_KEYS),
        "startTs": ts,
        "endTs": ts + 1,
        "limit": 1,
    }
    rows = _group_by_ts(await _get_timeseries(params))
    return rows[0] if rows else None
