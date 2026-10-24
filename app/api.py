"""REST API ядра (FastAPI), страница-макет портала с поп-ап виджетом и страница входа.

Роуты добавляются в FastAPI-приложение Chainlit и ставятся перед его catch-all маршрутом.
Вход — по учётной записи сотрудника (токен Chainlit в cookie); без входа открыты только /login
и /api/health. Роль и статус учётной записи проверяются на каждом запросе (app/security/users.py).
"""
from __future__ import annotations

import gzip
import re

from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from pydantic import BaseModel

from app.config import ROOT
from app.dialog.engine import Session, get_engine
from app.ingest.indexer import load_registry
from app.llm.client import get_client
from app.security import audit, users
from app.security.access import roles

router = APIRouter()
_sessions: dict[str, Session] = {}
_LOCAL = re.compile(r"/(?![/\\])\S*")   # куда можно вернуться после входа: только свой адрес


class AskIn(BaseModel):
    text: str
    session_id: str | None = None


class UserIn(BaseModel):
    login: str
    name: str
    role: str


class UserPatch(BaseModel):
    name: str | None = None
    role: str | None = None
    disabled: bool | None = None


def _account(request: Request) -> dict | None:
    """Учётная запись по токену входа Chainlit (cookie или заголовок Authorization: Bearer)."""
    from chainlit.auth import get_token_from_cookies
    from chainlit.auth.jwt import decode_jwt

    token = get_token_from_cookies(dict(request.cookies))
    header = request.headers.get("authorization", "")
    if not token and header.lower().startswith("bearer "):
        token = header[7:]
    if not token:
        return None
    try:
        user = decode_jwt(token)
    except Exception:
        return None
    return users.session_account(user.identifier, (user.metadata or {}).get("pwv"))


def account(request: Request) -> dict:
    acc = _account(request)
    if acc is None:
        raise HTTPException(401, "Требуется вход")
    return acc


def admin(acc: dict = Depends(account)) -> dict:
    if not users.can_manage(acc["role"]):
        raise HTTPException(403, "Доступно только администратору")
    return acc


def _login_redirect(request: Request) -> RedirectResponse:
    target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    return RedirectResponse(f"/login?next={quote(target, safe='')}", status_code=303)


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


# ---------------------------------------------------------------- учётные записи (администратор)

def _user_op(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except users.UserError as e:
        raise HTTPException(400, str(e)) from e


@router.get("/api/admin/users")
def admin_users(_: dict = Depends(admin)):
    return {"users": users.list_users(), "roles": [{"id": k, "title": r["title"]} for k, r in roles().items()]}


@router.post("/api/admin/users")
def admin_add(body: UserIn, acc: dict = Depends(admin)):
    password = _user_op(users.add, body.login, body.name, body.role)
    audit.log_admin(acc["login"], "add", body.login)
    return {"login": users.norm_login(body.login), "password": password}


@router.patch("/api/admin/users/{login}")
def admin_update(login: str, body: UserPatch, acc: dict = Depends(admin)):
    login = users.norm_login(login)
    if login == acc["login"] and (body.disabled or (body.role is not None and not users.can_manage(body.role))):
        raise HTTPException(400, "Нельзя заблокировать себя или снять с себя права администратора")
    user = _user_op(users.update, login, name=body.name, role=body.role, disabled=body.disabled)
    audit.log_admin(acc["login"], "update", login)
    return {"user": user}


@router.post("/api/admin/users/{login}/password")
def admin_password(login: str, acc: dict = Depends(admin)):
    password = _user_op(users.set_password, login)
    audit.log_admin(acc["login"], "password", login)
    return {"password": password}


@router.delete("/api/admin/users/{login}")
def admin_remove(login: str, acc: dict = Depends(admin)):
    login = users.norm_login(login)
    if login == acc["login"]:
        raise HTTPException(400, "Нельзя удалить свою учётную запись")
    _user_op(users.remove, login)
    audit.log_admin(acc["login"], "remove", login)
    return {"ok": True}


# ---------------------------------------------------------------- страницы

@router.get("/login", include_in_schema=False)
def login_page(request: Request):
    """Страница входа портала; форма отправляет логин и пароль в POST /login Chainlit."""
    nxt = request.query_params.get("next", "/")
    if _account(request) is not None:
        return RedirectResponse(nxt if _LOCAL.fullmatch(nxt) else "/", status_code=303)
    return HTMLResponse((ROOT / "public" / "login.html").read_text(encoding="utf-8"))


@router.get("/widget", response_class=HTMLResponse)
def widget(request: Request):
    if _account(request) is None:
        return _login_redirect(request)
    return HTMLResponse((ROOT / "public" / "portal_widget.html").read_text(encoding="utf-8"))


@router.get("/", include_in_schema=False)
async def index(request: Request):
    """Корень открывает портал; сам чат Chainlit — по адресу /?chat (так его грузит виджет)."""
    if "chat" in request.query_params:
        if _account(request) is None:
            return _login_redirect(request)
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
    app.middleware("http")(_strip_external)
    before = len(app.router.routes)
    app.include_router(router)
    new = app.router.routes[before:]
    del app.router.routes[before:]
    app.router.routes[0:0] = new
