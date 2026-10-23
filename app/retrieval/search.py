"""Гибридный поиск: вектор (Chroma, bge-m3) + BM25, фильтр доступа и статуса в запросе к индексу,
оценка релевантности в %, подтягивание связанных пунктов по refs."""
from __future__ import annotations

import math
import re
import threading
from collections import Counter
from dataclasses import dataclass, field

from app.config import get_config
from app.ingest.indexer import get_collection, load_registry
from app.retrieval.text import tokens
from app.security.access import allowed_levels, build_where

SECTION_REQ_RE = re.compile(
    r"(?:пункт\w*|подпункт\w*|п\.|пп\.)\s*(\d{1,2}(?:\.\d{1,2}){1,2})|(приложени\w*)\s*№?\s*(\d+)", re.I)


@dataclass
class Hit:
    id: str
    text: str
    meta: dict
    relevance: float          # 0..1
    vec: float = 0.0          # косинусная близость
    lex: float = 0.0          # доля покрытых терминов запроса (с весами idf)
    related_to: str = ""      # если подтянут по ссылке из другого пункта
    exact: bool = False       # запрошен по номеру пункта

    @property
    def percent(self) -> int:
        return int(round(self.relevance * 100))

    @property
    def section(self) -> str:
        return self.meta["section_path"]


@dataclass
class SearchResult:
    query: str
    hits: list[Hit] = field(default_factory=list)
    best: float = 0.0
    enough: bool = False


class BM25:
    def __init__(self, docs: list[list[str]], k1: float = 1.4, b: float = 0.75):
        self.docs = docs
        self.k1, self.b = k1, b
        self.N = len(docs)
        self.avgdl = sum(map(len, docs)) / max(self.N, 1)
        df = Counter(t for d in docs for t in set(d))
        self.idf = {t: math.log(1 + (self.N - n + 0.5) / (n + 0.5)) for t, n in df.items()}
        self.max_idf = max(self.idf.values()) if self.idf else 1.0
        self.tf = [Counter(d) for d in docs]

    def scores(self, q: list[str]) -> list[float]:
        out = []
        for d, tf in zip(self.docs, self.tf):
            s = 0.0
            dl = len(d)
            for t in q:
                f = tf.get(t)
                if f:
                    s += self.idf[t] * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * dl / self.avgdl))
            out.append(s)
        return out

    def coverage(self, q: list[str], i: int) -> float:
        """Доля «веса» терминов запроса, встречающихся в документе (0..1). Неизвестные термины
        запроса получают максимальный idf — запрос «про борщ» не покрыт базой."""
        q = list(dict.fromkeys(q))
        if not q:
            return 0.0
        total = sum(self.idf.get(t, self.max_idf) for t in q)
        got = sum(self.idf[t] for t in q if t in self.tf[i])
        return got / total if total else 0.0


