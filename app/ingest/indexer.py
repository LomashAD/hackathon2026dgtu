"""Индексация и версионность базы знаний.

- Чанки всех документов хранятся в одной коллекции Chroma «kb» (персистентно, локально).
- Новая версия документа загружается со статусом active, прежняя active-версия → archived.
- Реестр версий: storage/registry.json (версия, дата, хеш файла, статус, дифф пунктов).
- Матрица визирования дополнительно сохраняется таблицей (storage/matrix.json).
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import os
from pathlib import Path

os.environ.setdefault("ANONYMIZED_TELEMETRY", "False")

from app.config import ROOT, get_config, path  # noqa: E402
from app.ingest import parsers  # noqa: E402
from app.ingest.chunker import build_chunks  # noqa: E402

log = logging.getLogger(__name__)
COLLECTION = "knowledge_base"


_client_stamp: tuple | None = None


def get_collection():
    """Коллекция Chroma. Если реестр версий изменён другим процессом (scripts/ingest.py при работающем
    сервере), клиент пересоздаётся — иначе Chroma отдаёт закэшированное состояние индекса."""
    global _client_stamp
    import chromadb
    from chromadb.api.client import SharedSystemClient
    from chromadb.config import Settings

    reg = path("registry")
    stamp = (str(path("index_dir")), reg.stat().st_mtime_ns if reg.exists() else 0)
    if _client_stamp is not None and stamp != _client_stamp:
        SharedSystemClient.clear_system_cache()
    _client_stamp = stamp
    client = chromadb.PersistentClient(path=str(path("index_dir")),
                                       settings=Settings(anonymized_telemetry=False))
    return client.get_or_create_collection(COLLECTION, metadata={"hnsw:space": "cosine"},
                                           embedding_function=None)


# ---------------------------------------------------------------- реестр версий

def load_registry() -> dict:
    p = path("registry")
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return {"documents": {}}


def save_registry(reg: dict) -> None:
    p = path("registry")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(reg, ensure_ascii=False, indent=2), encoding="utf-8")


def active_version(doc_id: str, reg: dict | None = None) -> dict | None:
    reg = reg or load_registry()
    doc = reg["documents"].get(doc_id)
    if not doc:
        return None
    return next((v for v in doc["versions"] if v["status"] == "active"), None)


def _sha(file: Path) -> str:
    return hashlib.sha256(file.read_bytes()).hexdigest()


def _diff(old: dict, new: dict) -> dict:
    added = [k for k in new if k not in old]
    removed = [k for k in old if k not in new]
    changed = [k for k in new if k in old and old[k] != new[k]]
    return {"added": added, "removed": removed, "changed": changed}


# ---------------------------------------------------------------- парсинг источника

def parse_source(source: dict, file: Path) -> tuple[list[dict], dict | None]:
    """Возвращает (секции, таблица матрицы или None)."""
    parser = source["parser"]
    if parser == "approval_matrix":
        matrix = parsers.parse_matrix_docx(file)
        return parsers.matrix_sections(matrix), matrix
    if parser not in parsers.PARSERS:
        raise ValueError(f"Неизвестный парсер {parser!r}. Доступны: {list(parsers.PARSERS)} + approval_matrix")
    return parsers.PARSERS[parser](file), None


def ingest(source: dict, file: Path | None = None, version: str | None = None,
           effective_date: str | None = None, force: bool = False, embedder=None) -> dict:
    """Загрузить (новую версию) документа. Возвращает сводку."""
    file = Path(file or source["file"])
    if not file.is_absolute():
        file = ROOT / file
    version = str(version or source.get("version", "1.0"))
    effective_date = str(effective_date or source.get("effective_date", dt.date.today().isoformat()))
    doc_id = source["doc_id"]

    reg = load_registry()
    doc = reg["documents"].setdefault(doc_id, {"title": source["title"], "versions": []})
    doc["title"] = source["title"]
    doc["source"] = {k: v for k, v in source.items() if k not in ("version", "effective_date", "file")}
    sha = _sha(file)
    existing = next((v for v in doc["versions"] if v["version"] == version), None)
    if existing and existing["sha256"] == sha and existing["status"] == "active" and not force:
        return {"doc_id": doc_id, "version": version, "skipped": True, "reason": "версия уже загружена"}

    sections, matrix = parse_source(source, file)
    chunks = build_chunks(sections, source, version, effective_date)
    if embedder is None:
        from app.llm.client import get_client
        embedder = get_client().embed
    vectors = embedder([c["search_text"] for c in chunks])

    col = get_collection()
    # Удаляем прежние чанки той же версии (переиндексация) и добавляем новые.
    col.delete(where={"$and": [{"doc_id": doc_id}, {"version": version}]})
    col.add(ids=[c["id"] for c in chunks], embeddings=vectors,
            documents=[c["text"] for c in chunks],
            metadatas=[{**c["meta"], "search_text": c["search_text"]} for c in chunks])

    # Прежнюю активную версию переводим в архив.
    prev = next((v for v in doc["versions"] if v["status"] == "active" and v["version"] != version), None)
    archived = []
    for v in doc["versions"]:
        if v["status"] == "active" and v["version"] != version:
            got = col.get(where={"$and": [{"doc_id": doc_id}, {"version": v["version"]}]}, include=["metadatas"])
            if got["ids"]:
                col.update(ids=got["ids"], metadatas=[{**m, "status": "archived"} for m in got["metadatas"]])
            v["status"] = "archived"
            v["archived_at"] = dt.datetime.now().isoformat(timespec="seconds")
            archived.append(v["version"])

    section_hash = {s["section_path"]: hashlib.md5(s["text"].encode()).hexdigest()[:10] for s in sections}
    diff = _diff(prev.get("section_hash", {}), section_hash) if prev else None
    entry = {
        "version": version, "effective_date": effective_date, "status": "active",
        "file": str(file.relative_to(ROOT)) if file.is_relative_to(ROOT) else str(file),
        "sha256": sha, "ingested_at": dt.datetime.now().isoformat(timespec="seconds"),
        "sections": len(sections), "chunks": len(chunks), "section_hash": section_hash,
        "diff_from_previous": diff,
    }
    doc["versions"] = [v for v in doc["versions"] if v["version"] != version] + [entry]
    save_registry(reg)

    if matrix is not None:
        mpath = path("matrix")
        mpath.parent.mkdir(parents=True, exist_ok=True)
        all_m = json.loads(mpath.read_text(encoding="utf-8")) if mpath.exists() else {"versions": {}}
        all_m["versions"][version] = {**matrix, "effective_date": effective_date, "doc_id": doc_id}
        all_m["active"] = version
        mpath.write_text(json.dumps(all_m, ensure_ascii=False, indent=2), encoding="utf-8")

    return {"doc_id": doc_id, "version": version, "sections": len(sections), "chunks": len(chunks),
            "archived_versions": archived, "diff": diff}


def ingest_all(force: bool = False) -> list[dict]:
    """Загрузить все источники из config.yaml (если ещё не загружены)."""
    out = []
    for src in get_config()["sources"]:
        out.append(ingest(src, force=force))
    return out


def find_source(doc_id: str) -> dict | None:
    for s in get_config()["sources"]:
        if s["doc_id"] == doc_id:
            return s
    doc = load_registry()["documents"].get(doc_id)
    return dict(doc["source"], title=doc["title"]) if doc and "source" in doc else None
