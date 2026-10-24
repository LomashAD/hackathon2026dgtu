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
from app.dialog import intents, normalize
from app.llm import guard, prompts
from app.llm.client import get_client
from app.retrieval.search import SECTION_REQ_RE, Hit, get_retriever
from app.retrieval.text import tokens
from app.security import audit
from app.security.access import can_view_archive, roles
from app.templates.catalog import load_templates, pick_template
from app.templates.slots import FillSession, validate

log = logging.getLogger(__name__)

CANCEL_RE = re.compile(r"^\s*(отмена|отменить|стоп|выход|выйти|прервать|хватит|отбой|передумал\w*|прекрати\w*|"
                       r"остановись|cancel)\b", re.I)
BACK_RE = re.compile(r"^\s*(назад|вернуться|вернись|исправить предыдущ\w*|предыдущий вопрос)\b", re.I)
FOLLOWUP_RE = re.compile(r"^\s*(а|и|но|тогда|а если|а что|а как|а кто|а когда|а где|это|он|она|они|его|ее|их|там|"
                         r"при этом|в таком случае|а для)\b", re.I)
# Отсылка к предыдущему: без неё «а что такое РК?» — новый вопрос, а не продолжение вопроса про ЭКД
REFERS_RE = re.compile(r"\b(его|её|ее|него|неё|нее|им|ним|ней|их|них|это\w*|эт[аоиу]|тот|та|те|там|тогда|при этом|"
                       r"в таком случае|если)\b|(?<!что )\bтак(ой|ая|ое|ие|ую)\b", re.I)
# Во время заполнения шаблона: «не знаю», «а что писать?», «пример» — подсказка к текущему вопросу, а не ответ
FILL_HELP_RE = re.compile(r"^\s*(не знаю\w*( что (писать|указать))?|хз|без понятия|понятия не имею|затрудняюсь\w*( ответить)?|"
                          r"(а )?что (тут |здесь |сюда )?(писать|указать|указывать|вводить|ввести|написать)|"
                          r"как (это |тут |здесь )?(писать|заполнить|заполнять|указать|правильно)|пример\w*|"
                          r"(дай|покажи|приведи) пример\w*|подскажи\w*|помоги\w*|помощь|не понял\w*|непонятно|"
                          r"что это( значит)?|(а )?зачем( это)?( нужно)?|для чего( это)?( нужно)?)\s*[?!.]*\s*$", re.I)


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
    offer: dict | None = None            # предложенное действие («подготовить черновик?») — ждёт «да» следующей репликой
    last_hits: list = field(default_factory=list)   # процитированные пункты последнего ответа — для «объясни проще»
    last_alts: list = field(default_factory=list)   # другие найденные пункты — для «не то»
    last_simple: str = ""                # «проще говоря» для ответов по Матрице, срокам, контактам


def fmt_date(iso: str) -> str:
    try:
        return dt.date.fromisoformat(iso).strftime("%d.%m.%Y")
    except ValueError:
        return iso


def section_label(meta: dict) -> str:
    """Место в документе: п. 5.4, фрагмент 2, Приложение № 1; строка Матрицы или таблицы («лист «Дела», стр. 5») — как есть."""
    sec = meta["section_path"]
    if meta.get("appendix"):
        return f"Приложение № {meta['appendix']}"
    if meta.get("kind") in ("matrix_row", "row"):
        return sec
    if meta.get("kind") == "fragment":
        return f"фрагмент {sec}"
    return f"п. {sec}"


