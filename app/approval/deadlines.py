"""Сроки рассмотрения (п. 10.3) и исполнения (п. 6.6) — разбираются кодом из текста пунктов базы знаний
и выбираются по виду документа без участия LLM.

Калькулятор сроков в датах — тоже только код, без модели и сети: дата отправки из текста вопроса
(parse_date_from_text), производственный календарь РФ (config.yaml → paths.calendar), сложение рабочих
и календарных дней (add_workdays, add_calendar_days) и даты этапов (build_schedule). Числа сроков берутся
из текста пунктов (parse_term, find_term), поэтому новая редакция Инструкции меняет их без правки кода."""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from app.retrieval.text import STOP, TOKEN_RE

ITEM_RE = re.compile(r"^•\s*(.+?)\s+—\s+(.+?)[;.]?$")

TABLES = {
    # вид таблицы: (doc_id, пункт, что означает срок)
    "review": ("instr_dou", "10.3", "максимальный срок каждого первичного и повторного рассмотрения проекта"),
    "execution": ("instr_dou", "6.6", "предельный срок исполнения с даты регистрации (если специальный срок не указан)"),
}

# Синонимы разговорной речи → слова из формулировок пунктов
SYNONYMS = {
    "министерств": "органов", "ведомств": "органов", "госорган": "органов", "минфин": "органов",
    "гражданин": "граждан", "физлиц": "граждан", "служебк": "служебная переписка", "внутренн": "внутренняя",
    "банк": "финансового рынка", "компани": "юридических лиц", "ооо": "юридических лиц", "ао": "юридических лиц",
    "президент": "высшего должностного лица", "регламента": "регламент",
}


@dataclass
class Deadline:
    kind: str
    item: str
    term: str
    section: str
    doc_id: str


def _stems(text: str) -> set[str]:
    """Более точная основа, чем у BM25: «согласование» ≠ «соглашение», «письма» = «письмо»."""
    out = set()
    for t in TOKEN_RE.findall(text.lower().replace("ё", "е")):
        if t in STOP or len(t) < 3:
            continue
        out.add(re.sub(r"[аеиоуыэюяйь]+$", "", t)[:6])
    return out


def parse_items(text: str) -> list[tuple[str, str]]:
    out = []
    for line in text.split("\n"):
        m = ITEM_RE.match(line.strip())
        if m:
            out.append((m.group(1).strip(), m.group(2).strip()))
    return out


def _expand(query: str) -> str:
    q = query.lower()
    extra = [v for k, v in SYNONYMS.items() if re.search(rf"\b{k}", q)]
    return q + " " + " ".join(extra)


def match_item(items: list[tuple[str, str]], query: str) -> tuple[str, str] | None:
    """Выбор строки перечня по совпадению основ слов; при равенстве — более специфичная (длинная)."""
    q = _stems(_expand(query))
    best, best_score = None, 0.0
    for name, term in items:
        if name.startswith("иной"):
            continue
        nt = _stems(name)
        hit = len(q & nt)
        if not hit:
            continue
        score = hit / len(nt) + 0.01 * hit
        if score > best_score:
            best, best_score = (name, term), score
    return best


def detect_kind(text: str) -> str | None:
    t = text.lower()
    if not re.search(r"срок|сколько (дней|времени)|за сколько|когда (надо|нужно|должн)", t):
        return None
    if re.search(r"рассмотр|согласов|визир", t):
        return "review"
    if re.search(r"исполн|ответ", t):
        return "execution"
    return None


def lookup(kind: str, query: str, section_text: str) -> Deadline | None:
    doc_id, section, _ = TABLES[kind]
    m = match_item(parse_items(section_text), query)
    if not m:
        return None
    return Deadline(kind, m[0], m[1], section, doc_id)


def default_item(section_text: str) -> tuple[str, str] | None:
    for name, term in parse_items(section_text):
        if name.startswith("иной"):
            return name, term
    return None


# ================================================================ производственный календарь
DAY = dt.timedelta(days=1)


