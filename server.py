"""
Syrve MCP Server — Restaurant Surf
Подключает Claude к POS-системе Syrve для анализа продаж.
"""

import hashlib
import json
import os
from datetime import date, datetime, timedelta
from typing import Any

import httpx
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

load_dotenv()

SYRVE_URL = os.getenv("SYRVE_URL", "https://kala-restaurant.syrve.online")
SYRVE_LOGIN = os.getenv("SYRVE_LOGIN", "Viewer")
SYRVE_PASSWORD = os.getenv("SYRVE_PASSWORD", "112233")
ORG_NAME = os.getenv("ORG_NAME", "Kala")
API_KEY = os.getenv("MCP_API_KEY", "")

mcp = FastMCP("surf-syrve")

# ─── Auth ────────────────────────────────────────────────────────────────────

_token: str | None = None
_org_id: str | None = None


def _sha1(text: str) -> str:
    return hashlib.sha1(text.encode()).hexdigest()


def _get_token() -> str:
    global _token
    if _token:
        return _token
    resp = httpx.get(
        f"{SYRVE_URL}/resto/api/auth",
        params={"login": SYRVE_LOGIN, "pass": _sha1(SYRVE_PASSWORD)},
        timeout=15,
    )
    resp.raise_for_status()
    _token = resp.text.strip()
    return _token


def _get_org_id() -> str:
    global _org_id
    if _org_id:
        return _org_id
    token = _get_token()
    resp = httpx.get(
        f"{SYRVE_URL}/resto/api/corporation/organizations",
        params={"key": token},
        timeout=15,
    )
    resp.raise_for_status()
    orgs = resp.json()
    # Ищем по имени или берём первую
    for org in orgs:
        if ORG_NAME.lower() in org.get("name", "").lower():
            _org_id = org["id"]
            return _org_id
    _org_id = orgs[0]["id"]
    return _org_id


def _api_get(path: str, params: dict = None) -> Any:
    """GET-запрос к Syrve API с автоматическим токеном."""
    token = _get_token()
    p = {"key": token}
    if params:
        p.update(params)
    resp = httpx.get(f"{SYRVE_URL}{path}", params=p, timeout=30)
    if resp.status_code == 401:
        global _token
        _token = None
        token = _get_token()
        p["key"] = token
        resp = httpx.get(f"{SYRVE_URL}{path}", params=p, timeout=30)
    resp.raise_for_status()
    try:
        return resp.json()
    except Exception:
        return resp.text


