# -*- coding: utf-8 -*-
"""Logistics APIs with mandatory row-level scope."""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlmodel import Session

from app.auth.deps import require_role
from app.auth.models import User
from app.auth.scope import normalize_scope_key, resolve_data_scope
from app.database import get_session
from app.logistics import service

router = APIRouter(prefix="/api/logistics", tags=["logistics"])


def _scoped_shipments(rows: list[dict], user: User, session: Session) -> tuple[list[dict], str]:
    scope = resolve_data_scope(user, session)
    if scope.unrestricted:
        return rows, "全部"
    owners = {normalize_scope_key(value) for value in scope.owner_keys if normalize_scope_key(value)}
    dealers = {normalize_scope_key(value) for value in scope.dealer_names if normalize_scope_key(value)}
    filtered = [
        row for row in rows
        if normalize_scope_key(row.get("salesperson")) in owners
        or (user.role == "dealer" and normalize_scope_key(row.get("customer")) in dealers)
    ]
    return filtered, "当前权限范围"


def _load_scoped(
    date: str | None,
    salesperson: str,
    status: str,
    q: str,
    open_only: bool,
    user: User,
    session: Session,
) -> tuple[list[dict], str, str, str]:
    date_key = date or "all"
    rows, source_state = service.load_shipments(
        date_key, None, status, q, open_only, return_state=True
    )
    rows, label = _scoped_shipments(rows, user, session)
    scope = resolve_data_scope(user, session)
    if salesperson.strip() and scope.unrestricted:
        requested = service.canonical_sales_name(salesperson)
        rows = [row for row in rows if service.canonical_sales_name(row.get("salesperson", "")) == requested]
        label = requested
    return rows, label, date_key, source_state


@router.get("/dates")
async def logistics_dates(
    user: Annotated[User, Depends(require_role("viewer"))] = None,
    session: Annotated[Session, Depends(get_session)] = None,
):
    loaded, source_state = service.load_shipments("all", return_state=True)
    rows, _label = _scoped_shipments(loaded, user, session)
    dates = sorted({str(row.get("record_date") or "") for row in rows if row.get("record_date")}, reverse=True)
    return {"items": dates, "source_state": source_state}


@router.get("/summary")
async def logistics_summary(
    date: str | None = Query(None, description="YYYY-MM-DD 或 all 表示全部"),
    salesperson: str = Query(""),
    status: str = Query("all"),
    q: str = Query(""),
    open_only: bool = Query(False),
    user: Annotated[User, Depends(require_role("viewer"))] = None,
    session: Annotated[Session, Depends(get_session)] = None,
):
    shipments, label, date_key, source_state = _load_scoped(date, salesperson, status, q, open_only, user, session)
    return {"date": date_key, "salesperson": label, "role": user.role, "source_state": source_state, **service.build_summary(shipments)}


@router.get("/shipments")
async def logistics_shipments(
    date: str | None = Query(None),
    salesperson: str = Query(""),
    status: str = Query("all"),
    q: str = Query(""),
    open_only: bool = Query(False),
    user: Annotated[User, Depends(require_role("viewer"))] = None,
    session: Annotated[Session, Depends(get_session)] = None,
):
    rows, label, date_key, source_state = _load_scoped(date, salesperson, status, q, open_only, user, session)
    return {"date": date_key, "salesperson": label, "source_state": source_state, "count": len(rows), "items": rows}


@router.get("/salespeople")
async def logistics_salespeople(
    user: Annotated[User, Depends(require_role("manager"))],
    session: Annotated[Session, Depends(get_session)],
):
    loaded, _source_state = service.load_shipments("all", return_state=True)
    rows, _label = _scoped_shipments(loaded, user, session)
    names = sorted({service.canonical_sales_name(row.get("salesperson", "")) for row in rows if row.get("salesperson")})
    return {"items": names}


@router.post("/refresh-tracking")
async def refresh_tracking(
    _user: Annotated[User, Depends(require_role("admin"))],
):
    """Global carrier sync is an admin-only operation."""
    return await service.refresh_tracking_statuses()
