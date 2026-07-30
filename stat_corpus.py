# -*- coding: utf-8 -*-
"""
stat_corpus.py — честный сборщик выборки судебных актов поверх sudact MCP.

Backend для скилла «доказательная статистика»: повторяет метод из статьи
А.Зазулина (sudact + парсер + LLM), но исправляет его методологические дыры:

  1. ЧЕСТНАЯ ВЫБОРКА вместо «первых 100». sudact отдаёт максимум 50 страниц
     (≈500 результатов) на запрос — глубже пагинация зацикливается. Поэтому:
       • mode=random      — случайная выборка из достижимых ≤500 (с дисклеймером,
                            что это лишь верхушка корпуса по сортировке сайта);
       • mode=stratified  — нарезка периода на окна по датам (в каждом <500 →
                            достижимо целиком), пропорциональная выборка по окнам;
                            покрывает ВЕСЬ корпус, а не только верхушку.
  2. ПРОЗРАЧНОСТЬ. В отчёте: total_found, достижимый пул, размер выборки, метод,
     seed (воспроизводимость), и явные смещения (публикация, капа, обезличка).

Считает только сбор данных (HTTP), без LLM. Кодирование/агрегацию делает скилл.

Пример:
  py stat_corpus.py --query "158 Кража Смартфон" --scope regular \
     --mode stratified --windows 2024,2025,2026 --n 60 --with-text \
     --out corpus.jsonl --seed 42
"""
from __future__ import annotations

import argparse
import json
import random
import re
import sys

sys.path.insert(0, r"C:\Users\USER\Documents\Veritas\Code\sudact-mcp")
import server  # noqa: E402

MAX_PAGES = 50          # практический предел пагинации sudact (проверено: page>50 зацикливается)
PER_PAGE = 10


def parse_total(total_str) -> int | None:
    """'Найдено 9 604 документа' -> 9604; 'более 100 000' -> 100000; иначе None."""
    if not isinstance(total_str, str):
        return total_str if isinstance(total_str, int) else None
    digits = re.sub(r"[^\d]", "", total_str)
    return int(digits) if digits else None


def gather_reachable(query, scope, article, date_from, date_to, max_pages=MAX_PAGES,
                     area="", instance=""):
    """Собирает все достижимые результаты (≤ max_pages*10), отсекая зацикливание."""
    seen, pool, first1 = set(), [], None
    total = None
    empty_streak = stale_streak = 0
    for page in range(1, max_pages + 1):
        res = server._do_search(
            query=query, scope=scope, article=article, case_number="",
            date_from=date_from, date_to=date_to, page=page, max_per_section=PER_PAGE,
            area=area, instance=instance,
        )
        if total is None:
            sec = next(iter(res.get("total_found_by_section", {}).values()), None)
            total = parse_total(sec)
        rs = res.get("results", [])
        if not rs:
            # ВАЖНО: «меньше 10 на странице» НЕ означает конец выдачи — sudact
            # регулярно отдаёт неполную страницу (в т.ч. первую), а дальше снова
            # полные. Останавливаемся только на двух пустых подряд.
            empty_streak += 1
            if empty_streak >= 2:
                break
            continue
        empty_streak = 0
        if page == 1:
            first1 = rs[0]["doc_id"]
        elif first1 and rs[0]["doc_id"] == first1:  # выдача зациклилась — предел
            break
        new = [r for r in rs if r["doc_id"] not in seen]
        if new:
            stale_streak = 0
            for r in new:
                seen.add(r["doc_id"])
                pool.append(r)
        else:
            stale_streak += 1
            if stale_streak >= 2:
                break
        if total and len(pool) >= total:     # собрали всё, что обещал сайт
            break
    return pool, total


CAP = MAX_PAGES * PER_PAGE          # практический предел выдачи sudact (~500)


def regions_for(scope: str) -> list[str]:
    """Названия регионов (для арбитража — судебных округов) из справочника."""
    d = server.DICTS.get(scope, {})
    table = d.get("area") or d.get("region") or {}
    return sorted(table)


