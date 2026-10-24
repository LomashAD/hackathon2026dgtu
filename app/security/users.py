"""Учётные записи сотрудников: логин, ФИО, роль, хеш пароля (PBKDF2-SHA256), блокировка.

Хранятся в storage/users.json (paths.users). Роль и статус берутся из учётной записи при каждом
запросе, а не из токена входа: смена роли и блокировка действуют сразу, а смена пароля завершает
прежние сессии (в токене — отметка pwv, версия пароля).
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time

from app.config import get_config, path
from app.security.access import roles

_ITER = 200_000
_LOGIN = re.compile(r"[a-z0-9][a-z0-9._-]{1,31}")
_ALPHABET = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"   # без 0/O, 1/l/I
_lock = threading.Lock()
_failures: dict[str, list[float]] = {}
_dummy: str | None = None

# Учётные записи, которые создаёт `scripts/users.py init`, если записей ещё нет
INITIAL = [("admin", "Администратор", "admin"), ("sotrudnik", "Сотрудник (демо)", "employee")]


class UserError(ValueError):
    """Ошибка в данных учётной записи; текст показывается администратору."""


def _sec(key: str, default):
    return get_config().get("security", {}).get(key, default)


def _hash(password: str) -> str:
    salt = secrets.token_bytes(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _ITER)
    return f"pbkdf2_sha256${_ITER}${base64.b64encode(salt).decode()}${base64.b64encode(h).decode()}"


def _check(password: str, stored: str) -> bool:
    try:
        _, it, salt, h = stored.split("$")
        calc = hashlib.pbkdf2_hmac("sha256", password.encode(), base64.b64decode(salt), int(it))
        return hmac.compare_digest(calc, base64.b64decode(h))
    except (ValueError, TypeError):
        return False


def _load() -> dict[str, dict]:
    p = path("users")
    return json.loads(p.read_text(encoding="utf-8"))["users"] if p.exists() else {}


def _save(users: dict[str, dict]) -> None:
    p = path("users")
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps({"users": users}, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, p)


def norm_login(login: str) -> str:
    return (login or "").strip().lower()


def _public(login: str, u: dict) -> dict:
    r = roles().get(u["role"], {})
    return {"login": login, "name": u["name"], "role": u["role"], "role_title": r.get("title", u["role"]),
            "disabled": u.get("disabled", False), "created_at": u.get("created_at"),
            "can_manage_users": bool(r.get("can_manage_users"))}


def gen_password(n: int = 12) -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(n))


def _validate(name: str | None = None, role: str | None = None, password: str | None = None) -> None:
    if name is not None and not (1 <= len(name.strip()) <= 100):
        raise UserError("ФИО — от 1 до 100 символов")
    if role is not None and role not in roles():
        raise UserError(f"Неизвестная роль {role!r}. Доступны: {', '.join(roles())}")
    if password is not None and len(password) < _sec("password_min_length", 8):
        raise UserError(f"Пароль — не короче {_sec('password_min_length', 8)} символов")


def can_manage(role: str) -> bool:
    return bool(roles().get(role, {}).get("can_manage_users"))


def get(login: str) -> dict | None:
    u = _load().get(norm_login(login))
    return _public(norm_login(login), u) | {"pwv": u["pwv"]} if u else None


def list_users() -> list[dict]:
    return [_public(k, u) for k, u in sorted(_load().items())]


def add(login: str, name: str, role: str, password: str | None = None) -> str:
    """Создать учётную запись; возвращает пароль (сгенерированный, если не задан)."""
    login = norm_login(login)
    if not _LOGIN.fullmatch(login):
        raise UserError("Логин — 2–32 символа: латиница, цифры, точка, дефис, подчёркивание")
    password = password or gen_password()
    _validate(name, role, password)
    with _lock:
        users = _load()
        if login in users:
            raise UserError(f"Логин {login} уже занят")
        users[login] = {"name": name.strip(), "role": role, "password": _hash(password), "pwv": secrets.token_hex(8),
                        "disabled": False, "created_at": dt.datetime.now().isoformat(timespec="seconds")}
        _save(users)
    return password


def set_password(login: str, password: str | None = None) -> str:
    """Новый пароль (сгенерированный, если не задан); прежние сессии пользователя завершаются."""
    password = password or gen_password()
    _validate(password=password)
    with _lock:
        users = _load()
        u = users.get(norm_login(login))
        if not u:
            raise UserError(f"Нет учётной записи {login}")
        u["password"], u["pwv"] = _hash(password), secrets.token_hex(8)
        _save(users)
    _failures.pop(norm_login(login), None)
    return password


def update(login: str, name: str | None = None, role: str | None = None, disabled: bool | None = None) -> dict:
    _validate(name, role)
    with _lock:
        users = _load()
        u = users.get(norm_login(login))
        if not u:
            raise UserError(f"Нет учётной записи {login}")
        if name is not None:
            u["name"] = name.strip()
        if role is not None:
            u["role"] = role
        if disabled is not None:
            u["disabled"] = disabled
        _save(users)
    return _public(norm_login(login), u)


def remove(login: str) -> None:
    with _lock:
        users = _load()
        if users.pop(norm_login(login), None) is None:
            raise UserError(f"Нет учётной записи {login}")
        _save(users)


def authenticate(login: str, password: str) -> dict | None:
    """Проверка логина и пароля. После login_max_failures неудач подряд вход по логину закрыт
    на login_lock_minutes минут."""
    global _dummy
    login = norm_login(login)
    now = time.time()
    window = _sec("login_lock_minutes", 5) * 60
    recent = [t for t in _failures.get(login, []) if now - t < window]
    if len(recent) >= _sec("login_max_failures", 5):
        return None
    u = _load().get(login)
    if u is None:
        _dummy = _dummy or _hash("dummy")
        _check(password, _dummy)            # то же время ответа, что и для существующего логина
    if u is None or not _check(password, u["password"]) or u.get("disabled"):
        _failures[login] = recent + [now]
        return None
    _failures.pop(login, None)
    return get(login)


def session_account(login: str, pwv: str | None) -> dict | None:
    """Учётная запись для запроса с токеном входа, если она существует, не заблокирована и пароль
    с момента входа не менялся."""
    acc = get(login) if login else None
    if acc is None or acc["disabled"] or not pwv or not hmac.compare_digest(acc["pwv"], pwv):
        return None
    return acc


def init_accounts() -> list[tuple[str, str, str, str]]:
    """Если учётных записей нет — создать INITIAL; возвращает (логин, ФИО, роль, пароль) созданных."""
    if _load():
        return []
    return [(login, name, role, add(login, name, role)) for login, name, role in INITIAL]


def ensure_auth_secret() -> None:
    """Секрет подписи токенов входа Chainlit: из окружения или из storage/auth_secret (создаётся один раз)."""
    if os.environ.get("CHAINLIT_AUTH_SECRET"):
        return
    p = path("auth_secret")
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(secrets.token_urlsafe(48), encoding="utf-8")
        os.chmod(p, 0o600)
    os.environ["CHAINLIT_AUTH_SECRET"] = p.read_text(encoding="utf-8").strip()
