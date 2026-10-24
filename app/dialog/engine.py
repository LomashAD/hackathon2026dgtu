"""Движок диалога: намерение → уточнение (один вопрос) → поиск/матрица/шаблон → ответ со ссылками.

Ссылки на источники, цитаты пунктов, согласующие и сроки формирует код. LLM используется только
для короткого пояснения «простыми словами» (с проверкой), подбора шаблона и перефразирования вопросов.
"""
from __future__ import annotations

import datetime as dt
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from app.approval import deadlines
from app.approval.matrix import KIND_TITLES, get_matrix
from app.config import get_config
from app.dialog import intents
from app.llm import guard, prompts
from app.llm.client import get_client
from app.retrieval.search import Hit, get_retriever
from app.security import audit
from app.security.access import can_view_archive, roles
from app.templates.catalog import load_templates, pick_template
from app.templates.slots import FillSession

log = logging.getLogger(__name__)

CANCEL_RE = re.compile(r"^\s*(отмена|отменить|стоп|выход|прервать|хватит|cancel)\b", re.I)
BACK_RE = re.compile(r"^\s*(назад|вернуться|исправить предыдущ\w*)\b", re.I)
FOLLOWUP_RE = re.compile(r"^\s*(а|и|но|тогда|а если|а что|а как|а кто|а когда|а где|это|он|она|они|его|ее|их|там|"
                         r"при этом|в таком случае|а для)\b", re.I)


@dataclass
class Source:
    label: str
    percent: int | None
    text: str
    doc_id: str
    section: str
    related_to: str = ""
    kind: str = "fragment"   # fragment | matrix | template


@dataclass
class Reply:
    text: str
    sources: list[Source] = field(default_factory=list)
    actions: list[tuple[str, str]] = field(default_factory=list)   # (подпись кнопки, текст-ответ)
    files: list[Path] = field(default_factory=list)
    intent: str = ""
    refused: bool = False
    meta: dict = field(default_factory=dict)


@dataclass
class Session:
    role: str = "employee"
    user: str = "anonymous"
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    history: list[dict] = field(default_factory=list)
    last_query: str = ""
    last_intent: str = ""
    pending: dict | None = None          # ожидаемый ответ на уточняющий вопрос
    fill: FillSession | None = None      # заполнение шаблона
    params: dict = field(default_factory=dict)   # выбранные параметры (вид документа и т.п.)
    include_archived: bool = False


def fmt_date(iso: str) -> str:
    try:
        return dt.date.fromisoformat(iso).strftime("%d.%m.%Y")
    except ValueError:
        return iso


def citation(meta: dict) -> str:
    """Ссылка на источник из метаданных чанка: документ, редакция, пункт/приложение, глава."""
    sec = meta["section_path"]
    if meta.get("appendix"):
        where = f"Приложение № {meta['appendix']}"
    elif meta.get("kind") == "matrix_row":
        where = sec
    else:
        where = f"п. {sec}"
    s = f"{meta['short_title']}, ред. {meta['version']} (с {fmt_date(meta['effective_date'])}), {where}"
    if meta.get("chapter") and meta["doc_id"] == "instr_dou":
        s += f" — гл. {meta['chapter']} «{meta['heading']}»"
    elif meta.get("heading") and meta.get("kind") != "matrix_row":
        s += f" — разд. «{meta['heading']}»"
    if meta.get("status") == "archived":
        s += " [АРХИВНАЯ РЕДАКЦИЯ]"
    return s


def _quote(text: str, limit: int) -> str:
    text = text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + " …"
    return "\n".join("> " + ln for ln in text.split("\n"))


