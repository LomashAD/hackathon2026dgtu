"""Лента новостей портала: записи из news.yaml и автоматические новости о новых редакциях
документов базы знаний (из реестра версий)."""
from __future__ import annotations

import datetime as dt
import logging
import re
from pathlib import Path

import yaml

from app.config import path
from app.ingest.indexer import load_registry

log = logging.getLogger(__name__)
_FIELDS = ("date", "unit", "title", "text", "ask", "ask_label", "link", "link_label", "open_chat")
_SECTION = re.compile(r"\d+(\.\d+)*")
_LOCAL_LINK = re.compile(r"#\S*|/(?![/\\])\S*")  # «//host» и «/\host» браузер считает внешним адресом


def _fmt_date(iso: str) -> str:
    y, m, d = iso.split("-")
    return f"{d}.{m}.{y}"


def file_news(news_file: Path | None = None) -> list[dict]:
    """Новости из news.yaml. Запись без даты или заголовка пропускается, ссылка — только внутренняя."""
    p = news_file or path("news")
    if not p.exists():
        return []
    try:
        items = (yaml.safe_load(p.read_text(encoding="utf-8")) or {}).get("items") or []
    except yaml.YAMLError as e:
        log.error("news.yaml не разобран: %s", e)
        return []
    out = []
    for i, it in enumerate(items):
        if not isinstance(it, dict) or not it.get("date") or not it.get("title"):
            log.warning("news.yaml: запись %d пропущена — нужны date и title", i + 1)
            continue
        n = {k: it[k] for k in _FIELDS if it.get(k) not in (None, "")}
        n["date"] = str(n["date"])
        try:
            dt.date.fromisoformat(n["date"])
        except ValueError:
            log.warning("news.yaml: запись %d пропущена — дата %r не в формате ГГГГ-ММ-ДД", i + 1, n["date"])
            continue
        if "link" in n and not _LOCAL_LINK.fullmatch(str(n["link"])):
            log.warning("news.yaml: внешняя ссылка %r убрана (портал работает без внешних адресов)", n["link"])
            n.pop("link")
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
            out.append({
                "date": v["ingested_at"][:10], "unit": "База знаний помощника", "auto": True,
                "title": f"Новая редакция {v['version']}: {d['title']}",
                "text": f"Действует с {_fmt_date(v['effective_date'])}; прежняя редакция переведена в архив. "
                        f"{changes} Помощник уже отвечает по новой редакции.",
                "link": "#docs", "link_label": "К документам", "doc_id": doc_id,
            })
    return out


def news(news_file: Path | None = None, reg: dict | None = None) -> list[dict]:
    """Вся лента, свежие сверху; при равной дате — порядок файла, новости о редакциях первыми."""
    items = edition_news(reg) + file_news(news_file)
    return sorted(items, key=lambda n: n["date"], reverse=True)
