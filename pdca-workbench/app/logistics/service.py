# -*- coding: utf-8 -*-
"""物流数据加载、判断与按销售过滤。"""
from __future__ import annotations

import csv
import json
import re
from datetime import datetime
from pathlib import Path

from loguru import logger

from app.config import get_settings
from app.legacy import bridge

JUDGEMENT_ORDER = {
    "异常": 0,
    "待关注": 1,
    "待核查": 2,
    "运输中": 3,
    "正常": 4,
}

STATUS_GROUPS = {
    "attention": {"异常", "待关注"},
    "transit": {"运输中", "待核查"},
    "delivered": set(),
}

_PLACEHOLDER_TRACKING_NUMBERS = {"1Z0000000000000000"}


def _is_demo_record(row: dict) -> bool:
    """识别明确的演示/占位运单，避免测试数据进入生产看板。"""
    tracking = str(row.get("tracking_number") or "").strip().upper()
    if tracking in _PLACEHOLDER_TRACKING_NUMBERS or tracking.startswith(("TEST", "DEMO", "QA-")):
        return True
    customer = str(row.get("customer") or "").strip().casefold()
    salesperson = str(row.get("salesperson") or "").strip().casefold()
    note = str(row.get("note") or "").strip().casefold()
    return (
        customer.startswith(("演示客户", "测试客户"))
        or bool(re.match(r"^(demo|mock)([-_ ]|$)", customer))
        or bool(re.match(r"^(demo|mock)([-_ ]|$)", salesperson))
        or note in {"测试", "test", "qa"}
    )