@dataclass
class Calendar:
    weekend: frozenset[int]               # ISO-дни недели: 6 — суббота, 7 — воскресенье
    off: frozenset[dt.date]               # праздники и перенесённые выходные
    work: frozenset[dt.date]              # рабочие субботы и воскресенья по переносу
    short: frozenset[dt.date]             # сокращённые предпраздничные дни (рабочие)
    years: dict[int, str]                 # год → confirmed | preliminary
    check: dict[int, list[str]] = field(default_factory=dict)

    def is_workday(self, d: dt.date) -> bool:
        return d in self.work or (d.isoweekday() not in self.weekend and d not in self.off)

    def gaps(self, start: dt.date, end: dt.date) -> tuple[list[int], list[int]]:
        """Годы периода, которых нет в календаре, и годы с предварительным календарём."""
        ys = range(min(start, end).year, max(start, end).year + 1)
        return [y for y in ys if y not in self.years], [y for y in ys if self.years.get(y) == "preliminary"]


def _dates(items) -> frozenset[dt.date]:
    return frozenset(x if isinstance(x, dt.date) else dt.date.fromisoformat(str(x)) for x in items or [])


def parse_calendar(data: dict) -> Calendar:
    off, work, short, years, check = set(), set(), set(), {}, {}
    for y, v in (data.get("years") or {}).items():
        v = v or {}
        years[int(y)] = v.get("status", "confirmed")
        check[int(y)] = list(v.get("check") or [])
        off |= _dates(v.get("days_off"))
        work |= _dates(v.get("work"))
        short |= _dates(v.get("short"))
    return Calendar(frozenset(data.get("weekend") or (6, 7)), frozenset(off), frozenset(work), frozenset(short),
                    years, check)


_calendar: tuple[Path, int, Calendar] | None = None


def get_calendar() -> Calendar:
    """Календарь из config.yaml → paths.calendar; перечитывается при изменении файла (как Матрица).
    Нет файла — только правило выходных, и в ответе предупреждение о неучтённых праздниках."""
    global _calendar
    from app.config import path

    p = path("calendar")
    stamp = p.stat().st_mtime_ns if p.exists() else 0
    if _calendar is None or _calendar[:2] != (p, stamp):
        data = yaml.safe_load(p.read_text(encoding="utf-8")) if p.exists() else {}
        _calendar = (p, stamp, parse_calendar(data or {}))
    return _calendar[2]


def today() -> dt.date:
    """Сегодня по Москве; в тестах подменяется."""
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("Europe/Moscow")
    except Exception:  # noqa: BLE001  нет базы часовых поясов — Москва без летнего времени, UTC+3
        tz = dt.timezone(dt.timedelta(hours=3))
    return dt.datetime.now(tz).date()


def next_workday(d: dt.date, cal: Calendar) -> dt.date:
    while not cal.is_workday(d):
        d += DAY
    return d


def prev_workday(d: dt.date, cal: Calendar) -> dt.date:
    while not cal.is_workday(d):
        d -= DAY
    return d


def add_workdays(start: dt.date, n: int, cal: Calendar) -> dt.date:
    """N-й рабочий день после start. День отправки в срок не входит (срок начинается на следующий день,
    ст. 191 ГК РФ); отправка в выходной — отсчёт с ближайшего рабочего дня. n = 0 — сам день, если он рабочий,
    иначе ближайший рабочий."""
    if n < 0:
        raise ValueError("срок не может быть отрицательным")
    if n == 0:
        return next_workday(start, cal)
    d = start
    while n:
        d += DAY
        if cal.is_workday(d):
            n -= 1
    return d


def add_calendar_days(start: dt.date, n: int, cal: Calendar, shift: str = "prev") -> dt.date:
    """N календарных дней со дня start; последний день на выходном или празднике сдвигается на рабочий:
    shift="prev" — на предшествующий (п. 6.4 Инструкции), "next" — на следующий (ст. 193 ГК РФ)."""
    d = start + dt.timedelta(days=n)
    return prev_workday(d, cal) if shift == "prev" else next_workday(d, cal)


# ================================================================ сроки из текста пунктов
NUM_WORDS = {"один": 1, "одного": 1, "одной": 1, "два": 2, "двух": 2, "три": 3, "трех": 3, "четыре": 4,
             "четырех": 4, "пять": 5, "пяти": 5, "шесть": 6, "шести": 6, "семь": 7, "семи": 7, "восемь": 8,
             "восьми": 8, "девять": 9, "девяти": 9, "десять": 10, "десяти": 10, "пятнадцать": 15,
             "пятнадцати": 15, "двадцать": 20, "двадцати": 20, "тридцать": 30, "тридцати": 30}
