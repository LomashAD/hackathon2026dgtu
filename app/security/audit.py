"""Аудит-лог обращений (JSONL): кто, когда, запрос (с маскированием ПДн), намерение, источники."""
from __future__ import annotations

import datetime as dt
import json
import threading

from app.config import get_config, path
from app.security.pii import mask

_lock = threading.Lock()


def _write(key: str, record: dict) -> None:
    p = path(key)
    p.parent.mkdir(parents=True, exist_ok=True)
    record = {"ts": dt.datetime.now().isoformat(timespec="seconds"), **record}
    with _lock, open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _m(text: str) -> str:
    return mask(text) if get_config()["security"].get("mask_pii_in_logs", True) else text


def audit(user: str, role: str, session: str, query: str, intent: str, sources: list[str],
          refused: bool, latency_ms: int) -> None:
    _write("audit_log", {"user": user, "role": role, "session": session, "intent": intent,
                         "query": _m(query), "sources": sources, "refused": refused, "latency_ms": latency_ms})


def log_unanswered(role: str, query: str, best: float) -> None:
    _write("unanswered_log", {"role": role, "query": _m(query), "best_relevance": round(best, 3)})


def log_feedback(user: str, session: str, query: str, value: str, comment: str = "") -> None:
    _write("feedback_log", {"user": user, "session": session, "query": _m(query), "value": value,
                            "comment": _m(comment)})
