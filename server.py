# -*- coding: utf-8 -*-
"""
СудАкт MCP — поиск судебной практики РФ через общедоступный сайт sudact.ru.

Официального API у СудАкта нет, поэтому сервер работает как обычный браузер:
шлёт User-Agent, держит сессионную куку и опрашивает асинхронный поиск сайта
(/<раздел>/?<раздел>-txt=...), после чего разбирает HTML выдачи и текст решения.

Инструменты:
  - search_court_practice — поиск решений по тексту / статье / номеру дела
  - get_court_decision    — полный текст конкретного решения

Зависимость: пакет `mcp` (ставит за собой httpx).
"""
from __future__ import annotations

import html
import json
import pathlib
import re
import time
import urllib.parse

import httpx
from mcp.server.fastmcp import FastMCP

BASE = "https://sudact.ru"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

# Разделы сайта и их человекочитаемые названия.
SECTIONS = {
    "vsrf": "Верховный суд РФ",
    "arbitral": "Арбитражные суды",
    "regular": "Суды общей юрисдикции",
    "magistrate": "Мировые судьи",
    "law": "Законодательство",
}
# Разделы, относящиеся к судебной практике (для scope="all").
PRACTICE_SECTIONS = ["vsrf", "arbitral", "regular", "magistrate"]

# Какие фильтры поддерживает каждый раздел и как называется параметр на сайте.
# ВАЖНО: набор полей и справочники в разделах РАЗНЫЕ (проверено по формам):
#   regular    — area (85 регионов), court, judge, workflow_stage (инстанция)
#   magistrate — area (73 региона, ДРУГИЕ id!), court, judge; инстанции нет
#   arbitral   — region (10 судебных ОКРУГОВ, не регионов!), court, judge
#   vsrf       — только judge
# Неподдерживаемые фильтры не отправляем и честно сообщаем об этом в ответе.
SECTION_FILTERS: dict[str, dict[str, str]] = {
    "regular": {"area": "area", "court": "court", "judge": "judge",
                "instance": "workflow_stage"},
    "magistrate": {"area": "area", "court": "court", "judge": "judge"},
    "arbitral": {"area": "region", "court": "court", "judge": "judge"},
    "vsrf": {"judge": "judge"},
    "law": {},
}

# Справочники значений (регионы/округа/инстанции), собираются build_dicts.py.
try:
    DICTS: dict[str, dict[str, dict[str, str]]] = json.loads(
        pathlib.Path(__file__).with_name("sudact_dicts.json").read_text(encoding="utf-8")
    )
except Exception:  # без файла поиск по названию недоступен, но числовые id работают
    DICTS = {}

mcp = FastMCP("sudact")


def _resolve_dict_value(section: str, field: str, value: str) -> tuple[str | None, str | None]:
    """Название -> id по справочнику раздела. Возвращает (id, ошибка).

    Принимает и человеческое название («Москва», «апелляция»), и готовый id.
    """
    value = (value or "").strip()
    if not value:
        return None, None
    if value.isdigit():                     # уже id — отдаём как есть
        return value, None
    table = DICTS.get(section, {}).get(field, {})
    if not table:
        return None, (f"Для раздела «{SECTIONS.get(section, section)}» нет справочника "
                      f"«{field}» — укажите числовой id или обновите sudact_dicts.json "
                      f"(py build_dicts.py).")
    low = value.lower()
    exact = [v for n, v in table.items() if n.lower() == low]
    if exact:
        return exact[0], None
    part = {n: v for n, v in table.items() if low in n.lower()}
    if len(part) == 1:
        return next(iter(part.values())), None
    if len(part) > 1:
        return None, (f"Неоднозначное значение «{value}» для «{field}»: подходит "
                      f"{', '.join(sorted(part)[:8])}. Уточните.")
    return None, (f"Не найдено «{value}» в справочнике «{field}» раздела "
                  f"«{SECTIONS.get(section, section)}». Примеры допустимых: "
                  f"{', '.join(sorted(table)[:8])}.")


