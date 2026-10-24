"""REST API админ-панели портала — только для роли с can_manage_users (администратор):
учётные записи, журнал действий, вопросы без ответа и оценки, новости, база знаний и статус."""
from __future__ import annotations

import datetime as dt
import os
import platform
import re
import secrets
import shutil
import time
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from app import news as news_mod
from app.config import ROOT, get_config, path
from app.ingest import jobs
from app.ingest.indexer import disable, find_source, get_collection, ingest, load_registry, source_ext
from app.ingest.parsers import PLAIN_EXT
from app.llm.client import get_client
from app.security import audit, users
from app.security.access import roles
from app.security.session import admin

router = APIRouter(prefix="/api/admin")
STARTED = time.time()
MAX_UPLOAD = 20 * 1024 * 1024
_VERSION = re.compile(r"\d{1,3}(\.\d{1,3}){0,3}")


class UserIn(BaseModel):
    login: str
    name: str
    role: str
    password: str | None = None     # пусто — сгенерируется


class PasswordIn(BaseModel):
    password: str | None = None     # пусто — сгенерируется


class UserPatch(BaseModel):
    name: str | None = None
    role: str | None = None
    disabled: bool | None = None


class NewsIn(BaseModel):
    date: str
    title: str
    unit: str | None = None
    text: str | None = None
    ask: str | None = None
    ask_label: str | None = None
    link: str | None = None
    link_label: str | None = None
    open_chat: bool = False


class ActivateIn(BaseModel):
    doc_id: str
    version: str


class DocIn(BaseModel):
    doc_id: str