def collect_partitioned(query, scope, article, n, seed, cascade, years, log=print,
                        max_regions=0):
    """Партиционирование по регионам (+ каскад инстанция × год для упёршихся).

    Зачем: sudact отдаёт максимум ~500 результатов на ОДИН запрос, поэтому «весь
    корпус» недостижим напрямую. Но фильтр по региону режет корпус на 85 частей,
    каждая со своим лимитом — и на узких запросах становится достижим целиком.
    Если регион всё равно упирается в лимит, дробим его по инстанциям, а затем
    по годам. Всё, что осталось за лимитом, честно попадает в отчёт (capped).
    """
    rng = random.Random(seed)
    parts: list[dict] = []          # {label, pool, total, capped}
    regs = regions_for(scope)
    if not regs:
        return [], {"error": f"Для раздела «{scope}» нет справочника регионов "
                             f"(обновите sudact_dicts.json: py build_dicts.py)."}
    regions_total = len(regs)
    if max_regions and max_regions < len(regs):
        regs = regs[:max_regions]

    def grab(label, **flt):
        pool, total = gather_reachable(query, scope, article, flt.pop("date_from", ""),
                                       flt.pop("date_to", ""), **flt)
        capped = bool(total and total > len(pool) and len(pool) >= CAP - PER_PAGE)
        return {"label": label, "pool": pool, "total": total or 0, "capped": capped}

    for i, reg in enumerate(regs, 1):
        p = grab(reg, area=reg)
        log(f"  [{i}/{len(regs)}] {reg}: найдено {p['total']}, достижимо {len(p['pool'])}"
            + (" — УПЁРЛОСЬ В ЛИМИТ" if p["capped"] else ""))
        if p["capped"] and cascade:
            # дробим регион по инстанциям, при необходимости — ещё и по годам
            for inst in ("первая", "апелляция", "кассация"):
                q = grab(f"{reg} / {inst}", area=reg, instance=inst)
                if q["capped"] and years:
                    for y in years:
                        parts.append(grab(f"{reg} / {inst} / {y}", area=reg, instance=inst,
                                          date_from=f"01.01.{y}", date_to=f"31.12.{y}"))
                else:
                    parts.append(q)
        else:
            parts.append(p)

    # дедуп между партициями (одно дело может попасть в несколько окон)
    seen: set[str] = set()
    for p in parts:
        uniq = []
        for r in p["pool"]:
            if r["doc_id"] not in seen:
                seen.add(r["doc_id"])
                uniq.append(r)
        p["pool"] = uniq

    # пропорциональная аллокация выборки по объёму партиции
    grand = sum(p["total"] for p in parts) or 1
    selected: list[dict] = []
    for p in parts:
        if not p["pool"]:
            continue
        quota = min(len(p["pool"]), max(1, round(n * p["total"] / grand)))
        selected.extend(rng.sample(p["pool"], quota))
    if len(selected) > n:
        selected = rng.sample(selected, n)

    report = {
        "mode": "by-region", "scope": scope, "seed": seed,
        "partitions": len(parts),
        "regions_scanned": len(regs),
        "regions_available": regions_total,
        "regions_skipped_by_limit": max(0, regions_total - len(regs)),
        "total_found_sum": sum(p["total"] for p in parts),
        "reachable_sum": len(seen),
        "capped_partitions": [p["label"] for p in parts if p["capped"]],
        "cascade": bool(cascade),
    }
    return selected, report


# Резолютивные глаголы (в т.ч. в «разрядку»: «П Р И Г О В О Р И Л»).
OPERATIVE_KW = ["ПРИГОВОРИЛ", "ПОСТАНОВИЛ", "ОПРЕДЕЛИЛ", "РЕШИЛ"]
# Подвал страницы sudact — обрезаем как мусор.
CHROME_MARKERS = [
    "\nПоследние документы по делу:", "\nСудебная практика по:",
    "\nПоказать все документы по этому делу", "\nПечать документа",
]


def _spaced_re(word: str) -> re.Pattern:
    """Регэксп слова, допускающий пробелы между буквами (для текста «в разрядку»)."""
    return re.compile(r"\s*".join(map(re.escape, word)), re.IGNORECASE)


def _strip_site_chrome(text: str) -> str:
    """Срезает хвостовой служебный блок сайта (навигация, «практика по», печать)."""
    cut = len(text)
    for m in CHROME_MARKERS:
        i = text.find(m)
        if i != -1:
            cut = min(cut, i)
    # Подпись суда «\nСуд:<...> (подробнее)» в самом низу — тоже подвал.
    m = re.search(r"\nСуд:[^\n]+\(подробнее\)", text)
    if m:
        cut = min(cut, m.start())
    return text[:cut].rstrip()


