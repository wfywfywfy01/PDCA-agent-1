from __future__ import annotations

import asyncio
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from app.dashboard import service
from app.dashboard.router import _sales_payload
from app.vertu import sales


class DashboardPerformanceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        sales._SELL_IN_CACHE.clear()
        sales._SELL_IN_LOCKS.clear()

    async def test_sell_in_departments_run_concurrently_and_cache_result(self) -> None:
        started: list[str] = []
        all_started = asyncio.Event()

        async def fake_headline(_start: str, _end: str, department: str = "") -> dict:
            started.append(department)
            if len(started) == 3:
                all_started.set()
            await asyncio.wait_for(all_started.wait(), timeout=0.5)
            return {"period": {"销额": 10000, "销量": 2}}

        env = {
            "PDCA_VERTU_SELLIN_DEPARTMENTS": "经销商一部,经销商二部,经销商三部",
            "PDCA_SELL_IN_CACHE_SECONDS": "60",
        }
        with patch.dict(os.environ, env), patch.object(sales, "_headline", side_effect=fake_headline):
            first = await sales.fetch_sell_in("2026-07-20", "month")
            second = await sales.fetch_sell_in("2026-07-20", "month")

        self.assertEqual(started, ["经销商一部", "经销商二部", "经销商三部"])
        self.assertEqual(first["amount"], 30000)
        self.assertEqual(first["quantity"], 6)
        self.assertFalse(first["cached"])
        self.assertTrue(second["cached"])
        self.assertEqual(second["as_of"], first["as_of"])
        self.assertIn("更新", first["note"])

    async def test_salesperson_breakdown_normalizes_cli_rows(self) -> None:
        payload = {"rows": [{"name": "Alice", "amount": 123456, "qty": 3, "orders": 2}]}
        with (
            patch.dict(os.environ, {"PDCA_VERTU_SELLIN_DEPARTMENTS": ""}),
            patch.object(sales, "run_vertu_json", return_value=payload) as run,
        ):
            result = await sales.fetch_salesperson_breakdown("2026-08")
        self.assertEqual(result["rows"][0], {
            "rank": 1,
            "name": "Alice",
            "amount_wan": 12.35,
            "quantity": 3,
            "orders": 2,
        })
        self.assertIn("+dept-breakdown", run.call_args.args[0])
        self.assertIn("5000", run.call_args.args[0])

    async def test_salesperson_breakdown_matches_configured_sellin_departments(self) -> None:
        async def fake_run(args, timeout):
            department = args[args.index("--dept-l2") + 1]
            amount = 10000 if department == "经销商一部" else 20000
            return {"rows": [{"name": "Alice", "amount": amount, "qty": 1, "orders": 1}]}

        with (
            patch.dict(os.environ, {"PDCA_VERTU_SELLIN_DEPARTMENTS": "经销商一部,经销商二部"}),
            patch.object(sales, "run_vertu_json", side_effect=fake_run) as run,
        ):
            result = await sales.fetch_salesperson_breakdown("2026-08")
        self.assertEqual(run.call_count, 2)
        self.assertEqual(result["rows"][0]["amount_wan"], 3.0)
        self.assertEqual(result["rows"][0]["quantity"], 2)

    def test_workbench_overview_uses_authenticated_session_without_remote_lookup(self) -> None:
        session_user = {
            "username": "april",
            "display_name": "April",
            "role": "sales",
        }
        with patch.object(
            service.bridge,
            "api_dashboard_overview",
            side_effect=AssertionError("legacy remote overview must not run"),
        ):
            data = service.workbench_overview("2026-07-20", "month", session_user)

        self.assertEqual(data["managerName"], "April")
        self.assertEqual(data["managerRole"], "经销商销售 · 月视图 · 2026-07-20")
        self.assertIsNone(data["sellInWan"])
        self.assertEqual(data["dataState"]["sellIn"], "missing")

    def test_sales_payload_keeps_missing_distinct_from_real_zero(self) -> None:
        missing = _sales_payload(service.workbench_overview("2026-07-20"), "sellOut")
        zero = _sales_payload(
            {
                "sellOutWan": 0,
                "sellOutSub": "Odoo同步",
                "dataAsOf": "2026-07-20T09:00:00",
                "dataSource": {"sellOut": "dealer_sales_db"},
            },
            "sellOut",
        )

        self.assertIsNone(missing["amount"])
        self.assertIsNone(missing["wan"])
        self.assertEqual(zero["amount"], 0)
        self.assertEqual(zero["wan"], 0)
        self.assertEqual(zero["source"], "dealer_sales_db")

    def test_homepage_starts_sections_in_parallel(self) -> None:
        root = Path(__file__).resolve().parents[2]
        source = (
            root
            / "data_platform"
            / "data_role_pdca_mvp"
            / "modules"
            / "home_dashboard"
            / "index.html"
        ).read_text(encoding="utf-8")

        self.assertIn("const overviewTask = loadSection('overview'", source)
        self.assertIn("const sellInTask = loadSection('sellIn'", source)
        self.assertIn("const customersTask = loadSection('customers'", source)
        self.assertIn("await Promise.allSettled([", source)
        self.assertIn("currentPeriod === 'month' && overview", source)
        self.assertIn("当前周期 Sell-in 数据不可用", source)

    def test_sellin_page_labels_salesperson_data_truthfully(self) -> None:
        root = Path(__file__).resolve().parents[2]
        source = (root / "pdca-workbench" / "frontend" / "dealer_sellin.html").read_text(encoding="utf-8")
        self.assertIn("销售人员排行", source)
        self.assertIn("/api/dealer/sellin-salespeople", source)
        self.assertNotIn("经销商进货占比", source)
        self.assertIn("document.getElementById('updTime').textContent = '更新失败'", source)
        self.assertIn("${escapeHtml(msg)}", source)
        self.assertIn("const sequence = ++loadSequence", source)
        self.assertIn("if(sequence !== loadSequence) return", source)


if __name__ == "__main__":
    unittest.main()