def _op(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except (users.UserError, news_mod.NewsError) as e:
        raise HTTPException(400, str(e)) from e


# ---------------------------------------------------------------- учётные записи

@router.get("/users")
def admin_users(_: dict = Depends(admin)):
    return {"users": users.list_users(), "roles": [{"id": k, "title": r["title"]} for k, r in roles().items()]}


@router.post("/users")
def admin_add(body: UserIn, acc: dict = Depends(admin)):
    password = _op(users.add, body.login, body.name, body.role, body.password or None)
    audit.log_admin(acc["login"], "add", users.norm_login(body.login))
    return {"login": users.norm_login(body.login), "password": password}


@router.patch("/users/{login}")
def admin_update(login: str, body: UserPatch, acc: dict = Depends(admin)):
    login = users.norm_login(login)
    if login == acc["login"] and (body.disabled or (body.role is not None and not users.can_manage(body.role))):
        raise HTTPException(400, "Нельзя заблокировать себя или снять с себя права администратора")
    user = _op(users.update, login, name=body.name, role=body.role, disabled=body.disabled)
    action = "block" if body.disabled else "unblock" if body.disabled is False else "role" if body.role else "update"
    audit.log_admin(acc["login"], action, login)
    return {"user": user}


@router.post("/users/{login}/password")
def admin_password(login: str, body: PasswordIn | None = None, acc: dict = Depends(admin)):
    password = _op(users.set_password, login, (body.password if body else None) or None)
    audit.log_admin(acc["login"], "password", users.norm_login(login))
    return {"password": password}


@router.delete("/users/{login}")
def admin_remove(login: str, acc: dict = Depends(admin)):
    login = users.norm_login(login)
    if login == acc["login"]:
        raise HTTPException(400, "Нельзя удалить свою учётную запись")
    _op(users.remove, login)
    audit.log_admin(acc["login"], "remove", login)
    return {"ok": True}


# ---------------------------------------------------------------- журнал действий

_KINDS = {
    "question": lambda r: "intent" in r,
    "login": lambda r: r.get("event", "").startswith("login") or r.get("event") == "password_changed",
    "admin": lambda r: r.get("event", "").startswith("admin:"),
}


@router.get("/audit")
def audit_log(user: str = "", kind: str = "", date_from: str = "", date_to: str = "", q: str = "",
              offset: int = 0, limit: int = 100, _: dict = Depends(admin)):
    """Аудит-лог, свежие сверху. Запросы в журнале уже с маскированными ПДн."""
    recs = audit.read("audit_log")
    match = _KINDS.get(kind)
    needle = q.casefold().strip()

    def ok(r: dict) -> bool:
        day = r.get("ts", "")[:10]
        return ((not user or r.get("user", "").lower() == user.lower()) and (match is None or match(r))
                and (not date_from or day >= date_from) and (not date_to or day <= date_to)
                and (not needle or needle in f"{r.get('query', '')} {r.get('target', '')}".casefold()))

    hits = [r for r in recs if ok(r)]
    limit = max(1, min(limit, 500))
    return {"total": len(hits), "items": hits[max(offset, 0):max(offset, 0) + limit],
            "users": sorted({r["user"] for r in recs if r.get("user")})}


# ---------------------------------------------------------------- вопросы без ответа и оценки

@router.get("/feedback")
def feedback(_: dict = Depends(admin)):
    groups: dict[str, dict] = {}
    for r in audit.read("unanswered_log"):            # свежие первыми → первая запись группы = последняя по времени
        key = " ".join(r.get("query", "").casefold().split())
        g = groups.setdefault(key, {"query": r.get("query", ""), "count": 0, "last": r.get("ts"), "best": 0.0, "roles": set()})
        g["count"] += 1
        g["best"] = max(g["best"], r.get("best_relevance") or 0.0)
        g["roles"].add(r.get("role"))
    titles = {k: r["title"] for k, r in roles().items()}
    unanswered = sorted(({**g, "roles": sorted(titles.get(x, x) for x in g["roles"] if x)} for g in groups.values()),
                        key=lambda g: (g["count"], g["last"] or ""), reverse=True)
    items = audit.read("feedback_log")
    return {"unanswered": unanswered,
            "feedback": {"up": sum(i.get("value") == "up" for i in items),
                         "down": sum(i.get("value") == "down" for i in items), "items": items[:300]}}


# ---------------------------------------------------------------- новости

@router.get("/news")
def admin_news(_: dict = Depends(admin)):
    return {"items": news_mod.load_items(), "auto": news_mod.edition_news()}


@router.post("/news")
def admin_news_add(body: NewsIn, acc: dict = Depends(admin)):
    item = _op(news_mod.add_item, body.model_dump())
    audit.log_admin(acc["login"], "news_add", item["title"][:80])
    return item


@router.put("/news/{item_id}")
def admin_news_update(item_id: str, body: NewsIn, acc: dict = Depends(admin)):
    item = _op(news_mod.update_item, item_id, body.model_dump())
    audit.log_admin(acc["login"], "news_update", item["title"][:80])
    return item


@router.delete("/news/{item_id}")
def admin_news_remove(item_id: str, acc: dict = Depends(admin)):
    _op(news_mod.remove_item, item_id)
    audit.log_admin(acc["login"], "news_remove", item_id)
    return {"ok": True}


# ---------------------------------------------------------------- база знаний

@router.get("/kb")
def kb(_: dict = Depends(admin)):
    reg = load_registry()
    docs = []
    for doc_id, d in reg["documents"].items():
        src = find_source(doc_id) or {}
        versions = []
        for v in sorted(d["versions"], key=lambda v: v.get("ingested_at", ""), reverse=True):
            diff = v.get("diff_from_previous")
            versions.append({k: v.get(k) for k in ("version", "effective_date", "status", "sections", "chunks",
                                                    "ingested_at", "archived_at")}
                            | {"file": Path(v["file"]).name, "diff": {k: len(x) for k, x in diff.items()} if diff else None})
        flexible = src.get("parser") in ("auto", "plain")
        docs.append({"doc_id": doc_id, "title": d["title"],
                     "ext": ",".join(PLAIN_EXT) if flexible else source_ext(doc_id, src),
                     "access_level": src.get("access_level", "internal"), "parser": src.get("parser"),
                     "active": any(v["status"] == "active" for v in d["versions"]), "versions": versions})
    titles = {"public": "Общедоступный", "internal": "Для сотрудников", "confidential": "Ограниченный"}
    levels = [{"id": lv, "title": titles.get(lv, lv)} for lv in get_config()["access_levels"]]
    return {"documents": docs, "job": jobs.current(), "access_levels": levels}


def _start_ingest(title: str, *args, **kwargs) -> dict:
    try:
        return jobs.start(title, ingest, *args, **kwargs)
    except jobs.Busy as e:
        raise HTTPException(409, str(e)) from e


@router.post("/kb/upload")
async def kb_upload(doc_id: str = Form(...), version: str = Form(...), effective_date: str = Form(...),
                    file: UploadFile = File(...), acc: dict = Depends(admin)):
    """Новая редакция документа: файл сохраняется в data/uploads, индексация — в фоне (статус — /kb/job)."""
    src = find_source(doc_id)
    if not src:
        raise HTTPException(404, "Нет такого документа в базе знаний")
    version = version.strip()
    if not _VERSION.fullmatch(version):
        raise HTTPException(400, "Редакция — номер вида 1.1 или 2.0")
    try:
        dt.date.fromisoformat(effective_date)
    except ValueError:
        raise HTTPException(400, "Дата вступления в силу — в формате ГГГГ-ММ-ДД") from None
    ext = Path(file.filename or "").suffix.lower()
    allowed = PLAIN_EXT if src.get("parser") in ("auto", "plain") else (source_ext(doc_id, src),)
    if ext not in allowed:
        raise HTTPException(400, f"Для этого документа нужен файл {' / '.join(allowed)}")
    data = await _read_upload(file)
    dest = path("uploads") / f"{doc_id}_v{version}_{dt.datetime.now():%Y%m%d-%H%M%S}{ext}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    try:
        job = _start_ingest(f"{src.get('short_title') or src['title']}, ред. {version}", src, file=dest,
                            version=version, effective_date=effective_date)
    except HTTPException:
        dest.unlink(missing_ok=True)
        raise
    audit.log_admin(acc["login"], "kb_upload", f"{doc_id} {version}")
    return job


async def _read_upload(file: UploadFile) -> bytes:
    data = await file.read(MAX_UPLOAD + 1)
    if not data:
        raise HTTPException(400, "Файл пустой")
    if len(data) > MAX_UPLOAD:
        raise HTTPException(413, "Файл больше 20 МБ")
    return data


@router.post("/kb/add")
async def kb_add(title: str = Form(...), short_title: str = Form(""), access_level: str = Form("internal"),
                 version: str = Form("1.0"), effective_date: str = Form(...), file: UploadFile = File(...),
                 acc: dict = Depends(admin)):
    """Новый документ базы знаний без правки config.yaml и кода: регламент, памятка, положение (.docx, .pdf,
    .txt, .md) или таблица — реестр, справочник (.xlsx, .csv). Структура определяется по содержимому (парсер auto),
    индексация — в фоне."""
    title, short = " ".join(title.split()), " ".join(short_title.split())
    if not 3 <= len(title) <= 200:
        raise HTTPException(400, "Название — от 3 до 200 символов")
    short = short[:80] or title[:80]
    levels = get_config()["access_levels"]
    if access_level not in levels:
        raise HTTPException(400, f"Уровень доступа — один из: {', '.join(levels)}")
    version = version.strip()
    if not _VERSION.fullmatch(version):
        raise HTTPException(400, "Редакция — номер вида 1.0 или 2.1")
    try:
        dt.date.fromisoformat(effective_date)
    except ValueError:
        raise HTTPException(400, "Дата вступления в силу — в формате ГГГГ-ММ-ДД") from None
    ext = Path(file.filename or "").suffix.lower()
    if ext not in PLAIN_EXT:
        raise HTTPException(400, f"Поддерживаются файлы {', '.join(PLAIN_EXT)}")
    if any(" ".join(d["title"].split()).lower() == title.lower() for d in load_registry()["documents"].values()):
        raise HTTPException(400, "Документ с таким названием уже есть — загрузите его новую редакцию")
    data = await _read_upload(file)
    doc_id = "doc_" + secrets.token_hex(4)
    dest = path("uploads") / f"{doc_id}_v{version}_{dt.datetime.now():%Y%m%d-%H%M%S}{ext}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    src = {"doc_id": doc_id, "title": title, "short_title": short, "parser": "auto", "access_level": access_level}
    try:
        job = _start_ingest(f"Новый документ «{short}», ред. {version}", src, file=dest, version=version,
                            effective_date=effective_date)
    except HTTPException:
        dest.unlink(missing_ok=True)
        raise
    audit.log_admin(acc["login"], "kb_add", f"{doc_id} {short} ({access_level})")
    return job


@router.post("/kb/disable")
def kb_disable(body: DocIn, acc: dict = Depends(admin)):
    """Убрать документ из поиска и с портала: действующая редакция уходит в архив, вернуть — «Сделать действующей»."""
    src = find_source(body.doc_id)
    if not src:
        raise HTTPException(404, "Нет такого документа")
    if src.get("parser") == "approval_matrix":
        raise HTTPException(400, "По Матрице визирования код определяет согласующих — загрузите новую редакцию вместо отключения")
    version = disable(body.doc_id)
    if version is None:
        raise HTTPException(400, "У документа нет действующей редакции")
    audit.log_admin(acc["login"], "kb_disable", f"{body.doc_id} {version}")
    return {"doc_id": body.doc_id, "archived": version}


@router.post("/kb/activate")
def kb_activate(body: ActivateIn, acc: dict = Depends(admin)):
    """Сделать действующей архивную редакцию (откат): её файл индексируется заново, текущая уходит в архив."""
    d = load_registry()["documents"].get(body.doc_id)
    v = next((x for x in d["versions"] if x["version"] == body.version), None) if d else None
    src = find_source(body.doc_id)
    if not v or not src:
        raise HTTPException(404, "Нет такой редакции")
    if v["status"] == "active":
        raise HTTPException(400, "Эта редакция уже действует")
    file = ROOT / v["file"]
    if not file.exists():
        raise HTTPException(400, f"Файл редакции не найден: {v['file']}")
    job = _start_ingest(f"{src.get('short_title') or src['title']}: вернуть ред. {v['version']}", src, file=file,
                        version=v["version"], effective_date=v["effective_date"], force=True)
    audit.log_admin(acc["login"], "kb_activate", f"{body.doc_id} {body.version}")
    return job


@router.get("/kb/job")
def kb_job(_: dict = Depends(admin)):
    return jobs.current()


# ---------------------------------------------------------------- статус

def _meminfo() -> dict | None:
    p = Path("/proc/meminfo")
    if not p.exists():
        return None
    vals = {k: int(v.split()[0]) * 1024 for k, v in (ln.split(":", 1) for ln in p.read_text().splitlines() if ":" in ln)}
    return {"total": vals.get("MemTotal"), "available": vals.get("MemAvailable"),
            "swap_total": vals.get("SwapTotal"), "swap_free": vals.get("SwapFree")}


@router.get("/status")
def status(_: dict = Depends(admin)):
    cfg = get_config()
    try:
        chunks = get_collection().count()
    except Exception:  # noqa: BLE001
        chunks = None
    since = (dt.datetime.now() - dt.timedelta(days=1)).isoformat(timespec="seconds")
    day = [r for r in audit.read("audit_log") if r.get("ts", "") >= since]
    disk = shutil.disk_usage(ROOT)
    return {
        "started_at": dt.datetime.fromtimestamp(STARTED).isoformat(timespec="seconds"),
        "uptime_s": int(time.time() - STARTED),
        "ollama": get_client().health(),
        "models": {"llm": cfg["llm"]["model"], "embed": cfg["embeddings"]["model"]},
        "index_chunks": chunks,
        "users": len(users.list_users()),
        "day": {"questions": sum("intent" in r for r in day), "refused": sum(bool(r.get("refused")) for r in day),
                "logins": sum(r.get("event") == "login" for r in day),
                "failed_logins": sum(r.get("event") == "login_failed" for r in day),
                "people": len({r["user"] for r in day if "intent" in r and r.get("user")})},
        "load": os.getloadavg() if hasattr(os, "getloadavg") else None,
        "cpus": os.cpu_count(),
        "memory": _meminfo(),
        "disk": {"total": disk.total, "free": disk.free},
        "logs": {k: path(k).stat().st_size if path(k).exists() else 0
                 for k in ("audit_log", "unanswered_log", "feedback_log", "app_log")},
        "host": platform.node(), "python": platform.python_version(),
    }
