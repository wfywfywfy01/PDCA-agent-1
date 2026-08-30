# -*- coding: utf-8 -*-
"""Walk-in 与线上渠道 API，含门店五件套录入/查询。"""
from __future__ import annotations

import re
import time
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from loguru import logger
from sqlmodel import Session, select

from app.audit import log_action
from app.auth.deps import get_current_user, require_role
from app.auth.models import User
from app.auth.scope import is_reporting_store, scoped_active_store_ids, visible_dealer_names, visible_store_ids
from app.config import get_settings
from app.database import get_session
from app.legacy import bridge
from app.models.dealer_store import DealerStore
from app.models.walkin_daily_report import WalkinDailyReport
from app.validation import require_iso_date
from app.vertu.activation import ActivationQueryError, fetch_dealer_activation

router = APIRouter(tags=["walkin"])

# ── VPS dealer-sales 缓存（内存，TTL 10 分钟）──────────────────────────────────
_vps_cache: dict[str, tuple[float, dict]] = {}
_VPS_TTL = 600  # seconds


def _revenue_requires_review(amount: float) -> bool:
    settings = get_settings()
    threshold = min(
        settings.max_reported_revenue_usd,
        getattr(settings, "revenue_review_threshold_usd", settings.max_reported_revenue_usd),
    )
    return amount > threshold


# ---------------------------------------------------------------------------
# 现有 walkin / 线上渠道 API
# ---------------------------------------------------------------------------

@router.get("/api/walkin")
async def walkin_api(
    date: str | None = None,
    month: str = Query(""),
    user: Annotated[User, Depends(require_role("viewer"))] = None,
    session: Annotated[Session, Depends(get_session)] = None,
):
    date_text = require_iso_date(date or bridge.today_text())
    month_text = month.strip()
    if not re.fullmatch(r"\d{4}-\d{2}", month_text):
        month_text = date_text[:7]
    try:
        payload = bridge.build_walkin_payload(month_text, date_text)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    # 将本月五件套真实提交数据合并到 stores 列表
    try:
        payload = _merge_five_kit_into_payload(payload, month_text, session)
    except Exception:
        pass  # 合并失败不影响主数据

    # 按角色过滤：sales 只看自己名下门店
    payload = _filter_walkin_payload(payload, user, session)

    return payload


@router.get("/api/online-channel")
async def online_channel(
    date: str | None = None,
    user: Annotated[User, Depends(require_role("viewer"))] = None,
    session: Annotated[Session, Depends(get_session)] = None,
):
    if visible_store_ids(user, session) is not None:
        raise HTTPException(
            status_code=403,
            detail="线上渠道数据尚未具备可靠的门店归属字段，已对范围账号关闭以防止跨范围展示",
        )
    try:
        return bridge.build_online_channel_payload(require_iso_date(date or bridge.today_text()))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# 五件套提交 / 查询 API
# ---------------------------------------------------------------------------

class WalkinMetricsSubmit(BaseModel):
    report_date: str                  # YYYY-MM-DD
    dealer_id: str
    dealer_name: str
    walkin_visits: int = Field(0, ge=0)             # walkin 直接进店
    cross_visits: int = Field(0, ge=0)              # 异业：同业其他奢侈品员工介绍
    online_visits: int = Field(0, ge=0)             # 线上：各种社媒渠道
    recruit_visits: int = Field(0, ge=0)            # 招聘：招聘的新员工自带客户
    existing_visits: int = Field(0, ge=0)           # 存量：老客户
    touch_count: int = Field(0, ge=0)               # Products Shown 产品展示
    use_count: int = Field(0, ge=0)
    wechat_add_count: int = Field(0, ge=0)
    deal_count: int = Field(0, ge=0)                # Products Sold 成交台数
    # 历史字段名保留兼容；录入页从一开始使用 $，实际口径为 USD。
    deal_amount_yuan: float = Field(0.0, ge=0.0)    # Revenue (USD)
    notes: str = ""


