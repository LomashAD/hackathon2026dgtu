"""Учётные записи и вход: хеши паролей, блокировка перебора, сессии после смены пароля и блокировки,
права в REST API. Запуск: .venv/bin/pytest -q tests/test_auth.py"""
from __future__ import annotations

import pytest


@pytest.fixture
def users(tmp_path, monkeypatch):
    from app import config as cfgmod
    from app.security import users as mod
    real = cfgmod.path
    monkeypatch.setattr(mod, "path", lambda key: tmp_path / key if key in ("users", "auth_secret") else real(key))
    monkeypatch.setattr(mod, "_failures", {})
    monkeypatch.setenv("CHAINLIT_AUTH_SECRET", "test-secret-" + "x" * 32)
    return mod


def test_accounts_and_login(users):
    created = users.init_accounts()
    assert [(c[0], c[2]) for c in created] == [("admin", "admin"), ("sotrudnik", "employee")]
    assert users.init_accounts() == []                       # повторно ничего не создаёт
    pw = dict((c[0], c[3]) for c in created)
    assert "password" not in users.list_users()[0] and pw["admin"] not in (users.path("users")).read_text()
    assert users.authenticate("ADMIN ", pw["admin"])["role"] == "admin"   # логин без учёта регистра и пробелов
    assert users.authenticate("admin", "wrong") is None
    assert users.authenticate("nobody", "x") is None

    with pytest.raises(users.UserError):
        users.add("Иванов", "Иванов И. И.", "employee")        # логин — латиницей
    with pytest.raises(users.UserError):
        users.add("ivanov", "Иванов И. И.", "boss")             # нет такой роли
    with pytest.raises(users.UserError):
        users.add("ivanov", "Иванов И. И.", "employee", password="short")
    p = users.add("ivanov", "Иванов И. И.", "employee")
    with pytest.raises(users.UserError):
        users.add("ivanov", "Другой", "employee")               # логин занят
    assert len(p) == 12 and users.authenticate("ivanov", p)


def test_lockout(users):
    p = users.add("ivanov", "Иванов И. И.", "employee")
    for _ in range(5):
        assert users.authenticate("ivanov", "wrong") is None
    assert users.authenticate("ivanov", p) is None             # закрыт даже с верным паролем
    users._failures["ivanov"] = [t - 10 * 60 for t in users._failures["ivanov"]]   # прошло 10 минут
    assert users.authenticate("ivanov", p)


def test_session_after_password_change_and_block(users):
    users.add("ivanov", "Иванов И. И.", "employee")
    pwv = users.get("ivanov")["pwv"]
    assert users.session_account("ivanov", pwv)["role"] == "employee"
    users.update("ivanov", role="expert")
    assert users.session_account("ivanov", pwv)["role"] == "expert"      # смена роли — сразу, без перевхода
    users.update("ivanov", disabled=True)
    assert users.session_account("ivanov", pwv) is None
    users.update("ivanov", disabled=False)
    new = users.set_password("ivanov")
    assert users.session_account("ivanov", pwv) is None                  # прежние сессии завершены
    assert users.authenticate("ivanov", new)


def test_api_access(users):
    from chainlit.auth.jwt import create_jwt
    from chainlit.user import User
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api import router

    created = {c[0]: c[3] for c in users.init_accounts()}
    assert created
    app = FastAPI(routes=router.routes)
    client = TestClient(app, follow_redirects=False)

    def as_user(login):
        token = create_jwt(User(identifier=login, metadata={"pwv": users.get(login)["pwv"]}))
        return TestClient(app, follow_redirects=False, cookies={"access_token": token})

    assert client.get("/api/news").status_code == 401
    assert client.get("/api/health").status_code == 200                  # мониторинг — без входа
    r = client.get("/")
    assert r.status_code == 303 and r.headers["location"] == "/login?next=%2F"
    assert client.get("/?chat").headers["location"] == "/login?next=%2F%3Fchat"
    assert client.get("/login").status_code == 200
    assert as_user("admin").get("/login?next=//evil.example").headers["location"] == "/"

    emp = as_user("sotrudnik")
    assert emp.get("/api/me").json()["role"] == "employee"
    assert emp.get("/api/admin/users").status_code == 403
    assert emp.get("/").status_code == 200

    adm = as_user("admin")
    assert {u["login"] for u in adm.get("/api/admin/users").json()["users"]} == {"admin", "sotrudnik"}
    r = adm.post("/api/admin/users", json={"login": "ivanov", "name": "Иванов И. И.", "role": "employee"})
    assert r.status_code == 200 and len(r.json()["password"]) == 12
    assert adm.post("/api/admin/users", json={"login": "ivanov", "name": "x", "role": "employee"}).status_code == 400
    assert adm.patch("/api/admin/users/ADMIN", json={"disabled": True}).status_code == 400   # себя — нельзя
    assert adm.patch("/api/admin/users/admin", json={"role": "employee"}).status_code == 400
    assert adm.delete("/api/admin/users/admin").status_code == 400

    assert adm.patch("/api/admin/users/sotrudnik", json={"disabled": True}).status_code == 200
    assert emp.get("/api/me").status_code == 401          # заблокирован — сразу
    adm.patch("/api/admin/users/sotrudnik", json={"disabled": False})
    assert emp.get("/api/me").status_code == 200
    assert adm.post("/api/admin/users/sotrudnik/password").status_code == 200
    assert emp.get("/api/me").status_code == 401          # пароль сменён — старый токен не действует
    assert adm.delete("/api/admin/users/ivanov").status_code == 200

    # пароль задаёт администратор: при создании и при смене; короткий — отказ
    r = adm.post("/api/admin/users", json={"login": "petrova", "name": "Петрова П. П.", "role": "employee", "password": "Moy-parol-2026"})
    assert r.json()["password"] == "Moy-parol-2026" and users.authenticate("petrova", "Moy-parol-2026")
    assert adm.post("/api/admin/users", json={"login": "sidorov", "name": "С", "role": "employee", "password": "123"}).status_code == 400
    assert adm.post("/api/admin/users/petrova/password", json={"password": "short"}).status_code == 400
    assert adm.post("/api/admin/users/petrova/password", json={"password": "Novyi-parol-77"}).json()["password"] == "Novyi-parol-77"
    assert users.authenticate("petrova", "Novyi-parol-77") and not users.authenticate("petrova", "Moy-parol-2026")
    assert len(adm.post("/api/admin/users/petrova/password", json={"password": ""}).json()["password"]) == 12   # пусто — сгенерирован
