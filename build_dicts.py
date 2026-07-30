# -*- coding: utf-8 -*-
"""
build_dicts.py — генератор справочников фильтров sudact.

Вытаскивает из HTML-форм всех разделов sudact значения выпадающих списков
(регионы / судебные округа / инстанции) и сохраняет их в `sudact_dicts.json`
рядом с `server.py`.

ВАЖНО: справочники в разделах РАЗНЫЕ — у СОЮ и мировых судей одни и те же
регионы имеют разные ID (Алтайский край: 1018 в regular, 3002 в magistrate),
а у арбитража вообще не регионы, а 10 судебных округов, и поле называется
`region`, а не `area`. Поэтому справочник хранится по разделам.

Запускать вручную, если sudact поменяет справочники:
    py -X utf8 build_dicts.py
"""
from __future__ import annotations

import json
import pathlib
import re

import httpx

BASE = "https://sudact.ru"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

# Какие select-поля искать в каждом разделе.
TARGETS = {
    "regular": ["area", "workflow_stage"],
    "magistrate": ["area", "workflow_stage"],
    "arbitral": ["region", "workflow_stage"],
    "vsrf": ["area", "region", "workflow_stage"],
}


def extract_select(html_text: str, name: str) -> dict[str, str] | None:
    """Возвращает {название: value} для <select name="...">, либо None."""
    m = re.search(r'<select[^>]*name="%s"[^>]*>' % re.escape(name), html_text)
    if not m:
        return None
    seg = html_text[m.end():html_text.find("</select>", m.end())]
    opts = re.findall(r'<option value="([^"]*)"[^>]*>([^<]*)</option>', seg)
    return {n.strip(): v for v, n in opts if v.strip() and n.strip()}


def main() -> None:
    out: dict[str, dict[str, dict[str, str]]] = {}
    with httpx.Client(headers={"User-Agent": UA}, timeout=30.0,
                      follow_redirects=True) as c:
        for section, fields in TARGETS.items():
            r = c.get(f"{BASE}/{section}/doc/", params={f"{section}-txt": "test"})
            t = r.text
            out[section] = {}
            for f in fields:
                d = extract_select(t, f"{section}-{f}")
                if d:
                    out[section][f] = d
            got = {k: len(v) for k, v in out[section].items()}
            print(f"{section:11} -> {got or 'нет select-фильтров'}")

    dst = pathlib.Path(__file__).with_name("sudact_dicts.json")
    dst.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nсохранено: {dst}")


if __name__ == "__main__":
    main()