@router.get("/api/my-stores")
async def get_my_stores(
    user: Annotated[User, Depends(require_role("viewer"))],
    session: Annotated[Session, Depends(get_session)],
):
    """返回统一数据范围内的门店列表。"""
    stmt = select(DealerStore).where(DealerStore.is_active == True)
    allowed = visible_store_ids(user, session)
    if allowed is not None:
        if not allowed:
            return []
        stmt = stmt.where(DealerStore.store_id.in_(allowed))
    stores = [
        row for row in session.exec(stmt.order_by(DealerStore.region, DealerStore.sort_order)).all()
        if is_reporting_store(row)
    ]
    return [
        {
            "store_id": s.store_id,
            "name": s.name,
            "region": s.region,
            "country": s.country,
            "dealer_level": s.dealer_level,
            "sales_owner": s.sales_owner,
            "team_key": getattr(s, "team_key", "") or "",
        }
        for s in stores
    ]


@router.post("/api/walkin-metrics")
async def submit_walkin_metrics(
    body: WalkinMetricsSubmit,
    user: Annotated[User, Depends(require_role("dealer"))],
    session: Annotated[Session, Depends(get_session)],
):
    """提交门店五件套日报（dealer及以上权限）。dealer/sales角色只能提交自己名下的门店。"""
    max_revenue = get_settings().max_reported_revenue_usd
    if body.deal_amount_yuan > max_revenue:
        raise HTTPException(
            status_code=422,
            detail=f"Revenue must be entered in USD and cannot exceed ${max_revenue:,.0f}; check the currency/unit.",
        )
    require_iso_date(body.report_date, field="report_date")
    if not body.dealer_id.strip():
        raise HTTPException(status_code=422, detail="dealer_id 不能为空")
    if body.report_date > bridge.today_text():
        raise HTTPException(status_code=422, detail="report_date 不能是未来日期")

    dealer_store = session.exec(
        select(DealerStore).where(
            DealerStore.store_id == body.dealer_id,
            DealerStore.is_active == True,  # noqa: E712
        )
    ).first()
    if not dealer_store:
        raise HTTPException(status_code=422, detail="dealer_id 不存在，请从门店列表选择")

    allowed = visible_store_ids(user, session)
    if allowed is not None and body.dealer_id not in allowed:
        raise HTTPException(status_code=403, detail="该门店不在当前账号的数据权限范围内")

    # 同一门店同一天只保留最新一条（upsert）
    existing = session.exec(
        select(WalkinDailyReport).where(
            WalkinDailyReport.report_date == body.report_date,
            WalkinDailyReport.dealer_id == body.dealer_id,
        )
    ).first()

    data = dict(
        report_date=body.report_date,
        dealer_id=body.dealer_id,
        # The canonical store table is the source of truth.
        dealer_name=dealer_store.name,
        walkin_visits=body.walkin_visits,
        cross_visits=body.cross_visits,
        online_visits=body.online_visits,
        recruit_visits=body.recruit_visits,
        existing_visits=body.existing_visits,
        touch_count=body.touch_count,
        use_count=body.use_count,
        wechat_add_count=body.wechat_add_count,
        deal_count=body.deal_count,
        deal_amount_yuan=body.deal_amount_yuan,
        notes=body.notes,
        submitted_by=user.username,
    )

    if existing:
        for k, v in data.items():
            setattr(existing, k, v)
        session.add(existing)
    else:
        session.add(WalkinDailyReport(**data))

    session.commit()
    log_action(user.username, "submit_five_kit",
               resource=f"{body.dealer_id}:{body.report_date}",
               detail={"dealer": body.dealer_name, "revenue_usd": body.deal_amount_yuan})
    return {"ok": True, "message": "五件套数据已保存"}


def _dealer_ids_for_user(user: User, session) -> list[str] | None:
    """返回当前用户可见且启用的 dealer_id 列表。"""
    return scoped_active_store_ids(user, session)


def _filter_walkin_payload(payload: dict, user: User, session) -> dict:
    """按统一权限范围过滤 walkin payload 里的 stores 和 staff。"""
    allowed_set = set(scoped_active_store_ids(user, session))

    if not allowed_set:
        payload["stores"] = []
        payload["staff"] = []
        payload.setdefault("meta", {})["storeCount"] = 0
        return payload

    payload["stores"] = [s for s in payload.get("stores", []) if s.get("id") in allowed_set]
    payload["staff"] = [s for s in payload.get("staff", []) if s.get("storeId") in allowed_set]
    payload.setdefault("meta", {})["storeCount"] = len(payload["stores"])
    return payload


