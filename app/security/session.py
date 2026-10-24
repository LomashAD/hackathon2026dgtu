"""Вход для FastAPI-роутов: учётная запись по токену Chainlit (cookie или заголовок Authorization: Bearer).

Роль и статус берутся из учётной записи на каждом запросе (app/security/users.py), токен только
подтверждает, кто это и не менялся ли с тех пор пароль.
"""
from __future__ import annotations

from urllib.parse import quote

from fastapi import Depends, HTTPException, Request
from fastapi.responses import RedirectResponse, Response

from app.security import users


def account_from_request(request: Request) -> dict | None:
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
    acc = account_from_request(request)
    if acc is None:
        raise HTTPException(401, "Требуется вход")
    return acc


def admin(acc: dict = Depends(account)) -> dict:
    if not users.can_manage(acc["role"]):
        raise HTTPException(403, "Доступно только администратору")
    return acc


def login_redirect(request: Request) -> RedirectResponse:
    target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    return RedirectResponse(f"/login?next={quote(target, safe='')}", status_code=303)


def issue_cookie(request: Request, response: Response, acc: dict) -> None:
    """Новый токен входа (после смены своего пароля: прежний токен с этого момента недействителен)."""
    from chainlit.auth import create_jwt, set_auth_cookie
    from chainlit.user import User

    token = create_jwt(User(identifier=acc["login"], display_name=acc["name"], metadata={"pwv": acc["pwv"]}))
    set_auth_cookie(request, response, token)
