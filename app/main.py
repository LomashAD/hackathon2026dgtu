"""Веб-интерфейс чата (Chainlit) поверх ядра (app/dialog/engine.py) + REST API и макет виджета (FastAPI).

Запуск: chainlit run app/main.py --host 0.0.0.0 --port 8000 --headless
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

os.environ.setdefault("ANONYMIZED_TELEMETRY", "False")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import chainlit as cl  # noqa: E402
from chainlit.input_widget import Switch  # noqa: E402

from app.api import install_routes  # noqa: E402
from app.config import path  # noqa: E402
from app.dialog.engine import Reply, Session, get_engine  # noqa: E402
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
    await cl.Message(content=f"Роль: **{roles()[role]['title']}**.\n\nЧто я умею — команда **/help**.").send()


@cl.on_settings_update
async def settings_update(settings):
    s = _session()
    if s is not None:
        s.include_archived = bool(settings.get("archive")) and can_view_archive(s.role)


async def _send(reply: Reply) -> None:
    elements = []
    text = reply.text
    actions = [cl.Action(name="quick", payload={"text": payload}, label=label) for label, payload in reply.actions]
    if reply.sources:
        lines = ["", "**Источники:**"]
        for i, src in enumerate(reply.sources, 1):
            pct = f" — релевантность **{src.percent} %**" if src.percent is not None else ""
            rel = f" (связанный с п. {src.related_to})" if src.related_to else ""
            lines.append(f"{i}. {src.label}{pct}{rel}")
        text += "\n" + "\n".join(lines)
        # Полные тексты источников — по кнопке (side-элементы Chainlit 2.12 склеиваются по всему чату)
        store = cl.user_session.get("sources") or {}
        key = str(len(store) + 1)
        store[key] = [(s.label, s.percent, s.text) for s in reply.sources]
        cl.user_session.set("sources", store)
        actions.append(cl.Action(name="show_sources", payload={"key": key}, label="📄 Тексты пунктов"))
    for f in reply.files:
        elements.append(cl.File(name=f.name, path=str(f), display="inline",
                                mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document"))
    if reply.intent not in ("help", "smalltalk", "cancel", "") and not reply.meta.get("clarification"):
        actions += [cl.Action(name="feedback", payload={"v": "up"}, label="👍"),
                    cl.Action(name="feedback", payload={"v": "down"}, label="👎")]
    if reply.meta.get("latency_ms") is not None:
        text += f"\n\n_⏱ {reply.meta['latency_ms'] / 1000:.1f} с_"
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
