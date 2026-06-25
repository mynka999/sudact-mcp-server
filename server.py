# -*- coding: utf-8 -*-
"""
СудАкт MCP — поиск судебной практики РФ через общедоступный сайт sudact.ru.

Официального API у СудАкта нет, поэтому сервер работает как обычный браузер:
шлёт User-Agent, держит сессионную куку и опрашивает асинхронный поиск сайта
(/<раздел>/doc_ajax/), после чего разбирает HTML выдачи и текст решения.

Инструменты:
  - search_court_practice — поиск решений по тексту / статье / номеру дела
  - get_court_decision    — полный текст конкретного решения

Зависимость: пакет `mcp` (ставит за собой httpx).
"""
from __future__ import annotations

import html
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

mcp = FastMCP("sudact")


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
) -> str:
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
    if page and page > 1:
        p["page"] = str(page)
    return urllib.parse.urlencode(p)


def _search_section(client: httpx.Client, section: str, qs: str, max_wait: float = 20.0):
    """Ставит поисковую задачу и опрашивает её до готовности.

    Возвращает (content_html, total_found) либо (None, None) при таймауте.
    """
    doc_path = f"/{section}/doc/?{qs}"
    # Прайминг: получаем сессионную куку и регистрируем поисковую задачу.
    client.get(doc_path)
    ajax_path = f"/{section}/doc_ajax/?{qs}"
    deadline = time.monotonic() + max_wait
    while time.monotonic() < deadline:
        r = client.get(
            ajax_path,
            headers={
                "X-Requested-With": "XMLHttpRequest",
                "Referer": f"{BASE}{doc_path}",
            },
        )
        try:
            data = r.json()
        except Exception:
            time.sleep(0.8)
            continue
        status = data.get("search_status") or data.get("status")
        if status == "finished" and data.get("content"):
            return data["content"], data.get("total_found")
        time.sleep(0.8)
    return None, None


def _parse_results(content: str, section: str, limit: int) -> list[dict]:
    results: list[dict] = []
    if not content:
        return results
    m = re.search(r'<ul class="results">(.*?)</ul>', content, re.S)
    ul = m.group(1) if m else content
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
    with _client() as client:
        for section in sections:
            qs = _build_query(
                section,
                text=query,
                article=article,
                case_number=case_number,
                date_from=date_from,
                date_to=date_to,
                page=page,
            )
            content, total = _search_section(client, section, qs)
            found = _parse_results(content, section, max_per_section)
            if isinstance(total, str):
                total = _strip_tags(total) or "—"
            per_section[section] = total if total is not None else (len(found) if content else "—")
            all_results.extend(found)

    return {
        "query": query,
        "article": article,
        "case_number": case_number,
        "scope": scope,
        "page": page,
        "total_found_by_section": {SECTIONS[s]: per_section.get(s) for s in sections},
        "returned": len(all_results),
        "results": all_results,
        "note": "Источник: sudact.ru (СудАкт). total_found — общее число совпадений в разделе, "
        "results — текущая страница (по умолчанию 10 на раздел).",
    }


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

    Returns:
        dict со списком найденных решений: название, суд, ссылка, сниппет.
        Для полного текста используйте get_court_decision с url или doc_id.
    """
    return _do_search(
        query, scope, article, case_number, date_from, date_to, page, max_per_section
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


if __name__ == "__main__":
    mcp.run()