def trim_operative(text: str, head: int = 1100, op_window: int = 5000) -> str:
    """Голова (фабула/установил) + резолютивная часть от последнего «ПРИГОВОРИЛ».

    Гарантирует попадание назначенного наказания: режем не «хвост от конца», а
    блок начиная с последнего резолютивного глагола — наказание идёт сразу за ним.
    """
    text = _strip_site_chrome(text or "")
    if not text:
        return ""
    if len(text) <= head + op_window:
        return text
    # последний резолютивный глагол по всему тексту
    last = -1
    for kw in OPERATIVE_KW:
        for m in _spaced_re(kw).finditer(text):
            last = max(last, m.start())
    if last == -1:  # резолютив не нашёлся — подстраховка прежней логикой
        return text[:head] + "\n[...]\n" + text[-op_window:]
    op = text[last:last + op_window]
    if last <= head:                       # короткий документ — резолютив уже в голове
        return text[:head + op_window]
    return text[:head] + "\n[...]\n" + op


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--query", default="")
    ap.add_argument("--article", default="")
    ap.add_argument("--scope", default="regular")
    ap.add_argument("--mode", choices=["random", "stratified", "by-region"], default="random")
    ap.add_argument("--cascade", action="store_true",
                    help="by-region: упёршиеся регионы дробить по инстанциям и годам")
    ap.add_argument("--cascade-years", default="",
                    help="годы для каскада, напр. '2023,2024,2025'")
    ap.add_argument("--max-regions", type=int, default=0,
                    help="by-region: взять только первые N регионов (для быстрых пилотов; "
                         "0 = все). Ограничение попадает в отчёт.")
    ap.add_argument("--windows", default="",
                    help="для stratified: годы '2024,2025,2026' или диапазоны "
                         "'2024-01-01:2024-06-30,2024-07-01:2024-12-31'")
    ap.add_argument("--date-from", default="")
    ap.add_argument("--date-to", default="")
    ap.add_argument("--n", type=int, default=30, help="размер итоговой выборки")
    ap.add_argument("--with-text", action="store_true", help="тянуть полные тексты")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out", default="corpus.jsonl")
    args = ap.parse_args()

    rng = random.Random(args.seed)

    def to_window(w):
        w = w.strip()
        if ":" in w:
            a, b = w.split(":", 1)
            return a.strip(), b.strip(), w
        if re.fullmatch(r"\d{4}", w):
            return f"01.01.{w}", f"31.12.{w}", w
        return "", "", w

    report = {
        "query": args.query, "article": args.article, "scope": args.scope,
        "mode": args.mode, "seed": args.seed, "requested_n": args.n,
        "windows": [], "total_found_overall": None, "reachable_overall": 0,
    }
    selected = []

    if args.mode == "by-region":
        years = [y.strip() for y in args.cascade_years.split(",") if y.strip()]
        selected, rep = collect_partitioned(
            args.query, args.scope, args.article, args.n, args.seed,
            args.cascade, years, log=lambda m: print(m, file=sys.stderr),
            max_regions=args.max_regions)
        if isinstance(rep, dict) and rep.get("error"):
            print(json.dumps(rep, ensure_ascii=False, indent=2))
            return
        report.update(rep)
        report["total_found_overall"] = rep.get("total_found_sum")
        report["reachable_overall"] = rep.get("reachable_sum")
    elif args.mode == "random":
        pool, total = gather_reachable(
            args.query, args.scope, args.article, args.date_from, args.date_to)
        report["total_found_overall"] = total
        report["reachable_overall"] = len(pool)
        k = min(args.n, len(pool))
        selected = rng.sample(pool, k) if pool else []
        report["windows"].append(
            {"window": "all", "total_found": total, "reachable": len(pool), "picked": k})
    else:
        wins = [to_window(w) for w in args.windows.split(",") if w.strip()]
        # 0) глобальный total без дат — чтобы ловить окна, где фильтр sudact
        #    отвалился (известный глюк: оба предела внутри некоторых лет → сайт
        #    игнорирует фильтр и возвращает весь корпус).
        _, global_total = gather_reachable(args.query, args.scope, args.article, "", "", max_pages=1)
        report["global_total"] = global_total
        report["filter_failed_windows"] = []
        # 1) собрать достижимый пул и total по каждому окну
        per = []
        for df, dt, label in wins:
            pool, total = gather_reachable(args.query, args.scope, args.article, df, dt)
            failed = bool(global_total and total and total >= global_total)
            if failed:
                report["filter_failed_windows"].append(label)
                per.append({"label": label, "df": df, "dt": dt, "pool": [],
                            "total": 0, "reachable": 0, "failed": True})
            else:
                per.append({"label": label, "df": df, "dt": dt, "pool": pool,
                            "total": total or 0, "reachable": len(pool), "failed": False})
        grand_total = sum(p["total"] for p in per) or 1
        report["total_found_overall"] = sum(p["total"] for p in per)
        report["reachable_overall"] = sum(p["reachable"] for p in per)
        # 2) пропорциональная аллокация выборки по total окна
        for p in per:
            quota = max(1, round(args.n * p["total"] / grand_total)) if p["pool"] else 0
            quota = min(quota, len(p["pool"]))
            pick = rng.sample(p["pool"], quota) if quota else []
            selected.extend(pick)
            report["windows"].append({
                "window": p["label"], "total_found": p["total"],
                "reachable": p["reachable"], "picked": len(pick),
                "filter_failed": p.get("failed", False)})

    # тянем тексты при необходимости
    rows = []
    for i, r in enumerate(selected, 1):
        row = {k: r.get(k) for k in ("doc_id", "section", "court", "title", "url")}
        if args.with_text:
            doc = server._do_get(r["url"], r["section"])
            full = doc.get("text", "")
            row["char_count"] = len(full)
            row["text_excerpt"] = trim_operative(full)
        rows.append(row)

    with open(args.out, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    report["collected"] = len(rows)
    report["out"] = args.out
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
