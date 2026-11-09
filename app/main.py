"""App web: formulario de PIN + análisis bajo demanda sobre telemetría de ThingsBoard."""

from __future__ import annotations

import hmac
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates

from app import thingsboard
from app.analyzer import analyze

log = logging.getLogger("pulse")

app = FastAPI(title="PulsePPG", docs_url=None, redoc_url=None, openapi_url=None)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

PIN_RE = re.compile(r"^[0-9]{6}$")
MSG_INVALID = "PIN no válido o sin datos"
MSG_UNAVAILABLE = "No se pudo consultar el servicio. Inténtalo de nuevo en unos minutos."


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------

def _fmt_epoch_ms(value) -> str | None:
    """Epoch ms (int o string, como lo devuelve ThingsBoard) -> texto UTC."""
    try:
        ms = int(float(value))
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _parse_samples(raw) -> list[dict]:
    """`samples` llega como string JSON; por si acaso aceptamos también una lista."""
    if isinstance(raw, list):
        return raw
    if not isinstance(raw, str):
        raise ValueError("samples ausente o con tipo inesperado")
    parsed = json.loads(raw)
    if not isinstance(parsed, list):
        raise ValueError("samples no es una lista")
    return parsed


def _build_result(session: dict) -> dict:
    """Parsea samples, ejecuta el análisis y arma el contexto de la plantilla."""
    samples = _parse_samples(session.get("samples"))
    result = analyze(samples)
    return {
        "pin": session.get("pin"),
        "n_samples": len(samples),
        "sample_count": session.get("sample_count"),
        "started_at": _fmt_epoch_ms(session.get("started_at")),
        "finished_at": _fmt_epoch_ms(session.get("finished_at")),
        **result,
    }


def _index(request: Request, error: str | None = None, code: int = 200) -> HTMLResponse:
    return templates.TemplateResponse(request, "index.html", {"error": error}, status_code=code)


# ---------------------------------------------------------------------------
# Rutas públicas
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return _index(request)


@app.post("/lookup", response_class=HTMLResponse)
async def lookup(request: Request, pin: str = Form("")):
    pin = pin.strip()
    if not PIN_RE.match(pin):
        return _index(request, MSG_INVALID)

    try:
        session = await thingsboard.get_latest_session()
    except thingsboard.ThingsBoardError:
        log.exception("Fallo consultando ThingsBoard")
        return _index(request, MSG_UNAVAILABLE, 503)

    stored_pin = str((session or {}).get("pin") or "")
    # compare_digest: comparación en tiempo constante (detalle barato).
    if not stored_pin or not hmac.compare_digest(stored_pin, pin):
        return _index(request, MSG_INVALID)

    try:
        ctx = _build_result(session)
    except (ValueError, TypeError):
        # samples corrupto: para el usuario es lo mismo que "sin datos".
        log.exception("samples no parseable")
        return _index(request, MSG_INVALID)

    return templates.TemplateResponse(request, "result.html", ctx)


# ---------------------------------------------------------------------------
# /admin (opcional): solo se activa si ADMIN_USER y ADMIN_PASS están definidos
# ---------------------------------------------------------------------------

security = HTTPBasic(auto_error=False)


def require_admin(credentials: HTTPBasicCredentials | None = Depends(security)) -> None:
    user = os.environ.get("ADMIN_USER", "")
    password = os.environ.get("ADMIN_PASS", "")
    if not user or not password:
        # Admin deshabilitado: nos comportamos como si la ruta no existiera.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    ok = credentials is not None and (
        hmac.compare_digest(credentials.username.encode(), user.encode())
        and hmac.compare_digest(credentials.password.encode(), password.encode())
    )
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            headers={"WWW-Authenticate": "Basic"},
        )


@app.get("/admin", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
async def admin(request: Request):
    error = None
    sessions: list[dict] = []
    try:
        for row in await thingsboard.list_sessions():
            sessions.append({
                "ts": row["ts"],
                "pin": row.get("pin"),
                "sample_count": row.get("sample_count"),
                "started_at": _fmt_epoch_ms(row.get("started_at")),
                "finished_at": _fmt_epoch_ms(row.get("finished_at")),
                "published_at": _fmt_epoch_ms(row["ts"]),
            })
    except thingsboard.ThingsBoardError:
        log.exception("Fallo listando sesiones")
        error = MSG_UNAVAILABLE
    return templates.TemplateResponse(request, "admin.html", {"sessions": sessions, "error": error})


@app.post("/admin/analyze", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
async def admin_analyze(request: Request, ts: int = Form(...)):
    try:
        session = await thingsboard.get_session_at(ts)
    except thingsboard.ThingsBoardError:
        log.exception("Fallo consultando ThingsBoard")
        return templates.TemplateResponse(
            request, "admin.html", {"sessions": [], "error": MSG_UNAVAILABLE}, status_code=503
        )
    try:
        if not session:
            raise ValueError("sin datos")
        ctx = _build_result(session)
    except (ValueError, TypeError):
        log.exception("Sesión sin datos o samples no parseable")
        return templates.TemplateResponse(
            request, "admin.html", {"sessions": [], "error": MSG_INVALID}
        )
    return templates.TemplateResponse(request, "result.html", {**ctx, "from_admin": True})
