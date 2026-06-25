# -*- coding: utf-8 -*-
"""Демонстрация: поиск по всем судам и поиск по статье закона."""
import server


def show(title, res):
    print("=" * 72)
    print(title)
    print("-" * 72)
    if res.get("error"):
        print("ОШИБКА:", res["error"]); return
    print("Найдено по разделам:")
    for name, n in res["total_found_by_section"].items():
        print(f"   • {name}: {n}")
    print(f"Возвращено результатов: {res['returned']}")
    for r in res["results"]:
        print(f"   [{r['section_name']}] {r['court'][:55]}")
        print(f"      {r['title'][:85]}")
    print()


# 1) Все суды сразу (ВС + арбитраж + СОЮ + мировые)
show(
    'ДЕМО 1 — scope="all": "взыскание неустойки по ДДУ"',
    server._do_search(
        query="взыскание неустойки по ДДУ",
        scope="all", article="", case_number="",
        date_from="", date_to="", page=1, max_per_section=2,
    ),
)

# 2) Поиск по статье закона в арбитраже
show(
    'ДЕМО 2 — поиск по статье: article="395 ГК РФ", scope="arbitral"',
    server._do_search(
        query="", scope="arbitral", article="395 ГК РФ", case_number="",
        date_from="", date_to="", page=1, max_per_section=3,
    ),
)
