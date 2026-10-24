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
# Попытка переписать правила помощника: такому сообщению не помогаем вторым шансом поиска
INJECTION_RE = re.compile(r"игнорир\w*|забудь\w* (?:вс|предыдущ|инструкц|правил)|ignore|system prompt|системн\w* "
                          r"(?:промпт|инструкц)|ты теперь|притворись|представь,? что ты|jailbreak|developer mode", re.I)
FOREIGN_RE = re.compile(r"[一-鿿぀-ヿ]")
LATIN_RE = re.compile(r"[A-Za-z]{4,}")
# Вывод о допустимости («нельзя», «не допускается») должен опираться на норму во фрагменте, а не на вопрос:
# на «Можно ли …?» модель отвечает «Нет, нельзя …» и по пункту, где об этом ничего не сказано.
VERDICT_RE = re.compile(r"\b(нельзя|запрещ\w*|недопустим\w*|допуска\w*|разреш\w*|вправе|не\s+мож\w*|не\s+могут)\b", re.I)
NORM_RE = re.compile(r"\b(нельзя|можно|запрещ\w*|недопустим\w*|допуска\w*|разреш\w*|вправе|мож\w+|могут|обязан\w*|"
                     r"должн\w*|следует|подлеж\w*)\b", re.I)
# Отказ («Нет, нельзя …») — только по запрету во фрагменте, который касается того же, о чём вопрос: на «Можно ли
# подписать документ за руководителя?» по п. 8.19.12 («могут делегировать право подписи … Такое делегирование
# не предоставляет права действовать от имени всей Организации») модель отвечала «Нет, нельзя».
# «Нет, не нужно …» — не запрет, а «не требуется»: такой ответ этим правилом не проверяется.
NEG_VERDICT_RE = re.compile(r"\b(нельзя|запрещ\w*|недопустим\w*|не\s+допуска\w*|не\s+разреш\w*|не\s+вправе|"
                            r"не\s+мож\w*|не\s+могут)\b", re.I)
NEG_NORM_RE = re.compile(r"\b(нельзя|запрещ\w*|недопустим\w*|не\s+(?:допуска|разреш|вправе|мож|могут|долж|следует|"
                         r"подлеж|предоставл|име\w*\s+прав)\w*)", re.I)


def _roots(text: str) -> set[str]:
    """Корни по 4 буквам: «удалить» и «удаление» — одно действие (по 5 буквам «удали» ≠ «удале»)."""
    return {t[:4] for t in tokens(text) if t not in GENERIC and len(t) > 2} - {"доку"}


def ban_on_topic(src: str, question: str) -> bool:
    """Есть ли во фрагменте запрет в предложении со словом из вопроса («документ» не в счёт)."""
    q = _roots(question)
    return any(NEG_NORM_RE.search(s) and q & _roots(s) for s in re.split(r"(?<=[.;!?])\s+", src))


def foreign(text: str, src: str) -> bool:
    """Иероглифы или латинские слова, которых нет в источнике («Times New Roman» из пункта 8.16 — можно)."""
    low = src.lower()
    return bool(FOREIGN_RE.search(text)) or any(w.lower() not in low for w in LATIN_RE.findall(text))


def check_explanation(text: str, fragments: list[str], min_overlap: float = 0.55,
                      question: str = "") -> tuple[bool, str]:
    """Возвращает (годен, причина). Негоден, если есть числа/пункты не из фрагментов,
    иностранный текст или слишком много слов, которых нет во фрагментах."""
    if not text or len(text) < 15:
        return False, "пустой ответ"
    if len(text) > 900:
        return False, "слишком длинный ответ"
    src = " ".join(fragments)
    if foreign(text, src + " " + question):
        return False, "иностранный текст"
    src_nums = set(NUM_RE.findall(src))
    for n in NUM_RE.findall(text):
        if n not in src_nums:
            return False, f"число «{n}» отсутствует во фрагментах"
    if VERDICT_RE.search(text) and not NORM_RE.search(src):
        return False, "вывод о допустимости не следует из фрагментов"
    if question and NEG_VERDICT_RE.search(text) and not ban_on_topic(src, question):
        return False, "запрета по вопросу во фрагментах нет"
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