class Retriever:
    def __init__(self):
        self.cfg = get_config()["retrieval"]
        self._lock = threading.Lock()
        self._cache: dict = {}
        self._reg_stamp = None

    # ------------------------------------------------------------ кэш BM25 по фильтру
    def _corpus(self, where: dict) -> dict:
        stamp = load_registry()
        key = repr(where)
        with self._lock:
            if self._reg_stamp != stamp:
                self._cache.clear()
                self._reg_stamp = stamp
            if key not in self._cache:
                got = get_collection().get(where=where, include=["documents", "metadatas"])
                docs = [tokens(m.get("search_text", d)) for d, m in zip(got["documents"], got["metadatas"])]
                self._cache[key] = {"ids": got["ids"], "docs": got["documents"], "metas": got["metadatas"],
                                    "bm25": BM25(docs) if docs else None,
                                    "pos": {i: n for n, i in enumerate(got["ids"])}}
            return self._cache[key]

    def _vec_score(self, cos: float) -> float:
        lo, hi = self.cfg["cos_low"], self.cfg["cos_high"]
        return min(1.0, max(0.0, (cos - lo) / (hi - lo)))

    def search(self, query: str, role: str, include_archived: bool = False,
               doc_ids: list[str] | None = None) -> SearchResult:
        from app.llm.client import get_client

        where = build_where(allowed_levels(role), include_archived=include_archived, doc_ids=doc_ids)
        corpus = self._corpus(where)
        res = SearchResult(query=query)
        if not corpus["ids"]:
            return res
        qvec = get_client().embed([query])[0]
        col = get_collection()

        # 1) вектор: запрос к индексу уже с фильтром доступа/статуса
        vq = col.query(query_embeddings=[qvec], n_results=min(self.cfg["vector_k"], len(corpus["ids"])),
                       where=where, include=["distances"])
        cos = {i: 1 - d for i, d in zip(vq["ids"][0], vq["distances"][0])}

        # 2) BM25 по тому же отфильтрованному корпусу
        qt = tokens(query)
        bm = corpus["bm25"]
        bscores = bm.scores(qt)
        top_b = sorted(range(len(bscores)), key=lambda i: -bscores[i])[: self.cfg["bm25_k"]]
        cand = set(cos) | {corpus["ids"][i] for i in top_b if bscores[i] > 0}

        # косинус для кандидатов только из BM25
        missing = [i for i in cand if i not in cos]
        if missing:
            got = col.get(ids=missing, where=where, include=["embeddings"])
            qn = math.sqrt(sum(x * x for x in qvec))
            for i, e in zip(got["ids"], got["embeddings"]):
                en = math.sqrt(sum(float(x) * float(x) for x in e))
                cos[i] = sum(a * float(b) for a, b in zip(qvec, e)) / (qn * en)

        hits = []
        for cid in cand:
            n = corpus["pos"][cid]
            v = self._vec_score(cos.get(cid, 0))
            lex = bm.coverage(qt, n)
            rel = self.cfg["w_vector"] * v + self.cfg["w_bm25"] * lex
            hits.append(Hit(cid, corpus["docs"][n], corpus["metas"][n], rel, cos.get(cid, 0), lex))

        # 3) запрос конкретного пункта по номеру («пункт 5.6», «приложение 1»)
        for m in SECTION_REQ_RE.finditer(query):
            sec = m.group(1) or f"Приложение {m.group(3)}"
            doc_pref = "mr_modules" if re.search(r"рекомендац|модульн", query, re.I) else "instr_dou"
            for cid, meta in zip(corpus["ids"], corpus["metas"]):
                if meta["section_path"] == sec and meta["doc_id"] == doc_pref:
                    n = corpus["pos"][cid]
                    hits = [h for h in hits if h.id != cid]
                    hits.append(Hit(cid, corpus["docs"][n], meta, 1.0, 1.0, 1.0, exact=True))

        # склейка частей одного пункта: оставляем лучшую часть пункта
        best_by_sec: dict = {}
        for h in sorted(hits, key=lambda h: -h.relevance):
            key = (h.meta["doc_id"], h.meta["version"], h.section)
            best_by_sec.setdefault(key, h)
        ranked = sorted(best_by_sec.values(), key=lambda h: -h.relevance)
        if any(h.exact for h in ranked):
            ranked = [h for h in ranked if h.exact]   # запрошен конкретный пункт — показываем его (+ связанные)
        if not ranked:
            return res
        res.best = ranked[0].relevance
        res.enough = res.best >= self.cfg["min_relevance"]
        floor = max(self.cfg["min_show"], res.best - self.cfg["rel_gap"])
        top = [h for h in ranked if h.relevance >= floor][: self.cfg["top_k"]]

        # 4) связанные пункты по refs (тоже только из разрешённого корпуса)
        if self.cfg.get("expand_refs") and res.enough:
            have = {(h.meta["doc_id"], h.section) for h in top}
            added = 0
            for h in list(top[:2]):
                for ref in filter(None, h.meta.get("refs", "").split(",")):
                    if added >= self.cfg["expand_refs"] or (h.meta["doc_id"], ref) in have:
                        continue
                    for cid, meta in zip(corpus["ids"], corpus["metas"]):
                        if meta["doc_id"] == h.meta["doc_id"] and meta["section_path"] == ref and meta["part"] == 0:
                            n = corpus["pos"][cid]
                            # релевантность связанного пункта — по его собственной близости к запросу
                            got = col.get(ids=[cid], include=["embeddings"])
                            e = got["embeddings"][0]
                            qn = math.sqrt(sum(x * x for x in qvec))
                            en = math.sqrt(sum(float(x) * float(x) for x in e))
                            c = sum(a * float(b) for a, b in zip(qvec, e)) / (qn * en)
                            rel = self.cfg["w_vector"] * self._vec_score(c) + \
                                self.cfg["w_bm25"] * bm.coverage(qt, n)
                            top.append(Hit(cid, corpus["docs"][n], meta, rel, c, 0, related_to=h.section))
                            have.add((meta["doc_id"], ref))
                            added += 1
                            break
        res.hits = top
        return res

    def get_section(self, doc_id: str, section: str, role: str) -> Hit | None:
        """Точное получение пункта (с учётом доступа)."""
        where = build_where(allowed_levels(role), doc_ids=[doc_id])
        corpus = self._corpus(where)
        parts = [(m["part"], i, d, m) for i, d, m in zip(corpus["ids"], corpus["docs"], corpus["metas"])
                 if m["section_path"] == section]
        if not parts:
            return None
        parts.sort(key=lambda x: x[0])
        text = "\n".join(p[2] if k == 0 else p[2].split("\n", 1)[-1] for k, p in enumerate(parts))
        return Hit(parts[0][1], text, parts[0][3], 1.0, exact=True)


_retriever: Retriever | None = None


def get_retriever() -> Retriever:
    global _retriever
    if _retriever is None:
        _retriever = Retriever()
    return _retriever
