"""REST API ядра (FastAPI) и страница-макет портала с поп-ап виджетом.

Роуты добавляются в FastAPI-приложение Chainlit и ставятся перед его catch-all маршрутом.
"""
from __future__ import annotations

import gzip
import re

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, Response
from pydantic import BaseModel

from app.config import ROOT
from app.dialog.engine import Session, get_engine
from app.ingest.indexer import load_registry
from app.llm.client import get_client
from app.security.access import roles

router = APIRouter()
_sessions: dict[str, Session] = {}


class AskIn(BaseModel):
    text: str
    role: str = "employee"
    session_id: str | None = None


@router.get("/api/health")
def health():
    reg = load_registry()
    return {"status": "ok", "ollama": get_client().health(),
            "documents": {k: [v["version"] for v in d["versions"] if v["status"] == "active"]
                          for k, d in reg["documents"].items()}}


@router.post("/api/ask")
def ask(body: AskIn):
    if body.role not in roles():
        raise HTTPException(400, f"unknown role {body.role}")
    s = _sessions.get(body.session_id or "") if body.session_id else None
    if s is None or s.role != body.role:
        s = Session(role=body.role, user=f"api-{body.role}")
        _sessions[s.id] = s
    r = get_engine().handle(s, body.text)
    return {"session_id": s.id, "intent": r.intent, "refused": r.refused, "text": r.text,
            "sources": [{"label": x.label, "relevance": x.percent, "doc_id": x.doc_id, "section": x.section}
                        for x in r.sources],
            "actions": [{"label": a, "text": p} for a, p in r.actions],
            "files": [f"/api/files/{f.name}" for f in r.files], "latency_ms": r.meta.get("latency_ms")}


@router.get("/api/files/{name}")
def files(name: str):
    from app.config import path

    p = (path("output_dir") / name).resolve()
    if p.parent != path("output_dir").resolve() or not p.exists():
        raise HTTPException(404)
    return FileResponse(p, filename=p.name)


@router.get("/api/kb/versions")
def versions():
    reg = load_registry()
    return {k: [{x: v[x] for x in ("version", "effective_date", "status", "sections", "chunks", "ingested_at")}
                for v in d["versions"]] for k, d in reg["documents"].items()}


@router.get("/widget", response_class=HTMLResponse)
def widget():
    return HTMLResponse((ROOT / "public" / "portal_widget.html").read_text(encoding="utf-8"))


_EXTERNAL_TAG = re.compile(r'<link[^>]+href="https?://[^"]+"[^>]*>|<meta[^>]+content="https?://[^"]+"[^>]*>')


async def _strip_external(request, call_next):
    """Фронтенд Chainlit по умолчанию подгружает шрифты/стили с CDN — убираем внешние ссылки,
    чтобы при работе не было исходящего трафика и из браузера пользователя."""
    response = await call_next(request)
    if not response.headers.get("content-type", "").startswith("text/html"):
        return response
    body = b"".join([chunk async for chunk in response.body_iterator])
    if response.headers.get("content-encoding") == "gzip":
        body = gzip.decompress(body)
    html = _EXTERNAL_TAG.sub("", body.decode("utf-8"))
    headers = {k: v for k, v in response.headers.items() if k.lower() not in ("content-length", "content-encoding")}
    return Response(html, status_code=response.status_code, headers=headers, media_type="text/html")


def install_routes() -> None:
    from chainlit.server import app

    if any(getattr(r, "path", "") == "/api/health" for r in app.router.routes):
        return
    app.middleware("http")(_strip_external)
    before = len(app.router.routes)
    app.include_router(router)
    new = app.router.routes[before:]
    del app.router.routes[before:]
    app.router.routes[0:0] = new
