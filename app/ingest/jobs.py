"""Фоновая загрузка редакции из админ-панели: индексация идёт в отдельном потоке (на сервере без GPU —
около минуты), одновременно — одна задача; панель опрашивает статус."""
from __future__ import annotations

import datetime as dt
import logging
import secrets
import threading
from typing import Callable

log = logging.getLogger(__name__)
_lock = threading.Lock()
_job: dict | None = None


class Busy(RuntimeError):
    """Уже идёт другая загрузка."""


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def current() -> dict | None:
    """Текущая или последняя задача."""
    return dict(_job) if _job else None


def start(title: str, fn: Callable[..., dict], *args, **kwargs) -> dict:
    global _job
    with _lock:
        if _job and _job["status"] == "running":
            raise Busy(f"Уже идёт загрузка: {_job['title']}")
        job = _job = {"id": secrets.token_hex(4), "title": title, "status": "running", "started_at": _now(),
                      "finished_at": None, "result": None, "error": None}

    def run() -> None:
        try:
            job["result"] = fn(*args, **kwargs)
            job["status"] = "done"
        except Exception as e:  # noqa: BLE001 — ошибку показываем администратору
            log.exception("загрузка редакции «%s» не удалась", title)
            job["status"], job["error"] = "error", str(e) or e.__class__.__name__
        finally:
            job["finished_at"] = _now()

    threading.Thread(target=run, daemon=True, name=f"ingest-{job['id']}").start()
    return dict(job)