@router.get("/api/walkin-metrics")
async def list_walkin_metrics(
    user: Annotated[User, Depends(require_role("viewer"))],
    session: Annotated[Session, Depends(get_session)],
    month: str = Query(""),
    dealer_id: str = Query(""),
):
    """列出五件套日报，自动按权限过滤可见门店。"""
    stmt = select(WalkinDailyReport)
    if month and re.fullmatch(r"\d{4}-\d{2}", month):
        stmt = stmt.where(WalkinDailyReport.report_date.startswith(month))
    # 权限过滤
    allowed = _dealer_ids_for_user(user, session)
    if dealer_id and dealer_id not in allowed:
        raise HTTPException(status_code=403, detail="该门店不在当前账号的数据权限范围内")
    if not allowed:
        return {"count": 0, "items": []}
    stmt = stmt.where(WalkinDailyReport.dealer_id.in_(allowed))
    if dealer_id:
        stmt = stmt.where(WalkinDailyReport.dealer_id == dealer_id)
    rows = session.exec(stmt.order_by(WalkinDailyReport.report_date.desc())).all()
    return {
        "count": len(rows),
        "items": [
            {
                "id": r.id,
                "report_date": r.report_date,
                "dealer_id": r.dealer_id,
                "dealer_name": r.dealer_name,
                "five_kit": {
                    "walkin": r.walkin_visits,
                    "cross": r.cross_visits,
                    "online": r.online_visits,
                    "recruit": r.recruit_visits,
                    "existing": r.existing_visits,
                    "total": r.total_visits,
                },
                "funnel": {
                    "total_visits": r.total_visits,
                    "touch_count": r.touch_count,
                    "use_count": r.use_count,
                    "wechat_add_count": r.wechat_add_count,
                    "deal_count": r.deal_count,
                    "deal_amount_yuan": r.deal_amount_yuan,
                    "deal_amount_usd": r.deal_amount_yuan,
                    "amount_requires_review": _revenue_requires_review(r.deal_amount_yuan),
                },
                "notes": r.notes,
                "submitted_by": r.submitted_by,
                "created_at": r.created_at.isoformat() if r.created_at else "",
            }
            for r in rows
        ],
    }


