"""production_schedule：排程分類 + 三個工具的單元測試。"""
import datetime
import unittest
from unittest import mock

from agent_core import production_schedule as ps


def _d(y, m, day):
    return datetime.date(y, m, day)


class ClassifyTests(unittest.TestCase):
    def test_overdue_near_done_future_split(self):
        today = _d(2026, 6, 23)
        sched = [
            {"customer": "RICHTER", "work_order": "JFC1", "model": "FE",
             "want_ship": _d(2026, 5, 8), "pack_rem": 300, "pack_cum": 0},   # 落後46
            {"customer": "DECA", "work_order": "JFC2", "model": "DJS",
             "want_ship": _d(2026, 6, 25), "pack_rem": 100, "pack_cum": 50},  # near +2
            {"customer": "LURCHI", "work_order": "JFC3", "model": "EG",
             "want_ship": _d(2026, 7, 30), "pack_rem": 50, "pack_cum": 0},    # 未來、非near
            {"customer": "X", "work_order": "JFC4", "model": "Y",
             "want_ship": _d(2026, 5, 1), "pack_rem": 0, "pack_cum": 100},    # 已完(rem0)
        ]
        overdue, near = ps._classify(sched, today=today, near_days=7)
        self.assertEqual([o["work_order"] for o in overdue], ["JFC1"])
        self.assertEqual(overdue[0]["days_late"], 46)
        self.assertEqual([n["work_order"] for n in near], ["JFC2"])
        seen = {x["work_order"] for x in overdue + near}
        self.assertNotIn("JFC3", seen)  # 未來
        self.assertNotIn("JFC4", seen)  # 已完

    def test_overdue_sorted_most_late_first(self):
        today = _d(2026, 6, 23)
        sched = [
            {"customer": "A", "work_order": "W1", "model": "M", "want_ship": _d(2026, 6, 1),
             "pack_rem": 5, "pack_cum": 0},
            {"customer": "A", "work_order": "W2", "model": "M", "want_ship": _d(2026, 5, 1),
             "pack_rem": 5, "pack_cum": 0},
        ]
        overdue, _ = ps._classify(sched, today=today)
        self.assertEqual([o["work_order"] for o in overdue], ["W2", "W1"])  # 5/1 更晚→排前

    def test_no_want_ship_or_done_excluded(self):
        sched = [{"customer": "A", "work_order": "W", "model": "M",
                  "want_ship": None, "pack_rem": 10, "pack_cum": 0}]
        o, n = ps._classify(sched, today=_d(2026, 6, 23))
        self.assertEqual(o + n, [])


def _reset_cache():
    ps._CACHE.update(bytes=None, name="", modified="", ts=0.0, key="", schedule=None)


