"""Классификатор намерений: правила и ключевые слова; LLM — только запасной вариант."""
from __future__ import annotations

import logging
import re

from app.approval import deadlines

log = logging.getLogger(__name__)

INTENTS = ["question", "find_section", "create_doc", "check_doc", "approval", "deadline", "contact", "help"]

QUESTION_START = re.compile(r"^\s*(как|что|какой|какая|какие|каков|когда|где|можно ли|нужно ли|надо ли|"
                            r"обязательно ли|должен ли|кто|зачем|почему|сколько|в каком|в какой|чем)\b", re.I)
CREATE_VERB = re.compile(r"(подготов|состав|сформир|сдела|созда|оформ|напиш|напис|заполн|сгенерир|сформулир|"
                         r"набросай|помоги с)\w*", re.I)
CREATE_OBJ = re.compile(r"(приказ|распоряжени|документ|черновик|проект\b|проект (приказа|распоряжения))", re.I)
IMPERATIVE = re.compile(r"\b(подготовь\w*|составь\w*|сделай\w*|создай\w*|оформи|оформите|напиши\w*|заполни\w*|"
                        r"сгенерируй\w*|сформируй\w*|помоги\w* (подготовить|составить|оформить|сделать|заполнить))\b", re.I)
DOC_WORDS = re.compile(r"\b(шаблон\w*|черновик\w*|бланк\w*)\b", re.I)
GET_TEMPLATE = re.compile(r"(где|как) (взять|получить|найти|скачать)\b.{0,20}(шаблон|бланк|модул)|дай\w* шаблон", re.I)
NEED_DOC = re.compile(r"\b(нужен|нужно|нужна|надо|требуется|необходимо|хочу)\b.{0,30}"
                      r"\b(приказ|распоряжени|подготовить|оформить|составить|сделать|издать)", re.I)
CHECK = re.compile(r"(провер|сверь|оцени|посмотри|проанализир)\w*.{0,40}(документ|черновик|проект|приказ|"
                   r"распоряжени|файл)|соответству\w* ли.{0,30}(инструкци|требовани)", re.I)
APPROVAL = re.compile(r"(с кем|кому|кто|какие подразделени\w*|какие департамент\w*).{0,40}(согласов|визир|завизир)|"
                      r"(согласов|визир|завизир)\w*.{0,40}(с кем|кто|кому)|кто (должен )?(завизир|визиру|согласу)|"
                      r"какие (визы|согласующ)|маршрут\w* (согласования|визирования)|матриц\w* (обязательного )?визир|"
                      r"обязательн\w* виз|нужн\w* ли (виза|визировани|согласовани)", re.I)
CONTACT = re.compile(r"куда (мне )?обратиться|к кому (мне )?обратиться|кто (может )?(проконсультир|консультир|подскаж|"
                     r"поможет|разъясн)|контакт|кому (позвонить|написать)|за консультаци|за разъяснени", re.I)
FIND = re.compile(r"где (написано|сказано|указано|говорится|прописано)|в каком (пункте|разделе|главе)|"
                  r"какой пункт|найди\b|покажи (пункт|раздел|главу|приложени)|^\s*(пункт|п\.|раздел|глава|приложение)\s*№?\s*\d",
                  re.I)
HELP = re.compile(r"^\s*(привет|здравствуй\w*|добрый (день|вечер|утро)|помощь|help|/help|/start|что ты умеешь|"
                  r"что умеешь|начать|меню)\b", re.I)


def classify(text: str, has_file: bool = False, llm=None) -> tuple[str, str]:
    """Возвращает (намерение, источник решения: rule|llm|default)."""
    t = text.strip()
    if has_file:
        return "check_doc", "rule"
    if HELP.search(t) and len(t.split()) <= 4:
        return "help", "rule"
    if CHECK.search(t):
        return "check_doc", "rule"
    if APPROVAL.search(t):
        return "approval", "rule"
    if CONTACT.search(t):
        return "contact", "rule"
    is_question = bool(QUESTION_START.search(t)) or t.endswith("?")
    if IMPERATIVE.search(t) or GET_TEMPLATE.search(t) or (not is_question and (
            NEED_DOC.search(t) or DOC_WORDS.search(t) or (CREATE_VERB.search(t) and CREATE_OBJ.search(t)))):
        return "create_doc", "rule"
    if deadlines.detect_kind(t):
        return "deadline", "rule"
    if FIND.search(t):
        return "find_section", "rule"
    if is_question:
        return "question", "rule"
    if llm is not None and len(t.split()) >= 3:
        try:
            from app.llm.prompts import classify_messages
            ans = llm.chat(classify_messages(t), max_tokens=5).strip().lower()
            m = re.search(r"(question|create_doc|check_doc|approval|contact)", ans)
            if m:
                return m.group(1), "llm"
        except Exception as e:  # noqa: BLE001
            log.warning("LLM-классификация недоступна: %s", e)
    return "question", "default"