@router.get("/api/walkin-metrics/summary")
async def walkin_metrics_summary(
    user: Annotated[User, Depends(require_role("viewer"))],
    session: Annotated[Session, Depends(get_session)],
    month: str = Query(""),
    start: str = Query(""),
    end:   str = Query(""),
):
    """按期间聚合五件套数据，自动按权限过滤。
    优先使用 start/end（精确日期区间），否则按 month 前缀匹配。
    """
    stmt = select(WalkinDailyReport)
    # 日期过滤：start/end 优先，兜底 month 前缀
    if start and re.fullmatch(r"\d{4}-\d{2}-\d{2}", start) and end and re.fullmatch(r"\d{4}-\d{2}-\d{2}", end):
        stmt = stmt.where(WalkinDailyReport.report_date >= start)
        stmt = stmt.where(WalkinDailyReport.report_date <= end)
        if not month:
            month = start[:7]
    elif month and re.fullmatch(r"\d{4}-\d{2}", month):
        stmt = stmt.where(WalkinDailyReport.report_date.startswith(month))
    allowed = _dealer_ids_for_user(user, session)
    if not allowed:
        rows = []
    else:
        stmt = stmt.where(WalkinDailyReport.dealer_id.in_(allowed))
        rows = session.exec(stmt).all()

    if not rows:
        return {
            "month": month,
            "record_count": 0,
            "five_kit": {"walkin": 0, "cross": 0, "online": 0, "recruit": 0, "existing": 0, "total": 0},
            "funnel": {"total_visits": 0, "touch_count": 0, "use_count": 0, "wechat_add_count": 0, "deal_count": 0, "deal_amount_yuan": 0.0, "deal_amount_usd": 0.0},
            "by_dealer": [],
            "data_quality": {"excluded_record_count": 0, "reason": ""},
        }

    agg = dict(walkin=0, cross=0, online=0, recruit=0, existing=0,
               total_visits=0, touch=0, use=0, wechat=0, deal_count=0, deal_amount=0.0)
    excluded_amount_count = 0

    dealer_map: dict[str, dict] = {}

    for r in rows:
        agg["walkin"] += r.walkin_visits
        agg["cross"] += r.cross_visits
        agg["online"] += r.online_visits
        agg["recruit"] += r.recruit_visits
        agg["existing"] += r.existing_visits
        tv = r.total_visits
        agg["total_visits"] += tv
        agg["touch"] += r.touch_count
        agg["use"] += r.use_count
        agg["wechat"] += r.wechat_add_count
        agg["deal_count"] += r.deal_count
        amount_valid = not _revenue_requires_review(r.deal_amount_yuan)
        if amount_valid:
            agg["deal_amount"] += r.deal_amount_yuan
        else:
            excluded_amount_count += 1

        dm = dealer_map.setdefault(r.dealer_id, {
            "dealer_id": r.dealer_id, "dealer_name": r.dealer_name,
            "walkin": 0, "cross": 0, "online": 0, "recruit": 0, "existing": 0,
            "total_visits": 0, "deal_count": 0, "deal_amount_yuan": 0.0,
            "deal_amount_usd": 0.0, "amount_requires_review": False,
        })
        dm["walkin"] += r.walkin_visits
        dm["cross"] += r.cross_visits
        dm["online"] += r.online_visits
        dm["recruit"] += r.recruit_visits
        dm["existing"] += r.existing_visits
        dm["total_visits"] += tv
        dm["deal_count"] += r.deal_count
        if amount_valid:
            dm["deal_amount_yuan"] += r.deal_amount_yuan
            dm["deal_amount_usd"] += r.deal_amount_yuan
        else:
            dm["amount_requires_review"] = True

    total_src = agg["walkin"] + agg["cross"] + agg["online"] + agg["recruit"] + agg["existing"]

    def pct(n):
        return round(n / total_src * 100, 1) if total_src else 0

    return {
        "month": month,
        "record_count": len(rows),
        "five_kit": {
            "walkin": agg["walkin"],
            "cross": agg["cross"],
            "online": agg["online"],
            "recruit": agg["recruit"],
            "existing": agg["existing"],
            "total": total_src,
            "pct": {
                "walkin": pct(agg["walkin"]),
                "cross": pct(agg["cross"]),
                "online": pct(agg["online"]),
                "recruit": pct(agg["recruit"]),
                "existing": pct(agg["existing"]),
            },
        },
        "funnel": {
            "total_visits": agg["total_visits"],
            "touch_count": agg["touch"],
            "use_count": agg["use"],
            "wechat_add_count": agg["wechat"],
            "deal_count": agg["deal_count"],
            # deal_amount_yuan 为旧客户端兼容字段，金额口径实际为 USD。
            "deal_amount_yuan": round(agg["deal_amount"], 2),
            "deal_amount_usd": round(agg["deal_amount"], 2),
        },
        "by_dealer": sorted(dealer_map.values(), key=lambda x: -x["deal_amount_yuan"]),
        "data_quality": {
            "excluded_record_count": excluded_amount_count,
            "reason": "Revenue exceeds the USD review threshold; verify the original currency before including it in totals."
            if excluded_amount_count else "",
        },
    }


# ---------------------------------------------------------------------------
# 内部辅助：把真实五件套数据合并进 walkin payload 的 stores 列表
# ---------------------------------------------------------------------------