class LoadScheduleTests(unittest.TestCase):
    def setUp(self):
        _reset_cache()

    def tearDown(self):
        _reset_cache()

    def test_aggregates_by_workorder_latest_day_wins(self):
        rows = [
            {"customer": "RICHTER", "work_order": "JFC1", "model": "FE", "prod_day": 5,
             "want_ship": datetime.datetime(2026, 5, 8), "pack_rem": 300, "pack_cum": 0},
            {"customer": "RICHTER", "work_order": "JFC1", "model": "FE", "prod_day": 10,
             "want_ship": None, "pack_rem": 250, "pack_cum": 50},   # 較晚日→未完取這筆
            {"customer": "", "work_order": "", "model": "", "prod_day": 1},  # 無指令→跳
        ]
        with mock.patch.object(ps, "_get_report_bytes", return_value=(b"x", "rpt")), \
                mock.patch.object(ps._fpr, "iter_production_rows", return_value=iter(rows)):
            sched, name = ps._load_schedule()
        self.assertEqual(name, "rpt")
        self.assertEqual(len(sched), 1)
        s = sched[0]
        self.assertEqual(s["work_order"], "JFC1")
        self.assertEqual(s["pack_rem"], 250)              # 最後生產日的快照
        self.assertEqual(s["pack_cum"], 50)
        # 最大生產日那筆 want_ship 為 None → 保留較早分頁的日期
        self.assertEqual(s["want_ship"], _d(2026, 5, 8))

    def test_rescheduled_want_ship_takes_latest_day(self):
        # 改期情境：早分頁 5/8、最新分頁改成 6/15 → 要取 6/15（舊值會把落後天數算錯）
        rows = [
            {"customer": "RICHTER", "work_order": "JFC1", "model": "FE", "prod_day": 5,
             "want_ship": datetime.datetime(2026, 5, 8), "pack_rem": 300, "pack_cum": 0,
             "pairs": 300},
            {"customer": "RICHTER", "work_order": "JFC1", "model": "FE", "prod_day": 10,
             "want_ship": datetime.datetime(2026, 6, 15), "pack_rem": 250, "pack_cum": 50,
             "pairs": 300},
        ]
        with mock.patch.object(ps, "_get_report_bytes", return_value=(b"x", "rpt")), \
                mock.patch.object(ps._fpr, "iter_production_rows", return_value=iter(rows)):
            sched, _name = ps._load_schedule()
        self.assertEqual(sched[0]["want_ship"], _d(2026, 6, 15))
        self.assertEqual(sched[0]["pairs"], 300)

    def test_schedule_parse_cached_with_bytes(self):
        # 同一份下載（TTL 內）不重解析：iter_production_rows 只跑一次、下載只跑一次
        rows = [{"customer": "A", "work_order": "W1", "model": "M", "prod_day": 1,
                 "want_ship": datetime.datetime(2026, 6, 1), "pack_rem": 10, "pack_cum": 0,
                 "pairs": 10}]
        meta = {"name": "rpt.xlsx", "modifiedTime": "2026-07-01T00:00:00Z"}
        with mock.patch.object(ps._fpr, "_find_latest_progress_file",
                               return_value=("fid", "rpt.xlsx")) as m_find, \
                mock.patch.object(ps._fpr, "_download_xlsx_bytes",
                                  return_value=(b"xlsx-bytes", meta)) as m_dl, \
                mock.patch.object(ps._fpr, "iter_production_rows",
                                  side_effect=lambda b: iter(rows)) as m_iter:
            s1, _ = ps._load_schedule()
            s2, _ = ps._load_schedule()
        self.assertEqual(m_dl.call_count, 1)
        self.assertEqual(m_find.call_count, 1)
        self.assertEqual(m_iter.call_count, 1)          # 解析結果快取命中
        self.assertEqual(s1, s2)
        s1.append({"junk": True})                        # 呼叫端動回傳清單(外層)不可污染快取
        s3, _ = ps._load_schedule()
        self.assertEqual(len(s3), 1)

    def test_get_report_shared_by_fpr_load_latest_sheet(self):
        # factory_production_report._load_latest_sheet(留空) 走同一份 bytes 快取
        meta = {"name": "rpt.xlsx", "modifiedTime": "2026-07-01T00:00:00Z"}
        with mock.patch.object(ps._fpr, "_find_latest_progress_file",
                               return_value=("fid", "rpt.xlsx")), \
                mock.patch.object(ps._fpr, "_download_xlsx_bytes",
                                  return_value=(b"xlsx-bytes", meta)) as m_dl:
            b1, n1, mod1 = ps._get_report()
            b2, n2, mod2 = ps._fpr._load_latest_sheet()
        self.assertEqual(m_dl.call_count, 1)             # 共用下載
        self.assertIs(b1, b2)
        self.assertEqual((n1, mod1), ("rpt.xlsx", "2026-07-01"))
        self.assertEqual((n2, mod2), (n1, mod1))


