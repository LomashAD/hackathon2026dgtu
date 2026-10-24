#!/usr/bin/env python
"""Оценка качества на tests/eval_questions.yaml через полный пайплайн (движок диалога).

Метрики:
  - hit rate: доля вопросов с ответом, где ожидаемый пункт попал в источники;
  - корректные «не знаю»: доля вопросов без ответа/провокаций, где бот честно отказал;
  - утечки доступа: закрытые пункты в источниках у роли без доступа (должно быть 0);
  - доля ответов, где пояснение модели прошло проверку на соответствие фрагментам;
  - время ответа (среднее, p95);
  - разговорная речь (colloquial: dev / holdout): сленг, опечатки, продолжение диалога, вежливые фразы —
    ожидаемый пункт, строка Матрицы или шаблон в ответе, честный отказ, ответ на «спасибо» без отказа.
Результат: печать + tests/eval_results.md.

  python tests/run_eval.py [--no-llm]
"""
from __future__ import annotations

import argparse
import logging
import statistics
import sys
import time
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import get_config  # noqa: E402
from app.dialog.engine import Session, get_engine  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-llm", action="store_true", help="без пояснений модели (только поиск и код)")
    a = ap.parse_args()
    logging.basicConfig(level=logging.WARNING)
    cfg = get_config()
    if a.no_llm:
        cfg["llm"]["explain"] = False
    eng = get_engine()
    qs = yaml.safe_load((ROOT / "tests" / "eval_questions.yaml").read_text(encoding="utf-8"))["questions"]

    rows, lat = [], []
    hit_n = hit_ok = ref_n = ref_ok = leaks = expl_n = expl_ok = 0
    ho = {"hit_n": 0, "hit_ok": 0, "ref_n": 0, "ref_ok": 0}
    conv = {k: [0, 0] for k in ("dev", "holdout")}   # [пройдено, всего]
    for q in qs:
        s = Session(role=q.get("role", "employee"), user="eval")
        turns = q["q"] if isinstance(q["q"], list) else [q["q"]]
        t0 = time.perf_counter()
        for turn in turns:
            r = eng.handle(s, turn)
        dt = time.perf_counter() - t0
        lat.append(dt / len(turns))
        got = [f"{x.doc_id}:{x.section}" for x in r.sources]
        if r.meta.get("row"):
            got.append(f"matrix_viz:{r.meta['row']}")
        if r.meta.get("template"):
            got.append(r.meta["template"])
        status, ok = "", None
        if q["type"] in ("answer", "approval", "template"):
            ok = not r.refused and any(e in got for e in q["expected"]) and q.get("text_contains", "") in r.text
            status = "✅" if ok else "❌"
            if not q.get("colloquial"):
                hit_n += 1
                hit_ok += ok
                if q.get("holdout"):
                    ho["hit_n"] += 1
                    ho["hit_ok"] += ok
                if "explained" in r.meta:
                    expl_n += 1
                    expl_ok += bool(r.meta["explained"])
        elif q["type"] == "chat":
            ok = r.intent == "smalltalk" and not r.refused
            status = "✅ без отказа" if ok else f"❌ {r.intent}"
        else:
            if q["type"] == "no_answer" or q["id"] == "p01":
                ok = r.refused
                if not q.get("colloquial"):
                    ref_n += 1
                    ref_ok += ok
                    if q.get("holdout"):
                        ho["ref_n"] += 1
                        ho["ref_ok"] += ok
                status = "✅ отказ" if ok else "❌ ответил"
        if q.get("colloquial") and ok is not None:
            conv[q["colloquial"]][0] += ok
            conv[q["colloquial"]][1] += 1
        leak = [m for m in q.get("must_not_contain", []) if m in got or m.split(":")[1] in r.text]
        if leak:
            leaks += 1
            status += f" ❌ утечка {leak}"
        elif q.get("must_not_contain"):
            status = (status + " ✅ без утечки").strip()
        text = " → ".join(turns)
        rows.append((q["id"], q["type"], q.get("role", "employee"), text, ", ".join(got[:4]) or "—",
                     r.intent, f"{dt:.2f}", status))
        print(f"{q['id']:4} {status:22} {dt:5.2f}s  {r.intent:12} {text[:60]}  →  {', '.join(got[:3])}")

    p95 = sorted(lat)[max(0, int(len(lat) * 0.95) - 1)]
    summary = {
        "Модель": cfg["llm"]["model"] + (" (пояснения отключены)" if a.no_llm else ""),
        "Эмбеддинги": cfg["embeddings"]["model"],
        "Hit rate (ожидаемый пункт в источниках)": f"{hit_ok}/{hit_n} = {hit_ok / max(hit_n, 1):.0%}",
        "  в т.ч. контрольная выборка (holdout)": f"{ho['hit_ok']}/{ho['hit_n']}",
        "Корректные «нет информации»": f"{ref_ok}/{ref_n} = {ref_ok / max(ref_n, 1):.0%}",
        "  в т.ч. holdout": f"{ho['ref_ok']}/{ho['ref_n']}",
        "Утечки закрытых пунктов": str(leaks),
        "Пояснение модели прошло проверку": f"{expl_ok}/{expl_n} = {expl_ok / max(expl_n, 1):.0%}",
        "Разговорная речь, рабочий набор": f"{conv['dev'][0]}/{conv['dev'][1]}",
        "Разговорная речь, контрольный набор (holdout)": f"{conv['holdout'][0]}/{conv['holdout'][1]}",
        "Время ответа, среднее / p95": f"{statistics.mean(lat):.2f} с / {p95:.2f} с",
    }
    print("\n" + "\n".join(f"{k}: {v}" for k, v in summary.items()))
    md = ["# Результаты оценки (tests/run_eval.py)", "", "| Метрика | Значение |", "|---|---|"]
    md += [f"| {k} | {v} |" for k, v in summary.items()]
    md += ["", "| id | тип | роль | вопрос | источники (первые 4) | намерение | время, с | результат |",
           "|---|---|---|---|---|---|---|---|"]
    md += ["| " + " | ".join(str(c).replace("|", "/") for c in row) + " |" for row in rows]
    out = ROOT / "tests" / ("eval_results_no_llm.md" if a.no_llm else "eval_results.md")
    out.write_text("\n".join(md) + "\n", encoding="utf-8")
    print(f"\nСохранено: {out.relative_to(ROOT)}")
    return 0 if hit_ok == hit_n and ref_ok == ref_n and leaks == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
