# -*- coding: utf-8 -*-
"""Локальная проверка логики сервера без запуска MCP-транспорта."""
import json
import sys

import server

print("=" * 70)
print("ТЕСТ 1: поиск практики, scope=regular (СОЮ), 'снижение неустойки'")
res = server._do_search(
    query="снижение неустойки",
    scope="regular",
    article="",
    case_number="",
    date_from="",
    date_to="",
    page=1,
    max_per_section=3,
)
print("total_found_by_section:", res.get("total_found_by_section"))
print("returned:", res.get("returned"))
for r in res.get("results", [])[:3]:
    print("  -", r["doc_id"], "|", r["court"][:60])
    print("    ", r["title"][:90])
    print("     snippet:", r["snippet"][:120].replace("\n", " "))

if not res.get("results"):
    print("!!! НЕТ РЕЗУЛЬТАТОВ — поиск не сработал")
    sys.exit(1)

print()
print("=" * 70)
print("ТЕСТ 2: полный текст первого решения")
first = res["results"][0]
doc = server._do_get(first["url"], first["section"])
if doc.get("error"):
    print("ОШИБКА:", doc["error"])
    sys.exit(1)
print("title:", doc["title"][:90])
print("court:", doc["court"][:70])
print("char_count:", doc["char_count"])
print("--- начало текста ---")
print(doc["text"][:500])
print("--- конец фрагмента ---")
print()
print("OK: оба инструмента работают.")
