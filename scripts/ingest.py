#!/usr/bin/env python
"""Загрузка (новой версии) документа в базу знаний.

Примеры:
  python scripts/ingest.py --all                       # все источники из config.yaml
  python scripts/ingest.py --doc data/Инструкция_по_ДОУ_учебный_пример.pdf --version 1.1 --date 2027-01-15
  python scripts/ingest.py --doc data/new.docx --doc-id reg_x --title "Регламент X" --parser numbered_docx --version 1.0
  python scripts/ingest.py --list                      # реестр версий
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import ROOT, get_config  # noqa: E402
from app.ingest.indexer import find_source, ingest, ingest_all, load_registry  # noqa: E402


def _source_for_file(file: Path) -> dict | None:
    for s in get_config()["sources"]:
        if (ROOT / s["file"]).resolve() == file.resolve() or Path(s["file"]).name == file.name:
            return s
    return None


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--doc", help="файл документа")
    ap.add_argument("--doc-id", help="идентификатор документа (по умолчанию — из config.yaml по имени файла)")
    ap.add_argument("--version", help="номер версии, например 1.1")
    ap.add_argument("--date", help="дата вступления в силу YYYY-MM-DD")
    ap.add_argument("--title", help="название (для нового документа)")
    ap.add_argument("--parser", help="instruction_pdf | numbered_docx | approval_matrix")
    ap.add_argument("--access-level", default=None)
    ap.add_argument("--all", action="store_true", help="загрузить все источники из config.yaml")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--list", action="store_true", help="показать реестр версий")
    a = ap.parse_args()

    if a.list:
        reg = load_registry()
        for doc_id, d in reg["documents"].items():
            print(f"{doc_id}: {d['title']}")
            for v in d["versions"]:
                print(f"   v{v['version']:<6} {v['status']:<9} с {v['effective_date']}  "
                      f"пунктов={v['sections']} чанков={v['chunks']}  {v['file']}")
        return
    if a.all:
        for r in ingest_all(force=a.force):
            print(json.dumps(r, ensure_ascii=False))
        return
    if not a.doc:
        ap.error("укажите --doc или --all")

    file = Path(a.doc)
    source = find_source(a.doc_id) if a.doc_id else _source_for_file(file)
    if source is None:
        if not (a.doc_id and a.title and a.parser):
            ap.error("новый документ: укажите --doc-id, --title и --parser")
        source = {"doc_id": a.doc_id, "title": a.title, "short_title": a.title, "parser": a.parser,
                  "access_level": a.access_level or "internal"}
    else:
        source = dict(source)
        if a.title:
            source["title"] = a.title
        if a.access_level:
            source["access_level"] = a.access_level
    res = ingest(source, file=file, version=a.version, effective_date=a.date, force=a.force)
    print(json.dumps(res, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