TERM_RE = re.compile(r"(\d+|[а-яё]+)\s*(?:\([а-яё]+\)\s*)?(рабоч|календарн)\w*\s+(?:дн|день|дня)", re.I)


def parse_term(term: str) -> tuple[int, str] | None:
    """«3 рабочих дня» → (3, "work"); «30 календарных дней» → (30, "calendar"); «три рабочих дня» → (3, "work")."""
    for m in TERM_RE.finditer(term.lower().replace("ё", "е")):
        n = int(m.group(1)) if m.group(1).isdigit() else NUM_WORDS.get(m.group(1))
        if n is not None:   # «не позднее рабочего дня» — не число, ищем дальше
            return n, "work" if m.group(2).startswith("рабоч") else "calendar"
    return None


def find_term(text: str) -> tuple[int, str, bool] | None:
    """Первый срок в тексте пункта: (число, work|calendar, «как правило»). Нет срока — None."""
    for sent in re.split(r"(?<=[.;])\s+", text):
        t = parse_term(sent)
        if t:
            return t[0], t[1], "как правило" in sent.lower()
    return None


def review_mode(text: str) -> str:
    """Рассматривают ли подразделения проект одновременно (parallel) или по очереди (sequential) — по тексту
    п. 10.2; если в тексте не сказано — unknown (тогда в ответе оба варианта)."""
    for line in text.lower().replace("ё", "е").split("\n"):
        if "направля" in line and "рассмотрени" in line:
            if re.search(r"одновременно|параллельно", line):
                return "parallel"
            if re.search(r"поочередно|последовательно|по очереди", line):
                return "sequential"
    return "unknown"


def end_shift(text: str) -> str:
    """Куда сдвигается последний день срока, выпавший на выходной (п. 6.4): prev | next."""
    t = text.lower()
    return "prev" if "предшествующ" in t else "next" if re.search(r"следующ\w* (за ним )?рабоч", t) else "prev"


def urgent_term(text: str, very: bool) -> tuple[int, str] | None:
    """Срок для отметки «Срочно» / «Весьма срочно» из текста п. 6.7."""
    t = text.replace("ё", "е")
    if very:
        if re.search(r"«Весьма срочно»[^.;]*?рабочего дня,? следующего", t):
            return 1, "work"
        m = re.search(r"«Весьма срочно»([^.;]*)", t)
    else:
        m = re.search(r"«Срочно»([^.;]*)", t)
    return parse_term(m.group(1)) if m else None


# ================================================================ дата отправки из текста вопроса
MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября",
          "ноября", "декабря"]
WEEKDAYS = {"понедельник": 0, "вторник": 1, "среду": 2, "среда": 2, "четверг": 3, "пятницу": 4, "пятница": 4,
            "субботу": 5, "суббота": 5, "воскресенье": 6}
WEEKDAY_ABBR = {"пн": 0, "вт": 1, "ср": 2, "чт": 3, "пт": 4, "сб": 5, "вс": 6}
RELATIVE = {"сегодня": 0, "завтра": 1, "послезавтра": 2, "вчера": -1, "позавчера": -2}
SHORT_DAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
PAST_RE = re.compile(r"\b(отправил|направил|подал|передал|сдал|отдал|поступил|пришел|получил|зарегистрирова)\w*|"
                     r"\bпришл\w*|\bбыл[аио]?\b", re.I)
NUM_DATE_RE = re.compile(r"(?<![\d.,])(\d{1,2})[./](\d{1,2})(?:[./](\d{4}|\d{2}))?(?![\d]|[./]\d)")
ISO_DATE_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
TEXT_DATE_RE = re.compile(r"(?<!\d)(\d{1,2})(?:-?(?:го|е|ое))?\s+([а-я]+)(?:\s+(\d{4})(?:\s*(?:г\b\.?|год\w*))?)?")
POINT_BEFORE_RE = re.compile(r"(п\.|пп\.|пункт\w*|подпункт\w*|раздел\w*|глав\w*|стр\.|№)\s*$")


@dataclass
class DateRef:
    date: dt.date
    phrase: str           # как понято: «сегодня», «во вторник», «12 ноября»
    pos: int = 0


