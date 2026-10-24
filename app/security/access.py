"""Роли и уровни доступа. Фильтр строится как условие where запроса к индексу Chroma —
закрытые фрагменты не попадают ни в кандидаты поиска, ни в контекст LLM, ни в источники."""
from __future__ import annotations

from app.config import get_config


def roles() -> dict:
    return get_config()["roles"]


def allowed_levels(role: str) -> list[str]:
    r = roles().get(role) or roles()[get_config()["default_role"]]
    return list(r["levels"])


def doc_levels(source: dict) -> set[str]:
    """Уровни доступа, которые встречаются в документе: общий и у отдельных пунктов (access_overrides)."""
    return {source.get("access_level", "internal"), *(source.get("access_overrides") or {}).values()}


def can_open_original(role: str, source: dict) -> bool:
    """Оригинал файла целиком — только роли, которой доступны все его пункты: иначе закрытые пункты,
    скрытые фильтром в поиске, можно было бы прочитать в скачанном файле."""
    return doc_levels(source) <= set(allowed_levels(role))


def can_view_archive(role: str) -> bool:
    return bool(roles().get(role, {}).get("can_view_archive"))


def build_where(levels: list[str], include_archived: bool = False, doc_ids: list[str] | None = None) -> dict:
    conds: list[dict] = [{"access_level": {"$in": levels}}]
    if not include_archived:
        conds.append({"status": "active"})
    if doc_ids:
        conds.append({"doc_id": {"$in": doc_ids}})
    return conds[0] if len(conds) == 1 else {"$and": conds}