def citation(meta: dict) -> str:
    """Ссылка на источник из метаданных чанка: документ, редакция, пункт/приложение, глава."""
    s = f"{meta['short_title']}, ред. {meta['version']} (с {fmt_date(meta['effective_date'])}), {section_label(meta)}"
    if meta.get("chapter") and meta["doc_id"] == "instr_dou":
        s += f" — гл. {meta['chapter']} «{meta['heading']}»"
    elif meta.get("heading") and meta.get("kind") not in ("matrix_row", "row"):
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
        offer, s.offer = s.offer, None
        if file is not None:
            s.pending = None
            return self.review(s, file, text)
        if s.fill is not None:
            return self._fill_step(s, text)
        # Разговорная речь → текст для правил и поиска: сленг, опечатки, раскладка, вежливые обороты
        n = normalize.normalize(text, self.retriever.vocabulary(s.role))
        if offer and n.smalltalk in ("yes", "ack"):
            return self.begin_fill(s, offer["template"])
        if offer and n.smalltalk == "no":
            return Reply("Хорошо, черновик не готовлю. Если понадобится — напишите, какой документ подготовить.",
                         intent="smalltalk")
        if s.pending is not None:
            pend, s.pending = s.pending, None
            if CANCEL_RE.match(text) or n.smalltalk == "cancel":
                return Reply("Хорошо, отменил. Задайте новый вопрос.", intent="cancel")
            r = self._resolve_pending(s, pend, n.text)
            if r is not None:
                return r
        if n.smalltalk:
            return self.small_talk(s, n)
        r = self._dispatch(s, n)
        # Как прочитано сообщение: исправленные опечатки и раскладка — видно, что помощник понял
        notes = []
        if n.layout:
            notes.append(f"_Текст был набран в английской раскладке — прочитал как «{n.text}»._")
        if n.fixes:
            notes.append("_Исправил опечатк" + ("и" if len(n.fixes) > 1 else "у") + ": "
                         + ", ".join(f"«{a}» → «{b}»" for a, b in n.fixes) + "._")
        if notes and r.intent not in ("smalltalk", "help"):
            r.text = "\n".join(notes) + "\n\n" + r.text
        return r

    def _dispatch(self, s: Session, n: normalize.Normalized) -> Reply:
        q = n.text
        intent, how = intents.classify(q, llm=self.llm)
        log.info("intent=%s (%s)", intent, how)
        # «а приказ о выходных?» после вопроса о визировании, «а для приказа?» после срока — та же тема
        if intent == "question" and how != "rule" and self._is_followup(s, q):
            r = self._continue(s, q)
            if r is not None:
                return r
        s.last_intent = intent
        if intent == "help":
            return self.help()
        if intent == "create_doc":
            return self.start_template(s, q)
        if intent == "check_doc":
            return Reply("Загрузите черновик документа (.docx или .pdf) — кнопкой со скрепкой или перетащите файл "
                         "в окно чата. Я проверю его на соответствие требованиям Инструкции и модуля.",
                         intent="check_doc")
        if intent == "approval":
            return self.approval(s, q)
        if intent == "deadline_calc":
            # «а если приказ, к какому числу?» сразу после расчёта — к тем же дате и этапу
            last = s.history[-1]["intent"] if s.history else ""
            base = s.params.get("calc") if last == "deadline_calc" and FOLLOWUP_RE.match(q) else None
            r = self.deadline_calc(s, q, base=base, raw=n.original)
            if r is not None:
                return r
        if intent == "deadline":
            r = self.deadline(s, q)
            if r is not None:
                return r
        if intent == "contact":
            return self.contact(s, q)
        return self.answer(s, q, intent, statement=how in ("default", "llm"))

    # ================================================================ разговорные реплики
    SMALLTALK_TEXT = {
        "thanks": "Пожалуйста! Если появятся ещё вопросы по документам — пишите.",
        "ack": "Хорошо! Если понадобится что-то ещё — спрашивайте.",
        "filler": "Слушаю! Задайте вопрос о документах — например, «когда регистрировать входящее письмо?».",
        "bye": "До свидания! Обращайтесь, если понадобится помощь с документами.",
        "how": "Всё в порядке, спасибо! Готов помочь с документами: ответить на вопрос по Инструкции, подсказать, "
               "с кем согласовать проект, или подготовить черновик по модулю.",
        "who": "Я — помощник по документообороту, программа, а не человек. Отвечаю только по документам базы знаний — "
               "Инструкции по ДОУ, Матрице обязательного визирования и Методическим рекомендациям по модульным "
               "документам — и всегда показываю, на какой пункт опираюсь. Ещё готовлю черновики по модулям и проверяю "
               "готовые документы. Можно писать своими словами, с опечатками — я пойму. Подробнее — /help.",
        "cancel": "Хорошо, ничего не делаю. Когда понадобится — просто задайте вопрос.",
        "wait": "Конечно, жду. Когда будете готовы — пишите.",
        "yes": "Уточните, пожалуйста, что нужно сделать — например: «подготовь распоряжение о создании рабочей группы» "
               "или «кто визирует приказ о выходных?».",
        "no": "Хорошо. Если понадобится помощь — пишите.",
        "rude": "Извините, что ответ не помог. Попробуйте сказать иначе: какой документ и что с ним нужно сделать. "
                "Если вопрос сложный, проконсультируйтесь в Департаменте ДОУ (п. 2.1 Инструкции).",
    }
    # Первые три — карточки приветствия (app/main.py, сетка в 3 колонки); четвёртый — только кнопкой
    EXAMPLES = [("Когда регистрировать входящий?", "Когда нужно зарегистрировать входящий документ?"),
                ("Кто визирует приказ о выходных?", "Кто визирует приказ о работе в выходные дни?"),
                ("Подготовить распоряжение", "Нужно подготовить распоряжение о создании рабочей группы"),
                ("Когда согласуют распоряжение?", "Если отправлю распоряжение на согласование сегодня, когда будет готово?")]

    def small_talk(self, s: Session, n: normalize.Normalized) -> Reply:
        """Ответ на реплику без вопроса — без поиска и без отказа «недостаточно информации»."""
        kind = n.smalltalk
        if kind in ("greeting", "help"):
            r = self.help()
            if kind == "greeting":
                r.text = "Здравствуйте! " + r.text
            return r
        if kind == "confused":
            return self.reexplain(s)
        if kind == "wrong":
            return self.wrong_answer(s)
        actions = self.EXAMPLES if kind in ("who", "how", "yes", "filler") else []
        return Reply(self.SMALLTALK_TEXT[kind], actions=actions, intent="smalltalk")

    def reexplain(self, s: Session) -> Reply:
        """«Не понял», «объясни проще» — ещё раз и проще о последнем ответе."""
        if s.last_simple:
            return Reply(s.last_simple, intent="smalltalk")
        if not s.last_hits:
            return Reply("Давайте попробую иначе. Напишите вопрос своими словами — например: «когда регистрировать "
                         "входящее письмо?» или «кто визирует приказ о выходных?». Можно без официальных слов и "
                         "с опечатками — я пойму.", actions=self.EXAMPLES, intent="smalltalk")
        h = s.last_hits[0]
        ref = f"{h.meta['short_title']}, {section_label(h.meta)}"
        out = ""
        if self.cfg["llm"].get("explain", True):
            try:
                out = guard.clean_answer(self.llm.chat(prompts.simpler_messages(s.last_query, [h.text]), max_tokens=120))
                ok, why = guard.check_explanation(out, [h.text, f"п. {h.section}"], question=s.last_query)
                log.info("simpler check: %s (%s) %r", ok, why, out[:200])
                out = out if ok else ""
            except Exception as e:  # noqa: BLE001
                log.warning("LLM недоступна: %s", e)
                out = ""
        if out:
            text = f"Попробую проще: {out}\n\n_Основание — {ref}._"
        else:
            first = re.split(r"(?<=[.;])\s+", h.text.strip())[0]
            text = (f"Главное в {ref}: {first}\n\nЕсли непонятна конкретная часть — спросите о ней своими словами, "
                    "например: «а если документ пришёл вечером?» или «кто это делает?».")
        return Reply(text, sources=self._sources([h]), intent="smalltalk")

    def wrong_answer(self, s: Session) -> Reply:
        """«Не то», «неправильно» — оценка 👎 в журнал и другие найденные пункты на выбор."""
        if s.last_query:
            audit.log_feedback(s.user, s.id, s.last_query, "down")
        text = ("Похоже, я понял вопрос не так — извините. Уточните, пожалуйста: о каком документе речь (входящий, "
                "исходящий, приказ, письмо…) и что с ним нужно сделать.")
        actions = [(self._point_label(h), self._point_request(h)) for h in s.last_alts[:3]]
        if actions:
            text += "\n\nИли посмотрите другие пункты, которые я нашёл по этому вопросу:"
        return Reply(text, actions=actions, intent="smalltalk")

    @staticmethod
    def _point_label(h: Hit) -> str:
        head = re.sub(r"\s+", " ", h.text.split("\n", 1)[0])
        head = head if len(head) <= 48 else head[:46].rsplit(" ", 1)[0] + "…"
        return f"п. {h.section}: {head}"

    @staticmethod
    def _point_request(h: Hit) -> str:
        return f"пункт {h.section}" + (" методических рекомендаций" if h.meta["doc_id"] == "mr_modules" else "")

    # ================================================================ продолжение темы
    @staticmethod
    def _is_followup(s: Session, text: str) -> bool:
        if not s.last_query:
            return False
        return bool(FOLLOWUP_RE.match(text)) or (len(text.split()) <= 3 and not intents.QUESTION_START.search(text))

    def _continue(self, s: Session, text: str) -> Reply | None:
        if s.last_intent == "approval":
            m = get_matrix()
            if m.detect_kind(text) or m.detect_row(text, s.params.get("doc_kind")):
                return self.approval(s, text)
        if s.last_intent == "deadline" and s.params.get("deadline_kind"):
            return self.deadline(s, text, forced_kind=s.params["deadline_kind"], followup=True)
        if s.last_intent == "deadline_calc" and s.params.get("calc"):
            return self.deadline_calc(s, text, base=s.params["calc"], followup=True)
        return None

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
        if kind == "deadline_calc":
            c = pend["calc"]
            if re.match(r"\s*только срок", text, re.I):   # кнопка «Только срок в днях» — ответ без дат, как раньше
                return self.deadline(s, f"срок {c['query']} {c.get('doc') or c.get('sender') or ''}",
                                     forced_kind="execution" if c.get("stage") == "execution" else "review", strict=True)
            return self.deadline_calc(s, text, base=c, followup=True)
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

    def _rewrite(self, s: Session, text: str) -> tuple[str, str]:
        """Учёт истории: короткое уточнение подставляется в предыдущий вопрос («а исходящий?») или дополняет его.
        Возвращает (запрос, как получен: "" | subst | concat)."""
        asks = intents.QUESTION_START.search(text)
        short = len(text.split()) <= 3 and not asks
        own = asks and not REFERS_RE.search(text)   # свой вопрос без отсылок — новый вопрос
        if s.last_query and not SECTION_REQ_RE.search(text) and not own and (FOLLOWUP_RE.match(text) or short) and \
                s.last_intent in ("question", "find_section", "deadline"):
            sub = normalize.substitute(s.last_query, text, choose=self._closest)
            return (sub, "subst") if sub else (f"{s.last_query} {text}", "concat")
        return text, ""

    def _closest(self, word: str, cands: list[re.Match]) -> re.Match | None:
        """Из нескольких подходящих по грамматике слов вопроса — ближайшее по смыслу к слову уточнения."""
        try:
            vecs = self.llm.embed([word] + [m.group(0) for m in cands])
        except Exception as e:  # noqa: BLE001
            log.warning("эмбеддинги недоступны: %s", e)
            return None

        def cos(a, b):
            return sum(x * y for x, y in zip(a, b)) / ((sum(x * x for x in a) * sum(y * y for y in b)) ** 0.5 or 1)

        return max(zip(cands, vecs[1:]), key=lambda cv: cos(vecs[0], cv[1]))[0]

    def _reformulate(self, query: str) -> str:
        """Второй шанс поиска: разговорный вопрос, переписанный моделью деловым языком. Числа — только из вопроса."""
        # Без содержательных слов («ну и?») модели нечего переписывать — она бы придумала вопрос сама
        if not self.cfg["llm"].get("reformulate", True) or guard.INJECTION_RE.search(query) or len(tokens(query)) < 2:
            return ""
        try:
            out = guard.clean_answer(self.llm.chat(prompts.reformulate_messages(query), max_tokens=60))
        except Exception as e:  # noqa: BLE001
            log.warning("LLM-переформулировка недоступна: %s", e)
            return ""
        out = out.split("\n")[0].strip()
        if not 8 <= len(out) <= 220 or guard.foreign(out, query) or out.lower().strip(" ?") == query.lower().strip(" ?") \
                or set(guard.NUM_RE.findall(out)) - set(guard.NUM_RE.findall(query)):
            return ""
        return out

    def answer(self, s: Session, text: str, intent: str = "question", statement: bool = False) -> Reply:
        query, how = self._rewrite(s, text)
        archived = s.include_archived and can_view_archive(s.role)
        res = self.retriever.search(normalize.expand(query), s.role, include_archived=archived)
        alt = ""
        if not res.enough or statement and res.best < self.rcfg.get("weak_relevance", 0):
            alt = self._reformulate(query)
            res2 = self.retriever.search(normalize.expand(alt), s.role, include_archived=archived) if alt else None
            log.info("второй шанс: %r → %r (%.2f → %.2f)", query, alt, res.best, res2.best if res2 else 0)
            if res2 and res2.enough and res2.best >= res.best + self.rcfg.get("alt_gain", 0.05):
                res = res2
            else:
                alt = ""
        s.last_query = alt or query
        s.last_hits, s.last_alts, s.last_simple = [], [], ""
        if not res.enough or not res.hits:
            audit.log_unanswered(s.role, query, res.best)
            return self.refusal(text, res.best, res.hits)
        hits = res.hits
        main = [h for h in hits if not h.related_to]
        quotes = main[: self.rcfg["quote_count"]]
        if len(quotes) > 1 and quotes[1].relevance < quotes[0].relevance - self.rcfg.get("quote_gap", 0.03):
            quotes = quotes[:1]
        s.last_hits, s.last_alts = quotes, [h for h in main if h not in quotes and h.meta.get("kind") == "point"]
        # Как понят вопрос: уточнение в контексте диалога или вопрос, переписанный для второго шанса поиска
        notes = [f"_Понял вопрос так: «{alt or query}»_"] if alt or how == "subst" else []
        # Сначала — простыми словами (только по лучшему пункту: маленькая модель путает смысл при смешении),
        # затем — дословно, как в документе, с релевантностью
        expl = self.explain(alt or query, quotes[:1])
        parts = []
        for h in quotes:
            parts.append(f"**{h.meta['short_title']}, {section_label(h.meta)}** (релевантность {h.percent} %):\n"
                         f"{_quote(h.text, self.rcfg['quote_max_chars'])}")
        body = "\n".join(notes) + ("\n\n" if notes else "")
        if expl:
            body += f"**Простыми словами:** {expl}\n\n**Как в документе:**\n\n"
        body += "\n\n".join(parts)
        others = [h for h in hits if h not in quotes]
        if others:
            body += "\n\n**См. также:** " + "; ".join(
                f"{'п. ' + h.section if h.meta.get('kind') == 'point' else 'фрагмент ' + h.section if h.meta.get('kind') == 'fragment' else h.section} — {h.percent} %"
                + (f" (связан с п. {h.related_to})" if h.related_to else "") for h in others)
        offer, actions = self._offer_template(s, alt or query)
        return Reply(body + offer, sources=self._sources(hits), actions=actions, intent=intent,
                     meta={"best": res.best, "explained": bool(expl), "understood": alt or query})

    def _offer_template(self, s: Session, text: str) -> tuple[str, list[tuple[str, str]]]:
        """От ответа — к действию: если вопрос о документе, для которого есть модуль, предлагаем черновик."""
        tid, _ = pick_template(text)
        if not tid or s.params.get("_side"):
            return "", []
        title = load_templates()[tid]["title"]
        s.offer = {"template": tid}
        return (f"\n\n📝 Могу сразу подготовить черновик **«{title}»** по модулю: задам вопросы по очереди и соберу "
                f".docx. Напишите «да» или нажмите кнопку.", [("📝 Подготовить черновик", f"Подготовь черновик: {title}")])

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

    def refusal(self, text: str, best: float, near: list[Hit] | None = None) -> Reply:
        c = self._contact_for(text)
        msg = ("**В базе знаний недостаточно информации для ответа на этот вопрос** — гадать не буду. "
               "Я отвечаю только по загруженным документам: Инструкции по ДОУ, Матрице обязательного визирования "
               "и Методическим рекомендациям по модульным документам.\n\n"
               f"**Куда обратиться:** {c['unit']} ({c['basis']}).")
        # Пункты чуть ниже порога — не ответ, а подсказка: открыть их можно кнопкой
        near = [h for h in near or [] if h.meta.get("kind") == "point" and not h.related_to][:3]
        actions = [(self._point_label(h), self._point_request(h)) for h in near]
        if near:
            msg += ("\n\nБлизко по теме, но прямого ответа там может не быть: "
                    + "; ".join(f"п. {h.section} ({h.meta['short_title']}) — {h.percent} %" for h in near) + ".")
        msg += ("\n\nЕсли вопрос о документах — попробуйте сказать иначе: какой документ (приказ, письмо, служебная "
                "записка) и что с ним нужно сделать.")
        src = []
        hit = self.retriever.get_section(c["ref"]["doc_id"], c["ref"]["section"], "employee")
        if hit:
            src.append(Source(citation(hit.meta), None, hit.text, hit.meta["doc_id"], hit.section, kind="contact"))
        return Reply(msg, sources=src, actions=actions, intent="no_answer", refused=True, meta={"best": best})

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
        s.last_query, s.last_hits = text, []
        s.last_simple = (f"Проще говоря: с этим вопросом — в {c['unit'].split(';')[0]}. Телефоны и адреса — "
                         "на внутреннем портале.")
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
        s.last_query, s.last_intent, s.last_hits = query, "approval", []
        s.last_simple = ("Проще говоря: до подписания проект должны завизировать "
                         + (", ".join(a.required) if a.required else "те, кого определяет исполнитель (обязательных виз нет)")
                         + (". Если выполняется условие — ещё " + "; ".join(f"{u} ({c})" for u, c in a.conditional)
                            if a.conditional else "") + ". Порядок: сначала ответственный исполнитель и руководитель "
                         "его подразделения, затем заинтересованные подразделения и подразделения обязательного "
                         "визирования (п. 10.6 Инструкции).")
        offer, actions = self._offer_template(s, query)
        return Reply("\n".join(lines) + offer, sources=sources, actions=actions, intent="approval", meta={"row": row_id})

    # ================================================================ сроки (п. 10.3, 6.6)
    def deadline(self, s: Session, text: str, forced_kind: str | None = None, strict: bool = False,
                 followup: bool = False) -> Reply | None:
        """followup — уточнение к предыдущему вопросу о сроке («а для приказа?»): без подходящей строки — None."""
        kind = forced_kind or deadlines.detect_kind(text)
        if not kind:
            return None
        doc_id, section, meaning = deadlines.TABLES[kind]
        sec = self.retriever.get_section(doc_id, section, s.role)
        if not sec:
            return None
        items = deadlines.parse_items(sec.text)
        found = deadlines.match_item(items, text)
        if not found and followup:
            return None
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
        s.last_query, s.last_intent, s.last_hits = text, "deadline", []
        s.params["deadline_kind"] = kind
        s.last_simple = (f"Проще говоря: на рассмотрение проекта вида «{name}» у каждого согласующего — не больше {term}."
                         if kind == "review" else
                         f"Проще говоря: если в поручении не указан свой срок, документ вида «{name}» нужно исполнить "
                         f"в течение {term} с даты его регистрации.")
        return Reply(body, sources=sources, intent="deadline")

    # ================================================================ сроки в датах (калькулятор)
    # Срок в днях — из текста пунктов (п. 10.3, 10.15, 6.6, 6.7), одновременно или по очереди — из п. 10.2 и 10.6,
    # кто — из Матрицы, даты — по производственному календарю (deadlines.get_calendar). Модель не участвует.
    CALC_GEN = {"approval": "согласования", "review": "рассмотрения", "execution": "исполнения",
                "signing": "подписания", "visa": "визирования", "linguistic": "лингвистической экспертизы"}

    def deadline_calc(self, s: Session, text: str, base: dict | None = None, followup: bool = False,
                      raw: str = "") -> Reply | None:
        """«Когда будет готово, если отправлю сегодня?» — дата отправки + срок из Инструкции по календарю.
        base — уже известное (ответ на уточняющий вопрос, «а если приказ?»); followup — без новых сведений
        в реплике возвращает None, и реплика обрабатывается как обычно."""
        today = deadlines.today()
        got = self._calc_read(s, text, raw, today)
        if followup and not got:
            return None
        calc = {**(base or {}), **got}
        calc.setdefault("query", text)
        stage = calc.setdefault("stage", "approval")
        s.params["calc"] = calc
        s.last_query, s.last_intent, s.last_hits, s.last_simple = calc["query"], "deadline_calc", [], ""
        if stage in ("visa", "linguistic", "signing"):
            sec = self.retriever.get_section("instr_dou", deadlines.POINTS[stage], s.role)
            term = deadlines.find_term(sec.text) if sec else None
            if not term:
                return self._calc_no_term(s, text, calc, sec)
        else:
            doc_id, section, _ = deadlines.TABLES["execution" if stage == "execution" else "review"]
            if not self.retriever.get_section(doc_id, section, s.role):
                return None
        need_doc = (stage in ("approval", "review") and not calc.get("doc")) or \
                   (stage == "execution" and not calc.get("sender") and not calc.get("urgency"))
        if need_doc or not calc.get("date"):
            return self._calc_ask(s, calc, need_doc, not calc.get("date"), today)
        return self._calc_answer(s, calc, today)

    def _calc_read(self, s: Session, text: str, raw: str, today: dt.date) -> dict:
        """Что есть в реплике: этап, дата отправки, вид документа (п. 10.3), от кого поступил (п. 6.6), срочность."""
        got = {}
        if st := deadlines.detect_stage(text):
            got["stage"] = st
        ref = deadlines.parse_date_from_text(text, today) or (deadlines.parse_date_from_text(raw, today) if raw else None)
        if ref:
            got["date"], got["date_phrase"] = ref.date.isoformat(), ref.phrase
        rev = self.retriever.get_section(*deadlines.TABLES["review"][:2], s.role)
        if rev and (d := deadlines.match_doc(deadlines.parse_items(rev.text), text)):
            got["doc"], got["doc_text"] = d[0], text
        ex = self.retriever.get_section(*deadlines.TABLES["execution"][:2], s.role)
        if ex and (d := deadlines.match_sender(deadlines.parse_items(ex.text), text)):
            got["sender"] = d[0]
        if re.search(r"весьма срочн", text, re.I):
            got["urgency"] = "very"
        elif re.search(r"\bсрочн", text, re.I):
            got["urgency"] = "urgent"
        return got

    def _calc_ask(self, s: Session, calc: dict, need_doc: bool, need_date: bool, today: dt.date) -> Reply:
        """Один уточняющий вопрос с кнопками: вид документа и/или дата отправки — угадывать нельзя."""
        if need_doc:
            execution = calc["stage"] == "execution"
            sec = self.retriever.get_section(*deadlines.TABLES["execution" if execution else "review"][:2], s.role)
            names = [n for n, _ in deadlines.parse_items(sec.text)]
            if not execution:
                names = names[:6] + [n for n in names if n.startswith("иной")]
            what = "от кого поступил документ (или его вид)" if execution else "какой документ"
            q = f"Срок зависит от вида документа. Уточните, пожалуйста, **{what}**"
            if need_date:
                q += (f" и **когда его отправили**. Кнопки посчитают от сегодняшнего дня ({deadlines.fmt_day(today)}), "
                      "другую дату напишите сами — например: «приказ, 12 ноября».")
            else:
                q += ":"
            suffix = ", сегодня" if need_date else ""
            opts = [((n[:1].upper() + n[1:])[:60], n + suffix) for n in names]
        else:
            q = ("Уточните, пожалуйста, **когда документ отправлен** (или будет отправлен) — от этой даты считаю срок. "
                 "Можно написать дату: «12 ноября», «15.11», «во вторник».")
            mon = today + dt.timedelta(days=7 - today.weekday())
            opts = [(f"Сегодня ({deadlines.fmt_day(today)})", "сегодня"),
                    (f"Завтра ({deadlines.fmt_day(today + deadlines.DAY)})", "завтра"),
                    (f"В понедельник ({deadlines.fmt_day(mon)})", f"{mon:%d.%m.%Y}")]
            if calc["stage"] in ("approval", "review", "execution"):
                opts.append(("Только срок в днях", "только срок"))
        return self._ask(s, q, opts, {"type": "deadline_calc", "calc": calc}, "deadline_calc")

    def _calc_no_term(self, s: Session, text: str, calc: dict, sec: Hit | None) -> Reply:
        """Срока для этапа в Инструкции нет — честный отказ и куда обратиться, как в обычных отказах."""
        stage = calc["stage"]
        why = {"visa": "Инструкция устанавливает только порядок визирования — строго по очереди (п. 10.6), "
                       "а срок одной визы не указывает.",
               "linguistic": "Пункт 10.13 перечисляет, какие документы проходят лингвистическую экспертизу, "
                             "но срок экспертизы не указывает.",
               "signing": "Пункт 10.15 о подписании срока не указывает."}.get(stage, "")
        c = self._contact_for(text)
        msg = (f"**Срок {self.CALC_GEN[stage]} в базе знаний не указан** — посчитать дату не могу, гадать не буду. "
               f"{why} Срок в рабочих днях Инструкция устанавливает для рассмотрения проекта (п. 10.3).\n\n"
               f"**Куда обратиться:** {c['unit']} ({c['basis']}).")
        src = [Source(citation(sec.meta), None, sec.text, sec.meta["doc_id"], sec.section)] if sec else []
        hit = self.retriever.get_section(c["ref"]["doc_id"], c["ref"]["section"], s.role)
        if hit:
            src.append(Source(citation(hit.meta), None, hit.text, hit.meta["doc_id"], hit.section, kind="contact"))
        actions = []
        if calc.get("date"):
            doc = f" ({calc['doc']})" if calc.get("doc") else ""
            actions = [("Посчитать срок рассмотрения", f"Когда истекает срок рассмотрения{doc}, если отправить "
                                                       f"{dt.date.fromisoformat(calc['date']):%d.%m.%Y}?")]
        audit.log_unanswered(s.role, text, 0.0)
        return Reply(msg, sources=src, actions=actions, intent="deadline_calc", refused=True, meta={"best": 0.0})

    def _calc_who(self, calc: dict) -> tuple[list[str], list[tuple[str, str]], str | None, str | None]:
        """Подразделения обязательного визирования по Матрице для вида документа (строка — по теме, иначе «прочие»).
        Возвращает также вид документа по Матрице: для письма без адресата строка не выбирается."""
        m = get_matrix()
        txt = f"{calc.get('doc_text', '')} {calc.get('doc', '')}"
        kind = m.detect_kind(txt)
        row = (m.detect_row(txt, kind) or DEFAULT_ROW_FOR(kind)) if kind and m.rows else None
        if not row or row not in m.rows:
            return [], [], None, kind
        a = m.lookup(row)
        return a.required, a.conditional, row, kind

    def _calc_steps(self, s: Session, calc: dict) -> tuple[list[list[deadlines.Step]], list[deadlines.Step], dict]:
        """Этапы с датами (один вариант или два — одновременно / по очереди) и этапы после них без дат."""
        stage, info = calc["stage"], {"points": [], "row": None, "cond": []}
        pts = info["points"]
        if stage in ("signing", "visa", "linguistic"):
            n, unit, rule = deadlines.find_term(self.retriever.get_section(
                "instr_dou", deadlines.POINTS[stage], s.role).text)
            pts.append(deadlines.POINTS[stage])
            who = {"signing": "подписант", "visa": "визирующие по очереди (п. 10.6)",
                   "linguistic": "Департамент ДОУ"}[stage]
            title = {"signing": "Подписание", "visa": "Визирование", "linguistic": "Лингвистическая экспертиза"}[stage]
            basis = f"п. {deadlines.POINTS[stage]}" + (", как правило" if rule else "")
            return [[deadlines.Step(title, who, n, unit, basis, after="start")]], [], info
        if stage == "execution":
            if calc.get("urgency"):
                u = self.retriever.get_section("instr_dou", deadlines.POINTS["urgent"], s.role)
                term = deadlines.urgent_term(u.text, calc["urgency"] == "very") if u else None
                title = "Исполнение — отметка «" + ("Весьма срочно" if calc["urgency"] == "very" else "Срочно") + "»"
                basis = f"п. {deadlines.POINTS['urgent']}"
            else:
                ex = self.retriever.get_section(*deadlines.TABLES["execution"][:2], s.role)
                item = next(((n, t) for n, t in deadlines.parse_items(ex.text) if n == calc["sender"]), None)
                term = deadlines.parse_term(item[1]) if item else None
                title, basis = f"Исполнение — {calc['sender']}", f"п. {deadlines.TABLES['execution'][1]}"
            if not term:
                return [], [], info
            pts += [basis.removeprefix("п. "), deadlines.POINTS["count"]]
            return [[deadlines.Step(title, "исполнитель", term[0], term[1], basis, after="start")]], [], info
        # согласование и рассмотрение (п. 10.2, 10.3)
        rev = self.retriever.get_section(*deadlines.TABLES["review"][:2], s.role)
        item = next(((n, t) for n, t in deadlines.parse_items(rev.text) if n == calc["doc"]), None)
        term = deadlines.parse_term(item[1]) if item else None
        if not term:
            return [], [], info
        n, unit = term
        req, cond, row, mkind = self._calc_who(calc)
        info["row"], info["cond"] = row, cond
        order = self.retriever.get_section("instr_dou", deadlines.POINTS["order"], s.role)
        mode = deadlines.review_mode(order.text) if order else "unknown"
        info["mode"] = mode
        pts += [deadlines.POINTS["order"], deadlines.TABLES["review"][1]] if order else [deadlines.TABLES["review"][1]]
        who = "; ".join(filter(None, [
            ", ".join(req) + " — обязательно по Матрице" if req else "",
            "при условии — " + ", ".join(u for u, _ in cond) if cond else "",
            "обязательные по Матрице — зависят от адресата письма" if mkind == "letter" and not row else "",
            "другие заинтересованные подразделения (определяет исполнитель)"]))
        basis = f"п. {deadlines.TABLES['review'][1]}"
        together = [deadlines.Step("Первичное рассмотрение — одновременно во всех подразделениях", who, n, unit, basis,
                                   after="start")]
        chain = [deadlines.Step(f"Первичное рассмотрение по очереди: {u}", u, n, unit, basis,
                                after="start" if i == 0 else "prev") for i, u in enumerate(req)]
        variants = {"parallel": [together], "sequential": [chain or together]}.get(mode, [together, chain] if chain
                                                                                  else [together])
        later = [deadlines.Step("Повторное рассмотрение — если были замечания", "те же подразделения", n, unit, basis,
                                after="later", note=f"{deadlines.days_word(n, unit)} со дня направления новой редакции")]
        if stage == "approval":
            visa = self.retriever.get_section("instr_dou", deadlines.POINTS["visa"], s.role)
            sign = self.retriever.get_section("instr_dou", deadlines.POINTS["signing"], s.role)
            if visa:
                vt = deadlines.find_term(visa.text)
                steps = re.findall(r"^\d+\)\s*(.+?)[;.]?$", visa.text, re.M)
                vwho = f"по порядку п. {deadlines.POINTS['visa']}" + (f": {steps[0]} … {steps[-1]}" if steps else "")
                later.append(deadlines.Step("Визирование — строго по очереди", vwho, vt[0] if vt else None,
                                            vt[1] if vt else "work", f"п. {deadlines.POINTS['visa']}", after="later",
                                            note="—" if not vt else f"{deadlines.days_word(vt[0], vt[1])} на каждую визу"))
                pts.append(deadlines.POINTS["visa"])
            if sign and (st := deadlines.find_term(sign.text)):
                later.append(deadlines.Step("Подписание", "подписант", st[0], st[1],
                                            f"п. {deadlines.POINTS['signing']}" + (", как правило" if st[2] else ""),
                                            after="later",
                                            note=f"{deadlines.days_word(st[0], st[1])} со дня направления подписанту"))
                pts.append(deadlines.POINTS["signing"])
        return variants, later, info

    def _calc_answer(self, s: Session, calc: dict, today: dt.date) -> Reply:
        cal = deadlines.get_calendar()
        start = dt.date.fromisoformat(calc["date"])
        variants, later, info = self._calc_steps(s, calc)
        if not variants:
            sec = self.retriever.get_section(*deadlines.TABLES["execution" if calc["stage"] == "execution"
                                                                else "review"][:2], s.role)
            return self._calc_no_term(s, calc["query"], calc, sec)
        count = self.retriever.get_section("instr_dou", deadlines.POINTS["count"], s.role)
        shift = deadlines.end_shift(count.text) if count else "prev"
        scheds = [deadlines.build_schedule(start, v, cal, shift) for v in variants]
        dues = [rows[-1].due for rows in scheds]
        first = variants[0][0]
        stage = calc["stage"]
        what = {"execution": "исполнить документ", "signing": "подписать документ", "visa": "завизировать проект",
                "linguistic": "провести экспертизу"}.get(stage, "")
        # Итог
        point, _, rule = first.basis.partition(", ")
        term_ref = f"{deadlines.days_word(first.days, first.unit)} ({point} Инструкции{', ' + rule if rule else ''})"
        order = f"п. {deadlines.POINTS['order']}"
        if stage in ("approval", "review"):
            doc = f"проект вида «{calc['doc']}», отправленный {deadlines.fmt_day(start)}"
            if len(dues) == 2:
                head = (f"**Итог: рассмотрение завершится не позднее {deadlines.fmt_day(dues[0])}, если подразделения "
                        f"рассматривают проект одновременно, или {deadlines.fmt_day(dues[1])} — если по очереди.** "
                        f"В Инструкции не сказано, одновременно или по очереди рассматривают {doc}, — поэтому показаны "
                        f"оба варианта. Срок каждого рассмотрения — {term_ref}.")
            else:
                how = {"sequential": f"Рассматривают по очереди ({order}), у каждого подразделения — {term_ref}.",
                       "parallel": f"Рассматривают одновременно ({order}), срок — {term_ref}."}.get(
                    info.get("mode"), f"Срок — {term_ref}.")
                head = (f"**Итог: рассмотрение завершится не позднее {deadlines.fmt_day(dues[0])}** — к этой дате "
                        f"подразделения должны рассмотреть {doc}. {how}")
        else:
            reg = "зарегистрирован" if stage == "execution" else "направлен подписанту" if stage == "signing" \
                else "направлен"
            head = (f"**Итог: {what} не позднее {deadlines.fmt_day(dues[0])}.** Документ {reg} "
                    f"{deadlines.fmt_day(start)}, срок — {term_ref}.")
        # Таблица этапов
        rows = [r for rows in scheds for r in rows] + [deadlines.Row(st, None, None) for st in later]
        work_only = all(r.step.unit == "work" or r.step.days is None for r in rows)
        table = ["| Этап | Кто | " + ("Срок, раб. дн." if work_only else "Срок") + " | Крайняя дата |", "|---|---|---|---|"]
        for i, r in enumerate(rows, 1):
            st = r.step
            if st.days is None:
                term = "в Инструкции не указан"
            else:
                term = (str(st.days) if work_only else deadlines.days_word(st.days, st.unit)) + f" ({st.basis})"
            due = f"**{deadlines.fmt_day(r.due)}**" if r.due else (st.note or "—")
            table.append(f"| {i}. {st.title} | {st.who} | {term} | {due} |")
        # Как считал и предупреждения
        notes = []
        if stage == "execution":
            notes.append("_Как считал: срок — с даты регистрации документа (п. 6.4)"
                         + ("; последний день срока, выпавший на выходной или праздник, переносится на предшествующий "
                            "рабочий день (п. 6.4)._" if first.unit == "calendar" else
                            "; выходные и нерабочие праздничные дни не считаются._"))
            raw_end = start + dt.timedelta(days=first.days)
            if first.unit == "calendar" and raw_end != dues[0]:
                notes.append(f"_{deadlines.days_word(first.days, 'calendar')} истекают {deadlines.fmt_day(raw_end)} — "
                             f"это нерабочий день, поэтому срок — {deadlines.fmt_day(dues[0])}._")
        else:
            notes.append("_Как считал: день отправки в срок не входит — отсчёт со следующего рабочего дня; выходные "
                         "и нерабочие праздничные дни не считаются (производственный календарь РФ)._")
        if not cal.is_workday(start) and first.unit == "work":
            notes.append(f"_{deadlines.fmt_day(start)} — нерабочий день, отсчёт начинается с ближайшего рабочего дня: "
                         f"{deadlines.fmt_day(deadlines.next_workday(start, cal))}._")
        for u, cnd in info["cond"]:
            notes.append(f"_{u} визирует при условии: {cnd}._")
        for d in sorted({x for x in dues if x in cal.short}):
            notes.append(f"_{deadlines.fmt_day(d)} — предпраздничный день: рабочий день короче на 1 час._")
        missing, prelim = cal.gaps(start, max(dues))
        if missing:
            notes.append(f"⚠️ Праздничные дни за этот период не учтены: в производственном календаре помощника нет "
                         f"{', '.join(map(str, missing))} г. — учтены только выходные (суббота и воскресенье).")
        if prelim:
            notes.append(f"⚠️ Календарь на {', '.join(map(str, prelim))} г. предварительный: праздники по Трудовому "
                         "кодексу учтены, переносы выходных по постановлению Правительства РФ ещё не внесены — "
                         "дату стоит сверить.")
        # Источники: только пункты, доступные роли (get_section фильтрует по уровню доступа)
        sources, secs = [], []
        for sec_id in dict.fromkeys(info["points"]):
            hit = self.retriever.get_section("instr_dou", sec_id, s.role)
            if hit:
                sources.append(Source(citation(hit.meta), 100, hit.text, "instr_dou", sec_id))
                secs.append(sec_id)
        basis = []
        if sources:
            meta = self.retriever.get_section("instr_dou", secs[0], s.role).meta
            basis.append(f"{meta['short_title']}, ред. {meta['version']} (с {fmt_date(meta['effective_date'])}), "
                         f"п. {', '.join(secs)}")
            if start < dt.date.fromisoformat(meta["effective_date"]):
                notes.append(f"_Редакция {meta['version']} Инструкции действует с {fmt_date(meta['effective_date'])}: "
                             "при отправке раньше этой даты сроки могли быть другими._")
        m = get_matrix()
        if info["row"]:
            a = m.lookup(info["row"])
            grp = "Внутренние" if a.row["group"] == "internal" else "Внешние"
            sources.append(Source(m.citation(info["row"]), 100, a.row["title"], "matrix_viz",
                                  f"{grp} документы, строка {a.row['num']}", kind="matrix"))
            basis.append(m.citation(info["row"]))
        # Простыми словами — шаблонная фраза кода (модель не нужна, ответ корректен при любом её состоянии)
        when = deadlines.say_day(start, today)
        due_words = f"{deadlines.say_day(dues[0], today)} ({deadlines.WEEKDAY_NAMES[dues[0].weekday()]})"
        term = deadlines.days_word(first.days, first.unit)
        if stage in ("approval", "review"):
            simple = (f"от отправки {when} у подразделений есть {term} без выходных"
                      + ("" if missing else " и праздников")
                      + (" — все смотрят проект одновременно, поэтому ответы должны прийти" if info.get("mode") == "parallel"
                         else ", поэтому рассмотрение должно закончиться")
                      + f" не позже {due_words}.")
            if len(dues) == 2:
                simple += (f" Если подразделения смотрят проект по очереди — не позже "
                           f"{deadlines.say_day(dues[1], today)}.")
            simple += f" Если будут замечания, после доработки на повторное рассмотрение — снова {term}."
        elif stage == "execution":
            simple = f"документ, зарегистрированный {when}, нужно исполнить не позже {due_words}."
        else:
            obj = {"signing": "документ, направленный подписанту", "visa": "проект, направленный на визу",
                   "linguistic": "проект, направленный на экспертизу"}[stage]
            verb = {"signing": "подписать", "visa": "завизировать", "linguistic": "проверить"}[stage]
            rule = ", как правило," if "как правило" in first.basis else ""
            simple = f"{obj} {when}, нужно{rule} {verb} не позже {due_words}."
        text = (head + "\n\n" + "\n".join(table) + "\n\n" + "\n\n".join(notes)
                + ("\n\n**Основание:** " + "; ".join(basis) + "." if basis else "")
                + f"\n\n**Простыми словами:** {simple[:1].upper() + simple[1:]}")
        s.last_simple = "Проще говоря: " + simple
        offer, actions = self._offer_template(s, calc.get("doc_text") or calc["query"])
        return Reply(text + offer, sources=sources, actions=actions, intent="deadline_calc",
                     meta={"row": info["row"], "calc": {"stage": stage, "start": start.isoformat(),
                                                        "due": [d.isoformat() for d in dues]}})

    # ================================================================ шаблоны
    def start_template(self, s: Session, text: str) -> Reply:
        text = self._with_context(s, text)
        tid, scored = pick_template(text)
        templates = load_templates()
        # кнопка «Подготовить черновик» присылает точное название шаблона
        tid = next((t for t, tpl in templates.items() if tpl["title"].lower() in text.lower()), tid)
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
                 f"соберу черновик .docx. Отвечайте своими словами; «не знаю» или «пример» — подскажу, «назад» — "
                 f"исправить предыдущий ответ, «отмена» — прервать.")
        q = self._field_question(s)
        return Reply(intro + "\n\n" + q.text, sources=src, actions=q.actions, intent="create_doc",
                     meta={"template": tid})

    def _field_question(self, s: Session, error: str = "", lead: str = "") -> Reply:
        f = s.fill.current()
        n, total = s.fill.progress()
        question = self._rephrase(f["question"])
        lines = [lead] if lead else []
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

    FILL_SMALLTALK = {
        "thanks": "Пожалуйста! Продолжим.",
        "greeting": "Здравствуйте! Продолжим заполнение.",
        "how": "Всё хорошо, спасибо! Продолжим.",
        "who": "Я — помощник по документообороту: собираю черновик по модулю из ваших ответов. Продолжим.",
        "rude": "Извините, если неудобно. Прервать можно словом «отмена», а пока продолжим.",
        "bye": "Черновик ещё не готов. Чтобы прервать заполнение, напишите «отмена», или продолжим.",
    }

    def _fill_hint(self, f: dict) -> str:
        lead = "Подскажу. Ниже — что именно нужно и пример ответа."
        if f.get("optional"):
            lead += " Этот пункт можно пропустить — нажмите «Пропустить» или напишите «нет»."
        else:
            lead += " Если точных данных пока нет, впишите как есть — черновик потом можно поправить в Word."
        return lead

    def _fill_step(self, s: Session, text: str) -> Reply:
        if CANCEL_RE.match(text):
            s.fill = None
            return Reply("Заполнение шаблона прервано. Чем ещё помочь?", intent="create_doc")
        if BACK_RE.match(text):
            s.fill.back()
            return self._field_question(s)
        f = s.fill.current()
        # Живая речь вместо ответа: «не знаю», «а что писать?», «спасибо», вопрос по ходу заполнения
        if FILL_HELP_RE.match(text):
            return self._field_question(s, lead=self._fill_hint(f))
        kind = normalize.normalize(text).smalltalk
        if kind in self.FILL_SMALLTALK:
            return self._field_question(s, lead=self.FILL_SMALLTALK[kind])
        if kind in ("confused", "wrong"):
            return self._field_question(s, lead=self._fill_hint(f))
        if text.rstrip().endswith("?") and not (f.get("type", "text") != "text" and validate(f, text, s.fill.values).ok):
            return self._side_question(s, text)
        res = s.fill.answer(text)
        if not res.ok:
            return self._field_question(s, res.error)
        if not s.fill.done():
            return self._field_question(s)
        return self.finish_fill(s)

    def _side_question(self, s: Session, text: str) -> Reply:
        """Вопрос посреди заполнения («а с кем его потом согласовывать?») — ответ, и заполнение продолжается."""
        fill, s.fill = s.fill, None
        # «а с кем его согласовывать?» — «его» здесь заполняемый документ
        s.last_query, s.params["_side"] = fill.template["title"], True
        try:
            side = self._route(s, text, None)
        finally:
            s.fill, s.offer, s.pending = fill, None, None
            s.params.pop("_side", None)
        if side.intent in ("create_doc", "check_doc") or side.meta.get("clarification"):
            side = Reply(f"Сейчас заполняем **«{fill.template['title']}»**. С этим вопросом помогу после заполнения "
                         "или прервите его словом «отмена».", intent="create_doc")
        q = self._field_question(s, lead="**Продолжим заполнение.**")
        return Reply(side.text + "\n\n---\n\n" + q.text, sources=side.sources, actions=q.actions,
                     intent=side.intent, refused=side.refused, meta={"side": True})

    def finish_fill(self, s: Session) -> Reply:
        from app.templates.render import render

        fs, s.fill = s.fill, None
        tpl = fs.template
        out = render(tpl, fs.context(), owner=s.user)
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
            "3. **Считать сроки датами** по производственному календарю — «Если отправлю распоряжение на "
            "согласование сегодня, когда будет готово?»\n"
            "4. **Готовить черновики .docx по модулям** документов, задавая вопросы по очереди — "
            "«Нужно подготовить распоряжение о создании рабочей группы»\n"
            "5. **Проверять загруженный черновик** (.docx/.pdf) на соответствие требованиям\n"
            "6. **Подсказывать, куда обратиться**, если ответа в базе знаний нет.\n\n"
            f"Роли: {role_titles}. Отвечаю только по документам базы знаний.",
            actions=[("Когда регистрировать входящий?", "Когда нужно зарегистрировать входящий документ?"),
                     ("С кем согласовать распоряжение?", "С кем согласовать распоряжение?"),
                     ("Когда согласуют распоряжение?",
                      "Если отправлю распоряжение на согласование сегодня, когда будет готово?"),
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