class Engine:
    def __init__(self):
        self.cfg = get_config()
        self.rcfg = self.cfg["retrieval"]
        self.retriever = get_retriever()
        self.llm = get_client()

    # ================================================================ вход
    def handle(self, s: Session, text: str, file: Path | None = None) -> Reply:
        t0 = time.perf_counter()
        text = (text or "").strip()[: self.cfg["security"]["max_message_chars"]]
        try:
            reply = self._route(s, text, file)
        except (httpx.ConnectError, httpx.TimeoutException):
            log.exception("Ollama недоступна")
            reply = Reply("Сервис поиска временно недоступен: не отвечает локальная языковая модель (Ollama). "
                          "Переформулировать вопрос не нужно — повторите его через минуту. Если ошибка повторяется, "
                          "сообщите администратору: нужно запустить Ollama (`ollama serve` или `./run.sh`).",
                          intent="error")
        except Exception:  # noqa: BLE001
            log.exception("ошибка обработки")
            reply = Reply("Не удалось обработать запрос из-за внутренней ошибки. Повторите вопрос; "
                          "если ошибка повторяется, сообщите администратору.", intent="error")
        latency = int((time.perf_counter() - t0) * 1000)
        reply.meta["latency_ms"] = latency
        s.history.append({"user": text, "intent": reply.intent, "refused": reply.refused})
        s.history = s.history[-20:]
        audit.audit(s.user, s.role, s.id, text, reply.intent,
                    [f"{x.doc_id}:{x.section}" for x in reply.sources], reply.refused, latency)
        return reply

    def _route(self, s: Session, text: str, file: Path | None) -> Reply:
        if file is not None:
            s.pending = None
            return self.review(s, file, text)
        if s.fill is not None:
            return self._fill_step(s, text)
        if s.pending is not None:
            pend, s.pending = s.pending, None
            if CANCEL_RE.match(text):
                return Reply("Хорошо, отменил. Задайте новый вопрос.", intent="cancel")
            r = self._resolve_pending(s, pend, text)
            if r is not None:
                return r
        intent, how = intents.classify(text, llm=self.llm)
        log.info("intent=%s (%s)", intent, how)
        s.last_intent = intent
        if intent == "help":
            return self.help()
        if intent == "create_doc":
            return self.start_template(s, text)
        if intent == "check_doc":
            return Reply("Загрузите черновик документа (.docx или .pdf) — кнопкой со скрепкой или перетащите файл "
                         "в окно чата. Я проверю его на соответствие требованиям Инструкции и модуля.",
                         intent="check_doc")
        if intent == "approval":
            return self.approval(s, text)
        if intent == "deadline":
            r = self.deadline(s, text)
            if r is not None:
                return r
        if intent == "contact":
            return self.contact(s, text)
        return self.answer(s, text, intent)

    # ================================================================ уточнения
    def _ask(self, s: Session, question: str, options: list[tuple[str, str]], pending: dict, intent: str) -> Reply:
        s.pending = pending
        return Reply(question, actions=options, intent=intent, meta={"clarification": True})

    def _resolve_pending(self, s: Session, pend: dict, text: str) -> Reply | None:
        kind = pend["type"]
        if kind == "approval_kind":
            k = get_matrix().detect_kind(text) or {"1": "order", "2": "disposition", "3": "letter"}.get(text.strip())
            if not k:
                return None
            s.params["doc_kind"] = k
            return self.approval(s, pend["query"] + " " + KIND_TITLES[k], forced_kind=k)
        if kind == "approval_row":
            row = text.strip() if text.strip() in get_matrix().rows else \
                get_matrix().detect_row(text, pend["kind"]) or (DEFAULT_ROW_FOR(pend["kind"]) if re.search(
                    r"друг|проч|иное|нет", text, re.I) else None)
            if not row:
                return None
            return self.approval_answer(s, row, pend["query"])
        if kind == "deadline_item":
            return self.deadline(s, pend["query"] + " " + text, forced_kind=pend["kind"], strict=True)
        if kind == "template":
            tid = text.strip() if text.strip() in load_templates() else pick_template(text)[0]
            if not tid:
                return None
            return self.begin_fill(s, tid)
        return None

    # ================================================================ RAG-ответ
    @staticmethod
    def _with_context(s: Session, text: str) -> str:
        """Для «с кем его согласовать?» / «подготовь его» — добавляем предмет из предыдущего вопроса."""
        if s.last_query and re.search(r"\b(его|её|ее|это|этот|эту|такой|такое|такую|него|неё|нее|их)\b", text, re.I):
            return f"{text} {s.last_query}"
        return text

    def _rewrite(self, s: Session, text: str) -> str:
        """Учёт истории: короткое уточнение дополняется предыдущим вопросом."""
        short = len(text.split()) <= 3 and not intents.QUESTION_START.search(text)
        if s.last_query and (FOLLOWUP_RE.match(text) or short) and s.last_intent in (
                "question", "find_section", "deadline"):
            return f"{s.last_query} {text}"
        return text

    def answer(self, s: Session, text: str, intent: str = "question") -> Reply:
        query = self._rewrite(s, text)
        res = self.retriever.search(query, s.role, include_archived=s.include_archived and can_view_archive(s.role))
        s.last_query = query
        if not res.enough or not res.hits:
            audit.log_unanswered(s.role, query, res.best)
            return self.refusal(text, res.best)
        hits = res.hits
        main = [h for h in hits if not h.related_to]
        quotes = main[: self.rcfg["quote_count"]]
        if len(quotes) > 1 and quotes[1].relevance < quotes[0].relevance - self.rcfg.get("quote_gap", 0.03):
            quotes = quotes[:1]
        parts = []
        # Пояснение строится только по лучшему пункту: маленькая модель путает смысл при смешении пунктов
        expl = self.explain(text, quotes[:1])
        for h in quotes:
            head = "Приложение № " + h.meta["appendix"] if h.meta.get("appendix") else \
                h.section if h.meta.get("kind") == "matrix_row" else f"п. {h.section}"
            parts.append(f"**{h.meta['short_title']}, {head}** (релевантность {h.percent} %):\n"
                         f"{_quote(h.text, self.rcfg['quote_max_chars'])}")
        body = "\n\n".join(parts)
        if expl:
            body += f"\n\n**Простыми словами:** {expl}"
        others = [h for h in hits if h not in quotes]
        if others:
            body += "\n\n**См. также:** " + "; ".join(
                f"{'п. ' + h.section if h.meta.get('kind') == 'point' else h.section} — {h.percent} %"
                + (f" (связан с п. {h.related_to})" if h.related_to else "") for h in others)
        return Reply(body, sources=self._sources(hits), intent=intent,
                     meta={"best": res.best, "explained": bool(expl)})

    def explain(self, question: str, hits: list[Hit]) -> str:
        if not self.cfg["llm"].get("explain", True):
            return ""
        frags = [h.text for h in hits]
        allowed = frags + [f"п. {h.section}" for h in hits]
        try:
            out = guard.clean_answer(self.llm.chat(prompts.explain_messages(question, frags),
                                                   max_tokens=self.cfg["llm"]["max_explain_tokens"]))
        except Exception as e:  # noqa: BLE001
            log.warning("LLM недоступна: %s", e)
            return ""
        ok, why = guard.check_explanation(out, allowed, question=question)
        log.info("explain check: %s (%s) %r", ok, why, out[:200])
        return out if ok else ""

    def _sources(self, hits: list[Hit]) -> list[Source]:
        return [Source(citation(h.meta), h.percent, h.text, h.meta["doc_id"], h.section, h.related_to)
                for h in hits]

    def refusal(self, text: str, best: float) -> Reply:
        c = self._contact_for(text)
        msg = ("**В базе знаний недостаточно информации для ответа на этот вопрос.** "
               "Я отвечаю только по загруженным документам (Инструкция по ДОУ, Матрица обязательного визирования, "
               "Методические рекомендации по модульным документам) и не буду строить догадки.\n\n"
               f"**Куда обратиться:** {c['unit']} ({c['basis']}).\n\n"
               "Попробуйте переформулировать вопрос или уточнить вид документа.")
        src = []
        hit = self.retriever.get_section(c["ref"]["doc_id"], c["ref"]["section"], "employee")
        if hit:
            src.append(Source(citation(hit.meta), None, hit.text, hit.meta["doc_id"], hit.section, kind="contact"))
        return Reply(msg, sources=src, intent="no_answer", refused=True, meta={"best": best})

    # ================================================================ куда обратиться
    def _contact_for(self, text: str) -> dict:
        contacts = self.cfg["contacts"]
        t = text.lower()
        if re.search(r"доступ|прав[ао]? (пользоват|доступ)|учетн|парол", t):
            return contacts["access"]
        if re.search(r"иис|систем|сэд", t):
            return contacts["iis"]
        if re.search(r"кадр|личн\w* состав|отпуск|увольн|прием на работу|премир", t):
            return contacts["hr"]
        return contacts["default"]

    def contact(self, s: Session, text: str) -> Reply:
        c = self._contact_for(text)
        hit = self.retriever.get_section(c["ref"]["doc_id"], c["ref"]["section"], s.role)
        body = f"**Куда обратиться:** {c['unit']}.\n\nОснование — {c['basis']}."
        sources = []
        if hit:
            body += f"\n\n**{hit.meta['short_title']}, п. {hit.section}:**\n{_quote(hit.text, 900)}"
            sources.append(Source(citation(hit.meta), 100, hit.text, hit.meta["doc_id"], hit.section))
        body += ("\n\nКонтактные телефоны и адреса в Инструкции не указаны — их публикует Департамент ДОУ "
                 "на внутреннем информационном портале (п. 12.10).")
        return Reply(body, sources=sources, intent="contact")

    # ================================================================ согласование (Матрица)
    def approval(self, s: Session, text: str, forced_kind: str | None = None) -> Reply:
        m = get_matrix()
        if not m.rows:
            return Reply("Матрица визирования не загружена. Выполните scripts/ingest.py --all.", intent="approval")
        text = self._with_context(s, text)
        kind = forced_kind or m.detect_kind(text) or s.params.get("doc_kind")
        if not kind:
            return self._ask(s, "Уточните, пожалуйста, **какой документ** нужно согласовать?",
                             [("Приказ", "приказ"), ("Распоряжение", "распоряжение"), ("Письмо", "письмо")],
                             {"type": "approval_kind", "query": text}, "approval")
        s.params["doc_kind"] = kind
        row = m.detect_row(text, kind)
        if not row:
            opts = m.options(kind)
            labels = {"order": "приказ", "disposition": "распоряжение", "letter": "письмо"}
            q = (f"Уточните, к какой категории относится {labels[kind]} — от этого зависит состав обязательных виз "
                 f"по Матрице обязательного визирования:")
            q += "\n" + "\n".join(f"- {r[1]}" for r in opts)
            return self._ask(s, q, [(self._short_row(r[1]), r[0]) for r in opts],
                             {"type": "approval_row", "kind": kind, "query": text}, "approval")
        return self.approval_answer(s, row, text)

    @staticmethod
    def _short_row(title: str) -> str:
        t = re.sub(r",? подготовленные с использованием модульных документов", "", title)
        t = re.sub(r"\s*\(.*?\)", "", t)
        return t if len(t) <= 70 else t[:67].rsplit(" ", 1)[0] + "…"

    def approval_answer(self, s: Session, row_id: str, query: str) -> Reply:
        m = get_matrix()
        a = m.lookup(row_id)
        lines = [f"**Обязательное визирование по Матрице** — «{a.row['title']}»:"]
        lines += [f"- ✅ {u}" for u in a.required] or ["- обязательных виз по Матрице нет"]
        lines += [f"- ⚠️ {u} — {c}" for u, c in a.conditional]
        kind = "letter" if a.row["group"] == "external" else ("disposition" if "аспоряж" in a.row["title"] else "order")
        sources = [Source(m.citation(row_id), 100, a.row["title"], "matrix_viz",
                          f"{'Внутренние' if a.row['group'] == 'internal' else 'Внешние'} документы, строка {a.row['num']}",
                          kind="matrix")]
        # Общий порядок визирования (п. 10.6) и срок рассмотрения (п. 10.3) — из базы знаний
        seq = self.retriever.get_section("instr_dou", "10.6", s.role)
        rev = self.retriever.get_section("instr_dou", "10.3", s.role)
        if rev:
            d = deadlines.match_item(deadlines.parse_items(rev.text), KIND_TITLES[kind] if kind != "order"
                                     else "приказ по основной деятельности")
            if d:
                lines.append(f"\n**Срок рассмотрения** ({d[0]}): не более {d[1]} на каждое первичное и повторное "
                             f"рассмотрение (п. 10.3 Инструкции).")
                sources.append(Source(citation(rev.meta), 100, rev.text, "instr_dou", "10.3"))
        if m.preamble:
            lines.append(f"\n_Матрица, п. 3: {m.preamble[-1].split('. ', 1)[-1]}_")
        if seq:
            lines.append(f"\n**Общая последовательность визирования (п. 10.6 Инструкции):**\n{_quote(seq.text, 1200)}")
            sources.append(Source(citation(seq.meta), 100, seq.text, "instr_dou", "10.6"))
        s.last_query = query
        return Reply("\n".join(lines), sources=sources, intent="approval", meta={"row": row_id})

    # ================================================================ сроки (п. 10.3, 6.6)
    def deadline(self, s: Session, text: str, forced_kind: str | None = None, strict: bool = False) -> Reply | None:
        kind = forced_kind or deadlines.detect_kind(text)
        if not kind:
            return None
        doc_id, section, meaning = deadlines.TABLES[kind]
        sec = self.retriever.get_section(doc_id, section, s.role)
        if not sec:
            return None
        items = deadlines.parse_items(sec.text)
        found = deadlines.match_item(items, text)
        if not found and not strict:
            # Нельзя угадывать срок — уточняем вид документа одним вопросом.
            what = "вид проекта документа" if kind == "review" else "от кого поступил документ (или его вид)"
            opts = [(n[:60], n) for n, _ in items][:12]
            return self._ask(s, f"Срок зависит от вида документа. Уточните, пожалуйста, **{what}**:", opts,
                             {"type": "deadline_item", "kind": kind, "query": text}, "deadline")
        if not found:
            found = deadlines.default_item(sec.text)
            if not found:
                return None
        name, term = found
        body = (f"**Срок:** {term} — {meaning} для вида «{name}» "
                f"(п. {section} Инструкции).\n\n**{sec.meta['short_title']}, п. {section}:**\n{_quote(sec.text, 1500)}")
        sources = [Source(citation(sec.meta), 100, sec.text, doc_id, section)]
        if kind == "execution":
            extra = self.retriever.get_section(doc_id, "6.7", s.role)
            if extra:
                body += f"\n\n**Срочные документы (п. 6.7):**\n{_quote(extra.text, 700)}"
                sources.append(Source(citation(extra.meta), 90, extra.text, doc_id, "6.7", related_to=section))
        else:
            extra = self.retriever.get_section(doc_id, "10.5", s.role)
            if extra:
                body += "\n\nПри повторном рассмотрении обязательна таблица замечаний (п. 10.5, Приложение № 1)."
                sources.append(Source(citation(extra.meta), 80, extra.text, doc_id, "10.5", related_to=section))
        s.last_query = text
        return Reply(body, sources=sources, intent="deadline")

    # ================================================================ шаблоны
    def start_template(self, s: Session, text: str) -> Reply:
        text = self._with_context(s, text)
        tid, scored = pick_template(text)
        templates = load_templates()
        if not tid:
            # LLM — только для подбора, если правила не дали однозначного ответа
            ids = list(templates)
            try:
                ans = self.llm.chat(prompts.pick_template_messages(text, [templates[i]["title"] for i in ids]),
                                    max_tokens=4)
                m = re.search(r"\d+", ans)
                n = int(m.group(0)) if m else 0
                if 1 <= n <= len(ids) and scored and dict((t, sc) for sc, t in scored).get(ids[n - 1], 0) >= 0.3:
                    tid = ids[n - 1]
            except Exception as e:  # noqa: BLE001
                log.warning("LLM-подбор шаблона недоступен: %s", e)
        if not tid:
            opts = [(templates[t]["title"], t) for _, t in scored[:7]]
            return self._ask(s, "Какой документ нужно подготовить? Доступные шаблоны (модули документов):\n"
                             + "\n".join(f"- {templates[t]['title']}" for _, t in scored[:7]),
                             opts, {"type": "template"}, "create_doc")
        return self.begin_fill(s, tid)

    def _approvers_text(self, tpl: dict, values: dict) -> tuple[str, list[Source]]:
        m = get_matrix()
        row = tpl.get("matrix_row")
        if not row or row not in m.rows:
            return "", []
        a = m.lookup(row)
        units = list(a.required)
        cond_map = {c["unit"]: c["field"] for c in tpl.get("matrix_conditions", [])}
        pending = []
        for u, cond in a.conditional:
            fld = cond_map.get(u)
            if fld and fld in values:
                if values[fld]:
                    units.append(u)
            else:
                pending.append(f"{u} — {cond}")
        txt = "**Обязательное визирование (Матрица, " + m.citation(row).split(", ", 2)[-1] + "):** " + ", ".join(units)
        if pending:
            txt += "; при условии: " + "; ".join(pending)
        return txt, [Source(m.citation(row), 100, a.row["title"], "matrix_viz", row, kind="matrix")]

    def begin_fill(self, s: Session, tid: str) -> Reply:
        tpl = load_templates()[tid]
        s.fill = FillSession(tpl)
        appr, src = self._approvers_text(tpl, {})
        intro = (f"Подобран шаблон: **{tpl['title']}** (модуль документа из базы знаний).\n\n"
                 f"{appr}\n\nЯ задам {sum(1 for f in tpl['fields'] if not f.get('only_if'))} вопросов по очереди и "
                 f"соберу черновик .docx. Команды: «назад» — исправить предыдущий ответ, «отмена» — прервать.")
        q = self._field_question(s)
        return Reply(intro + "\n\n" + q.text, sources=src, actions=q.actions, intent="create_doc",
                     meta={"template": tid})

    def _field_question(self, s: Session, error: str = "") -> Reply:
        f = s.fill.current()
        n, total = s.fill.progress()
        question = self._rephrase(f["question"])
        lines = []
        if error:
            lines.append(f"⚠️ {error}")
        lines.append(f"**Вопрос {n} из {total}.** {question}")
        if f.get("hint"):
            lines.append(f"_Подсказка: {f['hint']}_")
        if f.get("example"):
            lines.append(f"_Пример: {f['example']}_")
        actions = []
        if f.get("default"):
            lines.append(f"Предлагаю значение по умолчанию: «{f['default']}» — нажмите кнопку или введите своё.")
            actions.append(("Оставить по умолчанию", "да"))
        if f.get("type") == "yesno":
            actions += [("Да", "да"), ("Нет", "нет")]
        if f.get("type") == "choice":
            actions += [(o, o) for o in f["options"]]
        if f.get("optional"):
            actions.append(("Пропустить", "нет"))
        if f.get("type") == "control":
            actions.append(("Оставляю за собой", "оставляю за собой"))
        return Reply("\n".join(lines), actions=actions, intent="create_doc")

    def _rephrase(self, question: str) -> str:
        if not self.cfg["llm"].get("rephrase_questions"):
            return question
        cache = self.__dict__.setdefault("_rephrase_cache", {})
        if question in cache:
            return cache[question]
        out = question
        try:
            cand = guard.clean_answer(self.llm.chat(prompts.rephrase_messages(question), max_tokens=80))
            base = question.split("(")[0]
            # принимаем перефразировку, только если смысл сохранён: те же уточнения в скобках и близкие слова
            paren = re.findall(r"\(.*?\)", question)
            ok, _ = guard.check_explanation(cand, [question], min_overlap=0.6)
            if ok and cand.endswith(("?", ")")) and all(p in cand for p in paren) and len(cand) < len(question) * 1.6 \
                    and len(cand) > len(base) * 0.5:
                out = cand
        except Exception as e:  # noqa: BLE001
            log.warning("LLM-перефразирование недоступно: %s", e)
            return out   # не кэшируем: модель могла быть занята, в следующий раз попробуем снова
        cache[question] = out
        return out

    def _fill_step(self, s: Session, text: str) -> Reply:
        if CANCEL_RE.match(text):
            s.fill = None
            return Reply("Заполнение шаблона прервано. Чем ещё помочь?", intent="create_doc")
        if BACK_RE.match(text):
            s.fill.back()
            return self._field_question(s)
        res = s.fill.answer(text)
        if not res.ok:
            return self._field_question(s, res.error)
        if not s.fill.done():
            return self._field_question(s)
        return self.finish_fill(s)

    def finish_fill(self, s: Session) -> Reply:
        from app.templates.render import render

        fs, s.fill = s.fill, None
        tpl = fs.template
        out = render(tpl, fs.context())
        appr, src = self._approvers_text(tpl, fs.values)
        lines = [f"✅ Черновик **«{tpl['title']}»** готов — файл `{out.name}` можно скачать ниже.", "",
                 "**Заполненные поля:**"]
        lines += [f"- {q}: {v}" for q, v in fs.summary()]
        lines += ["", appr, "", "**Чек-лист требований к этому документу:**"]
        for item in tpl.get("checklist", []):
            doc_id, sec = item["ref"].split(":", 1)
            hit = self.retriever.get_section(doc_id, sec, s.role)
            if hit:
                lines.append(f"- [ ] {item['text']} — {hit.meta['short_title']}, п. {sec}")
                src.append(Source(citation(hit.meta), None, hit.text, doc_id, sec, kind="checklist"))
        lines.append("\nДальше: создайте РК проекта в ИИС ДО, укажите в поле «Примечание» применение модуля "
                     "(п. 2.3 метод. рекомендаций) и направьте проект по маршруту визирования.")
        return Reply("\n".join(lines), sources=src, files=[out], intent="create_doc",
                     meta={"template": tpl["id"], "file": str(out)})

    # ================================================================ проверка документа
    def review(self, s: Session, file: Path, text: str = "") -> Reply:
        from app.review.checker import review_document

        rep = review_document(file, hint=text)
        sources = []
        for item in rep["items"]:
            if item.get("ref"):
                doc_id, sec = item["ref"].split(":", 1)
                hit = self.retriever.get_section(doc_id, sec, s.role)
                if hit and all(x.section != sec or x.doc_id != doc_id for x in sources):
                    sources.append(Source(citation(hit.meta), None, hit.text, doc_id, sec, kind="checklist"))
        return Reply(rep["markdown"], sources=sources, intent="check_doc",
                     meta={"ok": rep["ok"], "issues": rep["issues"]})

    # ================================================================ справка
    def help(self) -> Reply:
        role_titles = ", ".join(r["title"] for r in roles().values())
        return Reply(
            "Я помощник по Инструкции по документационному обеспечению управления и документообороту. Умею:\n"
            "1. **Отвечать на вопросы** по Инструкции и показывать пункты с процентом релевантности — "
            "«Когда регистрировать входящий документ?»\n"
            "2. **Подсказывать, с кем согласовать** документ (по Матрице визирования) и **сроки** — "
            "«Кто визирует распоряжение о создании рабочей группы?»\n"
            "3. **Готовить черновики .docx по модулям** документов, задавая вопросы по очереди — "
            "«Нужно подготовить распоряжение о создании рабочей группы»\n"
            "4. **Проверять загруженный черновик** (.docx/.pdf) на соответствие требованиям\n"
            "5. **Подсказывать, куда обратиться**, если ответа в базе знаний нет.\n\n"
            f"Роли: {role_titles}. Отвечаю только по документам базы знаний.",
            actions=[("Когда регистрировать входящий?", "Когда нужно зарегистрировать входящий документ?"),
                     ("С кем согласовать распоряжение?", "С кем согласовать распоряжение?"),
                     ("Подготовить распоряжение", "Нужно подготовить распоряжение о создании рабочей группы")],
            intent="help")


def DEFAULT_ROW_FOR(kind: str) -> str | None:  # noqa: N802
    from app.approval.matrix import DEFAULT_ROW
    return DEFAULT_ROW.get(kind)


_engine: Engine | None = None


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = Engine()
    return _engine
