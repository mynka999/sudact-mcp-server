# -*- coding: utf-8 -*-
"""
find_candidates.py — сбор кандидатов для АДРЕСНОГО ОТЛОВА редких решений.

Backend для режима «адресный отлов» скилла sudact-stats: прогоняет НЕСКОЛЬКО
поисковых запросов (углов) по одному/нескольким разделам sudact, объединяет и
дедуплицирует результаты в единый список кандидатов. Дальше каждого кандидата
проверяет LLM (отдельный шаг скилла — обычно Workflow на Sonnet).

Зачем несколько углов: поиск sudact буквальный, и редкий исход (напр. оправдание)
ловится разными формулировками резолютива. Кандидат, найденный НЕСКОЛЬКИМИ
углами, — более сильный (поле hit_count).

Пример (оправдания по ст.160):
  py find_candidates.py \
     --queries "оправдательный приговор оправдать||оправдать отсутствие состава 302 присвоение" \
     --article "160 УК РФ" --scopes regular,vsrf --per 12 --out candidates.jsonl
"""
from __future__ import annotations

import argparse
import json
import sys

sys.path.insert(0, r"C:\Users\USER\Documents\Veritas\Code\sudact-mcp")
import server  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--queries", default="", help="поисковые углы через ||")
    ap.add_argument("--article", default="")
    ap.add_argument("--case-number", default="")
    ap.add_argument("--scopes", default="regular", help="разделы через запятую: regular,vsrf,magistrate,arbitral")
    ap.add_argument("--date-from", default="")
    ap.add_argument("--date-to", default="")
    ap.add_argument("--per", type=int, default=12, help="результатов на угол на раздел")
    ap.add_argument("--out", default="candidates.jsonl")
    args = ap.parse_args()

    queries = [q.strip() for q in args.queries.split("||") if q.strip()] or [""]
    scopes = [s.strip() for s in args.scopes.split(",") if s.strip()]

    cand: dict[str, dict] = {}
    per_angle = []
    for scope in scopes:
        for q in queries:
            res = server._do_search(
                query=q, scope=scope, article=args.article, case_number=args.case_number,
                date_from=args.date_from, date_to=args.date_to, page=1, max_per_section=args.per,
            )
            rows = res.get("results", [])
            per_angle.append({"scope": scope, "query": q, "returned": len(rows)})
            for r in rows:
                did = r["doc_id"]
                if did not in cand:
                    cand[did] = {
                        "doc_id": did, "section": r["section"], "court": r.get("court", ""),
                        "title": r.get("title", ""), "url": r["url"], "hit_count": 0, "hit_angles": [],
                    }
                cand[did]["hit_count"] += 1
                cand[did]["hit_angles"].append(f"{scope}:{q[:30]}")

    # сильные кандидаты вперёд (найдены несколькими углами)
    out = sorted(cand.values(), key=lambda c: -c["hit_count"])
    with open(args.out, "w", encoding="utf-8") as f:
        for i, c in enumerate(out, 1):
            c["i"] = i
            f.write(json.dumps(c, ensure_ascii=False) + "\n")

    print(json.dumps({
        "queries": queries, "article": args.article, "scopes": scopes,
        "per_angle": per_angle, "unique_candidates": len(out), "out": args.out,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