def _read_csv(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    with path.open(encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def _load_carriers() -> dict:
    path = get_settings().config_dir / "carriers.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _tracking_url(carriers: dict, carrier: str, tracking_number: str) -> str:
    info = carriers.get(carrier) or carriers.get((carrier or "").upper()) or {}
    template = info.get("tracking_url", "")
    return template.replace("{tracking_number}", tracking_number) if template else ""


def _load_settings() -> dict:
    path = get_settings().config_dir / "settings.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _status_is_delivered(status: str) -> bool:
    value = (status or "").strip().lower()
    if not value:
        return False
    return (
        "delivered" in value
        or "已签收" in value
        or "签收完成" in value
        or "已送达" in value
    )


def _is_delivered(row: dict) -> bool:
    """是否视为已签收。"""
    status = (row.get("current_status") or row.get("status") or "").lower()
    if row.get("progress_pct", 0) >= 100:
        return True
    return _status_is_delivered(status)


def _days_since_ship(ship_date: str, ref_date: str) -> int | None:
    """发货至参考日的天数。"""
    try:
        ship_dt = datetime.strptime((ship_date or "")[:10], "%Y-%m-%d")
        ref_dt = datetime.strptime((ref_date or bridge.today_text())[:10], "%Y-%m-%d")
        return max(0, (ref_dt - ship_dt).days)
    except ValueError:
        return None


def _judge_status(row: dict, settings: dict, date_text: str) -> tuple[str, str, int]:
    """返回 judgement, reason, progress_pct。"""
    status = (row.get("current_status") or row.get("status") or "").strip()
    status_lower = status.lower()
    logistics_cfg = settings.get("logistics", {})
    for keyword in logistics_cfg.get("abnormal_keywords", []):
        if keyword.lower() in status_lower:
            return "异常", f"状态包含异常关键词：{keyword}", 20
    if _status_is_delivered(status):
        return "正常", "已签收或送达", 100
    ship_date = (row.get("ship_date") or date_text or "")[:10]
    try:
        ship_dt = datetime.strptime(ship_date, "%Y-%m-%d")
        run_dt = datetime.strptime(date_text, "%Y-%m-%d")
        if (run_dt - ship_dt).days >= 7:
            return "待关注", "发货超过 7 天，且未标记签收", 45
    except ValueError:
        pass
    for keyword in logistics_cfg.get("normal_keywords", []):
        if keyword.lower() in status_lower:
            return "正常", f"状态包含正常关键词：{keyword}", 70
    if status:
        return "运输中", "在途更新", 65
    return "待核查", "未填写当前状态", 30


def _sales_aliases() -> dict[str, str]:
    path = get_settings().config_dir / "sales_aliases.csv"
    aliases: dict[str, str] = {}
    if not path.is_file():
        return aliases
    try:
        with path.open(encoding="utf-8-sig", newline="") as fh:
            for row in csv.DictReader(fh):
                raw = (row.get("raw_sales") or "").strip()
                canonical = (row.get("canonical_sales") or "").strip()
                if raw and canonical:
                    aliases[raw.lower()] = canonical
                    aliases[canonical.lower()] = canonical
    except OSError:
        pass
    return aliases


def canonical_sales_name(value: str) -> str:
    text = (value or "").strip()
    if not text:
        return ""
    aliases = _sales_aliases()
    return aliases.get(text.lower(), text)


def resolve_sales_filter(
    role: str,
    sales_name: str,
    display_name: str,
    username: str,
    salesperson_query: str = "",
) -> str | None:
    """
    解析销售过滤名。

    sales 角色只能看自己的；manager/admin 可看全部或指定销售。
    """
    if role == "sales":
        return canonical_sales_name(sales_name or display_name or username)
    if salesperson_query.strip():
        return canonical_sales_name(salesperson_query.strip())
    return None


def _match_sales(row_sales: str, filter_sales: str | None) -> bool:
    if not filter_sales:
        return True
    row_canon = canonical_sales_name(row_sales)
    return row_canon == filter_sales or (row_sales or "").strip() == filter_sales


def _match_search(row: dict, query: str) -> bool:
    q = (query or "").strip().lower()
    if not q:
        return True
    haystack = " ".join(
        [
            row.get("tracking_number", ""),
            row.get("carrier", ""),
            row.get("customer", ""),
            row.get("salesperson", ""),
            row.get("note", ""),
            row.get("current_status", ""),
        ],
    ).lower()
    return q in haystack


def _match_status_group(row: dict, status_group: str) -> bool:
    group = (status_group or "all").strip().lower()
    if group in ("", "all"):
        return True
    if group == "delivered":
        return _is_delivered(row)
    if group == "attention":
        return row.get("judgement") in STATUS_GROUPS["attention"]
    if group == "transit":
        return not _is_delivered(row) and row.get("judgement") not in STATUS_GROUPS["attention"]
    return row.get("judgement") == status_group


def _sort_shipments(rows: list[dict]) -> list[dict]:
    """异常优先，其次发货日倒序。"""
    return sorted(
        rows,
        key=lambda r: (
            JUDGEMENT_ORDER.get(r.get("judgement", "待核查"), 9),
            0 if _is_delivered(r) else -1,
            -(r.get("days_in_transit") or 0),
            r.get("ship_date", ""),
            r.get("record_date", ""),
        ),
    )


def _apply_auto_status(enriched: dict, auto_map: dict[str, "object"]) -> None:
    """用官网自动抓取结果覆盖人工录入的 current_status（仅 UPS/FedEx/DHL）。"""
    tracking = (enriched.get("tracking_number") or "").strip()
    auto = auto_map.get(tracking) if tracking else None
    if not auto or not getattr(auto, "fetch_ok", False):
        return
    enriched["current_status"] = auto.status_text
    enriched["status_source"] = "auto"
    enriched["status_fetched_at"] = auto.fetched_at.isoformat() if auto.fetched_at else ""
    enriched.pop("judgement", None)  # 强制按最新抓取状态重新判定
    if auto.is_delivered:
        enriched["is_delivered_override"] = True


def _enrich_row(
    row: dict,
    carriers: dict,
    cfg: dict,
    file_date: str,
    ref_date: str,
    auto_map: dict[str, "object"] | None = None,
) -> dict:
    enriched = dict(row)
    tracking = (enriched.get("tracking_number") or "").strip()
    enriched["record_date"] = file_date
    enriched["tracking_url"] = enriched.get("tracking_url") or _tracking_url(
        carriers,
        enriched.get("carrier", ""),
        tracking,
    )
    enriched.setdefault("status_source", "manual")
    if auto_map:
        _apply_auto_status(enriched, auto_map)
    # 判定永远按真实今天重新算，不能只在"没有已存判定"时才算一次——
    # 否则录入批次当天算出的判定（比如 7 天预警）会被永久冻结在 outputs 里的旧结果 CSV 中，
    # 不会因为时间推移而重新升级/降级
    judgement, reason, progress = _judge_status(enriched, cfg, ref_date)
    enriched["judgement"] = judgement
    enriched["reason"] = reason
    enriched["progress_pct"] = progress
    enriched["is_delivered"] = _is_delivered(enriched) or bool(enriched.get("is_delivered_override"))
    enriched["days_in_transit"] = _days_since_ship(enriched.get("ship_date", ""), ref_date)
    enriched["check_report_path"] = check_report_relative_path(file_date)
    enriched["check_report_exists"] = bool(enriched["check_report_path"])
    return enriched


def list_available_dates() -> list[str]:
    """有物流录入数据的日期列表（新→旧）。"""
    settings = get_settings()
    inputs_dir = settings.mvp_root / "inputs" / "logistics"
    dates = {
        str(row.get("record_date") or "")
        for row in (_load_db_rows("all") or [])
        if row.get("record_date")
        and (settings.include_demo_data or not _is_demo_record(row))
    }
    for csv_path in inputs_dir.glob("*_tracking.csv") if inputs_dir.is_dir() else []:
        rows = _read_csv(csv_path)
        if any(
            (r.get("tracking_number") or "").strip()
            and (settings.include_demo_data or not _is_demo_record(r))
            for r in rows
        ):
            dates.add(csv_path.stem.replace("_tracking", ""))
    return sorted(dates, reverse=True)


def check_report_relative_path(date_text: str) -> str:
    """核查报告相对 MVP 根路径。"""
    path = get_settings().mvp_root / "outputs" / date_text / "logistics_check_report.md"
    if path.is_file():
        return f"outputs/{date_text}/logistics_check_report.md"
    return ""


def load_auto_status_map() -> dict[str, object]:
    """读取 tracking_auto_status 表，按运单号返回最新抓取记录。"""
    try:
        from sqlmodel import Session, select
        from app.database import get_engine
        from app.models.tracking_status import TrackingAutoStatus

        with Session(get_engine()) as session:
            rows = session.exec(select(TrackingAutoStatus)).all()
            return {r.tracking_number: r for r in rows}
    except Exception as exc:
        logger_warn = __import__("loguru").logger
        logger_warn.warning("读取自动物流状态失败: {}", exc)
        return {}


def _load_db_rows(date_text: str | None = None) -> list[dict] | None:
    """Read canonical logistics rows; CSV remains a history fallback."""
    try:
        from sqlmodel import Session, select
        from app.database import get_engine
        from app.models.logistics import LogisticsShipment

        stmt = select(LogisticsShipment)
        if date_text and date_text != "all":
            stmt = stmt.where(LogisticsShipment.record_date == date_text)
        with Session(get_engine()) as session:
            rows = list(session.exec(stmt).all())
    except Exception as exc:
        logger.warning("读取 logistics_shipments 失败: {}", exc)
        return None
    return [
        {
            "record_date": row.record_date,
            "tracking_number": row.tracking_number,
            "carrier": row.carrier,
            "customer": row.customer,
            "salesperson": row.salesperson,
            "ship_date": row.ship_date,
            "expected_status": row.expected_status,
            "current_status": row.current_status,
            "note": row.note,
            "tracking_url": row.tracking_url,
            "judgement": row.judgement,
            "reason": row.reason,
            "progress_pct": row.progress_pct,
        }
        for row in rows
    ]


def load_shipments(
    date_text: str | None = None,
    salesperson: str | None = None,
    status_group: str = "all",
    query: str = "",
    open_only: bool = False,
    return_state: bool = False,
) -> list[dict] | tuple[list[dict], str]:
    """合并运单数据：数据库优先，CSV 仅补充未迁移历史。"""
    settings = get_settings()
    inputs_dir = settings.mvp_root / "inputs" / "logistics"
    outputs_dir = settings.mvp_root / "outputs"
    carriers = _load_carriers()
    cfg = _load_settings()
    # "在途天数"/7天预警的参照点永远是真实今天，不能用录入批次日（date_text 可能是具体批次日
    # 甚至字面量 "all"）——否则默认视图（date=all）在途天数全部算不出来，选中某批次时算出来的
    # 天数也是"以录入那天为准"而不是"以现在为准"
    ref_date = bridge.today_text()
    auto_map = load_auto_status_map()
    merged: dict[str, dict] = {}

    db_rows = _load_db_rows(date_text)
    db_available = db_rows is not None
    for row in db_rows or []:
        tracking = (row.get("tracking_number") or "").strip()
        if not tracking or (not settings.include_demo_data and _is_demo_record(row)):
            continue
        sourced = {**row, "data_source": "logistics_db"}
        merged[tracking] = _enrich_row(
            sourced, carriers, cfg, row.get("record_date") or ref_date, ref_date, auto_map
        )

    for csv_path in sorted(inputs_dir.glob("*_tracking.csv")) if inputs_dir.is_dir() else []:
        file_date = csv_path.stem.replace("_tracking", "")
        if date_text and date_text != "all" and file_date != date_text:
            continue
        results_path = outputs_dir / file_date / f"{file_date}_logistics_results.csv"
        results_by_tracking = {
            (r.get("tracking_number") or "").strip(): r
            for r in _read_csv(results_path)
        }
        for row in _read_csv(csv_path):
            tracking = (row.get("tracking_number") or "").strip()
            if not tracking or tracking in merged:
                continue
            if not settings.include_demo_data and _is_demo_record(row):
                continue
            enriched = dict(row)
            enriched["data_source"] = "csv_history"
            if tracking in results_by_tracking:
                enriched.update(results_by_tracking[tracking])
            merged[tracking] = _enrich_row(enriched, carriers, cfg, file_date, ref_date, auto_map)

    rows = list(merged.values())
    if salesperson:
        rows = [r for r in rows if _match_sales(r.get("salesperson", ""), salesperson)]
    if open_only:
        rows = [r for r in rows if not r.get("is_delivered")]
    rows = [r for r in rows if _match_status_group(r, status_group)]
    rows = [r for r in rows if _match_search(r, query)]
    result = _sort_shipments(rows)
    if return_state:
        sources = {row.get("data_source") for row in result}
        if not db_available:
            source_state = "degraded"
        elif sources == {"logistics_db"} or not result:
            source_state = "available"
        elif "logistics_db" in sources:
            source_state = "mixed"
        else:
            source_state = "historical"
        return result, source_state
    return result


def build_summary(shipments: list[dict]) -> dict:
    """汇总物流进展统计。"""
    total = len(shipments)
    delivered = sum(1 for s in shipments if s.get("is_delivered"))
    attention = [s for s in shipments if s.get("judgement") in STATUS_GROUPS["attention"]]
    in_transit = sum(
        1
        for s in shipments
        if not s.get("is_delivered")
        and s.get("judgement") in ("运输中", "正常", "待核查")
    )
    abnormal = len(attention)
    pending = sum(1 for s in shipments if s.get("judgement") == "待核查" and not s.get("is_delivered"))
    rate = round(delivered / total * 100) if total else 0
    return {
        "total": total,
        "delivered": delivered,
        "in_transit": in_transit,
        "abnormal": abnormal,
        "pending": pending,
        "open": total - delivered,
        "delivery_rate_pct": rate,
        "attention_items": [
            {
                "tracking_number": s.get("tracking_number"),
                "customer": s.get("customer"),
                "judgement": s.get("judgement"),
                "reason": s.get("reason"),
            }
            for s in attention[:5]
        ],
    }


def list_salespeople() -> list[str]:
    """所有出现过的销售姓名。"""
    names = set()
    for row in load_shipments(date_text="all"):
        name = canonical_sales_name(row.get("salesperson", ""))
        if name:
            names.add(name)
    return sorted(names)


async def refresh_tracking_statuses() -> dict:
    """从 UPS/FedEx/DHL 官网抓取在途运单的最新状态，写入 tracking_auto_status 表。

    SF 顺丰官网强制图形验证码，跳过（继续使用人工录入）。
    """
    from app.logistics import tracking_fetch

    open_rows = load_shipments(date_text="all", open_only=True)
    candidates = [
        (row.get("carrier", ""), (row.get("tracking_number") or "").strip())
        for row in open_rows
        if tracking_fetch.is_supported_carrier(row.get("carrier", ""))
        and (row.get("tracking_number") or "").strip()
    ]
    # 去重（同一运单可能出现在多个日期批次）
    candidates = list({(c, t) for c, t in candidates})

    if not candidates:
        return {"attempted": 0, "ok": 0, "failed": 0, "skipped_no_candidates": True}

    results = await tracking_fetch.fetch_many(candidates)

    from datetime import datetime
    from sqlmodel import Session, select
    from app.database import get_engine
    from app.models.tracking_status import TrackingAutoStatus

    ok_count = 0
    with Session(get_engine()) as session:
        for r in results:
            existing = session.exec(
                select(TrackingAutoStatus).where(
                    TrackingAutoStatus.tracking_number == r.tracking_number
                )
            ).first()
            if existing:
                existing.carrier = r.carrier
                existing.status_text = r.status_text
                existing.is_delivered = r.is_delivered
                existing.fetch_ok = r.fetch_ok
                existing.error = r.error
                existing.fetched_at = datetime.utcnow()
                session.add(existing)
            else:
                session.add(TrackingAutoStatus(
                    tracking_number=r.tracking_number,
                    carrier=r.carrier,
                    status_text=r.status_text,
                    is_delivered=r.is_delivered,
                    fetch_ok=r.fetch_ok,
                    error=r.error,
                ))
            if r.fetch_ok:
                ok_count += 1
        session.commit()

    return {
        "attempted": len(results),
        "ok": ok_count,
        "failed": len(results) - ok_count,
    }