def _word(w: str, vocab: dict[str, int]) -> int | None:
    """Слово даты с опечаткой («втроник», «севодня», «нояьря») → значение; настоящие слова не трогаются."""
    if w in vocab:
        return vocab[w]
    if len(w) < 5:
        return None
    from app.dialog.normalize import _osa, known

    if known(w):
        return None
    limit = 1 if len(w) <= 7 else 2
    found = {vocab[c] for c in vocab if len(c) >= 5 and _osa(w, c, limit) <= limit}
    return found.pop() if len(found) == 1 else None


def _nearest_year(day: int, month: int, today: dt.date) -> dt.date | None:
    """Дата без года — ближайшая к сегодняшней: «12 января» в октябре — следующего года, «28 декабря» в январе — прошлого."""
    out = []
    for y in (today.year - 1, today.year, today.year + 1):
        try:
            out.append(dt.date(y, month, day))
        except ValueError:
            continue
    return min(out, key=lambda d: abs((d - today).days)) if out else None


def _full_year(y: str) -> int:
    return int(y) + 2000 if len(y) == 2 else int(y)


def parse_date_from_text(text: str, today: dt.date) -> DateRef | None:
    """Дата отправки из вопроса: «сегодня», «завтра», «во вторник», «в следующую пятницу», «15 ноября»,
    «12 ноября 2026», «15.11», «15.11.2026»; опечатки в словах даты исправляются. Несколько дат — первая по тексту.
    День недели без уточнения — ближайший начиная с сегодняшнего; в прошедшем времени («отправил во вторник») —
    последний прошедший; «в следующий» / «в прошлый» — на следующей / прошлой неделе."""
    t = text.lower().replace("ё", "е")
    found: list[DateRef] = []
    months = {m: i + 1 for i, m in enumerate(MONTHS)}
    for m in ISO_DATE_RE.finditer(t):
        try:
            found.append(DateRef(dt.date(int(m[1]), int(m[2]), int(m[3])), m.group(0), m.start()))
        except ValueError:
            pass
    for m in NUM_DATE_RE.finditer(t):
        if POINT_BEFORE_RE.search(t[: m.start()]) or any(r.pos <= m.start() < r.pos + len(r.phrase) for r in found):
            continue
        day, month = int(m[1]), int(m[2])
        try:
            d = dt.date(_full_year(m[3]), month, day) if m[3] else _nearest_year(day, month, today)
        except ValueError:
            d = None
        if d:
            found.append(DateRef(d, m.group(0), m.start()))
    for m in TEXT_DATE_RE.finditer(t):
        month = _word(m[2], months)
        if not month or POINT_BEFORE_RE.search(t[: m.start()]):
            continue
        try:
            d = dt.date(int(m[3]), month, int(m[1])) if m[3] else _nearest_year(int(m[1]), month, today)
        except ValueError:
            d = None
        if d:
            found.append(DateRef(d, f"{int(m[1])} {MONTHS[month - 1]}" + (f" {m[3]}" if m[3] else ""), m.start()))
    past = bool(PAST_RE.search(t))
    for m in re.finditer(r"[а-я]+", t):
        w = m.group(0)
        if (rel := _word(w, RELATIVE)) is not None:
            found.append(DateRef(today + dt.timedelta(days=rel), next(k for k, v in RELATIVE.items() if v == rel),
                                 m.start()))
            continue
        wd = _word(w, WEEKDAYS)
        if wd is None and w in WEEKDAY_ABBR and re.search(r"\bв[о]?\s+$", t[: m.start()]):
            wd = WEEKDAY_ABBR[w]
        if wd is None:
            continue
        before = t[max(0, m.start() - 25): m.start()]
        if re.search(r"следующ\w*\s+$", before):
            d = today + dt.timedelta(days=7 - today.weekday() + wd)
        elif re.search(r"прошл\w*\s+$", before):
            d = today - dt.timedelta(days=today.weekday() + 7 - wd)
        elif past:
            d = today - dt.timedelta(days=(today.weekday() - wd) % 7)
        else:
            d = today + dt.timedelta(days=(wd - today.weekday()) % 7)
        found.append(DateRef(d, ("во " if wd == 1 else "в ") + [k for k, v in WEEKDAYS.items() if v == wd][0], m.start()))
    return min(found, key=lambda r: r.pos) if found else None


def fmt_day(d: dt.date) -> str:
    """чт, 29.10.2026"""
    return f"{SHORT_DAYS[d.weekday()]}, {d:%d.%m.%Y}"


