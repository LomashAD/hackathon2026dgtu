"""Классификатор намерений: правила и ключевые слова; LLM — только запасной вариант."""
from __future__ import annotations

import logging
import re

from app.approval import deadlines

log = logging.getLogger(__name__)

INTENTS = ["question", "find_section", "create_doc", "check_doc", "approval", "deadline", "deadline_calc", "contact",
           "help"]

QUESTION_START = re.compile(r"^\s*((а|и|ну|так|тогда)\s+)?(как|что|какой|какая|какие|каков|какую|каком|какого|когда|где|"
                            r"куда|откуда|можно ли|нужно ли|надо ли|обязательно ли|должен ли|есть ли|вправе ли|"
                            r"допускается ли|кто|кому|чей|чья|чьи|зачем|почему|сколько|через сколько|за сколько|"
                            r"на сколько|до какого|с какого|в течение|в каком|в какой|чем)\b", re.I)
# Вопрос без вопросительного слова в начале: «мне принесли чужое письмо, что с ним делать»
QUESTION_ANY = re.compile(r"\b(что|как)\s+(делать|быть|поступить|теперь)\b|\bчто\s+с\s+(ним|ней|ними|этим)\b|"
                          r"\bкуда\s+(его|её|ее|их|девать|деть|отправ\w*|переда\w*|нести)\b", re.I)
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
APPROVAL = re.compile(r"(с кем|кому|кто|какие подразделени\w*|какие департамент\w*).{0,40}(согласов|визир|завизир|на виз[уы]\b)|"
                      r"(согласов|визир|завизир)\w*.{0,40}(с кем|кто|кому)|кто (должен )?(завизир|визиру|согласу)|"
                      r"какие (визы|согласующ)|маршрут\w* (согласования|визирования)|матриц\w* (обязательного )?визир|"
                      r"обязательн\w* виз|нужн\w* ли (виза|визировани|согласовани)", re.I)
CONTACT = re.compile(r"куда (мне )?обратиться|к кому (мне )?обратиться|кто (может )?(проконсультир|консультир|подскаж|"
                     r"поможет|разъясн)|контакт|кому (позвонить|написать)|за консультаци|за разъяснени", re.I)
FIND = re.compile(r"где (написано|сказано|указано|говорится|прописано)|в каком (пункте|разделе|главе)|"
                  r"какой пункт|найди\b|покажи (пункт|раздел|главу|приложени)|^\s*(пункт|п\.|раздел|глава|приложение)\s*№?\s*\d",
                  re.I)
# Модель-запасной вариант решает только при явных словах намерения: без них сообщение — вопрос к базе знаний
# («письмо пришло не по адресу» — ситуация, а не просьба проверить файл). Проверка документа — только с файлом.
LLM_EVIDENCE = {
    "create_doc": re.compile(r"(приказ|распоряжени|черновик|шаблон|бланк|модул)", re.I),
    "approval": re.compile(r"(согласов|визир|\bвиз[аыуе]\b)", re.I),
    "contact": re.compile(r"(обратит|консульт|разъясн|контакт|позвон)", re.I),
}
# Расчёт срока в датах: «когда будет готово», «к какому числу», «когда истекает», «крайний срок» — или дата отправки
# с вопросом «когда». Без даты и без этих слов вопрос о сроке остаётся вопросом о сроке в днях (намерение deadline):
# «через сколько дней после подписания регистрируют исходящий?», «дедлайн выпал на субботу — когда сдавать?».
DEADLINE_CALC = re.compile(r"когда (\w+ ){0,2}будет готов|к какому (числу|дню|сроку)|до какого (числа|дня)|"
                           r"какого числа (\w+ ){0,2}(будет|соглас|рассмотр|истека|истеч|готов|подпиш|исполн)|"
                           r"крайн\w* (срок|дат|день)|когда (\w+ ){0,3}(истека|истеч|заканчива|закончится|кончается)|"
                           r"срок\w* (\w+ ){0,3}(истека|истеч)|через сколько (рабочих )?дн", re.I)
SEND_VERB = re.compile(r"\b(отправ|направ|подал|подам|подаю|подать|переда|поступ|пришл|пришел|пришёл|зарегистрир|"
                       r"сдал|сдам|отдал|отдам|получил)\w*", re.I)
WHEN = re.compile(r"\bкогда\b|\bсколько\b|к какому|какого числа", re.I)
HELP = re.compile(r"^\s*(привет|здравствуй\w*|добрый (день|вечер|утро)|помощь|help|/help|/start|что ты умеешь|"
                  r"что умеешь|начать|меню)\b", re.I)


def wants_dates(text: str) -> bool:
    """Нужна конкретная дата, а не срок в днях: ключевые слова расчёта или дата отправки с вопросом «когда»."""
    send = SEND_VERB.search(text)
    dated = send is not None and deadlines.parse_date_from_text(text, deadlines.today()) is not None
    if DEADLINE_CALC.search(text):
        # «через сколько дней после подписания регистрируют» — вопрос о сроке в днях, дата не нужна
        return dated or not re.search(r"через сколько (рабочих )?дн", text, re.I)
    return dated and bool(WHEN.search(text))


def classify(text: str, has_file: bool = False, llm=None) -> tuple[str, str]:
    """Возвращает (намерение, источник решения: rule|mark|llm|default); mark — вопрос только по знаку «?»."""
    t = text.strip()
    if has_file:
        return "check_doc", "rule"
    if HELP.search(t) and len(t.split()) <= 4:
        return "help", "rule"
    if CHECK.search(t):
        return "check_doc", "rule"
    if wants_dates(t):
        return "deadline_calc", "rule"
    if APPROVAL.search(t):
        return "approval", "rule"
    if CONTACT.search(t):
        return "contact", "rule"
    asks = bool(QUESTION_START.search(t) or QUESTION_ANY.search(t))
    is_question = asks or t.endswith("?")
    if IMPERATIVE.search(t) or GET_TEMPLATE.search(t) or (not is_question and (
            NEED_DOC.search(t) or DOC_WORDS.search(t) or (CREATE_VERB.search(t) and CREATE_OBJ.search(t)))):
        return "create_doc", "rule"
    if deadlines.detect_kind(t):
        return "deadline", "rule"
    if FIND.search(t):
        return "find_section", "rule"
    if asks:
        return "question", "rule"
    if is_question:
        return "question", "mark"
    if llm is not None and len(t.split()) >= 3 and any(rx.search(t) for rx in LLM_EVIDENCE.values()):
        try:
            from app.llm.prompts import classify_messages
            ans = llm.chat(classify_messages(t), max_tokens=5).strip().lower()
            m = re.search(r"(question|create_doc|check_doc|approval|contact)", ans)
            if m and (m.group(1) == "question" or m.group(1) in LLM_EVIDENCE and LLM_EVIDENCE[m.group(1)].search(t)):
                return m.group(1), "llm"
        except Exception as e:  # noqa: BLE001
            log.warning("LLM-классификация недоступна: %s", e)
    return "question", "default"
