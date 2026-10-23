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
from app.config import get_config, path  # noqa: E402
from app.dialog.engine import Reply, Session, get_engine  # noqa: E402
from app.security import audit  # noqa: E402
from app.security.access import can_view_archive, roles  # noqa: E402

path("app_log").parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                    handlers=[logging.FileHandler(path("app_log"), encoding="utf-8"), logging.StreamHandler()])
for noisy in ("httpx", "chromadb", "httpcore"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

install_routes()
CFG = get_config()


@cl.set_chat_profiles
async def chat_profiles():
    # Роль выбирается в UI (в продакшне приходит из SSO)
    icons = {"employee": "👤", "expert": "🎓", "admin": "🛠"}
    return [cl.ChatProfile(name=r["title"], markdown_description=f"Роль **{key}**: уровни доступа "
                           f"{', '.join(r['levels'])}", default=(key == CFG["default_role"]),
                           icon=None) for key, r in roles().items()]


def _role_from_profile(profile: str | None) -> str:
    for key, r in roles().items():
        if r["title"] == profile:
            return key
    return CFG["default_role"]


@cl.on_chat_start
async def start():
    role = _role_from_profile(cl.user_session.get("chat_profile"))
    s = Session(role=role, user=f"demo-{role}")
    cl.user_session.set("session", s)
    if can_view_archive(role):
        await cl.ChatSettings([Switch(id="archive", label="Искать также в архивных редакциях", initial=False)]).send()
    await _send(get_engine().help(), greeting=f"Роль: **{roles()[role]['title']}**.\n\n")


@cl.on_settings_update
async def settings_update(settings):
    s: Session = cl.user_session.get("session")
    s.include_archived = bool(settings.get("archive"))


async def _send(reply: Reply, greeting: str = "") -> None:
    elements = []
    text = greeting + reply.text
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
    if reply.intent not in ("help", "") and not reply.meta.get("clarification"):
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
    s: Session = cl.user_session.get("session")
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
    s: Session = cl.user_session.get("session")
    audit.log_feedback(s.user, s.id, cl.user_session.get("last_query") or "", action.payload["v"])
    await action.remove()
    await cl.Message(content="Спасибо за оценку!" if action.payload["v"] == "up" else
                     "Спасибо! Вопрос сохранён для анализа и улучшения базы знаний.").send()
