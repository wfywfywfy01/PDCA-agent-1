# -*- coding: utf-8 -*-
"""HTML 页面与静态模块托管。"""
from __future__ import annotations

import html as html_lib
import mimetypes
from pathlib import Path
from typing import Annotated
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from loguru import logger
from sqlmodel import Session

from app.auth.deps import get_current_user, require_role
from app.auth.models import User
from app.auth.scope import effective_data_scope, resolve_data_scope
from app.database import get_session
from app.config import get_settings
from app.legacy import bridge
from app.pages.helpers import html_page, inject_vue_shell
from app.validation import require_iso_date, resolve_file_under
from app.acquisition.service import issue_login_ticket

router = APIRouter(tags=["pages"])


def _date_or_today(value: str | None) -> str:
    return require_iso_date(value or bridge.today_text())


def _guess_media(path: Path) -> str:
    media, _ = mimetypes.guess_type(str(path))
    return media or "application/octet-stream"


def _serve_asset(resolver, rel_path: str) -> FileResponse:
    target = resolver(rel_path)
    if not target:
        raise HTTPException(status_code=404)
    return FileResponse(target, media_type=_guess_media(target))


_NO_CACHE_HEADERS = {"Cache-Control": "no-store, no-cache, must-revalidate"}


def _html_file(path: Path) -> FileResponse:
    """返回 HTML 文件，禁止浏览器缓存（内容会随部署更新，避免手机端读到旧版本）。"""
    return FileResponse(path, media_type="text/html; charset=utf-8", headers=_NO_CACHE_HEADERS)


def _serve_skinned_html(path: Path, date_text: str, title: str, feature: str = "") -> HTMLResponse:
    if not path.is_file():
        logger.error("模块页面文件不存在: {}", path)
        return _unavailable(feature or title)
    try:
        source = path.read_text(encoding="utf-8")
        html = bridge.skin_cockpit_html(source, date_text, title)
        return html_page(html)
    except Exception:
        logger.exception("模块页面套壳失败，回退到原始页面: {}", path)
        try:
            return html_page(path.read_text(encoding="utf-8"))
        except Exception:
            logger.exception("模块原始页面读取失败: {}", path)
        return _unavailable(feature or title)


def _redirect_msg(path: str, date_text: str, message: str) -> RedirectResponse:
    return RedirectResponse(f"{path}?{urlencode({'date': date_text, 'message': message})}")


def _unavailable(feature: str = "") -> HTMLResponse:
    """Bridge/MVP 文件不可用时返回友好提示页，而不是 500。"""
    label = feature or "此功能"
    html = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8">
