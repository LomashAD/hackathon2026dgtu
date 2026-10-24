"""Веб-интерфейс чата (Chainlit) поверх ядра (app/dialog/engine.py) + REST API и макет виджета (FastAPI).

Запуск: chainlit run app/main.py --host 0.0.0.0 --port 8000 --headless
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
import uuid
from pathlib import Path

os.environ.setdefault("ANONYMIZED_TELEMETRY", "False")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import chainlit as cl  # noqa: E402
from chainlit.input_widget import Switch  # noqa: E402

from app.api import install_routes  # noqa: E402
from app.config import path  # noqa: E402
from app.dialog.engine import Engine, Reply, Session, get_engine  # noqa: E402
from app.security import audit, users  # noqa: E402
from app.security.access import can_view_archive, roles  # noqa: E402

path("app_log").parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                    handlers=[logging.FileHandler(path("app_log"), encoding="utf-8"), logging.StreamHandler()])
for noisy in ("httpx", "chromadb", "httpcore"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

install_routes()
users.ensure_auth_secret()
RELOGIN = "Сессия завершена: учётная запись заблокирована или пароль изменён. Войдите заново."

# ---------------------------------------------------------------- отображение (DESIGN.md «Graphite»)
# Карточки в сообщениях — CustomElement Chainlit (public/elements/*.jsx), стили — public/custom.css.
# Всё строится из того, что уже отдаёт движок (Reply), без новых полей.
WELCOME_TAGS = ("Вопрос по Инструкции", "Согласование по Матрице", "Черновик по модулю")
_ARCHIVED = " [АРХИВНАЯ РЕДАКЦИЯ]"
_CITE = re.compile(r"^(?P<doc>.+?), ред\. (?P<ver>[^\s,]+)(?: \(с (?P<date>[^)]+)\))?, (?P<where>.+)$")
_FILL_STEP = re.compile(r"\*\*Вопрос (\d+) из (\d+)\.\*\*")
_TEMPLATE = re.compile(r"(?:Подобран шаблон: \*\*|Черновик \*\*«|Сейчас заполняем \*\*«)(?P<title>[^*»]+)")
_CONTACT = re.compile(r"\n*^\*\*Куда обратиться:\*\* (?P<unit>.+) \((?P<basis>[^()]+)\)\.$", re.M)


def _action_id(kind: str) -> str:
    # Префикс id — вид кнопки для CSS: quick — вариант ответа, util — служебная, fb — оценка
    return f"g-{kind}-{uuid.uuid4().hex[:12]}"


def _source_card(n: int, src) -> dict:
    """Ссылка на источник (engine.citation) → поля карточки: пункт, документ, редакция, дата, глава."""
    label = src.label.removesuffix(_ARCHIVED)
    card = {"n": n, "where": label, "doc": "", "version": "", "date": "", "head": "", "percent": src.percent,
            "related": src.related_to, "archived": src.label.endswith(_ARCHIVED),
            "basis": "чек-лист" if src.kind == "checklist" else "куда обратиться" if src.kind == "contact" else ""}
    if m := _CITE.match(label):
        where, _, head = m["where"].partition(" — ")
        card.update(doc=m["doc"], version=m["ver"], date=m["date"] or "", where=where, head=head)
    return card


def _state(reply: Reply) -> dict | None:
    """Заголовок над ответом: шаг заполнения шаблона, готовый черновик, нет ответа, уточнение, проверка."""
    if m := _FILL_STEP.search(reply.text):
        if t := _TEMPLATE.search(reply.text):
            cl.user_session.set("fill_title", t["title"])
        return {"kind": "fill", "label": "Черновик по шаблону", "title": cl.user_session.get("fill_title") or "",
                "step": int(m[1]), "total": int(m[2])}
    if reply.files:
        t = _TEMPLATE.search(reply.text)
        return {"kind": "ready", "label": "Черновик готов", "title": t["title"] if t else ""}
    if reply.refused:
        return {"kind": "refused", "label": "Нет ответа в базе знаний"}
    if reply.meta.get("clarification"):
        return {"kind": "clarify", "label": "Уточняющий вопрос"}
    if reply.intent == "check_doc" and "ok" in reply.meta:
        issues = reply.meta.get("issues") or 0
        return {"kind": "review", "label": "Проверка черновика",
                "title": "Замечаний нет" if reply.meta["ok"] and not issues else f"Замечаний: {issues}"}
    return None


@cl.password_auth_callback
async def auth(username: str, password: str) -> cl.User | None:
    # Вход по учётной записи сотрудника; роль в токен не пишем — она читается из учётной записи
    acc = await asyncio.to_thread(users.authenticate, username, password)
    audit.log_login(username, acc is not None)
    if acc is None:
        return None
    return cl.User(identifier=acc["login"], display_name=acc["name"], metadata={"pwv": acc["pwv"]})


def _account() -> dict | None:
    user = cl.user_session.get("user")
    return users.session_account(user.identifier, (user.metadata or {}).get("pwv")) if user else None


def _session() -> Session | None:
    """Сессия диалога, если учётная запись ещё действует; роль — текущая из учётной записи."""
    s, acc = cl.user_session.get("session"), _account()
    if s is None or acc is None:
        return None
    s.role = acc["role"]
    return s


@cl.on_chat_start
async def start():
    acc = _account()
    if acc is None:
        await cl.Message(content=RELOGIN).send()
        return
    role = acc["role"]
    s = Session(role=role, user=acc["login"])
    cl.user_session.set("session", s)
    if can_view_archive(role):
        await cl.ChatSettings([Switch(id="archive", label="Искать также в архивных редакциях", initial=False)]).send()
    # Справка — по команде /help (обычный текст, его распознаёт классификатор намерений). Команды Chainlit
    # (set_commands) не подходят: Enter после «/help» только выбирает команду, а не отправляет сообщение.
    welcome = cl.CustomElement(name="Welcome", display="inline", props={
        "title": "Помощник по документообороту",
        "lead": "Отвечаю по Инструкции по ДОУ, Матрице обязательного визирования и модулям документов — "
                "со ссылками на пункты и процентом релевантности. Готовлю черновики .docx по шаблонам.",
        "role": roles()[role]["title"],
        "label": "Попробуйте спросить",
        "examples": [{"tag": tag, "label": label, "text": text}
                     for tag, (label, text) in zip(WELCOME_TAGS, Engine.EXAMPLES)],
        "hint": ["Что я умею — команда ", "/help", ". Черновик ", ".docx", " или ", ".pdf",
                 " можно перетащить в окно — проверю по требованиям Инструкции."]})
    await cl.Message(content="", elements=[welcome]).send()


@cl.on_settings_update
async def settings_update(settings):
    s = _session()
    if s is not None:
        s.include_archived = bool(settings.get("archive")) and can_view_archive(s.role)


async def _send(reply: Reply) -> None:
    elements = []
    text = reply.text
    if state := _state(reply):
        elements.append(cl.CustomElement(name="State", props=state, display="inline"))
        if state["kind"] == "ready":
            text = text.removeprefix("✅ ")   # «готово» уже в заголовке состояния
    if reply.refused and not reply.meta.get("side") and (m := _CONTACT.search(text)):
        # «Куда обратиться» — отдельной карточкой вместо строки в тексте
        text = text[:m.start()] + text[m.end():]
        elements.append(cl.CustomElement(name="Contact", display="inline",
                                         props={"label": "Куда обратиться", "unit": m["unit"], "basis": m["basis"]}))
    actions = [cl.Action(name="quick", payload={"text": payload}, label=label, id=_action_id("quick"))
               for label, payload in reply.actions]
    if reply.sources:
        elements.append(cl.CustomElement(name="Sources", display="inline", props={
            "title": "Источники", "items": [_source_card(i, s) for i, s in enumerate(reply.sources, 1)]}))
        # Полные тексты источников — по кнопке (side-элементы Chainlit 2.12 склеиваются по всему чату)
        store = cl.user_session.get("sources") or {}
        key = str(len(store) + 1)
        store[key] = [(s.label, s.percent, s.text) for s in reply.sources]
        cl.user_session.set("sources", store)
        actions.append(cl.Action(name="show_sources", payload={"key": key}, label="📄 Тексты пунктов",
                                 id=_action_id("util")))
    for f in reply.files:
        elements.append(cl.File(name=f.name, path=str(f), display="inline",
                                mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document"))
    if reply.intent not in ("help", "smalltalk", "cancel", "") and not reply.meta.get("clarification"):
        actions += [cl.Action(name="feedback", payload={"v": "up"}, icon="thumbs-up", tooltip="Ответ помог",
                              id=_action_id("fb")),
                    cl.Action(name="feedback", payload={"v": "down"}, icon="thumbs-down", tooltip="Ответ не помог",
                              id=_action_id("fb"))]
    if reply.meta.get("latency_ms") is not None:
        latency = f"⏱ {reply.meta['latency_ms'] / 1000:.1f} с"
        sources = next((e for e in elements if getattr(e, "name", "") == "Sources"), None)
        if sources is not None:
            sources.props["latency"] = latency   # время ответа — в шапке источников, под текстом
        else:
            text += f"\n\n_{latency}_"
    await cl.Message(content=text, elements=elements, actions=actions).send()


@cl.action_callback("show_sources")
async def on_show_sources(action: cl.Action):
    items = (cl.user_session.get("sources") or {}).get(action.payload["key"], [])
    parts = []
    for i, (label, pct, body) in enumerate(items, 1):
        quoted = "\n".join("> " + ln for ln in body.split("\n"))
        parts.append(f"**{i}. {label}**" + (f" — {pct} %" if pct is not None else "") + f"\n{quoted}")
    await cl.Message(content="**Полные тексты источников ответа:**\n\n" + ("\n\n".join(parts) or "—")).send()


async def _process(text: str, file: Path | None = None) -> None:
    s = _session()
    if s is None:
        await cl.Message(content=RELOGIN).send()
        return
    cl.user_session.set("last_query", text)
    async with cl.Step(name="Поиск по базе знаний", type="tool") as step:
        reply = await asyncio.to_thread(get_engine().handle, s, text, file)
        step.output = f"намерение: {reply.intent}; источников: {len(reply.sources)}"
    await _send(reply)


@cl.on_message
async def on_message(message: cl.Message):
    files = [e for e in (message.elements or []) if getattr(e, "path", None)]
    doc = next((Path(e.path) for e in files if Path(e.name).suffix.lower() in (".docx", ".pdf")), None)
    if files and doc is None:
        await cl.Message(content="Поддерживаются файлы .docx и .pdf.").send()
        return
    if doc is not None:
        # сохраняем исходное имя для отчёта
        named = doc.with_name(next(e.name for e in files if Path(e.path) == doc))
        try:
            named.write_bytes(doc.read_bytes())
            doc = named
        except OSError:
            pass
    await _process(message.content or "", doc)


@cl.action_callback("quick")
async def on_quick(action: cl.Action):
    text = action.payload["text"]
    await cl.Message(content=text, author="user", type="user_message").send()
    await _process(text)


@cl.action_callback("feedback")
async def on_feedback(action: cl.Action):
    s = _session()
    if s is None:
        return
    audit.log_feedback(s.user, s.id, cl.user_session.get("last_query") or "", action.payload["v"])
    await action.remove()
    await cl.Message(content="Спасибо за оценку!" if action.payload["v"] == "up" else
                     "Спасибо! Вопрос сохранён для анализа и улучшения базы знаний.").send()
