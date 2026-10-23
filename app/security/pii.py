"""Маскирование персональных данных перед записью в логи (регулярные выражения)."""
from __future__ import annotations

import re

PATTERNS = [
    ("EMAIL", re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")),
    ("PHONE", re.compile(r"(?<!\d)(?:\+7|8)[\s(-]*\d{3}[\s)-]*\d{3}[\s-]*\d{2}[\s-]*\d{2}(?!\d)")),
    ("SNILS", re.compile(r"(?<!\d)\d{3}-\d{3}-\d{3}[\s-]\d{2}(?!\d)")),
    ("PASSPORT", re.compile(r"(?<!\d)\d{2}\s?\d{2}\s?№?\s?\d{6}(?!\d)")),
    ("CARD", re.compile(r"(?<!\d)(?:\d{4}[\s-]?){3}\d{4}(?!\d)")),
    ("INN", re.compile(r"(?<![\d.])\d{10}(?:\d{2})?(?![\d.])")),
    # ФИО: «И. И. Иванов», «Иванов И. И.», «Иванов Иван Иванович»
    ("FIO", re.compile(r"[А-ЯЁ]\.\s?[А-ЯЁ]\.\s?[А-ЯЁ][а-яё]+(?:-[А-ЯЁ][а-яё]+)?")),
    ("FIO", re.compile(r"[А-ЯЁ][а-яё]+(?:-[А-ЯЁ][а-яё]+)?\s+[А-ЯЁ]\.\s?[А-ЯЁ]\.")),
    ("FIO", re.compile(r"[А-ЯЁ][а-яё]+\s+[А-ЯЁ][а-яё]+\s+[А-ЯЁ][а-яё]+(?:вич|вна|ична|чна|вича|вны|вичу|вне)\b")),
    ("ADDRESS", re.compile(r"(?:ул\.|улица|пр-т|проспект|пер\.|д\.|кв\.)\s*[А-ЯЁа-яё0-9-]+", re.I)),
]


def mask(text: str) -> str:
    if not text:
        return text
    for label, rx in PATTERNS:
        text = rx.sub(f"[{label}]", text)
    return text
