"""Заполнение шаблона по YAML-схеме: вопросы по очереди, валидация, вычисляемые поля (код, без LLM)."""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field

from jinja2 import Environment

MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября",
          "октября", "ноября", "декабря"]
MONTH_STEMS = ["янв", "фев", "мар", "апр", "ма", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"]
SKIP_RE = re.compile(r"^(нет|не нужн\w*|не требуется|пропусти\w*|-|—|отсутству\w*|без \w+|не отменя\w*|никак\w*)\b", re.I)
REQUIRED_SKIP_RE = re.compile(r"^(нет|пропусти\w*|пропуск|не нужно|не требуется|-|—)\s*[.!]*$", re.I)
YES_RE = re.compile(r"^(да|ага|конечно|верно|связан\w*|является|yes)\b", re.I)
NO_RE = re.compile(r"^(нет|не\b|no\b)", re.I)
ACCEPT_DEFAULT_RE = re.compile(r"^(да|ок|ok|оставь\w*|по умолчанию|подходит|согласен|верно|\+)\b", re.I)
NAME_RE = re.compile(r"([А-ЯЁ]\.\s?[А-ЯЁ]\.\s?[А-ЯЁ][а-яё]+(?:-[А-ЯЁ][а-яё]+)?)|"
                     r"([А-ЯЁ][а-яё]+(?:-[А-ЯЁ][а-яё]+)?\s+[А-ЯЁ]\.\s?[А-ЯЁ]\.)")
SELF_CONTROL_RE = re.compile(r"за собой|оставля\w*|подписант", re.I)

_env = Environment(autoescape=False)


def parse_date(s: str) -> dt.date | None:
    s = s.strip().lower().replace("г.", "").replace("года", "").replace("«", "").replace("»", "")
    m = re.search(r"(\d{1,2})[./-](\d{1,2})[./-](\d{2,4})", s)
    try:
        if m:
            d, mo, y = map(int, m.groups())
            y = y + 2000 if y < 100 else y
            return dt.date(y, mo, d)
        m = re.search(r"(\d{1,2})\s+([а-я]+)\s+(\d{4})", s)
        if m:
            d, mon, y = int(m.group(1)), m.group(2), int(m.group(3))
            for i, st in enumerate(MONTH_STEMS):
                if mon.startswith(st) and not (st == "ма" and mon.startswith("мар")):
                    return dt.date(y, i + 1, d)
    except ValueError:
        return None
    return None


def fmt_date(d: dt.date) -> str:
    return f"«{d.day:02d}» {MONTHS[d.month - 1]} {d.year} г."


def norm_name(s: str) -> str:
    return re.sub(r"([А-ЯЁ])\.\s?([А-ЯЁ])\.\s?", r"\1. \2. ", s.strip()).strip()


def _clean(s: str) -> str:
    s = " ".join(s.strip().split())
    return s[:-1] if s.endswith(".") and not re.search(r"[А-ЯЁ]\.$", s) else s


def _split_list(s: str) -> list[str]:
    return [x.strip(" ,.") for x in re.split(r"[;\n]+", s) if x.strip(" ,.")]


@dataclass
class FieldResult:
    ok: bool
    value: object = None
    error: str = ""


def validate(f: dict, raw: str, values: dict) -> FieldResult:
    text = raw.strip()
    ftype = f.get("type", "text")
    if f.get("default") and (ACCEPT_DEFAULT_RE.match(text) or not text):
        return FieldResult(True, f["default"])
    if f.get("optional") and SKIP_RE.match(text):
        return FieldResult(True, "")
    if not text:
        return FieldResult(False, error="Ответ пустой.")
    if ftype not in ("yesno", "choice") and REQUIRED_SKIP_RE.match(text):
        return FieldResult(False, error="Этот пункт обязательный — без него черновик не собрать. "
                                        "Не знаете, что указать, — напишите «пример».")

    if ftype == "date":
        d = parse_date(text)
        if not d:
            return FieldResult(False, error="Не удалось распознать дату. Укажите в формате ДД.ММ.ГГГГ, например 01.12.2026.")
        nb = f.get("not_before")
        if nb and values.get(f"{nb}__date") and d < values[f"{nb}__date"]:
            return FieldResult(False, error=f"Дата не может быть раньше {fmt_date(values[nb + '__date'])}.")
        return FieldResult(True, d)
    if ftype == "yesno":
        if YES_RE.match(text):
            return FieldResult(True, True)
        if NO_RE.match(text):
            return FieldResult(True, False)
        return FieldResult(False, error="Ответьте «да» или «нет».")
    if ftype == "choice":
        low = text.lower()
        for opt in f["options"]:
            if low.startswith(opt[:6].lower()) or any(s in low for s in f.get("synonyms", {}).get(opt, [])):
                return FieldResult(True, opt)
        return FieldResult(False, error="Выберите один из вариантов: " + ", ".join(f["options"]) + ".")
    if ftype in ("person", "signer"):
        if not NAME_RE.search(text):
            return FieldResult(False, error="Укажите инициалы и фамилию, например «И. И. Иванов».")
        if ftype == "signer":
            return FieldResult(True, norm_name(NAME_RE.search(text).group(0)))
        if len(text.split()) < 3:
            return FieldResult(False, error="Добавьте должность и подразделение, например «начальник отдела ДОУ И. И. Иванов».")
        return FieldResult(True, norm_name(_clean(text)))
    if ftype == "people":
        items = _split_list(text)
        bad = [x for x in items if not NAME_RE.search(x)]
        if not items or bad:
            return FieldResult(False, error="Для каждого участника укажите должность и И. О. Фамилию"
                                             + (f" (не распознано: «{bad[0]}»)." if bad else "."))
        return FieldResult(True, [norm_name(x) for x in items])
    if ftype == "pairs":
        rows = []
        for line in _split_list(text):
            parts = re.split(r"\s+[—–-]\s+|:\s+", line, maxsplit=1)
            if len(parts) != 2 or not NAME_RE.search(parts[1]):
                return FieldResult(False, error=f"Строка «{line}» не по формату «Подразделение — должность И. О. Фамилия».")
            rows.append({"left": parts[0].strip(), "right": norm_name(parts[1])})
        return FieldResult(True, rows)
    if ftype == "control":
        word = f.get("doc_word", "документа")
        if SELF_CONTROL_RE.search(text):
            return FieldResult(True, f"Контроль за исполнением настоящего {word} оставляю за собой.")
        if not NAME_RE.search(text):
            return FieldResult(False, error="Укажите должность и И. О. Фамилию (в винительном падеже) или напишите «оставляю за собой».")
        return FieldResult(True, f"Контроль за исполнением настоящего {word} возложить на {norm_name(_clean(text))}.")
    # text
    if len(text) < 3 or re.fullmatch(r"[\W\d_]+", text):
        return FieldResult(False, error="Ответ слишком короткий — уточните, пожалуйста.")
    return FieldResult(True, _clean(text))


@dataclass
class FillSession:
    template: dict
    values: dict = field(default_factory=dict)
    idx: int = 0
    history: list[int] = field(default_factory=list)

    def _applicable(self, f: dict) -> bool:
        dep = f.get("only_if")
        return not dep or bool(self.values.get(dep))

    def current(self) -> dict | None:
        fields = self.template["fields"]
        while self.idx < len(fields) and not self._applicable(fields[self.idx]):
            self.idx += 1
        return fields[self.idx] if self.idx < len(fields) else None

    def progress(self) -> tuple[int, int]:
        total = sum(1 for f in self.template["fields"] if self._applicable(f))
        done = sum(1 for f in self.template["fields"][: self.idx] if self._applicable(f))
        return done + 1, total

    def answer(self, raw: str) -> FieldResult:
        f = self.current()
        res = validate(f, raw, self.values)
        if res.ok:
            if isinstance(res.value, dt.date):
                self.values[f["name"] + "__date"] = res.value
                self.values[f["name"]] = fmt_date(res.value)
            else:
                self.values[f["name"]] = res.value
            self.history.append(self.idx)
            self.idx += 1
        return res

    def back(self) -> None:
        if self.history:
            self.idx = self.history.pop()

    def done(self) -> bool:
        return self.current() is None

    def context(self) -> dict:
        from docxtpl import Listing

        ctx: dict = {}
        for f in self.template["fields"]:
            v = self.values.get(f["name"], "")
            if f.get("type") == "people":
                ctx[f["name"]] = Listing(";\n".join(v)) if v else ""
            elif f.get("type") == "text" and isinstance(v, str) and ";" in v:
                items = _split_list(v)
                ctx[f["name"]] = ", ".join(items)
                ctx[f["name"] + "_plural"] = len(items) > 1
            else:
                # точку в конце ставит шаблон: «… Иванов И. И.» + «.» не должно давать «..»
                strip = f.get("type", "text") in ("text", "person") and isinstance(v, str) and v.endswith(".")
                ctx[f["name"]] = v[:-1] if strip else v
            ctx.setdefault(f["name"] + "_plural", False)
        for k, expr in (self.template.get("derived") or {}).items():
            ctx[k] = _env.from_string(expr).render(**ctx)
        num, n = {}, 0
        for item in self.template.get("numbering") or []:
            key, cond = (item, None) if isinstance(item, str) else (item["key"], item.get("if"))
            if cond is None or ctx.get(cond):
                n += 1
                num[key] = n
        ctx["num"] = num
        return ctx

    def summary(self) -> list[tuple[str, str]]:
        out = []
        for f in self.template["fields"]:
            if f["name"] not in self.values:
                continue
            v = self.values[f["name"]]
            if isinstance(v, list):
                v = "; ".join(x if isinstance(x, str) else f"{x['left']} — {x['right']}" for x in v)
            elif isinstance(v, bool):
                v = "да" if v else "нет"
            out.append((f["question"].split("?")[0], str(v) if v != "" else "—"))
        return out
