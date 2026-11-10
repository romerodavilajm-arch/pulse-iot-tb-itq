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

from app import analyzer, charts, thingsboard
from app.analyzer import analyze

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
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
    if isinstance(parsed, str):
        # Doble codificación (json.dumps aplicado dos veces en la Pi).
        parsed = json.loads(parsed)
    if not isinstance(parsed, list):
        raise ValueError("samples no es una lista")
    return parsed


def _build_result(session: dict) -> dict:
    """Parsea samples, ejecuta el análisis y arma el contexto de la plantilla."""
    samples = _parse_samples(session.get("samples"))
    result = analyze(samples)
    beats = result.pop("beats", [])
    if result["label"] == "low_quality":
        beats = []  # en una señal no válida los "latidos" serían ruido
    return {
        "pin": thingsboard.normalize_pin(session.get("pin")),
        "n_samples": len(samples),
        "sample_count": session.get("sample_count"),
        "started_at": _fmt_epoch_ms(session.get("started_at")),
        "finished_at": _fmt_epoch_ms(session.get("finished_at")),
        **result,
        "signal": charts.ir_signal(samples, beats),
        "total_rules": analyzer.TOTAL_RULES,
        "stress_min_rules": analyzer.STRESS_MIN_RULES,
        "indicators": analyzer.INDICATORS,
        "min_ir_mean": analyzer.MIN_IR_MEAN,
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
        session = await thingsboard.find_session_by_pin(pin)
    except thingsboard.ThingsBoardError:
        log.exception("Fallo consultando ThingsBoard")
        return _index(request, MSG_UNAVAILABLE, 503)

    if session is None:
        # Solo en los logs del servidor: al usuario no se le distingue el caso.
        log.warning("lookup: el PIN %s no aparece en las últimas sesiones de ThingsBoard", pin)
        return _index(request, MSG_INVALID)

    try:
        ctx = _build_result(session)
    except (ValueError, TypeError) as exc:
        raw = session.get("samples")
        log.warning(
            "lookup: PIN %s encontrado (ts=%s) pero samples no se pudo usar: %s "
            "(tipo=%s, longitud=%s, inicio=%r)",
            pin, session.get("ts"), exc, type(raw).__name__,
            len(raw) if isinstance(raw, (str, list)) else None,
            raw[:80] if isinstance(raw, str) else None,
        )
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
        rows = await thingsboard.list_sessions()
        names = await thingsboard.device_names()
        for row in rows:
            sessions.append({
                "ts": row["ts"],
                "device_id": row["device_id"],
                "device": names.get(row["device_id"], row["device_id"][:8]),
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
async def admin_analyze(request: Request, ts: int = Form(...), device_id: str = Form("")):
    # Solo se aceptan dispositivos configurados en TB_DEVICE_ID.
    if device_id not in thingsboard.device_ids():
        return templates.TemplateResponse(
            request, "admin.html", {"sessions": [], "error": MSG_INVALID}, status_code=400
        )
    try:
        session = await thingsboard.get_session_at(device_id, ts)
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
