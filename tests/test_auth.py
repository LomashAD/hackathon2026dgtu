"""Учётные записи и вход: хеши паролей, блокировка перебора, сессии после смены пароля и блокировки,
права в REST API, API админ-панели. Запуск: .venv/bin/pytest -q tests/test_auth.py"""
from __future__ import annotations

import json
import time

import pytest

# Файлы, которые тесты пишут во временный каталог (остальные пути — настоящие: config, данные, индекс)
_TMP_KEYS = ("users", "auth_secret", "audit_log", "unanswered_log", "feedback_log", "news", "uploads")


@pytest.fixture
def users(tmp_path, monkeypatch):
    from app import admin_api, news
    from app import config as cfgmod
    from app.security import audit
    from app.security import users as mod
    real = cfgmod.path

    def fake(key):
        return tmp_path / key if key in _TMP_KEYS else real(key)
    for m in (mod, audit, news, admin_api):
        monkeypatch.setattr(m, "path", fake)
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
    users.update("ivanov", role="admin")
    assert users.session_account("ivanov", pwv)["role"] == "admin"      # смена роли — сразу, без перевхода
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

    from app.admin_api import router as admin_router
    from app.api import router

    created = {c[0]: c[3] for c in users.init_accounts()}
    assert created
    app = FastAPI(routes=router.routes + admin_router.routes)
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


def _client(users):
    from chainlit.auth.jwt import create_jwt
    from chainlit.user import User
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.admin_api import router as admin_router
    from app.api import router

    app = FastAPI(routes=router.routes + admin_router.routes)

    def as_user(login):
        token = create_jwt(User(identifier=login, metadata={"pwv": users.get(login)["pwv"]}))
        return TestClient(app, follow_redirects=False, cookies={"access_token": token})
    return as_user


def test_own_password(users):
    pw = {c[0]: c[3] for c in users.init_accounts()}
    as_user = _client(users)
    emp, other_device = as_user("sotrudnik"), as_user("sotrudnik")
    assert emp.post("/api/me/password", json={"current": "wrong", "new": "Novyi-parol-1"}).status_code == 400
    assert emp.post("/api/me/password", json={"current": pw["sotrudnik"], "new": "123"}).status_code == 400
    r = emp.post("/api/me/password", json={"current": pw["sotrudnik"], "new": "Novyi-parol-1"})
    assert r.status_code == 200 and "access_token" in r.cookies        # текущая сессия получила новый токен
    assert emp.get("/api/me").status_code == 200                        # клиент взял новую cookie
    assert other_device.get("/api/me").status_code == 401               # токен, выпущенный до смены, не действует
    assert users.authenticate("sotrudnik", "Novyi-parol-1") and not users.authenticate("sotrudnik", pw["sotrudnik"])