<title>功能不可用</title>
<style>body{{font-family:system-ui,sans-serif;display:flex;align-items:center;
justify-content:center;min-height:100vh;margin:0;background:#0b0d13;color:#94a3b8}}
.box{{text-align:center;padding:48px;max-width:480px}}
h1{{font-size:2rem;color:#4e9ef5;margin-bottom:.5rem}}
p{{line-height:1.7;margin:.5rem 0}}
a{{color:#4e9ef5;text-decoration:none}}</style></head>
<body><div class="box">
<h1>⚙️</h1>
<p><strong style="color:#e2e8f0">{label}</strong> 在当前部署环境中不可用。</p>
<p>This feature is not available in the current deployment.</p>
<p style="margin-top:1.5rem"><a href="/">← 返回首页</a></p>
</div></body></html>"""
    return HTMLResponse(html, status_code=200)


def _safe_bridge_page(fn, *args, feature: str = "", **kwargs) -> HTMLResponse:
    """执行 bridge 渲染函数，失败时返回友好提示而非 500。"""
    try:
        return html_page(fn(*args, **kwargs))
    except Exception:
        logger.exception("Bridge 页面渲染失败: feature={} fn={}", feature, getattr(fn, "__name__", fn))
        return _unavailable(feature)


def _customer_summary_page(rows: list[dict], date_text: str) -> HTMLResponse:
    cards = "".join(
        f"""<article><strong>{html_lib.escape(str(row.get('level') or '—'))} 类</strong>
        <b>{int(row.get('total') or 0)}</b>
        <span>已触达 {int(row.get('touched') or 0)} / 目标 {int(row.get('target') or 0)}</span></article>"""
        for row in rows[:8]
    )
    content = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
    <title>客户管理 · PDCA 工作台</title><style>
    body{{margin:0;background:#f4f7fb;color:#172033;font-family:system-ui,sans-serif}}
    main{{max-width:1080px;margin:0 auto;padding:36px 24px}}
    h1{{margin:0 0 8px}}p{{color:#64748b}}.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:16px;margin:28px 0}}
    article{{background:#fff;border:1px solid #e2e8f0;border-radius:14px;padding:22px;display:grid;gap:8px}}
    article strong{{color:#2563eb}}article b{{font-size:32px}}article span{{color:#64748b;font-size:14px}}
    nav{{display:flex;gap:12px;flex-wrap:wrap}}nav a{{background:#2563eb;color:#fff;text-decoration:none;padding:10px 16px;border-radius:9px}}
    nav a.alt{{background:#fff;color:#2563eb;border:1px solid #bfdbfe}}</style></head>
    <body><main><h1>客户管理</h1><p>{date_text} · 客户分层与触达概览</p>
    <section class="grid">{cards or '<article><span>暂无客户分层数据</span></article>'}</section>
    <nav><a href="/dealer-sellin/">查看经销商进货</a><a class="alt" href="/walkin-cockpit/">查看客流与终销</a></nav>
    </main></body></html>"""
    return html_page(content)


def _customer_acquisition_page(frame_url: str, date_text: str) -> HTMLResponse:
    safe_url = html_lib.escape(frame_url, quote=True)
    content = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
    <meta name="viewport" content="width=device-width,initial-scale=1">
    <title>自动化获客 · PDCA 工作台</title><style>
    *{{box-sizing:border-box}}body{{margin:0;background:#0b0d13;color:#e2e8f0;font-family:system-ui,sans-serif}}
    .module-head{{display:flex;align-items:center;justify-content:space-between;gap:16px;padding:12px 20px;background:#111827;border-bottom:1px solid #253047}}
    .module-head h1{{font-size:18px;margin:0}}.module-head p{{font-size:13px;color:#94a3b8;margin:3px 0 0}}
    .actions{{display:flex;gap:8px;flex-wrap:wrap}}.actions a{{color:#bfdbfe;text-decoration:none;border:1px solid #334155;border-radius:8px;padding:8px 12px;font-size:13px}}
    iframe{{display:block;width:100%;height:calc(100vh - 118px);min-height:620px;border:0;background:#fff}}
    @media(max-width:640px){{.module-head{{align-items:flex-start;padding:10px 12px}}.module-head p{{display:none}}iframe{{height:calc(100vh - 104px);min-height:540px}}}}
    </style></head><body>
    <header class="module-head"><div><h1>自动化获客与客户管理</h1><p>{html_lib.escape(date_text)} · 线索、客户、触达、跟进和 PDCA 漏斗</p></div>
    <nav class="actions"><a href="/signalseller-center/">SignalSeller</a><a href="/customer-mgmt?legacy=1">旧版概览</a></nav></header>
    <iframe title="自动化获客与客户管理" src="{safe_url}" referrerpolicy="no-referrer"
      sandbox="allow-forms allow-modals allow-popups allow-same-origin allow-scripts allow-downloads"
      allow="clipboard-write"></iframe>
    </body></html>"""
    return html_page(content)


@router.get("/")
async def home(
    date: str | None = None,
    user: Annotated[User, Depends(get_current_user)] = None,
):
    settings = get_settings()
    if settings.home_redirect:
        return RedirectResponse(settings.home_redirect)
    return _home_dashboard(settings)


def _home_dashboard(settings):
    index = settings.home_dashboard_dir / "index.html"
    if not index.is_file():
        # MVP 目录未部署，降级到录入页
        return _unavailable("经营首页")
    return html_page(index.read_text(encoding="utf-8"))


@router.get("/app")
@router.get("/app/")
async def app_home(
    user: Annotated[User, Depends(get_current_user)] = None,
):
    """兼容历史首页地址，避免 PDCA_HOME_REDIRECT=/app/ 登录后落到 404。"""
    return _home_dashboard(get_settings())


@router.get("/login")
async def login_page():
    settings = get_settings()
    login_name = "login_en.html" if settings.home_redirect == "/walkin-submit" else "login.html"
    login = settings.frontend_dir / login_name
    if login.is_file():
        return _html_file(login)
    return HTMLResponse("<p>login.html 缺失</p>")


@router.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return Response(status_code=204)


@router.get("/home-classic")
async def home_classic(
    date: str | None = None,
    message: str = Query(""),
    user: Annotated[User, Depends(get_current_user)] = None,
):
    return _safe_bridge_page(bridge.render_home, _date_or_today(date), message, feature="经营看板")


@router.get("/pdca-vps")
async def pdca_vps(
    date: str | None = None,
    message: str = Query(""),
    user: Annotated[User, Depends(get_current_user)] = None,
):
    if effective_data_scope(user) != "all":
        raise HTTPException(status_code=403, detail="旧版 PDCA 页面包含全局文件，范围账号请使用首页今日任务")
    return _safe_bridge_page(
        bridge.render_pdca_vps,
        _date_or_today(date),
        message,
        feature="PDCA 日结",
    )


@router.get("/dashboard")
async def legacy_dashboard(
    date: str | None = None,
    user: Annotated[User, Depends(get_current_user)] = None,
):
    if effective_data_scope(user) != "all":
        raise HTTPException(status_code=403, detail="历史静态看板无法安全分割，范围账号请使用经营首页")
    """只展示已有日看板；GET 请求不隐式运行数据流水线。"""
    date_text = _date_or_today(date)
    try:
        dashboard = bridge.output_dir(date_text) / "dashboard.html"
    except Exception:
        logger.exception("解析日看板目录失败: date={}", date_text)
        return _unavailable("数据看板")
    if not dashboard.is_file():
        return _redirect_msg("/", date_text, "这个日期还没有看板，请先运行当天 PDCA。")
    return _serve_skinned_html(dashboard, date_text, "数据看板", "数据看板")


@router.get("/questionnaire")
async def questionnaire(
    date: str | None = None,
    message: str = Query(""),
    user: Annotated[User, Depends(get_current_user)] = None,
):
    if effective_data_scope(user) != "all":
        raise HTTPException(status_code=403, detail="旧版共享问卷尚未按账号分库，已对范围账号关闭")
    return _safe_bridge_page(bridge.render_questionnaire, _date_or_today(date), message, feature="问卷")


@router.get("/todos")
async def todos_page(
    date: str | None = None,
    message: str = Query(""),
    user: Annotated[User, Depends(get_current_user)] = None,
):
    if effective_data_scope(user) != "all":
        raise HTTPException(status_code=403, detail="旧版共享待办尚未按账号分库，已对范围账号关闭")
    return _safe_bridge_page(bridge.render_todos, _date_or_today(date), message, feature="待办中心")


@router.get("/logistics")
async def logistics(
    date: str | None = None,
    message: str = Query(""),
    user: Annotated[User, Depends(get_current_user)] = None,
):
    if user.role not in {"sales", "admin"}:
        raise HTTPException(status_code=403, detail="仅销售或管理员可录入物流单号")
    try:
        date_text = _date_or_today(date)
        if effective_data_scope(user) != "all":
            sales_label = getattr(user, "sales_name", "") or user.display_name or user.username
            action = f"/logistics?date={quote(date_text)}"
            return html_page(f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><title>录入物流单号</title>
<style>body{{font-family:system-ui,sans-serif;background:#f4f7fb;color:#172033;margin:0}}main{{max-width:820px;margin:32px auto;padding:24px}}section{{background:#fff;border:1px solid #e2e8f0;border-radius:14px;padding:24px}}.grid{{display:grid;grid-template-columns:1fr 1fr;gap:16px}}label{{display:grid;gap:6px;color:#475569}}input,select{{padding:10px;border:1px solid #cbd5e1;border-radius:8px}}button,a{{display:inline-block;margin-top:18px;padding:10px 16px;border-radius:8px;border:0;background:#2563eb;color:white;text-decoration:none}}a{{background:#64748b;margin-left:8px}}small{{color:#64748b}}@media(max-width:700px){{.grid{{grid-template-columns:1fr}}}}</style></head>
<body><main><section><h1>录入物流单号</h1><p><small>销售身份由服务器锁定为 {html_lib.escape(sales_label)}；历史运单请到物流中心查看当前账号范围。</small></p>
<form method="post" action="{action}"><div class="grid">
<label>物流单号<input name="tracking_number" required></label><label>承运商<select name="carrier"><option>UPS</option><option>FedEx</option><option>DHL</option><option>SF</option></select></label>
<label>客户<input name="customer"></label><label>发货日期<input type="date" name="ship_date" value="{date_text}"></label>
<label>当前状态<input name="current_status" placeholder="不知道可留空"></label><label>预期状态<input name="expected_status"></label>
<label>备注<input name="note"></label></div><button type="submit">保存</button><a href="/logistics-center/?date={date_text}">查看物流进展</a></form>
</section></main></body></html>""")
        html = bridge.render_logistics(date_text, message)
        html = html.replace(
            "录入物流单号</h2>",
            '录入物流单号</h2><p style="margin:6px 0 0"><a href="/logistics-center/?date='
            + date_text
            + '">查看物流进展看板 →</a></p>',
            1,
        )
        if user.role == "sales":
            sales_label = getattr(user, "sales_name", "") or user.display_name
            html = html.replace(
                '<label>销售<input name="salesperson"',
                f'<label>销售<input name="salesperson" value="{sales_label}" readonly',
                1,
            )
        return html_page(html)
    except Exception:
        return _unavailable("物流录入")


@router.get("/im-unread")
async def im_unread(
    date: str | None = None,
    message: str = Query(""),
    user: Annotated[User, Depends(get_current_user)] = None,
):
    if effective_data_scope(user) != "all":
        raise HTTPException(status_code=403, detail="服务器共享 IM 会话不代表当前登录用户，已对范围账号关闭")
    return _safe_bridge_page(bridge.render_im_unread, _date_or_today(date), message, feature="IM 未读")


@router.get("/customer-mgmt")
async def customer_mgmt(
    date: str | None = None,
    legacy: bool = Query(False),
    user: Annotated[User, Depends(get_current_user)] = None,
    session: Annotated[Session, Depends(get_session)] = None,
):
    try:
        date_text = _date_or_today(date)
        settings = get_settings()
        if (
            settings.acquisition_enabled
            and not legacy
            and user.role in {"admin", "manager", "sales"}
        ):
            scope = resolve_data_scope(user, session)
            code = issue_login_ticket(user, scope, session)
            frame_url = f"{settings.acquisition_url}/?{urlencode({'pdca_code': code})}#dashboard"
            return _customer_acquisition_page(frame_url, date_text)
        if settings.environment == "production":
            session_user = {
                "username": user.username,
                "display_name": user.display_name,
                "sales_name": getattr(user, "sales_name", "") or "",
                "role": user.role,
            }
            session_user.update(resolve_data_scope(user, session).as_session_user_fields())
            rows = bridge.api_customer_center_summary(session_user=session_user)
            return _customer_summary_page(rows if isinstance(rows, list) else [], date_text)
        err = bridge.ensure_customer_server()
        if err:
            return _redirect_msg("/", date_text, err)
        return html_page(bridge.render_customer_mgmt_frame(date_text))
    except Exception:
        return _unavailable("客户管理")


@router.get("/agent-soul")
async def agent_soul(
    agent: str = Query(""),
    date: str | None = None,
    message: str = Query(""),
    user: Annotated[User, Depends(require_role("admin"))] = None,
):
    return _safe_bridge_page(bridge.render_agent_soul, _date_or_today(date), agent, message, feature="Agent 管理")


@router.get("/agent-edit")
async def agent_edit(
    agent: str = Query(""),
    file: str = Query("SOUL.md"),
    date: str | None = None,
    message: str = Query(""),
    user: Annotated[User, Depends(require_role("admin"))] = None,
):
    return _safe_bridge_page(bridge.render_agent_edit, _date_or_today(date), agent, file, message, feature="Agent 编辑")


@router.get("/view-path")
async def view_path(
    path: str = Query(""),
    date: str | None = None,
    from_url: str = Query("", alias="from"),
    user: Annotated[User, Depends(require_role("admin"))] = None,
):
    from app.files.router import _resolve_safe

    resolved = _resolve_safe(path)
    return _safe_bridge_page(
        bridge.render_view_path,
        _date_or_today(date),
        str(resolved),
        from_url,
        feature="文件查看",
    )


@router.get("/open")
async def open_target(
    target: str = Query(""),
    date: str | None = None,
    user: Annotated[User, Depends(require_role("admin"))] = None,
):
    date_text = _date_or_today(date)
    file_path = bridge.latest_output_file(date_text, target)
    if file_path and file_path.is_file():
        return RedirectResponse(f"/api/files/download?path={quote(str(file_path))}")
    return _redirect_msg("/", date_text, "文件还不存在，请先运行今日 PDCA。")


@router.get("/open-path")
async def open_path_route(
    path: str = Query(""),
    date: str | None = None,
    user: Annotated[User, Depends(require_role("admin"))] = None,
):
    """打开一个文件（跳转到受控下载）。

    之前这里只判断 Path(path).resolve().is_file()，不检查是否在允许目录内——
    等于给任何已登录用户（包括 viewer）开了一个"服务器上任意绝对路径是否存在"的探测接口
    （存在返回跳转、不存在返回提示，响应可区分）。下载本身在 /api/files/download 有目录白名单，
    但这道口子在那之前就已经泄露了存在性，这里必须用同一套白名单先过滤。
    """
    from app.files.router import _resolve_safe

    date_text = _date_or_today(date)
    try:
        resolved = _resolve_safe(path)
        return RedirectResponse(f"/api/files/download?path={quote(str(resolved))}")
    except HTTPException:
        pass
    return _redirect_msg("/", date_text, "文件还不存在。")


@router.get("/open-im-channel")
async def open_im_channel(
    channel_id: str = Query(""),
    date: str | None = None,
    user: Annotated[User, Depends(require_role("admin"))] = None,
):
    date_text = _date_or_today(date)
    msg = bridge.open_im_channel(channel_id)
    return _redirect_msg("/im-unread", date_text, msg)



@router.get("/workbench-cockpit-shell.css")
async def cockpit_shell_css():
    settings = get_settings()
    path = settings.home_dashboard_dir / "workbench-cockpit-shell.css"
    if not path.is_file():
        raise HTTPException(status_code=404)
    return FileResponse(path, media_type="text/css; charset=utf-8")


@router.get("/dashboard-theme.css")
async def dashboard_theme_css():
    settings = get_settings()
    path = settings.home_dashboard_dir / "workbench-unified.css"
    if not path.is_file():
        raise HTTPException(status_code=404)
    return FileResponse(path, media_type="text/css; charset=utf-8")


def _serve_module(index: Path, date_text: str, title: str, feature: str) -> HTMLResponse:
    """通用模块页面加载，文件不存在时返回友好提示。"""
    if not index.is_file():
        logger.error("模块入口文件不存在: feature={} path={}", feature, index)
        return _unavailable(feature)
    try:
        source = index.read_text(encoding="utf-8")
        html = bridge.skin_cockpit_html(source, date_text, title)
        return html_page(html)
    except Exception:
        logger.exception("模块页面套壳失败，回退到原始页面: feature={} path={}", feature, index)
        try:
            return html_page(index.read_text(encoding="utf-8"))
        except Exception:
            logger.exception("模块原始页面读取失败: feature={} path={}", feature, index)
        return _unavailable(feature)


@router.get("/walkin-cockpit")
@router.get("/walkin-cockpit/")
async def walkin_index(
    date: str | None = None,
    user: Annotated[User, Depends(get_current_user)] = None,
):
    settings = get_settings()
    return _serve_module(
        settings.walkin_cockpit_dir / "index.html",
        _date_or_today(date),
        "客流接待台", "客流接待台",
    )


@router.get("/walkin-portal")
@router.get("/walkin-portal/")
async def walkin_portal() -> RedirectResponse:
    """Keep historical walk-in portal bookmarks on the supported entry page."""
    return RedirectResponse("/walkin-submit/", status_code=307)


@router.get("/walkin-cockpit/{rel_path:path}")
async def walkin_assets(
    rel_path: str,
    date: str | None = None,
    user: Annotated[User, Depends(get_current_user)] = None,
):
    if rel_path.endswith(".html"):
        settings = get_settings()
        path = resolve_file_under(settings.walkin_cockpit_dir, rel_path)
        return _serve_module(path, _date_or_today(date), "客流分析台", "客流分析台")
    try:
        return _serve_asset(bridge.resolve_walkin_asset, rel_path)
    except Exception:
        raise HTTPException(status_code=404)



@router.get("/online-cockpit")
@router.get("/online-cockpit/")
@router.get("/online-cockpit/{rel_path:path}")
async def online_redirect(date: str | None = None):
    q = f"?date={_date_or_today(date)}" if date else ""
    return RedirectResponse(f"/walkin-cockpit/{q}#oi-merged")


@router.get("/logistics-center")
@router.get("/logistics-center/")
async def logistics_center_index(
    date: str | None = None,
    user: Annotated[User, Depends(get_current_user)] = None,
):
    settings = get_settings()
    return _serve_module(
        settings.logistics_center_dir / "index.html",
        _date_or_today(date),
        "物流进展", "物流中心",
    )


@router.get("/logistics-center/{rel_path:path}")
async def logistics_center_assets(
    rel_path: str,
    user: Annotated[User, Depends(get_current_user)] = None,
):
    settings = get_settings()
    target = resolve_file_under(settings.logistics_center_dir, rel_path)
    return FileResponse(target, media_type=_guess_media(target))


@router.get("/meeting-center")
@router.get("/meeting-center/")
async def meeting_center_index(
    date: str | None = None,
    user: Annotated[User, Depends(get_current_user)] = None,
):
    settings = get_settings()
    return _serve_module(
        settings.meeting_center_dir / "index.html",
        _date_or_today(date),
        "会议中心", "会议中心",
    )


@router.get("/meeting-center/{rel_path:path}")
async def meeting_center_assets(
    rel_path: str,
    user: Annotated[User, Depends(get_current_user)] = None,
):
    settings = get_settings()
    target = resolve_file_under(settings.meeting_center_dir, rel_path)
    return FileResponse(target, media_type=_guess_media(target))


@router.get("/onboarding-center")
@router.get("/onboarding-center/")
async def onboarding_center_index(
    date: str | None = None,
    user: Annotated[User, Depends(get_current_user)] = None,
):
    settings = get_settings()
    return _serve_module(
        settings.onboarding_center_dir / "index.html",
        _date_or_today(date),
        "新人培训", "新人培训",
    )


@router.get("/onboarding-center/{rel_path:path}")
async def onboarding_center_assets(
    rel_path: str,
    user: Annotated[User, Depends(get_current_user)] = None,
):
    settings = get_settings()
    target = resolve_file_under(settings.onboarding_center_dir, rel_path)
    return FileResponse(target, media_type=_guess_media(target))


@router.get("/signalseller-center")
@router.get("/signalseller-center/")
async def signalseller_center_index(
    date: str | None = None,
    user: Annotated[User, Depends(get_current_user)] = None,
):
    settings = get_settings()
    return _serve_module(
        settings.signalseller_center_dir / "index.html",
        _date_or_today(date),
        "获客指挥", "SignalSeller 获客指挥",
    )


@router.get("/signalseller-center/{rel_path:path}")
async def signalseller_center_assets(
    rel_path: str,
    user: Annotated[User, Depends(get_current_user)] = None,
):
    settings = get_settings()
    target = resolve_file_under(settings.signalseller_center_dir, rel_path)
    return FileResponse(target, media_type=_guess_media(target))


@router.get("/shared/{rel_path:path}")
async def shared_assets(rel_path: str):
    settings = get_settings()
    target = resolve_file_under(settings.frontend_dir / "shared", rel_path)
    return FileResponse(target, media_type=_guess_media(target))


@router.get("/admin-panel")
@router.get("/admin-panel/")
async def admin_panel_page(
    user: Annotated[User, Depends(require_role("admin"))],
):
    settings = get_settings()
    html_path = settings.frontend_dir / "admin_panel.html"
    if not html_path.is_file():
        raise HTTPException(status_code=404)
    return _html_file(html_path)


@router.get("/dealer-sellin")
@router.get("/dealer-sellin/")
async def dealer_sellin_page(
    user: Annotated[User, Depends(get_current_user)] = None,
):
    settings = get_settings()
    html_path = settings.frontend_dir / "dealer_sellin.html"
    if not html_path.is_file():
        raise HTTPException(status_code=404, detail="dealer_sellin.html 缺失")
    return _html_file(html_path)



@router.get("/store-five-kit")
@router.get("/store-five-kit/")
async def store_five_kit_page(
    user: Annotated[User, Depends(get_current_user)] = None,
):
    """各门店五件套数据展示页面。"""
    settings = get_settings()
    html_path = settings.frontend_dir / "store_five_kit.html"
    if not html_path.is_file():
        raise HTTPException(status_code=404, detail="store_five_kit.html 缺失")
    return _html_file(html_path)


@router.get("/walkin-submit")
@router.get("/walkin-submit/")
async def walkin_submit_page(
    user: Annotated[User, Depends(get_current_user)] = None,
):
    """五件套录入系统（经销商独立门户，与PDCA工作台分开）。"""
    settings = get_settings()
    html_path = settings.frontend_dir / "walkin_submit.html"
    if not html_path.is_file():
        raise HTTPException(status_code=404, detail="walkin_submit.html 缺失")
    return _html_file(html_path)