def _sales_report(date_from: str, date_to: str, group_by: list[str] = None) -> dict:
    """Получить отчёт по продажам за период (YYYY-MM-DD)."""
    token = _get_token()
    if group_by is None:
        group_by = ["DishName", "DishCategory"]
    body = {
        "reportType": "SALES",
        "buildSummary": True,
        "groupByRowFields": group_by,
        "aggregateFields": ["DishAmountInt", "DishSumInt"],
        "filters": {
            "OpenDate.Typed": [
                "DateRange",
                {
                    "periodType": "CUSTOM",
                    "from": date_from,
                    "to": date_to,
                },
            ]
        },
    }
    resp = httpx.post(
        f"{SYRVE_URL}/resto/api/v2/reports/olap",
        params={"key": token},
        json=body,
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()


# ─── Tools ───────────────────────────────────────────────────────────────────


@mcp.tool()
def get_revenue(date_from: str = None, date_to: str = None) -> str:
    """
    Выручка ресторана за период.
    date_from, date_to — в формате YYYY-MM-DD. По умолчанию — сегодня.
    Пример: get_revenue("2026-04-01", "2026-04-26")
    """
    today = date.today().isoformat()
    date_from = date_from or today
    date_to = date_to or today

    data = _sales_report(date_from, date_to)
    total = sum(
        row.get("DishSumInt", 0)
        for row in data.get("data", [])
    )
    return json.dumps(
        {"date_from": date_from, "date_to": date_to, "revenue": round(total, 2)},
        ensure_ascii=False,
    )


@mcp.tool()
def get_top_dishes(date_from: str = None, date_to: str = None, limit: int = 10) -> str:
    """
    Топ блюд по выручке за период.
    date_from, date_to — YYYY-MM-DD. По умолчанию — последние 7 дней.
    limit — сколько позиций показать (по умолчанию 10).
    """
    date_to = date_to or date.today().isoformat()
    date_from = date_from or (date.today() - timedelta(days=7)).isoformat()

    data = _sales_report(date_from, date_to)
    rows = data.get("data", [])

    # Агрегируем по блюдам
    dishes: dict[str, dict] = {}
    for row in rows:
        name = row.get("DishName", "Unknown")
        if name not in dishes:
            dishes[name] = {"name": name, "quantity": 0, "revenue": 0}
        dishes[name]["quantity"] += row.get("DishAmountInt", 0)
        dishes[name]["revenue"] += row.get("DishSumInt", 0)

    top = sorted(dishes.values(), key=lambda x: x["revenue"], reverse=True)[:limit]
    for i, d in enumerate(top, 1):
        d["rank"] = i
        d["revenue"] = round(d["revenue"], 2)

    return json.dumps(
        {"date_from": date_from, "date_to": date_to, "top_dishes": top},
        ensure_ascii=False,
        indent=2,
    )


@mcp.tool()
def get_sales_by_category(date_from: str = None, date_to: str = None) -> str:
    """
    Продажи по категориям блюд за период.
    date_from, date_to — YYYY-MM-DD. По умолчанию — последние 7 дней.
    """
    date_to = date_to or date.today().isoformat()
    date_from = date_from or (date.today() - timedelta(days=7)).isoformat()

    data = _sales_report(date_from, date_to)
    rows = data.get("data", [])

    cats: dict[str, dict] = {}
    for row in rows:
        cat = row.get("DishCategory", "Без категории")
        if cat not in cats:
            cats[cat] = {"category": cat, "quantity": 0, "revenue": 0}
        cats[cat]["quantity"] += row.get("DishAmountInt", 0)
        cats[cat]["revenue"] += row.get("DishSumInt", 0)

    result = sorted(cats.values(), key=lambda x: x["revenue"], reverse=True)
    for d in result:
        d["revenue"] = round(d["revenue"], 2)

    return json.dumps(
        {"date_from": date_from, "date_to": date_to, "categories": result},
        ensure_ascii=False,
        indent=2,
    )


@mcp.tool()
def compare_periods(
    period1_from: str,
    period1_to: str,
    period2_from: str,
    period2_to: str,
) -> str:
    """
    Сравнивает выручку двух периодов.
    Например: текущий месяц vs прошлый месяц.
    Все даты в формате YYYY-MM-DD.
    """
    data1 = _sales_report(period1_from, period1_to)
    data2 = _sales_report(period2_from, period2_to)

    rev1 = round(sum(r.get("DishSumInt", 0) for r in data1.get("data", [])), 2)
    rev2 = round(sum(r.get("DishSumInt", 0) for r in data2.get("data", [])), 2)

    diff = round(rev1 - rev2, 2)
    pct = round((diff / rev2 * 100) if rev2 else 0, 1)

    return json.dumps(
        {
            "period1": {"from": period1_from, "to": period1_to, "revenue": rev1},
            "period2": {"from": period2_from, "to": period2_to, "revenue": rev2},
            "difference": diff,
            "change_pct": pct,
        },
        ensure_ascii=False,
        indent=2,
    )


@mcp.tool()
def get_daily_revenue(days: int = 7) -> str:
    """
    Выручка по дням за последние N дней (по умолчанию 7).
    Полезно для просмотра динамики и поиска просадок.
    """
    today = date.today()
    result = []

    for i in range(days - 1, -1, -1):
        d = (today - timedelta(days=i)).isoformat()
        data = _sales_report(d, d)
        revenue = round(
            sum(r.get("DishSumInt", 0) for r in data.get("data", [])), 2
        )
        result.append({"date": d, "revenue": revenue})

    total = round(sum(x["revenue"] for x in result), 2)
    avg = round(total / days, 2)

    return json.dumps(
        {"days": result, "total": total, "avg_per_day": avg},
        ensure_ascii=False,
        indent=2,
    )


@mcp.tool()
def get_organizations() -> str:
    """Список организаций в Syrve (для диагностики и проверки подключения)."""
    data = _api_get("/resto/api/corporation/organizations")
    return json.dumps(data, ensure_ascii=False, indent=2)


@mcp.tool()
def check_connection() -> str:
    """Проверяет подключение к Syrve и возвращает статус авторизации."""
    try:
        token = _get_token()
        org_id = _get_org_id()
        return json.dumps(
            {"status": "ok", "token_prefix": token[:8] + "...", "org_id": org_id},
            ensure_ascii=False,
        )
    except Exception as e:
        return json.dumps({"status": "error", "error": str(e)}, ensure_ascii=False)


if __name__ == "__main__":
    import sys

    # HTTP mode for Cowork (remote), stdio for Claude Code (local)
    if "--http" in sys.argv or os.getenv("MCP_TRANSPORT") == "http":
        port = int(os.getenv("PORT", 8000))
        # Embed API key in path — simple, no middleware needed
        path = f"/mcp/{API_KEY}" if API_KEY else "/mcp"
        mcp.settings.host = "0.0.0.0"
        mcp.settings.port = port
        mcp.settings.streamable_http_path = path
        mcp.run(transport="streamable-http")
    else:
        mcp.run()