def test_admin_panel_api(users, tmp_path, monkeypatch):
    from app import admin_api
    from app.security import audit

    users.init_accounts()
    as_user = _client(users)
    adm, emp = as_user("admin"), as_user("sotrudnik")
    for url in ("/api/admin/audit", "/api/admin/feedback", "/api/admin/news", "/api/admin/kb", "/api/admin/status"):
        assert emp.get(url).status_code == 403, url
        assert adm.get(url).status_code == 200, url

    # журнал: фильтры по сотруднику, типу события, тексту
    audit.audit("sotrudnik", "employee", "s1", "Когда регистрировать входящий?", "question", ["instr_dou:5.6"], False, 120)
    audit.log_login("sotrudnik", True)
    audit.log_login("ivanov", False)
    audit.log_admin("admin", "add", "petrova")
    d = adm.get("/api/admin/audit").json()
    assert d["total"] == 4 and d["items"][0]["event"] == "admin:add"            # свежие первыми
    assert adm.get("/api/admin/audit?kind=question").json()["items"][0]["query"].startswith("Когда")
    assert adm.get("/api/admin/audit?kind=login").json()["total"] == 2
    assert adm.get("/api/admin/audit?user=sotrudnik").json()["total"] == 2
    assert adm.get("/api/admin/audit?q=входящ").json()["total"] == 1
    assert {"sotrudnik", "ivanov", "admin"} <= set(d["users"])

    # вопросы без ответа группируются, оценки считаются
    for q in ("Сколько стоит парковка?", "сколько  стоит парковка?", "Какая зарплата?"):
        audit.log_unanswered("employee", q, 0.1)
    audit.log_feedback("sotrudnik", "s1", "Когда регистрировать?", "up")
    audit.log_feedback("sotrudnik", "s1", "Кто визирует?", "down")
    f = adm.get("/api/admin/feedback").json()
    assert f["unanswered"][0]["count"] == 2 and f["unanswered"][0]["roles"] == ["Сотрудник"]
    assert f["feedback"]["up"] == 1 and f["feedback"]["down"] == 1

    # новости: начальное наполнение из news.yaml, добавить, изменить, удалить; лента видит изменения
    items = adm.get("/api/admin/news").json()["items"]
    assert len(items) >= 4 and all("id" in n for n in items)
    bad = {"date": "24.10.2026", "title": "x"}
    assert adm.post("/api/admin/news", json=bad).status_code == 400
    assert adm.post("/api/admin/news", json={"date": "2026-10-25", "title": "x", "link": "https://evil.example"}).status_code == 400
    n = adm.post("/api/admin/news", json={"date": "2026-10-25", "title": "Тестовая новость", "open_chat": True}).json()
    assert emp.get("/api/news").json()[0]["title"] == "Тестовая новость"
    assert adm.put(f"/api/admin/news/{n['id']}", json={"date": "2026-10-25", "title": "Исправлено"}).json()["title"] == "Исправлено"
    assert adm.delete(f"/api/admin/news/{n['id']}").status_code == 200
    assert all(x["title"] != "Исправлено" for x in emp.get("/api/news").json())
    assert adm.delete("/api/admin/news/nope").status_code == 400

    # загрузка редакции: проверки и фоновая задача (индексация подменена — настоящая есть в test_versioning)
    calls = []
    monkeypatch.setattr(admin_api, "ingest", lambda src, **kw: calls.append((src["doc_id"], kw)) or
                        {"doc_id": src["doc_id"], "version": kw["version"], "sections": 16, "chunks": 16,
                         "archived_versions": ["1.0"], "diff": {"added": [], "removed": [], "changed": ["2.1"]}})
    form = {"doc_id": "mr_modules", "version": "1.1", "effective_date": "2027-01-15"}
    docx = {"file": ("mr.docx", b"PK fake docx", "application/octet-stream")}
    assert adm.post("/api/admin/kb/upload", data=form, files={"file": ("mr.pdf", b"%PDF", "application/pdf")}).status_code == 400
    assert adm.post("/api/admin/kb/upload", data=form | {"version": "новая"}, files=docx).status_code == 400
    assert emp.post("/api/admin/kb/upload", data=form, files=docx).status_code == 403
    job = adm.post("/api/admin/kb/upload", data=form, files=docx).json()
    for _ in range(50):
        job = adm.get("/api/admin/kb/job").json()
        if job["status"] != "running":
            break
        time.sleep(0.05)
    assert job["status"] == "done" and job["result"]["version"] == "1.1"
    assert calls[0][0] == "mr_modules" and calls[0][1]["file"].parent == tmp_path / "uploads"
    assert json.loads((tmp_path / "audit_log").read_text(encoding="utf-8").splitlines()[-1])["event"] == "admin:kb_upload"

    st = adm.get("/api/admin/status").json()
    assert st["users"] == 2 and st["models"]["llm"] and st["day"]["questions"] == 1


def test_drafts_only_for_author(users, tmp_path, monkeypatch):
    from app import config as cfgmod
    real = cfgmod.path
    monkeypatch.setattr(cfgmod, "path", lambda key: tmp_path / "output" if key == "output_dir" else real(key))
    users.init_accounts()
    users.add("ivanov", "Иванов И. И.", "employee")
    as_user = _client(users)
    out = tmp_path / "output"
    out.mkdir()
    mine = out / "Черновик_Распоряжение_20261024_101500_0a1b2c3d.docx"
    mine.write_bytes(b"PK draft")
    (out / (mine.name + ".owner")).write_text("sotrudnik", encoding="utf-8")
    (out / "Черновик_без_автора.docx").write_bytes(b"PK old")
    emp, other, adm = as_user("sotrudnik"), as_user("ivanov"), as_user("admin")
    assert emp.get(f"/api/files/{mine.name}").content == b"PK draft"
    assert other.get(f"/api/files/{mine.name}").status_code == 404       # чужой — как несуществующий
    assert adm.get(f"/api/files/{mine.name}").status_code == 404
    assert emp.get(f"/api/files/{mine.name}.owner").status_code == 404
    assert emp.get("/api/files/Черновик_без_автора.docx").status_code == 404
    assert emp.get("/api/files/..%2Fusers").status_code == 404


