"""Лента новостей портала: новости из storage/news.yaml (правятся в админ-панели) и автоматические
новости о новых редакциях документов базы знаний (из реестра версий).

storage/news.yaml создаётся при первом обращении из news.yaml репозитория (начальное наполнение) —
правки на сервере не конфликтуют с git pull.
"""
from __future__ import annotations

import datetime as dt
import logging
import re
import secrets
import threading
from pathlib import Path

import yaml

from app.config import path
from app.ingest.indexer import load_registry

log = logging.getLogger(__name__)
_FIELDS = ("date", "unit", "title", "text", "ask", "ask_label", "link", "link_label", "open_chat")
_SECTION = re.compile(r"\d+(\.\d+)*")
_LOCAL_LINK = re.compile(r"#\S*|/(?![/\\])\S*")  # «//host» и «/\host» браузер считает внешним адресом
_lock = threading.Lock()


class NewsError(ValueError):
    """Ошибка в новости; текст показывается администратору."""


def _fmt_date(iso: str) -> str:
    y, m, d = iso.split("-")
    return f"{d}.{m}.{y}"


def _read(p: Path) -> list[dict]:
    try:
        items = (yaml.safe_load(p.read_text(encoding="utf-8")) or {}).get("items") or []
    except yaml.YAMLError as e:
        log.error("%s не разобран: %s", p.name, e)
        return []
    return [it for it in items if isinstance(it, dict)]


def _write(items: list[dict]) -> None:
    p = path("news")
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(yaml.safe_dump({"items": items}, allow_unicode=True, sort_keys=False), encoding="utf-8")
    tmp.replace(p)


def load_items() -> list[dict]:
    """Новости как они хранятся (с id), в порядке файла. При первом обращении — копия news.yaml репозитория."""
    with _lock:
        p = path("news")
        if p.exists():
            items = _read(p)
        else:
            seed = path("news_seed")
            items = _read(seed) if seed.exists() else []
        if not p.exists() or any("id" not in it for it in items):
            for it in items:
                it.setdefault("id", secrets.token_hex(4))
                if it.get("date") is not None:
                    it["date"] = str(it["date"])
            _write(items)
        return items


def clean(data: dict) -> dict:
    """Проверить и нормализовать поля новости (для админ-панели); ошибки — NewsError."""
    n = {k: data[k] for k in _FIELDS if data.get(k) not in (None, "")}
    for k in ("unit", "title", "text", "ask", "ask_label", "link", "link_label"):
        if k in n:
            n[k] = str(n[k]).strip()
    if not n.get("title"):
        raise NewsError("Нужен заголовок")
    if len(n["title"]) > 200 or len(n.get("text", "")) > 3000:
        raise NewsError("Заголовок — до 200 символов, текст — до 3000")
    n["date"] = str(n.get("date") or "")
    try:
        dt.date.fromisoformat(n["date"])
    except ValueError:
        raise NewsError("Дата — в формате ГГГГ-ММ-ДД") from None
    if "link" in n and not _LOCAL_LINK.fullmatch(n["link"]):
        raise NewsError("Ссылка — только на страницы портала: #docs, #services, /?chat …")
    if "open_chat" in n:
        n["open_chat"] = bool(n["open_chat"])
        if not n["open_chat"]:
            n.pop("open_chat")
    return n


def add_item(data: dict) -> dict:
    item = {"id": secrets.token_hex(4), **clean(data)}
    items = load_items()
    with _lock:
        _write([item] + items)
    return item


def update_item(item_id: str, data: dict) -> dict:
    items = load_items()
    for i, it in enumerate(items):
        if it.get("id") == item_id:
            items[i] = {"id": item_id, **clean(data)}
            with _lock:
                _write(items)
            return items[i]
    raise NewsError("Новость не найдена")


def remove_item(item_id: str) -> None:
    items = load_items()
    rest = [it for it in items if it.get("id") != item_id]
    if len(rest) == len(items):
        raise NewsError("Новость не найдена")
    with _lock:
        _write(rest)


def file_news(news_file: Path | None = None) -> list[dict]:
    """Новости для ленты. Запись без даты или заголовка пропускается, ссылка — только внутренняя."""
    if news_file is not None:
        items = _read(news_file) if news_file.exists() else []
    else:
        items = load_items()
    out = []
    for i, it in enumerate(items):
        try:
            n = clean(it)
        except NewsError as e:
            if "Ссылка" in str(e):
                log.warning("новость %d: внешняя ссылка %r убрана (портал работает без внешних адресов)",
                            i + 1, it.get("link"))
                n = clean({k: v for k, v in it.items() if k != "link"})
            else:
                log.warning("новость %d пропущена: %s", i + 1, e)
                continue
        out.append(n)
    return out


def _sections(keys: list[str], limit: int = 5) -> str:
    names = [f"п. {k}" if _SECTION.fullmatch(k) else k for k in keys[:limit]]
    return ", ".join(names) + (" и др." if len(keys) > limit else "")


def edition_news(reg: dict | None = None) -> list[dict]:
    """Новость на каждую загруженную новую редакцию (у версии есть предыдущая — значит, есть дифф)."""
    reg = reg or load_registry()
    out = []
    for doc_id, d in reg["documents"].items():
        for v in d["versions"]:
            diff = v.get("diff_from_previous")
            if diff is None:
                continue
            parts = [f"{label}: {len(diff[k])} ({_sections(diff[k])})"
                     for k, label in (("changed", "изменено"), ("added", "добавлено"), ("removed", "исключено"))
                     if diff[k]]
            changes = "Пункты: " + "; ".join(parts) + "." if parts else "Текст пунктов не изменился."
            now = ("Помощник уже отвечает по этой редакции." if v.get("status") == "active"
                   else "Сейчас эта редакция в архиве.")
            out.append({
                "date": v["ingested_at"][:10], "unit": "База знаний помощника", "auto": True,
                "title": f"Новая редакция {v['version']}: {d['title']}",
                "text": f"Действует с {_fmt_date(v['effective_date'])}; прежняя редакция переведена в архив. "
                        f"{changes} {now}",
                "link": "#docs", "link_label": "К документам", "doc_id": doc_id,
            })
    return out


def news(news_file: Path | None = None, reg: dict | None = None) -> list[dict]:
    """Вся лента, свежие сверху; при равной дате — порядок файла, новости о редакциях первыми."""
    items = edition_news(reg) + file_news(news_file)
    return sorted(items, key=lambda n: n["date"], reverse=True)