class ToolTests(unittest.TestCase):
    def _sched(self):
        return [
            {"customer": "RICHTER", "work_order": "JFC26366", "model": "FE2304V-01AT",
             "want_ship": _d(2026, 5, 8), "pack_rem": 449, "pack_cum": 0},
            {"customer": "LURCHI", "work_order": "JFC26180", "model": "EG2305V-02",
             "want_ship": _d(2026, 5, 10), "pack_rem": 100, "pack_cum": 20},
            {"customer": "DECA.26", "work_order": "JFC26000", "model": "DONE",
             "want_ship": _d(2026, 5, 1), "pack_rem": 0, "pack_cum": 500},  # 已完
        ]

    def test_alert_formats_overdue_and_near(self):
        overdue = [{"customer": "RICHTER", "work_order": "JFC1", "model": "FE2304V",
                    "pack_rem": 449, "days_late": 46}]
        near = [{"customer": "DECA", "work_order": "JFC2", "model": "DJS336",
                 "pack_rem": 34, "days_late": -2}]
        with mock.patch.object(ps, "_load_schedule", return_value=([{}], "rpt")), \
                mock.patch.object(ps, "_classify", return_value=(overdue, near)):
            out = ps.production_alert()
        self.assertIn("落後", out)
        self.assertIn("RICHTER", out)
        self.assertIn("449", out)
        self.assertIn("即將到期", out)
        self.assertIn("JFC2", out)

    def test_alert_all_clear(self):
        with mock.patch.object(ps, "_load_schedule", return_value=([{}], "rpt")), \
                mock.patch.object(ps, "_classify", return_value=([], [])):
            out = ps.production_alert()
        self.assertIn("沒有落後", out)

    def test_query_filters_by_customer(self):
        with mock.patch.object(ps, "_load_schedule", return_value=(self._sched(), "rpt")):
            out = ps.query_production_schedule(customer="RICHTER")
        self.assertIn("JFC26366", out)
        self.assertNotIn("JFC26180", out)  # LURCHI 被過濾掉

    def test_query_overdue_only_drops_done(self):
        with mock.patch.object(ps, "_load_schedule", return_value=(self._sched(), "rpt")):
            out = ps.query_production_schedule(overdue_only=True)
        self.assertNotIn("JFC26000", out)  # 已完不在已落後清單
        self.assertIn("JFC26366", out)

    def test_query_no_hits(self):
        with mock.patch.object(ps, "_load_schedule", return_value=(self._sched(), "rpt")):
            out = ps.query_production_schedule(customer="NOSUCH")
        self.assertIn("沒找到", out)

    def test_dashboard_per_customer_summary(self):
        with mock.patch.object(ps, "_load_schedule", return_value=(self._sched(), "rpt")):
            out = ps.production_dashboard()
        self.assertIn("看板", out)
        self.assertIn("RICHTER", out)
        self.assertIn("LURCHI", out)


class RemAllZeroGuardTests(unittest.TestCase):
    """欠數欄解析失敗護欄：pairs>0 夠多筆、pack_rem 全 0 → 標可疑，別假全綠。"""

    @staticmethod
    def _sched(n, rem=0):
        return [{"customer": "A", "work_order": f"W{i}", "model": "M",
                 "want_ship": _d(2026, 6, 1), "pack_rem": rem, "pack_cum": 0,
                 "pairs": 100} for i in range(n)]

    def test_warns_when_all_rem_zero_over_threshold(self):
        warn = ps._rem_all_zero_warning(self._sched(ps._REM_SUSPECT_MIN))
        self.assertIn("⚠️", warn)
        self.assertIn("全為 0", warn)

    def test_no_warn_when_any_rem_nonzero(self):
        sched = self._sched(ps._REM_SUSPECT_MIN)
        sched[0]["pack_rem"] = 5
        self.assertEqual(ps._rem_all_zero_warning(sched), "")

    def test_no_warn_below_threshold(self):
        self.assertEqual(ps._rem_all_zero_warning(self._sched(ps._REM_SUSPECT_MIN - 1)), "")

    def test_no_warn_without_pairs_info(self):
        # 舊測試/舊呼叫端造的 schedule 沒 pairs 欄 → 不誤觸
        sched = [{"customer": "A", "work_order": "W", "model": "M",
                  "want_ship": _d(2026, 6, 1), "pack_rem": 0, "pack_cum": 0}] * 20
        self.assertEqual(ps._rem_all_zero_warning(sched), "")

    def test_alert_surfaces_suspicion(self):
        with mock.patch.object(ps, "_load_schedule",
                               return_value=(self._sched(ps._REM_SUSPECT_MIN), "rpt")):
            out = ps.production_alert()
        self.assertIn("資料可疑", out)          # 標可疑
        self.assertIn("沒有落後", out)          # 但不拒答，照常輸出

    def test_dashboard_surfaces_suspicion(self):
        with mock.patch.object(ps, "_load_schedule",
                               return_value=(self._sched(ps._REM_SUSPECT_MIN), "rpt")):
            out = ps.production_dashboard()
        self.assertIn("資料可疑", out)

    def test_alert_clean_schedule_no_suspicion(self):
        with mock.patch.object(ps, "_load_schedule",
                               return_value=(self._sched(ps._REM_SUSPECT_MIN, rem=50), "rpt")):
            out = ps.production_alert()
        self.assertNotIn("資料可疑", out)


