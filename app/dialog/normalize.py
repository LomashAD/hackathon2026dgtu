"""Разговорная речь сотрудника → текст для правил и поиска (без LLM).

Сотрудник пишет так, как говорит: «скока дней на согласавание», «когда регать входящие»,
«спс, а если вечером пришло?», иногда — в английской раскладке. Здесь:
  - раскладка клавиатуры (rjulf → когда);
  - сленг и сокращения (регать → регистрировать, служебка → служебная записка);
  - опечатки: слово, которого нет в словаре русского языка (pymorphy3), заменяется ближайшим словом
    из текстов базы знаний — не больше 1 исправленной буквы в коротком слове и 2 в длинном;
  - вежливые обороты и слова-связки в начале и в конце («подскажите, пожалуйста», «спасибо»);
  - реплики без вопроса («спасибо», «не понял», «ты бот?») — вид реплики, на который отвечают без поиска;
  - продолжение диалога «а исходящий?» — подстановка слова в предыдущий вопрос по грамматике.
Исходный текст не теряется: в журнал пишется он, исправления опечаток показываются в ответе.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from functools import lru_cache

WORD_RE = re.compile(r"[А-Яа-яЁё]+")

# ---------------------------------------------------------------- раскладка
LAT = "qwertyuiop[]asdfghjkl;'zxcvbnm`QWERTYUIOP{}ASDFGHJKL:\"ZXCVBNM~"
CYR = "йцукенгшщзхъфывапролджэячсмитьёЙЦУКЕНГШЩЗХЪФЫВАПРОЛДЖЭЯЧСМИТЬЁ"
LAYOUT = str.maketrans(LAT, CYR)
INNER_PUNCT = str.maketrans(",.<>", "бюБЮ")   # «б» и «ю» на месте запятой и точки — только внутри слова

# ---------------------------------------------------------------- сленг и сокращения
REG_FORMS = {"ть": "ировать", "ю": "ирую", "ет": "ирует", "ем": "ируем", "ете": "ируете", "ют": "ируют",
             "л": "ировал", "ла": "ировала", "ли": "ировали", "й": "ируй", "йте": "ируйте"}

SLANG = [(re.compile(p, re.I), r) for p, r in [
    (r"\b(че|чё|чо|шо|што)\b", "что"),
    (r"\b(скок[аоу]?|сколька|сколко)\b", "сколько"),
    (r"\bщас\b", "сейчас"),
    (r"\b(плиз|плз|пжл|пжлст|пж|пожалуйсто|пожалуста|please|pls)\b", "пожалуйста"),
    (r"\b(спс|спасибки|пасиб\w*|сенкс|thx|thanks)\b", "спасибо"),
    (r"\b(за)?рега(ть|ю|ет|ем|ете|ют|л|ла|ли|йте|й)\b", lambda m: (m.group(1) or "") + "регистр" + REG_FORMS[m.group(2).lower()]),
    (r"\b(за)?регн(уть|и|ите|у|ем|ул|ула|ули)\b", lambda m: (m.group(1) or "за") + "регистрировать"),
    (r"\b(за)?реган(\w*)", lambda m: "зарегистрирован" + m.group(2)),
    (r"\bрег(а|и|у|ой)\b", "регистрация"),
    (r"\b(в|ис)ходя[шщ]к\w*", lambda m: m.group(1) + "ходящий документ"),
    (r"\bвх\.?(?=\s|$)", "входящий"),
    (r"\bисх\.?(?=\s|$)", "исходящий"),
    (r"\bслужебк\w*", "служебная записка"),
    (r"\b(е|и)м[еэ]йл\w*|\bмейл\w*|\bэлектронк\w* почт\w*", "электронная почта"),
    (r"\bэлектронк\w*", "электронной форме"),
    (r"\bбумажк\w*", "бумажный документ"),
    (r"\bдок(и|ов|ам|ами|ах|у|а|ом|е)?\b|\bдокумент[иы]к\w*|\bдокументашк\w*", "документ"),
    (r"\bсогласовк\w*|\bсогласовалк\w*", "согласование"),
    (r"\bвизировк\w*", "визирование"),
    (r"\bсэдо?\b|\bсдо\b", "ИИС ДО"),
    (r"\bинф(а|у|ы|ой)\b", "информация"),
    (r"\bприказ(ик|ух)\w*", "приказ"),
    (r"\bраспоряж(ух|енька)\w*", "распоряжение"),
    (r"\bписьм(ец|ишк)\w*", "письмо"),
    (r"\b(шеф|босс|начальств)\w*", "руководитель"),
    (r"\bначальник\w*", "руководитель"),
    (r"\bдедлайн\w*", "срок"),
    (r"\bзам(а|у|ом)?\b", "заместитель"),
    (r"\bотдел\w* кадров\b|\bкадровик\w*|\bэйчар\w*|\bhr\b", "кадровое подразделение"),
    (r"\bвообщем\b", "в общем"),
    (r"\b(блин|короче|типа)\b,?", ""),
]]

# ---------------------------------------------------------------- вежливость, связки, реплики без вопроса
LEAD_WORDS = [
    ("greeting", r"привет\w*|здравствуй\w*|здрасьте|здрасте|добр\w+ (?:день|утро|вечер)|доброе утро|приветствую|"
                 r"хай|салют"),
    ("thanks", r"(?:большое |огромное )?спасибо(?: (?:большое|огромное|за ответ|за помощь))?|благодарю|благодарствую"),
    ("confused", r"не понял\w*|непонятно|не ясно|не очень понятно"),
    ("ack", r"ок|окей|ладно|хорошо|(?:вс[её] |теперь )?(?:понятно|ясно)|понял\w*|разобрал\w*|отлично|супер|класс|"
            r"круто|принято|угу|ага"),
    ("filler", r"а скажи\w*|а подскаж\w*|слушай\w*|скажи\w*|подскаж\w*|пожалуйста|извини\w*|прости\w*|ну|так|"
               r"коллега|бот|помощник|уважаем\w* помощник|у меня (?:есть )?(?:такой )?вопрос|вопрос(?: такой)?|"
               r"такой вопрос|хочу (?:спросить|узнать|уточнить)|хотел\w* (?:бы )?(?:спросить|узнать|уточнить)|интересует"),
]
LEAD_RE = re.compile(r"^\s*(?:" + "|".join(f"(?P<{k}>{p})" for k, p in LEAD_WORDS) + r")(?=$|[\s,.!?:;—–)-])"
                     r"[\s,.!:;—–)-]*", re.I)
TRAIL_RE = re.compile(r"[\s,.!]*(?:заранее\s+)?(?:спасибо\w*|благодарю|пожалуйста)\s*([?!.)]*)\s*$", re.I)

SMALLTALK = [(k, re.compile(rf"^(?:{p})$", re.I)) for k, p in [
    ("how", r"как (?:у (?:тебя|вас) )?(?:дела|жизнь|поживаешь|поживаете|сам|сама|ты|вы|настроение|оно)|что нового"),
    ("who", r"(?:а )?(?:ты|вы) кто(?: такой| такая| такие)?(?: вообще)?|(?:а )?кто (?:ты|вы)(?: такой| такая| такие)?(?: вообще)?|"
            r"(?:ты|вы) (?:бот|робот|человек|живой|живая|ии|нейросеть|настоящий|программа)|"
            r"с кем я (?:говорю|разговариваю|общаюсь)|как (?:тебя|вас) зовут|кто (?:тебя|вас) (?:создал|сделал|написал)"),
    ("confused", r"что|чего|а|и|ну и|и что|и что дальше|дальше|в смысле|то есть|это как|как это|"
                 r"ничего не (?:понял\w*|понятно|ясно)|"
                 r"(?:а |и )?что это (?:значит|означает)|объясни\w*(?: (?:проще|попроще|понятнее|по-человечески|нормально|подробнее))?|"
                 r"(?:а |можно |скажи |скажите )?(?:проще|попроще|понятнее|по-человечески)|поясни\w*|разжуй|"
                 r"переведи на (?:русский|человеческий)(?: язык)?"),
    ("wrong", r"(?:это )?не то|не так|неправильно|не правильно|неверно|не верно|ошибка|(?:ты )?ошибаешься|мимо|"
              r"не об этом|не про то|не про это|я (?:спрашивал\w* )?не (?:об этом|про это)(?: спрашивал\w*)?|"
              r"фигня|бред|ерунда|не помогло|не помог"),
    ("cancel", r"отмена|отмени\w*|стоп|хватит|отбой|забудь|проехали|неважно|не важно|передумал\w*|не надо|"
               r"выход|выйти|прервать|cancel"),
    ("wait", r"подожди\w*|погоди\w*|секунд\w*|сек|минутк\w*|минуту|одну минуту|сейчас|щас"),
    ("filler", r"можно (?:задать )?(?:один )?вопрос|(?:есть|ещ[её]) (?:один )?вопрос|вопрос есть|у меня ещ[её] вопрос"),
    ("bye", r"пока|до свидания|до встречи|до завтра|всего (?:доброго|хорошего)|хорошего дня|удачи|бывай|счастливо"),
    ("yes", r"да|давай\w*|хочу|конечно|го|можно|да давай|ок давай|согласен|согласна|yes|подготовь|сделай|делай"),
    ("no", r"нет|неа|не нужно|не хочу|no|ничего|не сейчас|потом"),
    ("rude", r"(?:ты )?(?:тупой|тупая|дурак|идиот|глупый|бесполезный|отстой|тупица)(?: бот)?"),
    ("help", r"помоги\w*|помощь|что (?:ты )?(?:умеешь|можешь)|чем (?:ты )?(?:можешь )?помочь|с чего начать|"
             r"как (?:тобой|этим) пользоваться|меню"),
]]
COURTESY_ORDER = ("confused", "thanks", "greeting", "ack", "filler")


@dataclass
class Normalized:
    original: str
    text: str                                                   # для правил и поиска
    fixes: list[tuple[str, str]] = field(default_factory=list)  # исправленные опечатки (было, стало)
    layout: bool = False                                        # набрано в английской раскладке
    courtesy: set[str] = field(default_factory=set)             # срезанные обороты: greeting, thanks, ack, …
    smalltalk: str | None = None                                # реплика без вопроса: thanks, bye, how, who, …

    @property
    def changed(self) -> bool:
        return bool(self.fixes) or self.layout


@lru_cache(maxsize=1)
def morph():
    import pymorphy3
    return pymorphy3.MorphAnalyzer()


def known(word: str) -> bool:
    return morph().word_is_known(word.lower())


# ---------------------------------------------------------------- шаги нормализации
def fix_layout(text: str) -> str | None:
    """Текст, набранный в английской раскладке, — по-русски; None, если это не тот случай."""
    if re.search(r"[А-Яа-яЁё]", text) or len(re.findall(r"[A-Za-z]", text)) < 4:
        return None
    conv = re.sub(r"(?<=[A-Za-z\[\];'`])[,.<>](?=[A-Za-z\[\];'`])", lambda m: m.group(0).translate(INNER_PUNCT), text)
    conv = re.sub(r"[A-Za-z\[\]{};:'\"`~]+", lambda m: m.group(0).translate(LAYOUT), conv)
    words = [w for w in WORD_RE.findall(conv) if len(w) >= 2]
    if not words or sum(known(w) for w in words) / len(words) < 0.6:
        return None
    return conv


def apply_slang(text: str) -> str:
    for rx, rep in SLANG:
        text = rx.sub(rep, text)
    return re.sub(r"\s{2,}", " ", text).strip()


def strip_courtesy(text: str) -> tuple[str, set[str]]:
    flags: set[str] = set()
    while True:
        m = LEAD_RE.match(text)
        if not m or not m.group(0).strip():
            break
        flags.update(k for k, v in m.groupdict().items() if v)
        text = text[m.end():]
    while m := TRAIL_RE.search(text):
        flags.add("thanks" if re.search(r"спасиб|благодар", m.group(0), re.I) else "filler")
        text = text[: m.start()] + m.group(1)
    text = re.sub(r"([?!.])[?!.]*$", lambda m: "?" if "?" in m.group(0) else m.group(1), text.strip())
    return text.strip(" ,;:—–-"), flags


def _osa(a: str, b: str, limit: int) -> int:
    """Расстояние Дамерау — Левенштейна (перестановка соседних букв — одна ошибка) с ранним выходом."""
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    prev2: list[int] = []
    prev = list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        cur = [i] + [0] * len(b)
        for j in range(1, len(b) + 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (a[i - 1] != b[j - 1]))
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                cur[j] = min(cur[j], prev2[j - 2] + 1)
        if min(cur) > limit:
            return limit + 1
        prev2, prev = prev, cur
    return prev[-1]


class Speller:
    """Исправление опечаток по словам базы знаний: кандидаты — слова корпуса и все формы этих слов
    («зарегистрировать» при «регистрирует» в тексте) на ту же букву, выбирается ближайшее,
    при равенстве — встречающееся в корпусе чаще."""

    def __init__(self, vocab: Counter):
        self.vocab = Counter(vocab)
        seen: set[str] = set()
        for w in list(vocab):
            if len(w) < 5:
                continue
            p = morph().parse(w)[0]
            if p.normal_form in seen:
                continue
            seen.add(p.normal_form)
            for f in p.lexeme:
                self.vocab.setdefault(f.word.replace("ё", "е"), 0)
        self.by_first: dict[str, list[tuple[str, int]]] = {}
        for w, n in self.vocab.items():
            if len(w) >= 4:
                self.by_first.setdefault(w[0], []).append((w, n))

    def correct(self, word: str) -> str | None:
        w = word.lower().replace("ё", "е")
        if len(w) < 5 or w in self.vocab or known(w):
            return None
        limit = 1 if len(w) <= 7 else 2
        best, best_key = None, (limit + 1, 0)
        for cand, n in self.by_first.get(w[0], ()):
            d = _osa(w, cand, limit)
            if d <= limit and (d, -n) < best_key:
                best, best_key = cand, (d, -n)
        return best


_speller: tuple[Counter, Speller] | None = None


def speller(vocab: Counter) -> Speller:
    global _speller
    if _speller is None or _speller[0] is not vocab:
        _speller = (vocab, Speller(vocab))
    return _speller[1]


def fix_typos(text: str, vocab: Counter | None) -> tuple[str, list[tuple[str, str]]]:
    if not vocab:
        return text, []
    sp, fixes = speller(vocab), []

    def rep(m: re.Match) -> str:
        w = m.group(0)
        first = not text[: m.start()].strip() or text[: m.start()].rstrip().endswith((".", "!", "?"))
        if w.isupper() or not w[0].islower() and not (first and w[1:].islower()):
            return w                    # аббревиатуры и имена собственные не трогаем
        c = sp.correct(w)
        if not c:
            return w
        c = c.capitalize() if w[0].isupper() else c
        fixes.append((w, c))
        return c

    return WORD_RE.sub(rep, text), fixes


def smalltalk_kind(core: str, flags: set[str]) -> str | None:
    t = re.sub(r"[^\w\s-]", " ", core.lower().replace("ё", "е"))
    t = re.sub(r"\s+", " ", t).strip(" -")
    if not t:
        return next((k for k in COURTESY_ORDER if k in flags), "ack")
    for kind, rx in SMALLTALK:
        if rx.match(t):
            return kind
    return None


def normalize(text: str, vocab: Counter | None = None) -> Normalized:
    n = Normalized(original=text, text=text.strip())
    conv = fix_layout(n.text)
    if conv:
        n.text, n.layout = conv, True
    n.text = apply_slang(n.text)
    n.text, n.courtesy = strip_courtesy(n.text)
    n.text, n.fixes = fix_typos(n.text, vocab)
    n.smalltalk = smalltalk_kind(n.text, n.courtesy)
    return n


# ---------------------------------------------------------------- термины Инструкции для поиска
def expand(query: str) -> str:
    """Разговорное выражение → термин Инструкции, дописывается к запросу поиска (colloquial_terms в config.yaml):
    «письмо пришло не по адресу» ищется и как «некорректная маршрутизация». В ответе не показывается."""
    from app.config import get_config

    extra = [t["add"] for t in get_config().get("colloquial_terms") or [] if re.search(t["when"], query, re.I)]
    return f"{query} ({'; '.join(extra)})" if extra else query


# ---------------------------------------------------------------- продолжение диалога
FOLLOW_SKIP = {"а", "и", "но", "тогда", "если", "это", "этот", "эта", "эти", "как", "что", "насчет", "насчёт",
               "так", "ну", "же", "ли", "там", "тут", "бы"}
REPLACE_PREP = {"для", "про", "о", "об", "по"}           # «а для приказа?» — тот же вопрос о другом предмете
NOUNISH = ("NOUN", "ADJF", "PRTF")


def _forms(word: str) -> set[tuple]:
    return {(p.tag.POS, p.tag.case, p.tag.number) for p in morph().parse(word.lower()) if p.tag.POS in NOUNISH}


def substitute(prev: str, follow: str, choose=None) -> str | None:
    """«Когда регистрировать входящий документ?» + «а исходящий?» → «Когда регистрировать исходящий документ?».
    Подставляется одно слово той же части речи, падежа и числа. Предлог в уточнении («а в выходной?») — это
    новое обстоятельство, а не замена: подстановка только при том же предлоге в вопросе. Иначе None."""
    tokens = re.findall(r"[А-Яа-яЁё-]+", follow.lower())
    words = [w for w in tokens if w not in FOLLOW_SKIP]
    preps = [w for w in words if w in REPLACE_PREP or (len(w) <= 3 and not _forms(w))]
    content = [w for w in words if w not in preps]
    if len(content) != 1:
        return None
    w, prep = content[0], (preps[0] if preps else "")
    wf = _forms(w)
    if not wf:
        return None
    lemmas = {p.normal_form for p in morph().parse(w)}
    cands = []
    for m in WORD_RE.finditer(prev):
        if {p.normal_form for p in morph().parse(m.group(0).lower())} & lemmas:
            return None                 # это слово уже есть в вопросе — подставлять нечего
        if not (_forms(m.group(0)) & wf):
            continue
        before = prev[: m.start()].split()
        if prep and prep not in REPLACE_PREP and (not before or before[-1].lower() != prep):
            continue
        cands.append(m)
    if not cands:
        return None
    m = cands[0] if len(cands) == 1 or choose is None else choose(w, cands)
    if m is None:
        return None
    new = _agree(w, m.group(0).lower())
    new = new.capitalize() if m.group(0)[0].isupper() else new
    return prev[: m.start()] + new + prev[m.end():]


def _agree(new: str, old: str) -> str:
    """Прилагательное из уточнения принимает род, число и падеж заменяемого: «входящее письмо» + «а исходящий?»
    → «исходящее письмо», а не «исходящий письмо». Существительное подставляется как есть."""
    adj = ("ADJF", "PRTF")
    o = next((p for p in morph().parse(old) if p.tag.POS in adj), None)
    n = next((p for p in morph().parse(new) if p.tag.POS in adj), None)
    if o is None or n is None:
        return new
    want = {g for g in (o.tag.gender, o.tag.number, o.tag.case, o.tag.animacy) if g}
    f = n.inflect(want)
    return f.word if f else new
