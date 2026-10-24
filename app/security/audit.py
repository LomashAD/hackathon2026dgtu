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


def log_event(event: str, user: str, **fields) -> None:
    """Событие безопасности в аудит-лог: вход, смена пароля, действие администратора."""
    _write("audit_log", {"event": event, "user": user, **fields})


def log_login(login: str, ok: bool) -> None:
    log_event("login" if ok else "login_failed", _m(login)[:64])


def log_admin(actor: str, action: str, target: str) -> None:
    log_event(f"admin:{action}", actor, target=target)


def read(key: str) -> list[dict]:
    """Записи журнала (audit_log, unanswered_log, feedback_log), свежие первыми."""
    p = path(key)
    if not p.exists():
        return []
    out = []
    with _lock:
        lines = p.read_text(encoding="utf-8").splitlines()
    for line in reversed(lines):
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out
