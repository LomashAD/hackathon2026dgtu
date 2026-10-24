"""Проверка текста модели на соответствие найденным фрагментам (защита от выдумок слабой модели)."""
from __future__ import annotations

import re

from app.retrieval.text import tokens

# Служебные слова объяснения, которых может не быть во фрагментах
GENERIC = set(tokens("""вы вам вас ваш нужно необходимо следует можно надо должны должен должна тот же день
это этот если когда то также или либо только так случае значит например простыми словами
сотрудник работник ответ важно обязательно сначала затем потом после пришел пришло пришла придет
вечером вечер выходной выходные день дня дней тот та те такой такие нужен нужна поэтому""", drop_stop=False))

NUM_RE = re.compile(r"\d+(?:[.,]\d+)*")
FOREIGN_RE = re.compile(r"[一-鿿぀-ヿ]|[A-Za-z]{4,}")
# Вывод о допустимости («нельзя», «не допускается») должен опираться на норму во фрагменте, а не на вопрос:
# на «Можно ли …?» модель отвечает «Нет, нельзя …» и по пункту, где об этом ничего не сказано.
VERDICT_RE = re.compile(r"\b(нельзя|запрещ\w*|недопустим\w*|допуска\w*|разреш\w*|вправе|не\s+мож\w*|не\s+могут)\b", re.I)
NORM_RE = re.compile(r"\b(нельзя|можно|запрещ\w*|недопустим\w*|допуска\w*|разреш\w*|вправе|мож\w+|могут|обязан\w*|"
                     r"должн\w*|следует|подлеж\w*)\b", re.I)


def check_explanation(text: str, fragments: list[str], min_overlap: float = 0.55,
                      question: str = "") -> tuple[bool, str]:
    """Возвращает (годен, причина). Негоден, если есть числа/пункты не из фрагментов,
    иностранный текст или слишком много слов, которых нет во фрагментах."""
    if not text or len(text) < 15:
        return False, "пустой ответ"
    if len(text) > 900:
        return False, "слишком длинный ответ"
    if FOREIGN_RE.search(text):
        return False, "иностранный текст"
    src = " ".join(fragments)
    src_nums = set(NUM_RE.findall(src))
    for n in NUM_RE.findall(text):
        if n not in src_nums:
            return False, f"число «{n}» отсутствует во фрагментах"
    if VERDICT_RE.search(text) and not NORM_RE.search(src):
        return False, "вывод о допустимости не следует из фрагментов"
    src_t = set(tokens(src + " " + question, drop_stop=False))   # слова вопроса допустимы, числа — нет
    out_t = [t for t in tokens(text) if t not in GENERIC and len(t) > 2]
    if not out_t:
        return False, "нет содержательных слов"
    # Новые (отсутствующие во фрагментах) имена собственные, термины через дефис, расшифровки — признак выдумки.
    src_low = (src + " " + question).lower().replace("ё", "е")

    def novel(w: str) -> bool:
        return w.lower().replace("ё", "е")[:5] not in src_low

    for w in re.findall(r"\b[А-ЯЁа-яё]+-[а-яё]+\b", text):
        if novel(w):
            return False, f"новый термин «{w}»"
    for m in re.finditer(r"(?<![.!?:]\s)(?<!^)(?<!«)\b([А-ЯЁ][а-яё]{2,})", text):
        if m.start() > 0 and novel(m.group(1)):
            return False, f"новое имя «{m.group(1)}»"
    for m in re.finditer(r"([А-ЯЁа-яё]+(?:[\s-][А-ЯЁа-яё]+)?)\s*\(([А-ЯЁ]{2,})\)", text):
        if any(novel(w) for w in re.split(r"[\s-]", m.group(1))):
            return False, f"расшифровка «{m.group(0)}» отсутствует во фрагментах"
    overlap = sum(t in src_t for t in out_t) / len(out_t)
    if overlap < min_overlap:
        return False, f"доля слов из фрагментов {overlap:.0%}"
    return True, f"доля слов из фрагментов {overlap:.0%}"


def clean_answer(text: str) -> str:
    text = re.sub(r"^\s*(Ответ|Простыми словами)\s*:\s*", "", text.strip(), flags=re.I)
    return text.strip().strip('"«»').strip()