class SupremoCrosswalkTests(unittest.TestCase):
    """業務訂單型體(客戶型體/Supremo) → 生管型體 對照（用生管日報客戶型體欄）。"""

    def _sched(self):
        return [
            {"customer": "LURCHI", "work_order": "JFC26220", "model": "EH2303V-01AU",
             "cust_style": "63L1073004-AU(S)", "want_ship": _d(2026, 6, 8),
             "pack_rem": 157, "pack_cum": 0},
            {"customer": "LURCHI", "work_order": "JFC26273", "model": "EH2303V-01AT",
             "cust_style": "63L1073004-AT", "want_ship": _d(2026, 6, 8),
             "pack_rem": 234, "pack_cum": 0},
            {"customer": "LURCHI", "work_order": "JFC25662", "model": "EJ2401-01NY",
             "cust_style": "74L1303003", "want_ship": _d(2025, 11, 15),
             "pack_rem": 0, "pack_cum": 40},
        ]

    def test_looks_supremo_and_norm(self):
        self.assertTrue(ps._looks_supremo("74L130300300"))
        self.assertTrue(ps._looks_supremo("63L107300400"))
        self.assertFalse(ps._looks_supremo("EG2305V-02"))   # 生管型體不是 Supremo
        self.assertFalse(ps._looks_supremo("JFC26180"))     # 指令號不是
        self.assertEqual(ps._norm_style("63L1073004-AU(S)"), "63L1073004")  # 去顏色後綴
        self.assertEqual(ps._norm_style("63L107300400"), "63L107300400")

    def test_resolve_prefix_tolerant(self):
        # 業務碼尾多兩碼(…00) 仍前綴對到報表的 63L1073004；74L 不混入
        models = ps._resolve_supremo("63L107300400", self._sched())
        self.assertIn("EH2303V-01AU", models)
        self.assertIn("EH2303V-01AT", models)
        self.assertNotIn("EJ2401-01NY", models)

    def test_query_by_supremo_resolves_and_filters(self):
        with mock.patch.object(ps, "_load_schedule", return_value=(self._sched(), "rpt")):
            out = ps.query_production_schedule(model="63L107300400")
        self.assertIn("🔗", out)              # 顯示客戶型體→生管型體對照
        self.assertIn("EH2303V-01", out)
        self.assertIn("JFC26220", out)
        self.assertNotIn("JFC25662", out)     # 74L 那筆不該被帶進來

    def test_query_supremo_not_in_report(self):
        with mock.patch.object(ps, "_load_schedule", return_value=(self._sched(), "rpt")):
            out = ps.query_production_schedule(model="99Z999999")
        self.assertIn("還沒排進生產", out)

    def test_query_seikan_model_unaffected(self):
        # 生管型體仍走原本子字串比對、不觸發對照、不顯示 🔗
        with mock.patch.object(ps, "_load_schedule", return_value=(self._sched(), "rpt")):
            out = ps.query_production_schedule(model="EH2303V")
        self.assertIn("JFC26220", out)
        self.assertNotIn("🔗", out)