# --------------------------------------------------------------------------- #
# Вспомогательные функции (без MCP — чтобы их можно было тестировать отдельно)
# --------------------------------------------------------------------------- #
def _client() -> httpx.Client:
    return httpx.Client(
        base_url=BASE,
        headers={"User-Agent": UA, "Accept-Language": "ru,en;q=0.8"},
        timeout=30.0,
        follow_redirects=True,
    )


def _clean_html(s: str) -> str:
    """Удаляет скрипты, стили и рекламные блоки до извлечения текста."""
    s = re.sub(r"(?is)<script.*?</script>", " ", s)
    s = re.sub(r"(?is)<style.*?</style>", " ", s)
    s = re.sub(r'(?is)<div id="adfox[^"]*".*?</div>', " ", s)
    s = re.sub(r"(?is)<!--.*?-->", " ", s)
    return s


def _strip_tags(s: str) -> str:
    s = _clean_html(s)
    s = re.sub(r"(?i)<br\s*/?>", "\n", s)
    s = re.sub(r"(?i)</p>|</div>|</tr>", "\n", s)
    s = re.sub(r"<[^>]+>", "", s)
    s = html.unescape(s)
    s = re.sub(r"[ \t ]+", " ", s)
    s = re.sub(r" *\n *", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def _build_query(
    section: str,
    *,
    text: str = "",
    article: str = "",
    case_number: str = "",
    date_from: str = "",
    date_to: str = "",
    page: int = 1,
    area: str = "",
    court: str = "",
    judge: str = "",
    instance: str = "",
) -> tuple[str, list[str], str | None]:
    """Собирает query string. Возвращает (qs, пропущенные_фильтры, ошибка)."""
    p: dict[str, str] = {}
    if text:
        p[f"{section}-txt"] = text
    if case_number:
        p[f"{section}-case_doc"] = case_number
    if article:
        p[f"{section}-lawchunkinfo"] = article
    if date_from:
        p[f"{section}-date_from"] = date_from
    if date_to:
        p[f"{section}-date_to"] = date_to

    supported = SECTION_FILTERS.get(section, {})
    skipped: list[str] = []
    for logical, value in (("area", area), ("court", court),
                           ("judge", judge), ("instance", instance)):
        if not value:
            continue
        param = supported.get(logical)
        if not param:                       # раздел такого фильтра не имеет
            skipped.append(logical)
            continue
        if logical in ("area", "instance"):  # значения из справочника
            resolved, err = _resolve_dict_value(section, param, value)
            if err:
                return "", skipped, err
            p[f"{section}-{param}"] = resolved or value
        else:                                # court/judge — свободный текст
            p[f"{section}-{param}"] = value

    if page and page > 1:
        p["page"] = str(page)
    return urllib.parse.urlencode(p), skipped, None


def _search_section(client: httpx.Client, section: str, qs: str, max_wait: float = 20.0):
    """Запрашивает страницу поиска раздела и возвращает (html, total_found).

    Историческая справка: до августа 2026 sudact искал асинхронно — прайминг
    /{section}/doc/ и опрос /{section}/doc_ajax/ до search_status="finished".
    С конца августа 2026 doc_ajax отдаёт HTTP 500: сайт перешёл на обычную
    синхронную выдачу, результаты приходят сразу в HTML по /{section}/?{qs}.
    """
    path = f"/{section}/?{qs}"
    try:
        r = client.get(path)
    except Exception:
        return None, None
    if r.status_code != 200:
        return None, None
    text = r.text
    total = None
    m = re.search(r"(?:Найдено|найдено)[^0-9<]{0,40}([0-9][0-9  ]*)", text)
    if m:
        digits = re.sub(r"[^0-9]", "", m.group(1))
        if digits:
            total = int(digits)
    return text, total


def _parse_results(content: str, section: str, limit: int) -> list[dict]:
    results: list[dict] = []
    if not content:
        return results
    # Класс контейнера менялся: "results" -> "results2" (рядом живёт служебный
    # "results d" без карточек). Берём блок, где реально есть карточки (<h4>),
    # иначе разбираем весь HTML — так переживём следующий редизайн.
    ul = content
    for _m in re.finditer(r'<ul class="results[^"]*">(.*?)</ul>', content, re.S):
        if "<h4>" in _m.group(1):
            ul = _m.group(1)
            break
    for li in re.findall(r"<li.*?</li>", ul, re.S):
        a = re.search(r"<h4>.*?<a href=\"([^\"]+)\"[^>]*>(.*?)</a>", li, re.S)
        if not a:
            continue
        href = html.unescape(a.group(1))
        title = _strip_tags(a.group(2))
        id_m = re.search(r"/doc/([^/?]+)", href)
        doc_id = id_m.group(1) if id_m else ""
        court_m = re.search(r'<div class="b-justice">(.*?)</div>', li, re.S)
        court = _strip_tags(court_m.group(1)) if court_m else ""
        # Сниппет = текст элемента без заголовка и названия суда.
        body = re.sub(r"(?s)<h4>.*?</h4>", " ", li)
        body = re.sub(r'(?s)<div class="b-justice">.*?</div>', " ", body)
        body = re.sub(r'(?s)<div class="bookmark.*?</div>\s*</div>', " ", body)
        snippet = _strip_tags(body)
        results.append(
            {
                "doc_id": doc_id,
                "section": section,
                "section_name": SECTIONS.get(section, section),
                "title": title,
                "court": court,
                "url": urllib.parse.urljoin(BASE, href.split("?")[0]),
                "snippet": snippet[:700],
            }
        )
        if len(results) >= limit:
            break
    return results


def _do_search(
    query: str,
    scope: str,
    article: str,
    case_number: str,
    date_from: str,
    date_to: str,
    page: int,
    max_per_section: int,
    area: str = "",
    court: str = "",
    judge: str = "",
    instance: str = "",
) -> dict:
    scope = (scope or "all").strip().lower()
    if scope == "all":
        sections = PRACTICE_SECTIONS
    elif scope in SECTIONS:
        sections = [scope]
    else:
        return {
            "error": f"Неизвестный раздел scope='{scope}'. "
            f"Допустимо: all, {', '.join(SECTIONS)}.",
        }
    if not any([query, article, case_number]):
        return {"error": "Нужен хотя бы один из параметров: query, article или case_number."}

    all_results: list[dict] = []
    per_section: dict[str, str | int | None] = {}
    skipped_by_section: dict[str, list[str]] = {}
    with _client() as client:
        for section in sections:
            qs, skipped, err = _build_query(
                section,
                text=query,
                article=article,
                case_number=case_number,
                date_from=date_from,
                date_to=date_to,
                page=page,
                area=area,
                court=court,
                judge=judge,
                instance=instance,
            )
            if err:
                return {"error": err}
            if skipped:
                skipped_by_section[SECTIONS.get(section, section)] = skipped
            content, total = _search_section(client, section, qs)
            found = _parse_results(content, section, max_per_section)
            if isinstance(total, str):
                total = _strip_tags(total) or "—"
            per_section[section] = total if total is not None else (len(found) if content else "—")
            all_results.extend(found)

    out = {
        "query": query,
        "article": article,
        "case_number": case_number,
        "scope": scope,
        "page": page,
        "filters": {k: v for k, v in
                    (("area", area), ("court", court), ("judge", judge),
                     ("instance", instance)) if v},
        "total_found_by_section": {SECTIONS[s]: per_section.get(s) for s in sections},
        "returned": len(all_results),
        "results": all_results,
        "note": "Источник: sudact.ru (СудАкт). total_found — общее число совпадений в разделе, "
        "results — текущая страница (по умолчанию 10 на раздел).",
    }
    if skipped_by_section:
        out["filters_ignored"] = skipped_by_section
        out["note"] += (" ВНИМАНИЕ: часть фильтров не поддерживается некоторыми разделами "
                        "и была проигнорирована — см. filters_ignored.")
    return out


def _do_get(url_or_id: str, section: str) -> dict:
    url_or_id = (url_or_id or "").strip()
    if url_or_id.startswith("http"):
        path = urllib.parse.urlparse(url_or_id).path
        parts = [p for p in path.split("/") if p]
        if len(parts) >= 3 and parts[1] == "doc":
            section = parts[0]
            doc_id = parts[2]
        else:
            return {"error": f"Не удалось разобрать URL: {url_or_id}"}
    else:
        doc_id = url_or_id
    section = section if section in SECTIONS else "regular"
    doc_path = f"/{section}/doc/{doc_id}/"

    with _client() as client:
        r = client.get(doc_path)
    if r.status_code != 200:
        return {"error": f"HTTP {r.status_code} при загрузке {doc_path}"}
    t = r.text

    title_m = re.search(r"<title>(.*?)</title>", t, re.S)
    title = html.unescape(title_m.group(1).strip()) if title_m else ""
    title = re.sub(r"\s*::.*$", "", title)  # убрать "... :: СудАкт.ру"
    court_m = re.search(r'<div class="b-justice">(.*?)</div>', t, re.S)
    court = _strip_tags(court_m.group(1)) if court_m else ""

    # Текст решения начинается после первого <hr class="hr-h1"> и идёт до подвала.
    hr = re.search(r'<hr class="hr-h1">', t)
    body = t[hr.end():] if hr else t
    for marker in ('<div class="go_top"', '<div class="h-footer"', 'class="counter-block"'):
        idx = body.find(marker)
        if idx != -1:
            body = body[:idx]
    text = _strip_tags(body)

    return {
        "doc_id": doc_id,
        "section": section,
        "section_name": SECTIONS.get(section, section),
        "title": title,
        "court": court,
        "url": urllib.parse.urljoin(BASE, doc_path),
        "char_count": len(text),
        "text": text,
    }


# --------------------------------------------------------------------------- #
# MCP-инструменты
# --------------------------------------------------------------------------- #
@mcp.tool()
def search_court_practice(
    query: str = "",
    scope: str = "all",
    article: str = "",
    case_number: str = "",
    date_from: str = "",
    date_to: str = "",
    page: int = 1,
    max_per_section: int = 10,
    area: str = "",
    court: str = "",
    judge: str = "",
    instance: str = "",
) -> dict:
    """Поиск судебной практики РФ на sudact.ru.

    Args:
        query: поисковый текст, напр. "снижение неустойки 333 ГК".
        scope: "all" — вся практика (ВС, арбитраж, СОЮ, мировые), либо один
            раздел: vsrf | arbitral | regular | magistrate | law.
        article: статья закона, напр. "333 ГК РФ".
        case_number: номер дела.
        date_from: дата с (ДД.ММ.ГГГГ).
        date_to: дата по (ДД.ММ.ГГГГ).
        page: страница выдачи (по 10 результатов на раздел).
        max_per_section: сколько результатов вернуть из каждого раздела.
        area: регион по-русски («Москва», «Алтайский край») или его id. Для
            арбитража — судебный ОКРУГ («Волго-Вятский»). У ВС РФ не работает.
        court: НАЗВАНИЕ СУДА, нужно ТОЧНОЕ, со скобками региона, напр.
            "Стерлитамакский городской суд (Республика Башкортостан)".
            Частичное название не сработает — сперва вызовите find_court_name.
        judge: фамилия судьи (достаточно фамилии), напр. "Акбашева".
        instance: инстанция — «первая», «апелляция», «кассация», «пересмотр»,
            «надзор» или id. Поддерживается только разделом СОЮ (regular).

    Returns:
        dict со списком найденных решений: название, суд, ссылка, сниппет.
        Для полного текста используйте get_court_decision с url или doc_id.
        Если фильтр не поддержан разделом — он будет проигнорирован, а в ответе
        появится ключ filters_ignored.

    Полезно: фильтр area заметно расширяет доступную глубину выдачи (sudact
    отдаёт максимум ~500 результатов на один запрос), поэтому для сбора больших
    корпусов лучше идти по регионам — см. stat_corpus.py --mode by-region.
    """
    return _do_search(
        query, scope, article, case_number, date_from, date_to, page,
        max_per_section, area=area, court=court, judge=judge, instance=instance,
    )


@mcp.tool()
def get_court_decision(url_or_id: str, section: str = "regular") -> dict:
    """Получить полный текст судебного решения с sudact.ru.

    Args:
        url_or_id: полный URL решения (https://sudact.ru/regular/doc/XXXX/)
            либо только его ID. При полном URL раздел определяется автоматически.
        section: раздел (regular|arbitral|vsrf|magistrate|law), если передан
            только ID.

    Returns:
        dict с заголовком, судом, полным текстом решения и ссылкой.
    """
    return _do_get(url_or_id, section)


def _do_find_court(name: str, scope: str, area: str) -> dict:
    """Подбирает ТОЧНЫЕ названия судов по частичному.

    У sudact нет ни автокомплита, ни индекса судов, а фильтр `court` требует
    точного названия со скобками. Поэтому ищем решения, в тексте которых
    упоминается искомое название, и собираем канонические названия судов из
    самой выдачи (поле b-justice), отсекая суффикс категории (« - Уголовное»).
    """
    name = (name or "").strip()
    if not name:
        return {"error": "Укажите хотя бы часть названия суда."}
    scope = (scope or "regular").strip().lower()
    sections = PRACTICE_SECTIONS if scope == "all" else [scope]
    if scope != "all" and scope not in SECTIONS:
        return {"error": f"Неизвестный раздел scope='{scope}'."}

    found: dict[str, dict] = {}
    with _client() as client:
        for section in sections:
            qs, _sk, err = _build_query(section, text=name, area=area, page=1)
            if err:
                return {"error": err}
            content, _total = _search_section(client, section, qs)
            for r in _parse_results(content, section, 30):
                raw = (r.get("court") or "").strip()
                if not raw:
                    continue
                # «Ленинский районный суд (Пермский край) - Уголовное» -> без хвоста
                canon = re.split(r"\s+[-–]\s+(?=[А-ЯЁ])", raw)[0].strip()
                if not canon:
                    continue
                item = found.setdefault(
                    canon, {"court_name": canon, "section": section,
                            "section_name": SECTIONS.get(section, section), "seen": 0})
                item["seen"] += 1

    matches = sorted(found.values(), key=lambda x: -x["seen"])
    exact_hint = [m for m in matches if name.lower() in m["court_name"].lower()]
    return {
        "query": name,
        "candidates": exact_hint or matches,
        "note": ("Передайте court_name в search_court_practice(court=...) БЕЗ изменений — "
                 "фильтр требует точного совпадения. Список собран из выдачи sudact, "
                 "поэтому он не является полным справочником судов."),
    }


@mcp.tool()
def find_court_name(name: str, scope: str = "regular", area: str = "") -> dict:
    """Подобрать ТОЧНОЕ название суда для фильтра `court`.

    Фильтр court в search_court_practice требует точного названия со скобками
    региона; частичное («Стерлитамакский») не работает. Этот инструмент
    подбирает канонические названия по части имени.

    Args:
        name: часть названия суда, напр. "Стерлитамакский городской".
        scope: раздел поиска (regular|arbitral|magistrate|vsrf|all).
        area: опционально сузить регионом («Республика Башкортостан»).

    Returns:
        dict со списком кандидатов; поле court_name подставляйте в
        search_court_practice(court=...) без изменений.
    """
    return _do_find_court(name, scope, area)


if __name__ == "__main__":
    mcp.run()
