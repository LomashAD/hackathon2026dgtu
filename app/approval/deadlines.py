"""Сроки рассмотрения (п. 10.3) и исполнения (п. 6.6) — разбираются кодом из текста пунктов базы знаний
и выбираются по виду документа без участия LLM."""
from __future__ import annotations

import re
from dataclasses import dataclass

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