class OverdueBomTests(unittest.TestCase):
    """production_overdue_bom：落後指令 × 料表狀態（去重 / 上限 / 客戶過濾）。"""

    _OVERDUE = [
        {"customer": "LURCHI", "work_order": "JFC1", "model": "EH2303V-01AU 25.",
         "pack_rem": 1337, "days_late": 17},
        {"customer": "LURCHI", "work_order": "JFC2", "model": "EH2303V-01AU 25.",
         "pack_rem": 189, "days_late": 17},   # 同型體核心 → 應與 JFC1 併
        {"customer": "RICHTER", "work_order": "JFC3", "model": "FE2303V-02 AT",
         "pack_rem": 300, "days_late": 17},
        {"customer": "RICHTER", "work_order": "JFC4", "model": "FE2303V4-01MYSTIC",
         "pack_rem": 579, "days_late": 10},
    ]

    @staticmethod
    def _probe(model):
        return {
            "EH2303V-01AU 25.": ("yes", "Limango CBD LURCHI EH2303V.xlsx", 0),
            "FE2303V4-01MYSTIC": ("yes", "CBD 5001-4292 (2026).xlsx", 79),
            "FE2303V-02 AT": ("no", "", 0),
        }.get(model, ("unsupported", "", 0))

    def test_dedup_and_status(self):
        with mock.patch.object(ps, "_load_schedule", return_value=([{}], "rpt")), \
                mock.patch.object(ps, "_classify", return_value=(self._OVERDUE, [])), \
                mock.patch("agent_core.factory_bom.bom_probe", side_effect=self._probe):
            out = ps.production_overdue_bom()
        self.assertIn("EH2303V-01AU", out)
        self.assertIn("1,526", out)                  # 1337+189 → 同型體兩單併
        self.assertIn("✅ 有料表", out)
        self.assertIn("79 料", out)                   # RICHTER 命中帶料數
        self.assertIn("❌ 沒料表", out)
        self.assertIn("✅2 有料表、❌1 沒料表", out)

    def test_customer_filter(self):
        with mock.patch.object(ps, "_load_schedule", return_value=([{}], "rpt")), \
                mock.patch.object(ps, "_classify", return_value=(self._OVERDUE, [])), \
                mock.patch("agent_core.factory_bom.bom_probe", side_effect=self._probe):
            out = ps.production_overdue_bom(customer="RICHTER")
        self.assertIn("FE2303V", out)
        self.assertNotIn("EH2303V", out)             # LURCHI 被過濾

    def test_top_cap_notes_remainder(self):
        with mock.patch.object(ps, "_load_schedule", return_value=([{}], "rpt")), \
                mock.patch.object(ps, "_classify", return_value=(self._OVERDUE, [])), \
                mock.patch("agent_core.factory_bom.bom_probe", side_effect=self._probe):
            out = ps.production_overdue_bom(top=1)
        self.assertIn("還有 2 個型體未查", out)

    def test_none_overdue(self):
        with mock.patch.object(ps, "_load_schedule", return_value=([{}], "rpt")), \
                mock.patch.object(ps, "_classify", return_value=([], [])):
            out = ps.production_overdue_bom()
        self.assertIn("沒有落後", out)


class RegistrationTests(unittest.TestCase):
    def test_tools_registered(self):
        from agent_core import tool_registry_catalog as cat
        names = {getattr(t, "__name__", "") for t in cat.BASE_BUILTIN_TOOLS}
        for n in ("production_alert", "query_production_schedule", "production_dashboard",
                  "production_overdue_bom"):
            self.assertIn(n, names)


if __name__ == "__main__":
    unittest.main()
