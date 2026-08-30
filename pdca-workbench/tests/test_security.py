from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine

from app.auth.models import User
from app.auth.scope import visible_dealer_names, visible_store_ids
from app.config import Settings
from app.main import app, health
from app.models.dealer_store import DealerStore
from app.models.dealer_assignment import DealerAssignment
from app.models.walkin_daily_report import WalkinDailyReport
from app.logistics.service import _is_delivered, _is_demo_record, _judge_status
from app.vertu.sales import fetch_dealer_sales_orders_sync
from app.pages.router import _serve_module, view_path, walkin_assets, walkin_portal
from app.pages.helpers import inject_vue_shell
from app.pdca.post_router import post_questionnaire
from app.validation import require_iso_date, resolve_file_under
from app.walkin.router import walkin_metrics_summary


class InputValidationTests(unittest.TestCase):
    def test_shared_shell_injection_supports_body_attributes(self):
        source = '<!doctype html><html><body class="dashboard">content</body></html>'
        result = inject_vue_shell(source)
        self.assertIn('<body class="dashboard"><div id="pdca-shell-root"></div>', result)
        self.assertIn('/shared/shell.js?v=3', result)
        self.assertEqual(inject_vue_shell(result).count('pdca-shell-root'), 1)

    def test_require_iso_date_accepts_real_date(self):
        self.assertEqual(require_iso_date("2026-07-13"), "2026-07-13")

    def test_require_iso_date_rejects_traversal_and_impossible_date(self):
        for value in ("../../tmp", "2026-02-31", "2026-7-1"):
            with self.subTest(value=value), self.assertRaises(HTTPException):
                require_iso_date(value)

    def test_resolve_file_under_rejects_sibling_and_parent(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "assets"
            sibling = Path(tmp) / "assets_backup"
            base.mkdir()
            sibling.mkdir()
            inside = base / "ok.html"
            outside = sibling / "secret.html"
            inside.write_text("ok", encoding="utf-8")
            outside.write_text("secret", encoding="utf-8")
            self.assertEqual(resolve_file_under(base, "ok.html"), inside.resolve())
            with self.assertRaises(HTTPException):
                resolve_file_under(base, "../assets_backup/secret.html")

    def test_view_path_rejects_file_outside_allowlist(self):
        outside = Path(__file__).resolve().parents[1] / "README.md"
        with self.assertRaises(HTTPException):
            asyncio.run(view_path(path=str(outside), date="2026-07-13", from_url="", user=User()))

    def test_walkin_html_rejects_encoded_traversal(self):
        with self.assertRaises(HTTPException):
            asyncio.run(
                walkin_assets(
                    rel_path="..%2F..%2F..%2F..%2Fpdca-workbench%2Ffrontend%2Flogin.html",
                    date="2026-07-13",
                    user=User(),
                )
            )

    def test_legacy_walkin_portal_redirects_to_supported_entry(self):
        response = asyncio.run(walkin_portal())
        self.assertEqual(response.status_code, 307)
        self.assertEqual(response.headers["location"], "/walkin-submit/")

    def test_write_route_rejects_invalid_date_before_reading_form(self):
        with self.assertRaises(HTTPException):
            asyncio.run(post_questionnaire(request=None, date="../../outside", _user=User()))

    def test_module_page_falls_back_when_skinning_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            index = Path(tmp) / "index.html"
            index.write_text("<!doctype html><html><body>raw module</body></html>", encoding="utf-8")
            with patch("app.pages.router.bridge.skin_cockpit_html", side_effect=RuntimeError("boom")):
                response = _serve_module(index, "2026-07-13", "模块", "模块")
            self.assertEqual(response.status_code, 200)
            self.assertIn(b"raw module", response.body)

    def test_module_dir_falls_back_to_repository_layout(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            expected = root / "data_platform" / "data_role_pdca_mvp" / "modules" / "walkin_cockpit"
            expected.mkdir(parents=True)
            (expected / "index.html").write_text("ok", encoding="utf-8")
            env = {
                "PDCA_REPO_ROOT": str(root),
                "PDCA_MVP_ROOT": str(root / "wrong-mvp-root"),
            }
            with patch.dict(os.environ, env, clear=False):
                settings = Settings()
            self.assertEqual(settings.walkin_cockpit_dir, expected.resolve())

    def test_legacy_bridge_imports_with_flat_docker_mounts(self):
        """模拟 /mvp 与 /repo 分离时，workbench_data 必须信任 PDCA_REPO_ROOT。"""
        import importlib
        import sys

        scripts_dir = Path(__file__).resolve().parents[2] / "data_platform" / "data_role_pdca_mvp" / "scripts"
        with tempfile.TemporaryDirectory() as repo, patch.dict(
            os.environ,
            {"PDCA_REPO_ROOT": repo},
            clear=False,
        ):
            sys.path.insert(0, str(scripts_dir))
            try:
                sys.modules.pop("workbench_data", None)
                module = importlib.import_module("workbench_data")
                self.assertEqual(module.REPO_ROOT, Path(repo).resolve())
            finally:
                sys.path.remove(str(scripts_dir))
                sys.modules.pop("workbench_data", None)


class DataScopeTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self.session.add_all([
            DealerStore(store_id="store-a", name="Dealer A", sales_owner="alice"),
            DealerStore(store_id="store-b", name="Dealer B", sales_owner="bob"),
        ])
        self.session.commit()

    def tearDown(self):
        self.session.close()
        self.engine.dispose()

    def test_dealer_scope_is_single_store(self):
        user = User(role="dealer", dealer_id="store-a")
        self.assertEqual(visible_store_ids(user, self.session), ["store-a"])
        self.assertEqual(visible_dealer_names(user, self.session), ["Dealer A"])

    def test_sales_scope_uses_store_owner(self):
        user = User(role="sales", username="alice", hashed_password="test", owner_key="alice", data_scope="self")
        self.session.add(user)
        self.session.commit()
        self.session.refresh(user)
        self.session.add(DealerAssignment(user_id=user.id, store_id="store-a"))
        self.session.commit()
        self.assertEqual(visible_store_ids(user, self.session), ["store-a"])

    def test_manager_scope_is_limited_to_explicit_team(self):
        self.assertEqual(visible_store_ids(User(role="manager"), self.session), [])
        manager = User(role="manager", team_key="overseas", data_scope="team")
        self.assertEqual(visible_store_ids(manager, self.session), ["store-a", "store-b"])

    def test_unconfigured_sales_fails_closed(self):
        user = User(role="sales", username="alice", hashed_password="test")
        self.session.add(user)
        self.session.commit()
        self.assertEqual(visible_store_ids(user, self.session), [])

    def test_admin_scope_is_unrestricted(self):
        self.assertIsNone(visible_store_ids(User(role="admin"), self.session))

    def test_meeting_counts_are_recomputed_after_scope_filter(self):
        from app.meeting.router import _apply_meeting_scope
        user = User(role="sales", username="alice-meeting", hashed_password="test", owner_key="alice", sales_name="Alice Sales", data_scope="self")
        self.session.add(user); self.session.commit(); self.session.refresh(user)
        self.session.add(DealerAssignment(user_id=user.id, store_id="store-a")); self.session.commit()
        payload = {"counts": {"total": 2, "customer": 2}, "meetings": [
            {"id": "a", "bucket": "customer", "participants": [{"name": "Alice Sales"}]},
            {"id": "b", "bucket": "customer", "participants": [{"name": "Bob Sales"}]},
        ]}
        scoped = _apply_meeting_scope(payload, user, self.session)
        self.assertEqual([row["id"] for row in scoped["meetings"]], ["a"])
        self.assertEqual(scoped["counts"], {"total": 1, "interview": 0, "report": 0, "customer": 1})


class RouteRegistrationTests(unittest.TestCase):
    def test_public_workbench_routes_are_registered(self):
        paths = {route.path for route in app.routes}
        expected = {
            "/app",
            "/app/",
            "/favicon.ico",
            "/pdca-vps",
            "/dashboard",
            "/signalseller-center/",
            "/api/signalseller/summary",
            "/api/signalseller/customers",
        }
        self.assertFalse(expected - paths)


class ProductionHardeningTests(unittest.TestCase):
    def test_pdca_completion_requires_exact_status_and_title(self):
        from app.legacy import bridge
        legacy = bridge.wb()
        self.assertFalse(legacy.is_done_status("未完成"))
        self.assertTrue(legacy.is_done_status("已完成"))
        rows = [{"title": "跟进印度客户报价补充", "status": "已完成"}]
        self.assertIsNone(legacy.find_matching_task("跟进印度客户报价", rows))
        self.assertIsNotNone(legacy.find_matching_task("跟进印度客户报价补充", rows))

    def test_empty_customer_scope_returns_one_row_per_grade(self):
        from app.legacy import bridge
        rows = bridge.wb().api_customer_center_summary({"data_scope": "self", "allowed_dealer_names": []})
        self.assertEqual([row["level"] for row in rows], ["S", "A", "B", "C"])
        self.assertTrue(all(row["total"] == 0 and row["touched"] is None for row in rows))

    def test_degraded_health_returns_service_unavailable(self):
        settings = SimpleNamespace(environment="production", require_vertu=True)
        with (
            patch("app.main.get_db_mode", return_value="postgresql"),
            patch("app.main.backup_status", return_value={"ok": False}),
            patch("app.main.get_settings", return_value=settings),
            patch("app.main.vertu_health", new=AsyncMock(return_value={"ok": True})),
        ):
            response = asyncio.run(health())
        self.assertEqual(response.status_code, 503)
        self.assertIn(b'"status":"degraded"', response.body)

    def test_security_headers_cover_pages_redirects_and_auth_api(self):
        client = TestClient(app)
        for path in ("/login", "/admin-panel"):
            with self.subTest(path=path):
                response = client.get(path, follow_redirects=False)
                self.assertEqual(response.headers["x-content-type-options"], "nosniff")
                self.assertEqual(response.headers["x-frame-options"], "SAMEORIGIN")
                self.assertIn("frame-ancestors 'self'", response.headers["content-security-policy"])
        auth_response = client.get("/api/auth/config")
        self.assertEqual(auth_response.headers["cache-control"], "no-store, max-age=0")
        self.assertEqual(auth_response.headers["pragma"], "no-cache")

    def test_shared_shell_does_not_require_unsafe_eval(self):
        root = Path(__file__).resolve().parents[1]
        shell = (root / "frontend" / "shared" / "shell.js").read_text(encoding="utf-8")
        response = TestClient(app).get("/login")
        policy = response.headers["content-security-policy"]
        self.assertNotIn("createApp", shell)
        self.assertNotIn("https://", shell)
        self.assertNotIn("unsafe-eval", policy)
        self.assertIn("newPassword.value.length < 12", shell)

    def test_walkin_history_filters_have_accessible_names(self):
        root = Path(__file__).resolve().parents[1]
        source = (root / "frontend" / "walkin_submit.html").read_text(encoding="utf-8")
        self.assertIn('id="hist_month" aria-label="History month"', source)
        self.assertIn('id="hist_dealer" aria-label="History store"', source)

    def test_demo_logistics_records_are_detected(self):
        self.assertTrue(
            _is_demo_record(
                {
                    "tracking_number": "1Z0000000000000000",
                    "customer": "演示客户A",
                    "note": "演示单号",
                }
            )
        )
        self.assertFalse(
            _is_demo_record(
                {
                    "tracking_number": "1Z999AA10123456784",
                    "customer": "真实客户",
                    "note": "等待清关",
                }
            )
        )
        self.assertTrue(_is_demo_record({
            "tracking_number": "NEW123",
            "customer": "新客户",
            "note": "测试",
        }))
        self.assertFalse(_is_demo_record({
            "tracking_number": "REAL456",
            "customer": "真实客户",
            "note": "已测试清关文件，可正常使用",
        }))
        self.assertFalse(_is_demo_record({
            "tracking_number": "REAL789",
            "customer": "真实客户",
            "note": "demo unit 已交付客户",
        }))

    def test_stale_in_transit_shipment_requires_attention(self):
        row = {
            "ship_date": "2026-06-16",
            "current_status": "In Transit - On the Way",
            "expected_status": "in_transit",
        }
        settings = {
            "logistics": {
                "abnormal_keywords": ["exception", "delay"],
                "normal_keywords": ["in transit", "delivered"],
            }
        }
        judgement, reason, _ = _judge_status(row, settings, "2026-07-14")
        self.assertEqual(judgement, "待关注")
        self.assertIn("超过 7 天", reason)
        self.assertFalse(_is_delivered({"status": "Out For Delivery"}))
        self.assertTrue(_is_delivered({"status": "Delivered"}))

    def test_walkin_page_uses_dynamic_month_and_no_visible_mock_metrics(self):
        path = (
            Path(__file__).resolve().parents[2]
            / "data_platform"
            / "data_role_pdca_mvp"
            / "modules"
            / "walkin_cockpit"
            / "index.html"
        )
        source = path.read_text(encoding="utf-8")
        self.assertIn("var INITIAL_MONTH = initialMonthKey();", source)
        self.assertNotIn("selectedMonth: '2026-06'", source)
        self.assertNotIn("2.4 天（mock）", source)
        self.assertNotIn("92%（mock）", source)
        self.assertNotIn("客户管理 8787", source)

        merged_source = (path.parent / "online-merged-insights.js").read_text(encoding="utf-8")
        self.assertNotIn("mockVertuSales", merged_source)
        self.assertNotIn("mockRegionMetrics", merged_source)
        self.assertNotIn("预估占比", merged_source)
        self.assertNotIn("VERTU 推送", merged_source)

    def test_walkin_revenue_is_labeled_usd(self):
        root = Path(__file__).resolve().parents[1]
        form = (root / "frontend" / "walkin_submit.html").read_text(encoding="utf-8")
        self.assertIn("Revenue (USD $)", form)
        self.assertIn("Do not enter VND", form)
        self.assertNotIn("toISOString().slice(0,10)", form)

    def test_deployment_sets_business_timezone(self):
        root = Path(__file__).resolve().parents[1]
        self.assertIn("TZ=Asia/Shanghai", (root / "scripts" / "deploy_remote_docker.ps1").read_text(encoding="utf-8"))
        self.assertIn("TZ: Asia/Shanghai", (root / "docker-compose.yml").read_text(encoding="utf-8"))

    def test_login_copy_matches_password_policy_and_escapes_vps_identity(self):
        root = Path(__file__).resolve().parents[1]
        for name in ("login.html", "login_en.html"):
            source = (root / "frontend" / name).read_text(encoding="utf-8")
            self.assertIn("12", source)
            self.assertNotIn("vpsBox.innerHTML", source)
            self.assertIn('rel="icon"', source)

    def test_walkin_summary_excludes_currency_outlier(self):
        engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
        SQLModel.metadata.create_all(engine)
        with Session(engine) as session:
            session.add_all(
                [
                    DealerStore(store_id="store-a", name="Dealer A", is_active=True),
                    DealerStore(store_id="store-b", name="Dealer B", is_active=True),
                    WalkinDailyReport(
                        report_date="2026-07-05",
                        dealer_id="store-a",
                        dealer_name="Dealer A",
                        deal_count=1,
                        deal_amount_yuan=5618,
                    ),
                    WalkinDailyReport(
                        report_date="2026-07-09",
                        dealer_id="store-b",
                        dealer_name="Dealer B",
                        deal_count=1,
                        deal_amount_yuan=215_900_000,
                    ),
                    WalkinDailyReport(
                        report_date="2026-07-10",
                        dealer_id="store-a",
                        dealer_name="Dealer A",
                        deal_count=3,
                        deal_amount_yuan=2_170_622,
                    ),
                ]
            )
            session.commit()
            with patch(
                "app.walkin.router.get_settings",
                return_value=SimpleNamespace(
                    max_reported_revenue_usd=5_000_000,
                    revenue_review_threshold_usd=1_000_000,
                ),
            ):
                result = asyncio.run(
                    walkin_metrics_summary(
                        user=User(role="admin"),
                        session=session,
                        month="2026-07",
                        start="",
                        end="",
                    )
                )
            self.assertEqual(result["funnel"]["deal_amount_usd"], 5618)
            self.assertEqual(result["data_quality"]["excluded_record_count"], 2)
        engine.dispose()

    def test_vertu_cli_order_rows_are_aggregated_by_customer(self):
        payload = {
            "columns": ["客户名称", "金额", "数量"],
            "rows": [
                ["Dealer A", 12000, 2],
                ["Dealer A", 3000, 1],
                ["Dealer B", 8000, 1],
            ],
        }
        with patch("app.vertu.sales.run_vertu_sync_json", return_value=payload):
            result = fetch_dealer_sales_orders_sync("2026-07-01", "2026-07-14")
        self.assertEqual(result["total"], 23000)
        self.assertEqual(result["dealers"][0]["dealer_name"], "Dealer A")
        self.assertEqual(result["dealers"][0]["qty"], 3)

    def test_dependency_lock_and_dockerignore_exist(self):
        root = Path(__file__).resolve().parents[1]
        lock = (root / "requirements.lock").read_text(encoding="utf-8")
        dockerignore = (root / ".dockerignore").read_text(encoding="utf-8")
        self.assertIn("fastapi==", lock)
        self.assertIn("uvicorn==", lock)
        self.assertIn(".env", dockerignore.splitlines())


if __name__ == "__main__":
    unittest.main()
