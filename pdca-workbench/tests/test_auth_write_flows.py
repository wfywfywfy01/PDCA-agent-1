from __future__ import annotations

import tempfile
import unittest
import sys
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine, select

from app.auth.models import User
from app.auth.security import hash_password, verify_password
from app.database import get_session
from app.main import app
from app.logistics import service as logistics_service
from app.models.daily_report import DailyReport
from app.models.dealer_store import DealerStore
from app.models.dealer_assignment import DealerAssignment
from app.models.logistics import LogisticsShipment
from app.models.walkin_daily_report import WalkinDailyReport


class AuthAndWriteFlowTests(unittest.TestCase):
    """Exercise real auth and representative writes without external services."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        repo_root = Path(__file__).resolve().parents[2]
        database_path = Path(self.temp_dir.name) / "pdca-test.sqlite"
        self.engine = create_engine(
            f"sqlite:///{database_path.as_posix()}",
            connect_args={"check_same_thread": False},
        )
        SQLModel.metadata.create_all(self.engine)

        self.settings = SimpleNamespace(
            auth_mode="local",
            trust_proxy_headers=False,
            secure_cookies=False,
            access_token_expire_minutes=60,
            secret_key="test-secret-key-that-is-long-enough-for-jwt",
            algorithm="HS256",
            vps_login_url="https://example.invalid",
            max_reported_revenue_usd=5_000_000,
            revenue_review_threshold_usd=1_000_000,
            repo_root=repo_root,
            scripts_dir=(
                repo_root
                / "data_platform"
                / "data_role_pdca_mvp"
                / "scripts"
            ),
        )
        self.patches = ExitStack()
        for target in (
            "app.main.get_settings",
            "app.auth.deps.get_settings",
            "app.auth.router.get_settings",
            "app.auth.security.get_settings",
            "app.legacy.bridge.get_settings",
        ):
            self.patches.enter_context(patch(target, return_value=self.settings))
        # Audit logs and legacy-form DB mirrors must use the same temporary DB.
        self.patches.enter_context(patch("app.audit.get_engine", return_value=self.engine))
        self.patches.enter_context(patch("app.models.writes.get_engine", return_value=self.engine))

        def override_session():
            with Session(self.engine) as session:
                yield session

        app.dependency_overrides[get_session] = override_session
        self.client = TestClient(app)
        self._seed_data()

    def tearDown(self):
        self.client.close()
        app.dependency_overrides.clear()
        self.patches.close()
        self.engine.dispose()
        self.temp_dir.cleanup()

    def _seed_data(self):
        password = hash_password("Correct-password-123")
        with Session(self.engine) as session:
            session.add_all(
                [
                    User(
                        username="admin",
                        hashed_password=password,
                        role="admin",
                        display_name="Admin",
                        data_scope="all",
                        must_change_password=False,
                    ),
                    User(
                        username="forced-admin",
                        hashed_password=password,
                        role="admin",
                        display_name="Forced Admin",
                        must_change_password=True,
                    ),
                    User(
                        username="viewer",
                        hashed_password=password,
                        role="viewer",
                        display_name="Viewer",
                        must_change_password=False,
                    ),
                    User(
                        username="sales",
                        hashed_password=password,
                        role="sales",
                        display_name="Sales",
                        sales_name="Alice Sales",
                        owner_key="alice-owner",
                        data_scope="self",
                        must_change_password=False,
                    ),
                    User(
                        username="dealer",
                        hashed_password=password,
                        role="dealer",
                        display_name="Dealer",
                        dealer_id="store-a",
                        must_change_password=False,
                    ),
                    User(
                        username="sales-bob",
                        hashed_password=password,
                        role="sales",
                        display_name="Bob",
                        sales_name="Bob Sales",
                        owner_key="bob-owner",
                        data_scope="self",
                        must_change_password=False,
                    ),
                    DealerStore(
                        store_id="store-a",
                        name="Dealer A",
                        sales_owner="alice-owner",
                        team_key="overseas",
                        is_active=True,
                    ),
                    DealerStore(
                        store_id="store-b",
                        name="Dealer B",
                        sales_owner="bob-owner",
                        team_key="overseas",
                        is_active=True,
                    ),
                ]
            )
            session.commit()
            sales = session.exec(select(User).where(User.username == "sales")).one()
            bob = session.exec(select(User).where(User.username == "sales-bob")).one()
            session.add_all([
                DealerAssignment(user_id=sales.id, store_id="store-a"),
                DealerAssignment(user_id=bob.id, store_id="store-b"),
            ])
            session.commit()

    def _login(self, username: str, password: str = "Correct-password-123"):
        return self.client.post(
            "/api/auth/login",
            json={"username": username, "password": password},
        )

    def test_login_success_and_failure(self):
        failed = self._login("viewer", "wrong-password")
        self.assertEqual(failed.status_code, 401)
        self.assertNotIn("pdca_token", failed.cookies)

        success = self._login("viewer")
        self.assertEqual(success.status_code, 200)
        self.assertEqual(success.json()["user"]["username"], "viewer")
        self.assertFalse(success.json()["must_change_password"])
        self.assertTrue(success.cookies.get("pdca_token"))

        me = self.client.get("/api/auth/me")
        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.json()["role"], "viewer")

    def test_public_vps_probe_does_not_expose_server_session(self):
        with patch("app.auth.vps_identity.fetch_vps_me_payload") as fetch_vps:
            response = self.client.get("/api/auth/vps-check")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["ok"])
        fetch_vps.assert_not_called()

    def test_forced_password_change_blocks_business_api_then_rotates_token(self):
        login = self._login("forced-admin")
        self.assertEqual(login.status_code, 200)
        self.assertTrue(login.json()["must_change_password"])

        blocked = self.client.get("/api/admin/diagnostics")
        self.assertEqual(blocked.status_code, 403)

        changed = self.client.post(
            "/api/auth/change-password",
            json={
                "old_password": "Correct-password-123",
                "new_password": "Changed-password-456",
            },
        )
        self.assertEqual(changed.status_code, 200)
        self.assertTrue(changed.json()["ok"])

        me = self.client.get("/api/auth/me")
        self.assertEqual(me.status_code, 200)
        self.assertFalse(me.json()["must_change_password"])
        with Session(self.engine) as session:
            user = session.exec(select(User).where(User.username == "forced-admin")).one()
            self.assertEqual(user.pwd_version, 1)
            self.assertFalse(user.must_change_password)
            self.assertTrue(verify_password("Changed-password-456", user.hashed_password))

    def test_representative_role_guards_return_401_and_403(self):
        anonymous = self.client.get("/api/admin/diagnostics")
        self.assertEqual(anonymous.status_code, 401)

        self.assertEqual(self._login("viewer").status_code, 200)
        forbidden = self.client.get("/api/admin/diagnostics")
        self.assertEqual(forbidden.status_code, 403)

        write_forbidden = self.client.post(
            "/api/walkin-metrics",
            json={
                "report_date": "2024-01-02",
                "dealer_id": "store-a",
                "dealer_name": "Dealer A",
            },
        )
        self.assertEqual(write_forbidden.status_code, 403)

    def test_questionnaire_write_saves_file_and_database_mirror(self):
        self.assertEqual(self._login("admin").status_code, 200)
        questionnaire_path = Path(self.temp_dir.name) / "2024-01-02_questionnaire.md"

        def save_questionnaire(date_text: str, form: dict[str, list[str]]):
            answer = (form.get("answer", [""])[0] or "").strip()
            questionnaire_path.write_text(
                f"# Questionnaire {date_text}\n\n{answer}\n",
                encoding="utf-8",
            )

        with (
            patch(
                "app.pdca.post_router.bridge.save_questionnaire",
                side_effect=save_questionnaire,
            ),
            patch(
                "app.pdca.post_router.bridge.questionnaire_path",
                return_value=questionnaire_path,
            ),
        ):
            response = self.client.post(
                "/questionnaire?date=2024-01-02",
                data={"answer": "Customer follow-up completed"},
                follow_redirects=False,
            )

        self.assertEqual(response.status_code, 303)
        self.assertTrue(questionnaire_path.is_file())
        self.assertIn("Customer follow-up completed", questionnaire_path.read_text(encoding="utf-8"))
        with Session(self.engine) as session:
            row = session.exec(
                select(DailyReport).where(
                    DailyReport.report_date == "2024-01-02",
                    DailyReport.report_type == "questionnaire",
                )
            ).one()
            self.assertIn("Customer follow-up completed", row.content)
            self.assertEqual(row.file_path, str(questionnaire_path))

    def test_logistics_write_saves_file_and_database_record(self):
        self.assertEqual(self._login("sales").status_code, 200)
        logistics_path = Path(self.temp_dir.name) / "logistics.csv"

        def append_logistics(date_text: str, form: dict[str, list[str]]):
            fields = [
                date_text,
                (form.get("tracking_number", [""])[0] or "").strip(),
                (form.get("salesperson", [""])[0] or "").strip(),
            ]
            logistics_path.write_text(",".join(fields) + "\n", encoding="utf-8")

        with patch(
            "app.pdca.post_router.bridge.append_logistics",
            side_effect=append_logistics,
        ):
            response = self.client.post(
                "/logistics?date=2024-01-02",
                data={
                    "tracking_number": "TRACK-TEST-001",
                    "carrier": "DHL",
                    "customer": "Customer A",
                    "ship_date": "2024-01-02",
                    "expected_status": "in_transit",
                },
                follow_redirects=False,
            )

        self.assertEqual(response.status_code, 303)
        self.assertIn("Alice Sales", logistics_path.read_text(encoding="utf-8"))
        with Session(self.engine) as session:
            row = session.exec(
                select(LogisticsShipment).where(
                    LogisticsShipment.tracking_number == "TRACK-TEST-001"
                )
            ).one()
            self.assertEqual(row.salesperson, "Alice Sales")
            self.assertEqual(row.customer, "Customer A")

    def test_walkin_write_persists_daily_metrics(self):
        self.assertEqual(self._login("dealer").status_code, 200)
        response = self.client.post(
            "/api/walkin-metrics",
            json={
                "report_date": "2024-01-02",
                "dealer_id": "store-a",
                "dealer_name": "Dealer A",
                "walkin_visits": 7,
                "cross_visits": 2,
                "online_visits": 3,
                "recruit_visits": 1,
                "existing_visits": 4,
                "touch_count": 9,
                "use_count": 5,
                "wechat_add_count": 6,
                "deal_count": 2,
                "deal_amount_yuan": 1280.5,
                "notes": "test-only submission",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        with Session(self.engine) as session:
            row = session.exec(
                select(WalkinDailyReport).where(
                    WalkinDailyReport.report_date == "2024-01-02",
                    WalkinDailyReport.dealer_id == "store-a",
                )
            ).one()
            self.assertEqual(row.submitted_by, "dealer")
            self.assertEqual(row.total_visits, 17)
            self.assertEqual(row.deal_count, 2)
            self.assertEqual(row.deal_amount_yuan, 1280.5)

    def test_walkin_write_uses_canonical_active_store_and_valid_date(self):
        self.assertEqual(self._login("dealer").status_code, 200)
        response = self.client.post(
            "/api/walkin-metrics",
            json={
                "report_date": "2024-01-03",
                "dealer_id": "store-a",
                "dealer_name": "Forged Dealer Name",
            },
        )
        self.assertEqual(response.status_code, 200)
        with Session(self.engine) as session:
            row = session.exec(
                select(WalkinDailyReport).where(
                    WalkinDailyReport.report_date == "2024-01-03",
                    WalkinDailyReport.dealer_id == "store-a",
                )
            ).one()
            self.assertEqual(row.dealer_name, "Dealer A")
            store = session.exec(select(DealerStore).where(DealerStore.store_id == "store-a")).one()
            store.is_active = False
            session.add(store)
            session.commit()

        inactive = self.client.post(
            "/api/walkin-metrics",
            json={"report_date": "2024-01-04", "dealer_id": "store-a", "dealer_name": "Dealer A"},
        )
        self.assertEqual(inactive.status_code, 422)
        invalid_date = self.client.post(
            "/api/walkin-metrics",
            json={"report_date": "2024-99-99", "dealer_id": "store-a", "dealer_name": "Dealer A"},
        )
        self.assertEqual(invalid_date.status_code, 422)

    def test_admin_can_deactivate_dealer_after_store_is_disabled(self):
        with Session(self.engine) as session:
            store = session.exec(select(DealerStore).where(DealerStore.store_id == "store-a")).one()
            store.is_active = False
            session.add(store)
            session.commit()
        self.assertEqual(self._login("admin").status_code, 200)
        response = self.client.patch("/api/admin/users/dealer", json={"is_active": False})
        self.assertEqual(response.status_code, 200)
        with Session(self.engine) as session:
            dealer = session.exec(select(User).where(User.username == "dealer")).one()
            self.assertFalse(dealer.is_active)

    def test_sales_accounts_cannot_read_or_write_each_others_stores(self):
        with Session(self.engine) as session:
            session.add(WalkinDailyReport(
                report_date="2024-01-02", dealer_id="store-b", dealer_name="Dealer B",
                walkin_visits=9, submitted_by="sales-bob",
            ))
            session.commit()
        self.assertEqual(self._login("sales").status_code, 200)
        stores = self.client.get("/api/my-stores")
        self.assertEqual([row["store_id"] for row in stores.json()], ["store-a"])
        denied_read = self.client.get("/api/walkin-metrics?month=2024-01&dealer_id=store-b")
        self.assertEqual(denied_read.status_code, 403)
        denied_write = self.client.post("/api/walkin-metrics", json={
            "report_date": "2024-01-02", "dealer_id": "store-b", "dealer_name": "Dealer B",
        })
        self.assertEqual(denied_write.status_code, 403)

    def test_today_workbench_reports_only_current_scope_and_truth_state(self):
        with Session(self.engine) as session:
            session.add_all([
                WalkinDailyReport(report_date="2024-01-02", dealer_id="store-a", dealer_name="Dealer A", walkin_visits=0),
                WalkinDailyReport(report_date="2024-01-02", dealer_id="store-b", dealer_name="Dealer B", walkin_visits=12),
            ])
            session.commit()
        self.assertEqual(self._login("sales").status_code, 200)
        response = self.client.get("/api/workbench/today?date=2024-01-02")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["scope"]["store_ids"], ["store-a"])
        self.assertEqual(payload["facts"]["walkin_reported"]["value"], 1)
        self.assertEqual(payload["facts"]["walkin_visits"]["value"], 0)
        self.assertTrue(payload["closure"]["complete"])
        self.assertEqual(payload["closure"]["roster_state"], "confirmed")

    def test_admin_workbench_excludes_qa_store_from_expected_roster(self):
        with Session(self.engine) as session:
            session.add_all([
                DealerStore(store_id="qa-test-01", name="QA Test Store", team_key="overseas", is_active=True),
                DealerStore(store_id="qa-001", name="QA Store", team_key="overseas", is_active=True),
                DealerStore(store_id="test-store", name="Test Store", team_key="overseas", is_active=True),
            ])
            session.commit()
        self.assertEqual(self._login("admin").status_code, 200)
        response = self.client.get("/api/workbench/today?date=2024-01-02")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["closure"]["expected"], 2)

    def test_admin_walkin_payload_excludes_qa_store(self):
        with Session(self.engine) as session:
            session.add(DealerStore(
                store_id="qa-test-01",
                name="QA测试门店（可删除）",
                team_key="overseas",
                is_active=True,
            ))
            session.commit()
        payload = {
            "meta": {"storeCount": 2},
            "stores": [
                {"id": "store-a", "name": "Dealer A"},
                {"id": "qa-test-01", "name": "QA测试门店（可删除）"},
            ],
            "staff": [
                {"id": "real", "storeId": "store-a"},
                {"id": "qa", "storeId": "qa-test-01"},
            ],
        }
        self.assertEqual(self._login("admin").status_code, 200)
        with (
            patch("app.walkin.router.bridge.build_walkin_payload", return_value=payload),
            patch("app.walkin.router._merge_five_kit_into_payload", side_effect=lambda value, *_: value),
        ):
            response = self.client.get("/api/walkin?month=2024-01&date=2024-01-31")
        self.assertEqual(response.status_code, 200)
        self.assertEqual([row["id"] for row in response.json()["stores"]], ["store-a"])
        self.assertEqual(response.json()["meta"]["storeCount"], 1)

    def test_workbench_uses_database_logistics_without_csv_directory(self):
        with Session(self.engine) as session:
            session.add(LogisticsShipment(
                record_date="2024-01-02",
                tracking_number="REAL123",
                customer="Real Customer",
                salesperson="Alice Sales",
                current_status="",
                judgement="异常",
            ))
            session.commit()
        settings = SimpleNamespace(
            mvp_root=Path(self.temp_dir.name),
            config_dir=Path(self.temp_dir.name),
            include_demo_data=False,
        )
        self.assertEqual(self._login("admin").status_code, 200)
        with (
            patch("app.database.get_engine", return_value=self.engine),
            patch("app.logistics.service.get_settings", return_value=settings),
        ):
            response = self.client.get("/api/workbench/today?date=2024-01-02")
            dates = self.client.get("/api/logistics/dates")
        self.assertEqual(response.status_code, 200)
        fact = response.json()["facts"]["logistics_attention"]
        self.assertEqual(fact["state"], "available")
        self.assertEqual(fact["value"], 1)
        self.assertEqual(dates.json()["items"], ["2024-01-02"])
        self.assertEqual(dates.json()["source_state"], "available")

    def test_logistics_db_failure_marks_csv_fallback_degraded(self):
        root = Path(self.temp_dir.name)
        logistics_dir = root / "inputs" / "logistics"
        logistics_dir.mkdir(parents=True)
        (logistics_dir / "2024-01-02_tracking.csv").write_text(
            "tracking_number,customer,salesperson,current_status\n"
            "REAL123,Real Customer,Alice Sales,\n",
            encoding="utf-8",
        )
        settings = SimpleNamespace(
            mvp_root=root,
            config_dir=root,
            include_demo_data=False,
        )
        with (
            patch("app.logistics.service.get_settings", return_value=settings),
            patch("app.logistics.service._load_db_rows", return_value=None),
            patch("app.logistics.service.load_auto_status_map", return_value={}),
        ):
            rows, state = logistics_service.load_shipments("all", return_state=True)
        self.assertEqual(len(rows), 1)
        self.assertEqual(state, "degraded")
        self.assertEqual(rows[0]["data_source"], "csv_history")

        with (
            patch("app.logistics.service.get_settings", return_value=settings),
            patch("app.logistics.service._load_db_rows", return_value=[]),
            patch("app.logistics.service.load_auto_status_map", return_value={}),
        ):
            _rows, healthy_db_state = logistics_service.load_shipments("all", return_state=True)
        self.assertEqual(healthy_db_state, "historical")

    def test_logistics_db_failure_without_matching_history_stays_degraded(self):
        root = Path(self.temp_dir.name) / "empty-logistics"
        settings = SimpleNamespace(
            mvp_root=root,
            config_dir=root,
            include_demo_data=False,
        )
        with (
            patch("app.logistics.service.get_settings", return_value=settings),
            patch("app.logistics.service._load_db_rows", return_value=None),
            patch("app.logistics.service.load_auto_status_map", return_value={}),
        ):
            rows, state = logistics_service.load_shipments("all", return_state=True)

        self.assertEqual(rows, [])
        self.assertEqual(state, "degraded")

    def test_daily_sellout_does_not_present_monthly_snapshot_as_today(self):
        with Session(self.engine) as session:
            session.add(WalkinDailyReport(
                report_date="2024-01-01",
                dealer_id="store-a",
                dealer_name="Dealer A",
                deal_amount_yuan=100,
            ))
            session.commit()
        self.assertEqual(self._login("admin").status_code, 200)
        response = self.client.get("/api/dashboard/sell-out?date=2024-01-02&period=day")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIsNone(payload["wan"])
        self.assertEqual(payload["latest_available_date"], "2024-01-01")

    def test_salesperson_ranking_is_filtered_to_current_owner(self):
        self.assertEqual(self._login("sales").status_code, 200)
        remote = {
            "month": "2024-01",
            "rows": [
                {"rank": 1, "name": "Alice Sales", "amount_wan": 12, "quantity": 2, "orders": 1},
                {"rank": 2, "name": "Bob Sales", "amount_wan": 9, "quantity": 1, "orders": 1},
            ],
            "source": "vertu-cli sales +dept-breakdown",
        }
        with patch("app.vertu.sales.fetch_salesperson_breakdown", return_value=remote):
            response = self.client.get("/api/dealer/sellin-salespeople?month=2024-01")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["rows"], [{
            "rank": 1,
            "name": "Alice Sales",
            "amount_wan": 12,
            "quantity": 2,
            "orders": 1,
        }])

    def test_dashboard_periods_reject_unknown_values(self):
        self.assertEqual(self._login("admin").status_code, 200)
        self.assertEqual(self.client.get("/api/dashboard/overview?period=year").status_code, 422)
        self.assertEqual(self.client.get("/api/dashboard/sell-in?period=year").status_code, 422)
        self.assertEqual(self.client.get("/api/dashboard/sell-out?period=year").status_code, 422)

    def test_scoped_daily_sellin_does_not_use_monthly_snapshot(self):
        self.assertEqual(self._login("sales").status_code, 200)
        response = self.client.get("/api/dashboard/sell-in?date=2024-01-02&period=day")
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()["wan"])
        self.assertEqual(response.json()["state"], "missing")

    def test_customer_summary_does_not_mix_stale_legacy_rows_with_acquisition(self):
        self.settings.acquisition_enabled = True
        self.assertEqual(self._login("sales").status_code, 200)
        with patch(
            "app.dashboard.router.bridge.api_customer_center_summary",
            side_effect=AssertionError("stale legacy rows must not be shown"),
        ):
            response = self.client.get("/api/customer-center/summary")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])

    def test_inactive_store_history_is_kept_but_excluded_from_current_reporting(self):
        with Session(self.engine) as session:
            session.add(DealerStore(
                store_id="inactive-store",
                name="Inactive Store",
                team_key="overseas",
                is_active=False,
            ))
            session.add(WalkinDailyReport(
                report_date="2024-01-05",
                dealer_id="inactive-store",
                dealer_name="Inactive Store",
                walkin_visits=99,
                deal_amount_yuan=9999,
            ))
            session.commit()

        self.assertEqual(self._login("admin").status_code, 200)
        listed = self.client.get("/api/walkin-metrics?month=2024-01")
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.json()["count"], 0)
        summary = self.client.get("/api/walkin-metrics/summary?month=2024-01")
        self.assertEqual(summary.status_code, 200)
        self.assertEqual(summary.json()["record_count"], 0)
        today = self.client.get("/api/workbench/today?date=2024-01-05")
        self.assertEqual(today.status_code, 200)
        self.assertEqual(today.json()["facts"]["walkin_visits"]["value"], 0)

    def test_walkin_get_rebuilds_payload_from_database_without_monthly_json(self):
        with Session(self.engine) as session:
            session.add(
                WalkinDailyReport(
                    report_date="2024-01-02",
                    dealer_id="store-a",
                    dealer_name="Dealer A",
                    walkin_visits=7,
                    cross_visits=2,
                    online_visits=3,
                    recruit_visits=1,
                    existing_visits=4,
                    touch_count=9,
                    use_count=5,
                    wechat_add_count=6,
                    deal_count=2,
                    deal_amount_yuan=1280.5,
                    submitted_by="dealer",
                )
            )
            session.commit()

        # Exercise the real legacy no-bundle response, but point every source
        # path at an empty temporary tree so the test cannot read repo data or
        # generate a walkin-YYYY-MM.json file.
        from app.legacy import bridge

        legacy = bridge.wb()
        builder = legacy.build_walkin_api_payload
        self.assertIsNotNone(builder)
        workbench_data = sys.modules[builder.__module__]
        empty_data = Path(self.temp_dir.name) / "walkin-data"
        empty_raw = Path(self.temp_dir.name) / "data-raw"
        empty_data.mkdir()
        empty_raw.mkdir()
        missing_bundle = empty_data / "walkin-2024-01.json"

        with (
            patch.object(workbench_data, "WALKIN_DATA", empty_data),
            patch.object(workbench_data, "BUILD_WALKIN", Path(self.temp_dir.name) / "missing-builder.py"),
            patch.object(workbench_data, "DATA_RAW", empty_raw),
            patch.object(workbench_data, "DATA_SOURCES", Path(self.temp_dir.name) / "missing-sources.json"),
            patch.object(workbench_data, "VN_METRICS", empty_data / "missing-vietnam.json"),
            patch.object(workbench_data, "VN_COLLECT", empty_data / "missing-collect.json"),
        ):
            self.assertFalse(missing_bundle.exists())
            self.assertEqual(self._login("dealer").status_code, 200)
            response = self.client.get(
                "/api/walkin?month=2024-01&date=2024-01-31"
            )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(missing_bundle.exists())
        payload = response.json()
        self.assertEqual(payload["meta"]["storeCount"], 1)
        self.assertNotIn("unavailable", payload["meta"]["dataSources"])
        self.assertIn("dealer_store_db", payload["meta"]["dataSources"])
        self.assertIn("five_kit_db", payload["meta"]["dataSources"])

        self.assertEqual(len(payload["stores"]), 1)
        store = payload["stores"][0]
        self.assertEqual(store["id"], "store-a")
        self.assertEqual(
            store["fiveKit"],
            {
                "walkin": 7,
                "cross": 2,
                "online": 3,
                "recruit": 1,
                "existing": 4,
                "total": 17,
            },
        )
        self.assertEqual(store["walkinPeople"], 17)
        self.assertAlmostEqual(store["avgAddRate"], 6 / 17, places=4)
        self.assertAlmostEqual(store["avgTouchRate"], 9 / 17, places=4)
        self.assertAlmostEqual(store["avgUseRate"], 5 / 17, places=4)


if __name__ == "__main__":
    unittest.main()
