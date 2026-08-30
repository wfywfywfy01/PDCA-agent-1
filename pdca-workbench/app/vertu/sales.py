# -*- coding: utf-8 -*-
"""通过 vertu-cli 2.x 的业务快捷命令读取销售数据。"""
from __future__ import annotations

import asyncio
import os
import time
from datetime import date as _date, datetime, timedelta

from app.vertu.client import run_vertu_json, run_vertu_sync_json


# Sell-in is requested on every workbench visit. Keep a short, explicitly
# timestamped cache so repeated visits do not start three identical CLI
# processes. Failed refreshes never overwrite the last successful fact.
_SELL_IN_CACHE: dict[tuple, dict] = {}
_SELL_IN_LOCKS: dict[tuple, asyncio.Lock] = {}


def _sell_in_cache_seconds() -> float:
    try:
        return max(float(os.environ.get("PDCA_SELL_IN_CACHE_SECONDS", "60")), 0.0)
    except ValueError:
        return 60.0


def _date_range(date_text: str, period: str) -> tuple[str, str]:
    today = _date.fromisoformat(date_text) if date_text else _date.today()
    if period in ("day", "today"):
        return str(today), str(today)
    if period == "week":
        return str(today - timedelta(days=today.weekday())), str(today)
    if period == "month":
        return str(today.replace(day=1)), str(today)
    if period == "quarter":
        quarter_month = ((today.month - 1) // 3) * 3 + 1
        return str(today.replace(month=quarter_month, day=1)), str(today)
    return str(today), str(today)


def _trend_months(month: str) -> list[dict[str, str]]:
    year, number = int(month[:4]), int(month[5:7])
    result: list[dict[str, str]] = []
    for offset in range(5, -1, -1):
        current = number - offset
        current_year = year
        while current <= 0:
            current += 12
            current_year -= 1
        next_month = current + 1 if current < 12 else 1
        next_year = current_year if current < 12 else current_year + 1
        result.append(
            {
                "label": f"{current_year:04d}-{current:02d}",
                "start": f"{current_year:04d}-{current:02d}-01",
                "end": str(_date(next_year, next_month, 1) - timedelta(days=1)),
            }
        )
    return result


_PERIOD_LABEL = {
    "day": "今日",
    "today": "今日",
    "week": "本周",
    "month": "本月",
    "quarter": "本季度",
}


async def _headline(start: str, end: str, department: str = "") -> dict:
    args = [
        "sales",
        "+headline-kpi",
        "--start-date",
        start,
        "--end-date",
        end,
    ]
    dept_l1 = os.environ.get("PDCA_VERTU_DEPT_L1", "海外渠道").strip()
    if dept_l1:
        args += ["--dept-l1", dept_l1]
    if department:
        args += ["--dept-l2", department]
    payload = await run_vertu_json(args, timeout=45.0)
    if not isinstance(payload, dict):
        raise RuntimeError("vertu-cli sales +headline-kpi 未返回 JSON")
    return payload


async def fetch_sell_in(date_text: str, period: str = "day") -> dict:
    """Sell-in KPI：按配置的经销商部门聚合 vertu-cli 销售口径。"""
    start, end = _date_range(date_text, period)
    configured = os.environ.get(
        "PDCA_VERTU_SELLIN_DEPARTMENTS",
        "经销商一部,经销商二部,经销商三部",
    )
    departments = [item.strip() for item in configured.split(",") if item.strip()]
    dept_l1 = os.environ.get("PDCA_VERTU_DEPT_L1", "海外渠道").strip()
    cache_key = (start, end, period, dept_l1, tuple(departments))
    ttl = _sell_in_cache_seconds()

    def cached_value(now: float) -> dict | None:
        entry = _SELL_IN_CACHE.get(cache_key)
        if entry and now - float(entry["monotonic"]) < ttl:
            return {**entry["data"], "cached": True}
        return None

    cached = cached_value(time.monotonic())
    if cached is not None:
        return cached

    # Single-flight identical requests while still allowing the independent
    # department calls to run concurrently.
    lock = _SELL_IN_LOCKS.setdefault(cache_key, asyncio.Lock())
    async with lock:
        cached = cached_value(time.monotonic())
        if cached is not None:
            return cached

        if departments:
            payloads = await asyncio.gather(
                *(_headline(start, end, department) for department in departments)
            )
        else:
            payloads = [await _headline(start, end)]

        amount = sum(float((item.get("period") or {}).get("销额") or 0) for item in payloads)
        quantity = sum(int((item.get("period") or {}).get("销量") or 0) for item in payloads)
        label = _PERIOD_LABEL.get(period, "当前区间")
        fetched_at = datetime.now().astimezone()
        result = {
            "amount": amount,
            "wan": round(amount / 10000, 2),
            "quantity": quantity,
            "note": f"{label}实时 · vertu-cli sales · {fetched_at:%H:%M:%S}更新",
            "as_of": fetched_at.isoformat(timespec="seconds"),
            "cached": False,
        }
        _SELL_IN_CACHE[cache_key] = {
            "monotonic": time.monotonic(),
            "data": result,
        }
        return dict(result)


async def fetch_sell_out(date_text: str, period: str = "day") -> dict:
    """vertu-cli 2.x 暂无代理商终端 Sell-out 快捷命令，交由本地实报数据兜底。"""
    del date_text, period
    raise RuntimeError("vertu-cli 暂未提供 dealer sell-out 数据源")


def _row_dict(row, columns: list[str]) -> dict:
    if isinstance(row, dict):
        return row
    if isinstance(row, list):
        return {columns[index]: value for index, value in enumerate(row) if index < len(columns)}
    return {}


def fetch_dealer_sales_orders_sync(start: str, end: str) -> dict:
    """通过 vertu-cli 订单快捷命令按客户聚合经销商销售数据。"""
    payload = run_vertu_sync_json(
        [
            "sales",
            "+orders",
            "--start-date",
            start,
            "--end-date",
            end,
            "--dept-l1",
            os.environ.get("PDCA_VERTU_DEPT_L1", "海外渠道"),
            "--limit",
            "5000",
        ],
        timeout=90.0,
    )
    if not isinstance(payload, dict):
        raise RuntimeError("vertu-cli sales +orders 未返回数据")
    columns = [str(item) for item in payload.get("columns") or []]
    grouped: dict[str, dict] = {}
    for raw in payload.get("rows") or []:
        row = _row_dict(raw, columns)
        name = str(row.get("客户名称") or row.get("客户") or "").strip()
        if not name:
            continue
        item = grouped.setdefault(name, {"dealer_name": name, "sell_out_yuan": 0.0, "qty": 0})
        item["sell_out_yuan"] += float(row.get("金额") or 0)
        item["qty"] += int(float(row.get("数量") or 0))
    dealers = sorted(grouped.values(), key=lambda item: -item["sell_out_yuan"])
    return {
        "ok": True,
        "start_date": start,
        "end_date": end,
        "month": end[:7],
        "total": round(sum(float(item["sell_out_yuan"]) for item in dealers), 2),
        "dealers": dealers,
        "source": "vertu-cli sales +orders",
        "source_metric": "dealer_sales",
    }


async def _orders(start: str, end: str) -> dict:
    payload = await run_vertu_json(
        [
            "sales",
            "+orders",
            "--start-date",
            start,
            "--end-date",
            end,
            "--limit",
            "5000",
        ],
        timeout=60.0,
    )
    if not isinstance(payload, dict):
        raise RuntimeError("vertu-cli sales +orders 未返回 JSON")
    return payload


async def fetch_sellin_summary(month: str | None = None) -> dict:
    """按客户汇总当月 Sell-in，并给出最近六个月趋势。"""
    month = month or _date.today().strftime("%Y-%m")
    months = _trend_months(month)
    current = await _orders(months[-1]["start"], months[-1]["end"])
    columns = [str(item) for item in current.get("columns") or []]
    grouped: dict[str, dict] = {}
    for raw in current.get("rows") or []:
        row = _row_dict(raw, columns)
        name = str(row.get("客户名称") or row.get("客户") or "").strip()
        if not name:
            continue
        item = grouped.setdefault(name, {"name": name, "amount": 0.0, "quantity": 0})
        item["amount"] += float(row.get("金额") or 0)
        item["quantity"] += int(float(row.get("数量") or 0))
    ordered = sorted(grouped.values(), key=lambda item: -item["amount"])
    dealers = [
        {
            "rank": index + 1,
            "name": item["name"],
            "wan": round(item["amount"] / 10000, 2),
            "quantity": item["quantity"],
        }
        for index, item in enumerate(ordered[:50])
    ]

    trend: list[dict] = []
    for item in months:
        payload = current if item is months[-1] else await _orders(item["start"], item["end"])
        amount = float((payload.get("summary") or {}).get("amount") or 0)
        trend.append({"month": item["label"], "wan": round(amount / 10000, 2)})
    total = sum(float(item["amount"]) for item in ordered)
    return {
        "month": month,
        "total_wan": round(total / 10000, 2),
        "dealers": dealers,
        "has_data": bool(dealers),
        "trend": trend,
        "source": "vertu-cli sales +orders",
    }


async def fetch_salesperson_breakdown(month: str) -> dict:
    """Return actionable Sell-in ranking without masked customer identities."""
    period = _trend_months(month)[-1]
    end = min(_date.fromisoformat(period["end"]), _date.today())
    configured = os.environ.get(
        "PDCA_VERTU_SELLIN_DEPARTMENTS",
        "经销商一部,经销商二部,经销商三部",
    )
    departments = [item.strip() for item in configured.split(",") if item.strip()]

    async def fetch_department(department: str = "") -> dict:
        args = [
            "sales",
            "+dept-breakdown",
            "--start-date",
            period["start"],
            "--end-date",
            str(end),
            "--dept-l1",
            os.environ.get("PDCA_VERTU_DEPT_L1", "海外渠道"),
            "--group-by",
            "salesperson",
            "--limit",
            "5000",
        ]
        if department:
            args += ["--dept-l2", department]
        payload = await run_vertu_json(args, timeout=20.0)
        if not isinstance(payload, dict):
            raise RuntimeError("vertu-cli 销售人员汇总返回格式异常")
        return payload

    payloads = await asyncio.gather(
        *(fetch_department(department) for department in departments)
    ) if departments else [await fetch_department()]
    grouped: dict[str, dict] = {}
    for raw in (row for payload in payloads for row in (payload.get("rows") or [])):
        name = str(raw.get("name") or "").strip()
        if not name:
            continue
        item = grouped.setdefault(name, {"name": name, "amount": 0.0, "quantity": 0, "orders": 0})
        item["amount"] += float(raw.get("amount") or 0)
        item["quantity"] += int(raw.get("qty") or 0)
        item["orders"] += int(raw.get("orders") or 0)
    ordered = sorted(grouped.values(), key=lambda item: -item["amount"])
    rows = [
        {
            "rank": rank,
            "name": item["name"],
            "amount_wan": round(item["amount"] / 10000, 2),
            "quantity": item["quantity"],
            "orders": item["orders"],
        }
        for rank, item in enumerate(ordered, start=1)
    ]
    return {
        "month": month,
        "rows": rows,
        "total_wan": round(sum(row["amount_wan"] for row in rows), 2),
        "has_data": bool(rows),
        "source": "vertu-cli sales +dept-breakdown",
        "as_of": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
