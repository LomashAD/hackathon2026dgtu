"""Чанкинг по структуре пунктов: один пункт/подпункт = один чанк (длинные делятся по абзацам)."""
from __future__ import annotations

import re

ID_RE = re.compile(r"(?<![\d.,])(\d{1,2}\.\d{1,2}(?:\.\d{1,2})?)(?![\d,]|\.\d)")
RANGE_RE = re.compile(r"(\d{1,2}\.\d{1,2}(?:\.\d{1,2})?)\s*[–—-]\s*(\d{1,2}\.\d{1,2}(?:\.\d{1,2})?)")
APPX_RE = re.compile(r"приложени\w*\s*№\s*(\d+)", re.I)


def extract_refs(text: str, known: list[str], self_id: str) -> list[str]:
    """Номера пунктов, на которые есть отсылки в тексте (диапазоны «4.1.1–4.1.3» раскрываются)."""
    order = {s: i for i, s in enumerate(known)}
    refs: list[str] = []
    for a, b in RANGE_RE.findall(text):
        if a in order and b in order:
            refs.extend(known[order[a]:order[b] + 1])
    for m in ID_RE.findall(text):
        if m in order:
            refs.append(m)
    for n in APPX_RE.findall(text):
        name = f"Приложение {n}"
        if name in order:
            refs.append(name)
    seen, out = set(), []
    for r in refs:
        if r != self_id and r not in seen:
            seen.add(r)
            out.append(r)
    return out


def _split_long(text: str, max_chars: int) -> list[str]:
    paras = text.split("\n")
    if len(paras) > 8 and paras[0].endswith(":") and sum(" — " in p for p in paras[1:]) > len(paras) * 0.6:
        # Перечень терминов «термин — определение»: одно определение = один чанк.
        return ["\n".join([paras[0], p]) for p in paras[1:]]
    if len(text) <= max_chars or len(paras) < 3:
        return [text]
    lead, rest = paras[0], paras[1:]
    parts, cur = [], []
    for p in rest:
        if cur and len(lead) + sum(len(x) for x in cur) + len(p) > max_chars:
            parts.append(cur)
            cur = []
        cur.append(p)
    if cur:
        parts.append(cur)
    return ["\n".join([lead] + chunk) for chunk in parts]


def build_chunks(sections: list[dict], source: dict, version: str, effective_date: str,
                 max_chars: int = 1400) -> list[dict]:
    """Секции → чанки с метаданными (doc_id, version, section_path, refs, access_level...)."""
    known = [s["section_path"] for s in sections]
    by_id = {s["section_path"]: s for s in sections}
    overrides = source.get("access_overrides") or {}
    chunks = []
    for s in sections:
        sid = s["section_path"]
        refs = extract_refs(s["text"], known, sid)
        parent_id = sid.rsplit(".", 1)[0] if s["kind"] == "point" and sid.count(".") >= 2 else ""
        parent_lead = ""
        if parent_id in by_id:
            parent_lead = by_id[parent_id]["text"].split("\n")[0][:220]
        access = overrides.get(sid) or (overrides.get(parent_id) if parent_id else None) \
            or source.get("access_level", "internal")
        heading = s.get("chapter_title", "")
        parts = _split_long(s["text"], max_chars)
        for i, part in enumerate(parts):
            label = f"п. {sid}" if s["kind"] == "point" else sid
            prefix = f"{source.get('short_title', source['title'])}. "
            if s.get("chapter"):
                prefix += f"Глава {s['chapter']}. {heading}. " if source["parser"] == "instruction_pdf" \
                    else f"Раздел {s['chapter']}. {heading}. "
            elif heading:
                prefix += f"{heading}. "
            prefix += f"{label}. "
            if parent_lead:
                prefix += f"(К п. {parent_id}: {parent_lead}) "
            chunks.append({
                "id": f"{source['doc_id']}@{version}#{sid}#{i}",
                "text": part,
                "search_text": prefix + part,
                "meta": {
                    "doc_id": source["doc_id"],
                    "doc_title": source["title"],
                    "short_title": source.get("short_title", source["title"]),
                    "version": str(version),
                    "effective_date": str(effective_date),
                    "status": "active",
                    "section_path": sid,
                    "part": i,
                    "parts": len(parts),
                    "kind": s["kind"],
                    "chapter": s.get("chapter", ""),
                    "appendix": s.get("appendix", ""),
                    "heading": heading,
                    "access_level": access,
                    "refs": ",".join(refs),
                },
            })
    return chunks