def _merge_five_kit_into_payload(payload: dict, month_text: str, session) -> dict:
    """用门店主数据建立稳定骨架，再注入当月五件套汇总。

    生产发布包不携带运行时生成的 ``walkin-YYYY-MM.json``。即使该文件
    缺失，数据库中的门店和实报也必须能够独立驱动客流驾驶舱。
    """
    if not session:
        return payload
    master_stores = session.exec(
        select(DealerStore)
        .where(DealerStore.is_active == True)
        .order_by(DealerStore.sort_order, DealerStore.store_id)
    ).all()
    active_ids = {row.store_id for row in master_stores}
    stmt = select(WalkinDailyReport).where(
        WalkinDailyReport.report_date.startswith(month_text),
        WalkinDailyReport.dealer_id.in_(active_ids),
    )
    rows = session.exec(stmt).all()

    # 按 dealer_id 聚合
    dealer_agg: dict[str, dict] = {}
    for r in rows:
        d = dealer_agg.setdefault(r.dealer_id, {
            "walkin": 0, "cross": 0, "online": 0, "recruit": 0, "existing": 0,
            "total_visits": 0, "touch": 0, "use": 0, "wechat": 0,
            "deal_count": 0, "deal_amount_yuan": 0.0,
        })
        d["walkin"] += r.walkin_visits
        d["cross"] += r.cross_visits
        d["online"] += r.online_visits
        d["recruit"] += r.recruit_visits
        d["existing"] += r.existing_visits
        d["total_visits"] += r.total_visits
        d["touch"] += r.touch_count
        d["use"] += r.use_count
        d["wechat"] += r.wechat_add_count
        d["deal_count"] += r.deal_count
        if not _revenue_requires_review(r.deal_amount_yuan):
            d["deal_amount_yuan"] += r.deal_amount_yuan

    stores = [
        store for store in payload.setdefault("stores", [])
        if str(store.get("id") or "") in active_ids
    ]
    payload["stores"] = stores
    existing_ids = {str(store.get("id") or "") for store in stores}
    for master in master_stores:
        if master.store_id in existing_ids:
            continue
        stores.append({
            "id": master.store_id,
            "name": master.name,
            "city": master.country,
            "region": master.region,
            "class": "Class 1" if master.dealer_level == "L1" else "Class 2",
            "totalVisitGroups": 0,
            "avgAddRate": 0,
            "totalSalesAmount": 0,
            "anomalies": [],
            "avgTouchRate": 0,
            "avgUseRate": 0,
            "dealerTeam": master.sales_owner,
            "customerType": "",
            "dataSource": "dealer_store_db",
        })

    for store in stores:
        sid = store.get("id", "")
        if sid in dealer_agg:
            d = dealer_agg[sid]
            # 注入五件套来源字段
            store["fiveKit"] = {
                "walkin": d["walkin"],
                "cross": d["cross"],
                "online": d["online"],
                "recruit": d["recruit"],
                "existing": d["existing"],
                "total": d["total_visits"],
            }
            # 五件套录入金额为 USD；不得覆盖以人民币计价的销售主指标。
            if 0 < d["deal_amount_yuan"] <= get_settings().max_reported_revenue_usd:
                store["reportedSellOutUsd"] = d["deal_amount_yuan"]
            if d["total_visits"] > 0:
                store["totalVisitGroups"] = d["total_visits"]
                store["walkinPeople"] = d["total_visits"]
                store["avgAddRate"] = min(1.0, round(d["wechat"] / d["total_visits"], 4))
                store["avgTouchRate"] = min(1.0, round(d["touch"] / d["total_visits"], 4))
                store["avgUseRate"] = min(1.0, round(d["use"] / d["total_visits"], 4))
            if d["touch"] > 0:
                store["touchCount"] = d["touch"]
            if d["use"] > 0:
                store["useCount"] = d["use"]
            if d["wechat"] > 0:
                store["wechatAddCount"] = d["wechat"]
            if d["deal_count"] > 0:
                store["dealGroups"] = d["deal_count"]

    meta = payload.setdefault("meta", {})
    meta["storeCount"] = len(stores)
    sources = meta.setdefault("dataSources", [])
    if master_stores and "dealer_store_db" not in sources:
        sources.append("dealer_store_db")
    if rows and "five_kit_db" not in sources:
        sources.append("five_kit_db")
    if stores and "unavailable" in sources:
        sources.remove("unavailable")
    if master_stores:
        meta["dataSourceDetail"] = "数据来源：门店主数据 + 门店五件套实报"

    return payload


# ---------------------------------------------------------------------------
# VPS 真实经销商 Sell-out（海外，来自 odoo_sale 视图）
# ---------------------------------------------------------------------------

def _run_dealer_vps_query(run_date: str, *, start_date: str = "", end_date: str = "") -> dict:
    """调用 vertu-cli 销售订单快捷命令，按客户聚合经销商数据并缓存。
    缓存 key = start_date:end_date，精确区间各自独立缓存。
    """
    from app.vertu.sales import fetch_dealer_sales_orders_sync

    if not start_date:
        start_date = run_date[:8] + "01"
    if not end_date:
        end_date = run_date

    cache_key = f"{start_date}:{end_date}"
    cached = _vps_cache.get(cache_key)
    if cached and time.time() - cached[0] < _VPS_TTL:
        return cached[1]

    result = fetch_dealer_sales_orders_sync(start_date, end_date)

    _vps_cache[cache_key] = (time.time(), result)
    return result