def test_originals_respect_access_levels(users):
    users.init_accounts()
    as_user = _client(users)
    emp, adm = as_user("sotrudnik"), as_user("admin")
    docs = {d["doc_id"]: d for d in emp.get("/api/kb/documents").json()}
    if "instr_dou" not in docs:
        pytest.skip("индекс не построен")
    # в Инструкции есть закрытые пункты (2.11, 11.13, 11.23): сотрудник не скачает оригинал целиком
    assert docs["instr_dou"]["restricted"] and docs["instr_dou"]["download"] is None
    assert emp.get("/api/kb/source/instr_dou").status_code == 403
    assert emp.get("/api/kb/source/mr_modules").status_code == 200
    assert adm.get("/api/kb/source/instr_dou").status_code == 200
    assert {d["doc_id"]: d for d in adm.get("/api/kb/documents").json()}["instr_dou"]["download"]


def test_security_headers_and_widget(users, monkeypatch):
    from chainlit.auth.jwt import create_jwt
    from chainlit.user import User
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app import api
    from app.config import get_config

    users.init_accounts()
    app = FastAPI(routes=api.router.routes)
    app.middleware("http")(api._security_headers)
    client = TestClient(app, follow_redirects=False)
    r = client.get("/login")
    assert r.headers["content-security-policy"] == "frame-ancestors 'self'"   # чужой сайт не встроит чат
    assert r.headers["x-content-type-options"] == "nosniff"
    monkeypatch.setitem(get_config(), "portal", {"embed_origins": "https://portal.corp.example javascript:alert(1)"})
    assert client.get("/login").headers["content-security-policy"] == "frame-ancestors 'self' https://portal.corp.example"
    js = client.get("/widget.js")
    assert js.status_code == 200 and js.headers["content-type"].startswith("application/javascript")
    assert "attachShadow" in js.text and "/?chat" in js.text
    token = create_jwt(User(identifier="sotrudnik", metadata={"pwv": users.get("sotrudnik")["pwv"]}))
    me = TestClient(app, cookies={"access_token": token}).get("/api/me")
    assert me.status_code == 200 and me.headers["cache-control"] == "no-store"


def test_admin_adds_and_disables_documents(users, tmp_path, monkeypatch):
    from app import admin_api

    users.init_accounts()
    as_user = _client(users)
    adm, emp = as_user("admin"), as_user("sotrudnik")
    calls = []
    monkeypatch.setattr(admin_api, "ingest", lambda src, **kw: calls.append((src, kw)) or
                        {"doc_id": src["doc_id"], "version": kw["version"], "sections": 3, "chunks": 3,
                         "archived_versions": [], "diff": None})
    form = {"title": "Регламент работы с электронным архивом", "short_title": "Регламент архива",
            "access_level": "confidential", "version": "1.0", "effective_date": "2027-01-01"}
    docx = {"file": ("reg.docx", b"PK fake docx", "application/octet-stream")}
    assert emp.post("/api/admin/kb/add", data=form, files=docx).status_code == 403
    assert adm.post("/api/admin/kb/add", data=form, files={"file": ("x.xlsx", b"PK", "application/octet-stream")}).status_code == 400
    assert adm.post("/api/admin/kb/add", data=form | {"access_level": "top"}, files=docx).status_code == 400
    assert adm.post("/api/admin/kb/add", data=form | {"effective_date": "01.01.2027"}, files=docx).status_code == 400
    assert adm.post("/api/admin/kb/add", data=form, files={"file": ("reg.docx", b"", "application/octet-stream")}).status_code == 400
    job = adm.post("/api/admin/kb/add", data=form, files=docx).json()
    for _ in range(50):
        job = adm.get("/api/admin/kb/job").json()
        if job["status"] != "running":
            break
        time.sleep(0.05)
    assert job["status"] == "done"
    src, kw = calls[0]
    assert src["doc_id"].startswith("doc_") and src["parser"] == "auto" and src["access_level"] == "confidential"
    assert kw["file"].parent == tmp_path / "uploads" and kw["file"].suffix == ".docx" and kw["version"] == "1.0"
    assert json.loads((tmp_path / "audit_log").read_text(encoding="utf-8").splitlines()[-1])["event"] == "admin:kb_add"
    levels = adm.get("/api/admin/kb").json()["access_levels"]
    assert [x["id"] for x in levels] == ["public", "internal", "confidential"]

    disabled = []
    monkeypatch.setattr(admin_api, "disable", lambda doc_id: disabled.append(doc_id) or "1.0")
    assert emp.post("/api/admin/kb/disable", json={"doc_id": "mr_modules"}).status_code == 403
    assert adm.post("/api/admin/kb/disable", json={"doc_id": "nope"}).status_code == 404
    assert adm.post("/api/admin/kb/disable", json={"doc_id": "matrix_viz"}).status_code == 400   # нужна коду согласований
    assert adm.post("/api/admin/kb/disable", json={"doc_id": "mr_modules"}).json() == {"doc_id": "mr_modules", "archived": "1.0"}
    assert disabled == ["mr_modules"]