def say_day(d: dt.date, today: dt.date | None = None) -> str:
    """«сегодня», «завтра» или «29 октября» — для пояснения простыми словами."""
    if today is not None and d == today:
        return "сегодня"
    if today is not None and d == today + DAY:
        return "завтра"
    return f"{d.day} {MONTHS[d.month - 1]}" + ("" if today is None or d.year == today.year else f" {d.year} г.")


# ================================================================ этап и вид документа из вопроса
STAGE_RULES = [   # порядок важен: первая подходящая
    ("review", r"рассмотр"),
    ("approval", r"согласов|согласу|согласят"),
    ("execution", r"исполн|\bответ(а|у|ом|ы|ить|ят|им|ит|ишь)?\b|ответи"),
    ("linguistic", r"лингвист|экспертиз"),
    ("signing", r"подпис|подпиш"),
    ("visa", r"\bвиз(а|у|ы|ой|ирова\w*)\b|завизир"),
]
EXEC_ALIASES = [(r"\bприказ|\bраспоряжени|распорядительн", "распорядительные акты")]


def detect_stage(text: str) -> str | None:
    t = text.lower().replace("ё", "е")
    return next((st for st, rx in STAGE_RULES if re.search(rx, t)), None)


def match_doc(items: list[tuple[str, str]], text: str) -> tuple[str, str] | None:
    """Вид проекта по п. 10.3: «проект приказа» — это приказ, а не «проект модуля»; «иной документ» — строка «иной»."""
    t = text.lower().replace("ё", "е")
    if "модул" not in t:
        t = re.sub(r"\bпроект\w*", " ", t)
    t = re.sub(r"\bположен[оаы]?\b", " ", t)   # «сколько дней положено» — не «положение»
    found = match_item(items, t)
    if found:
        return found
    if re.search(r"\b(ино\w+|друг\w+|проч\w+)\s+(вид\w*\s+)?документ", t):
        return next(((n, term) for n, term in items if n.startswith("иной")), None)
    return None


def match_sender(items: list[tuple[str, str]], text: str) -> tuple[str, str] | None:
    """Строка п. 6.6 для исполнения: «приказ» и «распоряжение» — распорядительные акты."""
    t = text.lower()
    for rx, add in EXEC_ALIASES:
        if re.search(rx, t):
            t += " " + add
    return match_item(items, t)


# ================================================================ даты этапов
@dataclass
class Step:
    title: str
    who: str
    days: int | None                # None — срок в Инструкции не указан
    unit: str = "work"              # work | calendar
    basis: str = ""                 # «п. 10.3»
    after: str = "prev"             # start — от даты отправки; prev — от окончания предыдущего этапа; later — дата неизвестна
    note: str = ""                  # «как правило», «если были замечания»


@dataclass
class Row:
    step: Step
    start: dt.date | None
    due: dt.date | None


def build_schedule(start: dt.date, steps: list[Step], cal: Calendar, shift: str = "prev") -> list[Row]:
    """Крайние даты этапов: этап «start» считается от даты отправки, «prev» — от окончания предыдущего,
    «later» и этапы без срока дат не получают (и следующие за ними «prev» — тоже)."""
    rows, cursor = [], start
    for st in steps:
        base = start if st.after == "start" else cursor if st.after == "prev" else None
        due = None
        if base is not None and st.days is not None:
            due = add_workdays(base, st.days, cal) if st.unit == "work" else add_calendar_days(base, st.days, cal, shift)
        rows.append(Row(st, base, due))
        if st.after != "later":
            cursor = due
    return rows


# Пункты Инструкции, из которых калькулятор берёт сроки и порядок (doc_id — как в TABLES)
POINTS = {"order": "10.2", "visa": "10.6", "signing": "10.15", "linguistic": "10.13", "count": "6.4", "urgent": "6.7"}
WEEKDAY_NAMES = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]


def days_word(n: int, unit: str = "work") -> str:
    """3 рабочих дня, 1 рабочий день, 30 календарных дней."""
    adj = ("рабочий", "рабочих", "рабочих") if unit == "work" else ("календарный", "календарных", "календарных")
    i = 0 if n % 10 == 1 and n % 100 != 11 else 1 if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else 2
    return f"{n} {adj[i]} {('день', 'дня', 'дней')[i]}"