@router.get("/api/vps/dealer-sales")
async def vps_dealer_sales(
    user: Annotated[User, Depends(require_role("viewer"))],
    session: Annotated[Session, Depends(get_session)],
    month: str = Query(""),
    start: str = Query(""),
    end:   str = Query(""),
):
    """从 vertu-cli 拉取海外经销商订单汇总。
    优先使用 start/end 精确区间；否则按 month 推算月初到月末（或今日）。
    返回 {start_date, end_date, month, total, dealers:[{dealer_name, sell_out_yuan, qty}]}
    """
    import datetime, calendar as _cal

    today = datetime.date.today().isoformat()

    if start and re.fullmatch(r"\d{4}-\d{2}-\d{2}", start) and end and re.fullmatch(r"\d{4}-\d{2}-\d{2}", end):
        start_date = start
        end_date   = min(end, today)
        run_date   = end_date
    elif month and re.fullmatch(r"\d{4}-\d{2}", month):
        y, m = map(int, month.split("-"))
        start_date = f"{month}-01"
        last_day   = _cal.monthrange(y, m)[1]
        end_date   = min(f"{month}-{last_day:02d}", today)
        run_date   = end_date
    else:
        start_date = today[:8] + "01"
        end_date   = today
        run_date   = today

    try:
        data = _run_dealer_vps_query(run_date, start_date=start_date, end_date=end_date)
        names = visible_dealer_names(user, session)
        if names is not None:
            allowed = {name.casefold() for name in names}
            data = dict(data)
            data["dealers"] = [
                row for row in data.get("dealers", [])
                if str(row.get("dealer_name") or "").casefold() in allowed
            ]
            data["total"] = round(
                sum(float(row.get("sell_out_yuan") or 0) for row in data["dealers"]),
                2,
            )
        return data
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Vertu 查询失败: {exc}") from exc


# ---------------------------------------------------------------------------
# VPS 激活数据（累计，不依赖月份）
# ---------------------------------------------------------------------------

@router.get("/api/vps/dealer-activation")
async def vps_dealer_activation(
    user: Annotated[User, Depends(require_role("viewer"))],
    session: Annotated[Session, Depends(get_session)],
):
    """Read activation data from the legacy CLI only when the new CLI lacks it."""
    try:
        data = await fetch_dealer_activation()
    except ActivationQueryError as exc:
        logger.warning("Dealer activation data is unavailable: {}", exc)
        return {
            "ok": False,
            "available": False,
            "detail": "激活设备数据暂时不可用",
            "dealers": [],
            "products": [],
            "total_overseas_stock": None,
            "state": "missing",
            "source": "legacy-vertu:mobile.activation.report",
        }
    return _scope_activation_payload(data, visible_dealer_names(user, session))


def _scope_activation_payload(data: dict, names: list[str] | None) -> dict:
    """dealer/sales 仅返回自己门店的激活数据，并重算汇总。"""
    if names is None:
        return data
    allowed = {name.casefold() for name in names}
    scoped = dict(data)
    dealers = [
        row for row in data.get("dealers", [])
        if str(row.get("dealer_name") or "").casefold() in allowed
    ]
    shipped = sum(int(row.get("shipped") or 0) for row in dealers)
    activated = sum(int(row.get("activated") or 0) for row in dealers)
    local_activated = sum(int(row.get("local_activated") or 0) for row in dealers)
    remote_activated = sum(int(row.get("remote_activated") or 0) for row in dealers)
    classified = local_activated + remote_activated
    scoped.update({
        "dealers": dealers,
        "products": [],
        # mv_inventory only exposes a global stock total, so do not relabel scoped
        # dealers' unactivated devices as physical overseas inventory.
        "total_overseas_stock": None,
        "total_shipped": shipped,
        "total_activated": activated,
        "total_local_activated": local_activated,
        "total_remote_activated": remote_activated,
        "overall_activation_rate": round(activated / shipped * 100, 1) if shipped else 0,
        "overall_local_rate": round(local_activated / classified * 100, 1) if classified else 0,
    })
    return scoped
