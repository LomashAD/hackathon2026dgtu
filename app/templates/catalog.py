"""Каталог шаблонов (templates_yaml/*.yaml) и подбор шаблона по описанию задачи."""
from __future__ import annotations

import re
from functools import lru_cache

import yaml

from app.config import path
from app.retrieval.text import tokens


def load_templates() -> dict[str, dict]:
    """Каталог перечитывается при изменении YAML-файлов (новый шаблон подключается без перезапуска)."""
    files = sorted(path("templates_yaml").glob("*.yaml"))
    return _load(tuple((str(f), f.stat().st_mtime_ns) for f in files))


@lru_cache(maxsize=4)
def _load(_stamp: tuple) -> dict[str, dict]:
    out = {}
    for f in sorted(path("templates_yaml").glob("*.yaml")):
        tpl = yaml.safe_load(f.read_text(encoding="utf-8"))
        tpl["_yaml_path"] = str(f)
        out[tpl["id"]] = tpl
    return out


def score_templates(text: str) -> list[tuple[float, str]]:
    """Детерминированный подбор: ключевые выражения (сильный сигнал) + пересечение основ слов
    с названием/описанием (слабый сигнал) + совпадение вида документа."""
    t = text.lower()
    q = set(tokens(text))
    kind = "order" if re.search(r"приказ", t) else "disposition" if re.search(r"распоряжени", t) else None
    scored = []
    for tid, tpl in load_templates().items():
        s = 0.0
        if any(re.search(k, t) for k in tpl.get("keywords", [])):
            s += 2.0
        title_t = set(tokens(tpl["title"]))
        desc_t = set(tokens(tpl.get("description", "")))
        s += 0.5 * len(q & title_t) / max(len(title_t), 1) + 0.3 * len(q & desc_t) / max(len(q), 1)
        if kind and tpl["doc_kind"] == kind:
            s += 0.3
        scored.append((s, tid))
    scored.sort(reverse=True)
    return scored


def pick_template(text: str) -> tuple[str | None, list[tuple[float, str]]]:
    """Возвращает (id шаблона или None, если неоднозначно; список кандидатов)."""
    scored = score_templates(text)
    if not scored:
        return None, []
    best, second = scored[0], scored[1] if len(scored) > 1 else (0, None)
    if best[0] >= 2.0 and best[0] - second[0] >= 0.5:
        return best[1], scored
    return None, scored
