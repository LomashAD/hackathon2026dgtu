"""Матрица обязательного визирования: детерминированный подбор строки и согласующих (без LLM)."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

from app.config import path

KIND_PATTERNS = {
    "order": r"\bприказ",
    "disposition": r"\bраспоряжени",
    "letter": r"\bписьм|\bписем|\bобращени[ея] в\b",
}
KIND_TITLES = {"order": "приказ", "disposition": "распоряжение", "letter": "письмо"}

# Ключевые слова для выбора строки матрицы внутри вида документа (порядок важен — первая подходящая).
ROW_RULES = [
    ("internal-1", "order", r"выходн|праздничн|рабоч\w* календар"),
    ("internal-2", "order", r"при[её]м\w*.{0,20}передач|передач\w* дел|сдач\w* дел"),
    ("internal-3", "order", r"информационн\w* систем|\bис\b|эксплуатац|программн|компонент"),
    ("internal-5", "disposition", r"материальн\w* помощ|выплат"),
    ("internal-6", "disposition", r"рабоч\w* групп|проектн\w* комитет|комисси|временн\w* объединен"),
    ("external-2", "letter", r"иностран|международн|зарубежн|национальн\w* банк"),
    ("external-1", "letter", r"орган\w* (государственной |гос)?власт|министерств|ведомств|администраци\w* президент|"
                             r"совет\w* федерации|государственн\w* дум|депутат|правительств|счетн\w* палат|"
                             r"федеральн\w* (служб|агентств)|минфин|минюст|минтруд|минэконом\w*|минцифр\w*|мвд|фнс|"
                             r"налогов\w*|казначейств\w*|прокуратур\w*|(пенсионн|социальн)\w* фонд|госорган"),
    ("external-3", "letter", r"юридическ\w* лиц|компани|\bао\b|\bооо\b|\bпао\b|банк|организаци|контрагент|фирм"),
]
DEFAULT_ROW = {"order": "internal-4", "disposition": "internal-7"}

UNIT_TITLES = {"legal": "Юридическое подразделение", "dou": "Департамент ДОУ", "hr": "Кадровое подразделение"}


@dataclass
class ApprovalAnswer:
    row: dict
    required: list[str]
    conditional: list[tuple[str, str]]
    matrix_version: str
    effective_date: str


class ApprovalMatrix:
    def __init__(self):
        p = path("matrix")
        self.data = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {"versions": {}}
        self.version = self.data.get("active", "")
        m = self.data["versions"].get(self.version, {})
        self.rows = {r["id"]: r for r in m.get("rows", [])}
        self.preamble = m.get("preamble", [])
        self.effective_date = m.get("effective_date", "")

    @staticmethod
    def detect_kind(text: str) -> str | None:
        t = text.lower()
        for kind, pat in KIND_PATTERNS.items():
            if re.search(pat, t):
                return kind
        return None

    def detect_row(self, text: str, kind: str | None) -> str | None:
        t = text.lower()
        for row_id, k, pat in ROW_RULES:
            if (kind is None or k == kind) and re.search(pat, t):
                return row_id
        return None

    def options(self, kind: str) -> list[tuple[str, str]]:
        """Строки матрицы для уточняющего вопроса."""
        group = "external" if kind == "letter" else "internal"
        out = []
        for rid, r in self.rows.items():
            if r["group"] != group:
                continue
            if kind == "order" and not r["title"].startswith(("Приказ", "Прочие приказ")):
                continue
            if kind == "disposition" and not r["title"].startswith(("Распоряж", "Прочие распоряж")):
                continue
            out.append((rid, r["title"]))
        return out

    def lookup(self, row_id: str) -> ApprovalAnswer:
        r = self.rows[row_id]
        req, cond = [], []
        for key in ("legal", "dou", "hr", "other"):
            v = r[key]
            units = v.get("units") or [UNIT_TITLES.get(key, "")]
            if v["required"] == "yes":
                req.extend(units)
            elif v["required"] == "conditional":
                cond.extend((u, v["condition"]) for u in units)
        return ApprovalAnswer(r, req, cond, self.version, self.effective_date)

    def citation(self, row_id: str) -> str:
        r = self.rows[row_id]
        grp = "Внутренние документы" if r["group"] == "internal" else "Внешние документы"
        return f"Матрица обязательного визирования, ред. {self.version}, раздел «{grp}», строка {r['num']}"


_matrix: ApprovalMatrix | None = None


_matrix_stamp = None


def get_matrix(reload: bool = False) -> ApprovalMatrix:
    """Матрица перечитывается при изменении storage/matrix.json (новая версия после ingest)."""
    global _matrix, _matrix_stamp
    p = path("matrix")
    stamp = p.stat().st_mtime_ns if p.exists() else 0
    if _matrix is None or reload or stamp != _matrix_stamp:
        _matrix, _matrix_stamp = ApprovalMatrix(), stamp
    return _matrix
