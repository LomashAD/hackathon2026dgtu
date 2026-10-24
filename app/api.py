"""REST API ядра (FastAPI), страница-макет портала с поп-ап виджетом и страница входа.

Роуты добавляются в FastAPI-приложение Chainlit и ставятся перед его catch-all маршрутом.
Вход — по учётной записи сотрудника (токен Chainlit в cookie); без входа открыты только /login
и /api/health. Роль и статус учётной записи проверяются на каждом запросе (app/security/users.py).
"""
from __future__ import annotations

import gzip
import re

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel

from app.config import ROOT
from app.dialog.engine import Session, get_engine
from app.ingest.indexer import load_registry
from app.llm.client import get_client
from app.security import audit, users
from app.security.session import account, account_from_request, issue_cookie, login_redirect

router = APIRouter()
_sessions: dict[str, Session] = {}
_MAX_SESSIONS = 1000                 # сессии /api/ask в памяти: самые старые вытесняются
_LOCAL = re.compile(r"/(?![/\\])\S*")   # куда можно вернуться после входа: только свой адрес


class AskIn(BaseModel):
    text: str
    session_id: str | None = None


class OwnPasswordIn(BaseModel):
    current: str
    new: str




@router.get("/api/health")
def health():
    reg = load_registry()
    return {"status": "ok", "ollama": get_client().health(),
            "documents": {k: [v["version"] for v in d["versions"] if v["status"] == "active"]
                          for k, d in reg["documents"].items()}}


@router.post("/api/ask")
def ask(body: AskIn, acc: dict = Depends(account)):
    # Роль — из учётной записи, а не из запроса
    s = _sessions.get(body.session_id or "") if body.session_id else None
    if s is None or s.user != acc["login"]:
        s = Session(role=acc["role"], user=acc["login"])
        _sessions[s.id] = s
        while len(_sessions) > _MAX_SESSIONS:
            _sessions.pop(next(iter(_sessions)))
    s.role = acc["role"]
    r = get_engine().handle(s, body.text)
    return {"session_id": s.id, "intent": r.intent, "refused": r.refused, "text": r.text,
            "sources": [{"label": x.label, "relevance": x.percent, "doc_id": x.doc_id, "section": x.section}
                        for x in r.sources],
            "actions": [{"label": a, "text": p} for a, p in r.actions],
            "files": [f"/api/files/{f.name}" for f in r.files], "latency_ms": r.meta.get("latency_ms")}


@router.get("/api/files/{name}")
def files(name: str, _: dict = Depends(account)):
    from app.config import path

    p = (path("output_dir") / name).resolve()
    if p.parent != path("output_dir").resolve() or not p.exists():
        raise HTTPException(404)
    return FileResponse(p, filename=p.name)


@router.get("/api/kb/versions")
def versions(_: dict = Depends(account)):
    reg = load_registry()
    return {k: [{x: v[x] for x in ("version", "effective_date", "status", "sections", "chunks", "ingested_at")}
                for v in d["versions"]] for k, d in reg["documents"].items()}


@router.get("/api/kb/documents")
def documents(_: dict = Depends(account)):
    """Документы базы знаний (действующие редакции) — для раздела «Документы» портала."""
    reg = load_registry()
    out = []
    for doc_id, d in reg["documents"].items():
        active = next((v for v in d["versions"] if v["status"] == "active"), None)
        if active:
            out.append({"doc_id": doc_id, "title": d["title"], "version": active["version"],
                        "effective_date": active["effective_date"], "sections": active["sections"],
                        "archived": [v["version"] for v in d["versions"] if v["status"] == "archived"],
                        "download": f"/api/kb/source/{doc_id}"})
    return out


@router.get("/api/kb/source/{doc_id}")
def source_file(doc_id: str, _: dict = Depends(account)):
    reg = load_registry()
    d = reg["documents"].get(doc_id)
    active = next((v for v in d["versions"] if v["status"] == "active"), None) if d else None
    if not active:
        raise HTTPException(404)
    p = (ROOT / active["file"]).resolve()
    if not p.is_relative_to(ROOT) or not p.exists():
        raise HTTPException(404)
    return FileResponse(p, filename=p.name)


@router.get("/api/news")
def news(_: dict = Depends(account)):
    """Лента вкладки «Новости»: news.yaml + новости о новых редакциях из реестра версий."""
    from app.news import news as load_news

    return load_news()


@router.get("/api/templates")
def templates(_: dict = Depends(account)):
    from app.templates.catalog import load_templates

    return [{"id": t["id"], "title": t["title"], "description": " ".join(t.get("description", "").split()),
             "download": f"/api/templates/{t['id']}/source"} for t in load_templates().values()]


@router.get("/api/templates/{tid}/source")
def template_source(tid: str, _: dict = Depends(account)):
    from app.templates.catalog import load_templates

    tpl = load_templates().get(tid)
    if not tpl:
        raise HTTPException(404)
    p = (ROOT / tpl["source_file"]).resolve()
    if not p.is_relative_to(ROOT) or not p.exists():
        raise HTTPException(404)
    return FileResponse(p, filename=p.name)


@router.get("/api/me")
def me(acc: dict = Depends(account)):
    return {k: acc[k] for k in ("login", "name", "role", "role_title", "can_manage_users")}


@router.post("/api/me/password")
def my_password(body: OwnPasswordIn, request: Request, acc: dict = Depends(account)):
    """Смена своего пароля. Другие сессии сотрудника завершаются, текущая получает новый токен."""
    if users.authenticate(acc["login"], body.current) is None:
        raise HTTPException(400, "Текущий пароль неверный (после нескольких ошибок подряд проверка на время закрывается)")
    try:
        users.set_password(acc["login"], body.new)
    except users.UserError as e:
        raise HTTPException(400, str(e)) from e
    audit.log_event("password_changed", acc["login"])
    response = JSONResponse({"ok": True})
    issue_cookie(request, response, users.get(acc["login"]))
    return response


# ---------------------------------------------------------------- страницы

@router.get("/login", include_in_schema=False)
def login_page(request: Request):
    """Страница входа портала; форма отправляет логин и пароль в POST /login Chainlit."""
    nxt = request.query_params.get("next", "/")
    if account_from_request(request) is not None:
        return RedirectResponse(nxt if _LOCAL.fullmatch(nxt) else "/", status_code=303)
    return HTMLResponse((ROOT / "public" / "login.html").read_text(encoding="utf-8"))


@router.get("/widget", response_class=HTMLResponse)
def widget(request: Request):
    if account_from_request(request) is None:
        return login_redirect(request)
    return HTMLResponse((ROOT / "public" / "portal_widget.html").read_text(encoding="utf-8"))


@router.get("/", include_in_schema=False)
async def index(request: Request):
    """Корень открывает портал; сам чат Chainlit — по адресу /?chat (так его грузит виджет)."""
    if "chat" in request.query_params:
        if account_from_request(request) is None:
            return login_redirect(request)
        from chainlit.server import serve

        return await serve(request)
    return widget(request)


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
    from app.admin_api import router as admin_router

    app.middleware("http")(_strip_external)
    before = len(app.router.routes)
    app.include_router(router)
    app.include_router(admin_router)
    new = app.router.routes[before:]
    del app.router.routes[before:]
    app.router.routes[0:0] = new
