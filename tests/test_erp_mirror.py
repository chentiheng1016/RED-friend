"""agent_core/erp_mirror 單元測試 —— 不連 ERP 主機。

純函式（build_select/duck_table/manifest）+ 真正的 DuckDB read_csv 往返（驗證 CHR(1)
分隔載入正確）。build_select 產生的 SELECT 也丟進真正的 guard_select_only 驗證放行。
"""
import gzip
import json
import os
import tempfile
import unittest

from agent_core.erp_oracle_client import ErpOracleError, guard_select_only
from agent_core.erp_mirror import (
    build_select,
    duck_table,
    load_gz_into_duckdb,
    load_manifest,
    save_manifest,
)
import agent_core.erp_mirror as mirror_mod


_COLS = [
    {"id": 1, "name": "SE_ID", "type": "VARCHAR2", "length": "20", "nullable": False},
    {"id": 2, "name": "SE_QTY", "type": "NUMBER", "length": "", "nullable": True},
    {"id": 3, "name": "FAC_DATE", "type": "DATE", "length": "", "nullable": True},
    {"id": 4, "name": "REMARK", "type": "CLOB", "length": "", "nullable": True},   # 跳過
    {"id": 5, "name": "UPDATE", "type": "VARCHAR2", "length": "10", "nullable": True},  # 保留字名
]


class BuildSelectTests(unittest.TestCase):
    def test_types_and_skips(self):
        sql, kept, skipped, width = build_select("SC00", "SE_BOM_SIZE", _COLS)
        self.assertIn("||CHR(1)||", sql)
        self.assertIn('FROM "SC00"."SE_BOM_SIZE"', sql)
        self.assertIn("TO_CHAR(\"FAC_DATE\"", sql)          # DATE → TO_CHAR
        self.assertIn("SUBSTR(REPLACE(REPLACE(\"SE_ID\"", sql)  # 文字 → 剝換行+截斷
        self.assertIn('"SE_QTY"', sql)                       # NUMBER → 原樣
        self.assertEqual(kept, ["SE_ID", "SE_QTY", "FAC_DATE", "UPDATE"])
        self.assertEqual(skipped, ["REMARK"])                # CLOB 跳過
        self.assertGreater(width, 0)

    def test_generated_sql_passes_guard(self):
        # 含保留字欄名 "UPDATE"（雙引號）也要過守門（守門會剝掉雙引號識別碼）。
        sql, *_ = build_select("SC00", "SE_BOM_SIZE", _COLS)
        self.assertTrue(guard_select_only(sql).lower().startswith("select"))

    def test_all_skipped_returns_none(self):
        only_lob = [{"id": 1, "name": "DATA", "type": "BLOB", "length": ""}]
        sql, kept, skipped, width = build_select("X", "Y", only_lob)
        self.assertIsNone(sql)
        self.assertEqual(kept, [])
        self.assertEqual(skipped, ["DATA"])

    def test_bad_identifier_skipped(self):
        cols = [{"id": 1, "name": "OK_COL", "type": "NUMBER"},
                {"id": 2, "name": "bad name;drop", "type": "VARCHAR2", "length": "5"}]
        sql, kept, skipped, _ = build_select("X", "Y", cols)
        self.assertEqual(kept, ["OK_COL"])
        self.assertIn("bad name;drop", skipped)

    def test_wide_row_flagged_by_width(self):
        wide = [{"id": i, "name": f"C{i}", "type": "VARCHAR2", "length": "4000"} for i in range(20)]
        _, _, _, width = build_select("X", "Y", wide)
        self.assertGreater(width, 32767)   # 20×min(4000,2000)=40000 → 標 wide

    def test_wide_table_sql_lines_under_sqlplus_limit(self):
        # 75 欄的寬表：每個物理行必須 < 2499（sqlplus SP2-0027 上限），否則整條被忽略。
        wide = [{"id": i, "name": f"COL_{i:03d}", "type": "VARCHAR2", "length": "200"}
                for i in range(75)]
        sql, kept, _, _ = build_select("SC00", "RD_ITEM", wide)
        self.assertEqual(len(kept), 75)
        self.assertTrue(all(len(line) < 2499 for line in sql.split("\n")))
        self.assertGreater(len(sql.split("\n")), 1)   # 確實有換行拆成多物理行
        self.assertTrue(guard_select_only(sql).lower().startswith("select"))  # 換行後仍過守門


class ParseCountTests(unittest.TestCase):
    def test_handles_leading_empty_cell_from_tab(self):
        # sqlplus COUNT 前導 tab → CHR(9) 切出空欄；要掃到 '92'，不能死抓 [0][0]
        self.assertEqual(mirror_mod._parse_count([["", "92"]]), 92)
        self.assertEqual(mirror_mod._parse_count([["92"]]), 92)
        self.assertEqual(mirror_mod._parse_count([["  0 "]]), 0)
        self.assertEqual(mirror_mod._parse_count([]), 0)
        self.assertEqual(mirror_mod._parse_count([["", ""]]), 0)


class ShouldRetryTests(unittest.TestCase):
    def test_significant_shortfall_retries(self):
        # SSH 串流中途 EOF 靜默短少 → 重抽（2026-07-11 SF_TRANS_HEADER 192098→66085）
        self.assertTrue(mirror_mod._should_retry(192098, 66085))
        self.assertTrue(mirror_mod._should_retry(9684, 1247))

    def test_small_churn_no_retry(self):
        # 幾列 live churn（db 比 load 多一點點）不重抽——重抽也補不齊
        self.assertFalse(mirror_mod._should_retry(1000, 995))
        self.assertFalse(mirror_mod._should_retry(1000, 1000))
        self.assertFalse(mirror_mod._should_retry(100000, 99900))  # 0.1% < 0.2% 門檻

    def test_threshold_boundary(self):
        # 門檻 max(20, db//500)：db=10000 → //500=20 → 短少>20 才重抽
        self.assertFalse(mirror_mod._should_retry(10000, 9980))  # 短少 20，不重抽
        self.assertTrue(mirror_mod._should_retry(10000, 9979))   # 短少 21，重抽


class DuckTableNameTests(unittest.TestCase):
    def test_name(self):
        self.assertEqual(duck_table("SC00", "SE_BOM_SIZE"), "SC00__SE_BOM_SIZE")


class PrecheckSignalTests(unittest.TestCase):
    """變更訊號選擇：LAST_DATE 優先、ORA_ROWSCN 需 opt-in、業務日期欄一律拒用。"""

    _COLS_WITH_LD = [{"name": "SE_ID", "type": "VARCHAR2", "length": "20"},
                     {"name": "LAST_DATE", "type": "DATE"}]
    _COLS_BUSINESS_DATE = [{"name": "SE_ID", "type": "VARCHAR2", "length": "20"},
                           {"name": "CR_REQDATE", "type": "DATE"},   # 業務日期，非修改時戳
                           {"name": "ITEM_DATE", "type": "DATE"}]
    _COLS_NO_DATE = [{"name": "SE_ID", "type": "VARCHAR2", "length": "20"}]

    def test_last_date_preferred(self):
        sigkey, expr = mirror_mod._precheck_signal(self._COLS_WITH_LD)
        self.assertEqual(sigkey, "last_date")
        self.assertIn('MAX("LAST_DATE")', expr)

    def test_business_date_columns_rejected(self):
        # 有業務日期欄但沒 LAST_DATE、rowscn 沒開 → None（不賭業務日期＝不會漏改）
        from unittest import mock
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("RED_ERP_PRECHECK_ROWSCN", None)
            self.assertIsNone(mirror_mod._precheck_signal(self._COLS_BUSINESS_DATE))
            self.assertIsNone(mirror_mod._precheck_signal(self._COLS_NO_DATE))

    def test_rowscn_opt_in_covers_no_date_table(self):
        from unittest import mock
        with mock.patch.dict(os.environ, {"RED_ERP_PRECHECK_ROWSCN": "1"}):
            sigkey, expr = mirror_mod._precheck_signal(self._COLS_NO_DATE)
            self.assertEqual(sigkey, "row_scn")
            self.assertIn("MAX(ORA_ROWSCN)", expr)
            # 即使 rowscn 開著，有 LAST_DATE 的表仍優先用 LAST_DATE
            self.assertEqual(mirror_mod._precheck_signal(self._COLS_WITH_LD)[0], "last_date")
            # 業務日期欄在 rowscn 模式下改用 ROWSCN（不用業務日期）
            self.assertEqual(
                mirror_mod._precheck_signal(self._COLS_BUSINESS_DATE)[0], "row_scn")


class PrecheckTests(unittest.TestCase):
    """夜刷變更預檢：COUNT+變更訊號 與上次真刷相同、且未過兜底天數 → 跳過重刷。"""

    _COLS_WITH_LD = [{"name": "SE_ID", "type": "VARCHAR2", "length": "20"},
                     {"name": "LAST_DATE", "type": "DATE"}]
    _COLS_NO_LD = [{"name": "SE_ID", "type": "VARCHAR2", "length": "20"}]

    @staticmethod
    def _ts(days_ago=0.0):
        import time
        return time.strftime("%Y-%m-%d %H:%M:%S",
                             time.localtime(time.time() - days_ago * 86400))

    def test_script_one_select_per_table(self):
        script = mirror_mod._precheck_script([
            ("SC00.SE_ORD_M", "SC00", "SE_ORD_M", 'NVL(TO_CHAR(MAX("LAST_DATE")),\'-\')'),
            ("SP00.SP_PROD_SP", "SP00", "SP_PROD_SP", "NVL(TO_CHAR(MAX(ORA_ROWSCN)),'-')")])
        self.assertEqual(script.count("SELECT "), 2)
        self.assertIn('FROM "SC00"."SE_ORD_M";', script)
        self.assertIn('MAX("LAST_DATE")', script)
        self.assertIn("MAX(ORA_ROWSCN)", script)

    def test_parse_skips_garbage_lines(self):
        out = ["SC00.SE_ORD_M\t460\t2026-07-10 18:03:11",
               "SP00.SP_PROD_SP\t9684\t-",          # 訊號全 NULL → '-'
               "garbage line",                        # 格式不對 → 略過
               "X.Y\tnotanum\t2026-01-01 00:00:00"]  # count 非數字 → 略過
        stats = mirror_mod._parse_precheck(out)
        self.assertEqual(stats["SC00.SE_ORD_M"], (460, "2026-07-10 18:03:11"))
        self.assertEqual(stats["SP00.SP_PROD_SP"], (9684, "-"))
        self.assertEqual(len(stats), 2)

    def _targets(self):
        return [("SC00", "SE_ORD_M", self._COLS_WITH_LD, 460),
                ("SP00", "SP_PROD_SP", self._COLS_NO_LD, 9684)]

    def test_unchanged_requires_done_rows_signal_and_fresh_full_ts(self):
        from unittest import mock
        man = {"SC00.SE_ORD_M": {"status": "done", "db_rows": 460,
                                 "last_date": "2026-07-10 18:03:11",
                                 "full_ts": self._ts(days_ago=1)}}  # 昨天才真刷 → 未過兜底
        with mock.patch.object(mirror_mod.erp, "_ssh_sqlplus",
                               return_value="SC00.SE_ORD_M\t460\t2026-07-10 18:03:11"):
            unchanged, stats = mirror_mod.precheck_hot_unchanged(self._targets(), man,
                                                                 log=lambda m: None)
        self.assertEqual(unchanged, {"SC00.SE_ORD_M"})
        # stats 帶 sigkey；無 LAST_DATE 欄且 rowscn 沒開 → 不進預檢腳本
        self.assertEqual(stats["SC00.SE_ORD_M"][2], "last_date")
        self.assertNotIn("SP00.SP_PROD_SP", stats)

    def test_stale_full_ts_forces_refresh(self):
        # 訊號說沒變，但距上次真刷超過兜底天數（預設 7）→ 強制重刷（收斂訊號盲點）
        from unittest import mock
        man = {"SC00.SE_ORD_M": {"status": "done", "db_rows": 460,
                                 "last_date": "2026-07-10 18:03:11",
                                 "full_ts": self._ts(days_ago=9)}}
        with mock.patch.object(mirror_mod.erp, "_ssh_sqlplus",
                               return_value="SC00.SE_ORD_M\t460\t2026-07-10 18:03:11"):
            unchanged, _ = mirror_mod.precheck_hot_unchanged(self._targets(), man,
                                                             log=lambda m: None)
        self.assertEqual(unchanged, set())

    def test_missing_full_ts_forces_refresh(self):
        # 升級首晚：manifest 有 last_date 但沒 full_ts → 視為過期 → 重刷（安全側）
        from unittest import mock
        man = {"SC00.SE_ORD_M": {"status": "done", "db_rows": 460,
                                 "last_date": "2026-07-10 18:03:11"}}
        with mock.patch.object(mirror_mod.erp, "_ssh_sqlplus",
                               return_value="SC00.SE_ORD_M\t460\t2026-07-10 18:03:11"):
            unchanged, _ = mirror_mod.precheck_hot_unchanged(self._targets(), man,
                                                             log=lambda m: None)
        self.assertEqual(unchanged, set())

    def test_rowscn_mode_skips_no_date_table_when_unchanged(self):
        # opt-in ORA_ROWSCN：無日期欄的表也能靠 row_scn 跳過（SE_BOM_SIZE 場景）
        from unittest import mock
        man = {"SP00.SP_PROD_SP": {"status": "done", "db_rows": 9684,
                                   "row_scn": "12345678",
                                   "full_ts": self._ts(days_ago=1)}}
        canned = "SP00.SP_PROD_SP\t9684\t12345678"
        with mock.patch.dict(os.environ, {"RED_ERP_PRECHECK_ROWSCN": "1"}), \
                mock.patch.object(mirror_mod.erp, "_ssh_sqlplus", return_value=canned):
            unchanged, stats = mirror_mod.precheck_hot_unchanged(
                [("SP00", "SP_PROD_SP", self._COLS_NO_LD, 9684)], man, log=lambda m: None)
        self.assertEqual(unchanged, {"SP00.SP_PROD_SP"})
        self.assertEqual(stats["SP00.SP_PROD_SP"][2], "row_scn")
        # ROWSCN 前進（有人改了）→ 不跳
        with mock.patch.dict(os.environ, {"RED_ERP_PRECHECK_ROWSCN": "1"}), \
                mock.patch.object(mirror_mod.erp, "_ssh_sqlplus",
                                  return_value="SP00.SP_PROD_SP\t9684\t99999999"):
            unchanged2, _ = mirror_mod.precheck_hot_unchanged(
                [("SP00", "SP_PROD_SP", self._COLS_NO_LD, 9684)], man, log=lambda m: None)
        self.assertEqual(unchanged2, set())

    def test_changed_or_first_night_not_skipped(self):
        from unittest import mock
        canned = "SC00.SE_ORD_M\t461\t2026-07-11 09:00:00"
        # 列數變了 → 不跳
        man = {"SC00.SE_ORD_M": {"status": "done", "db_rows": 460,
                                 "last_date": "2026-07-10 18:03:11",
                                 "full_ts": self._ts(days_ago=1)}}
        with mock.patch.object(mirror_mod.erp, "_ssh_sqlplus", return_value=canned):
            unchanged, _ = mirror_mod.precheck_hot_unchanged(self._targets(), man,
                                                             log=lambda m: None)
        self.assertEqual(unchanged, set())
        # 升級首晚（manifest 沒記 last_date）→ 不跳
        man2 = {"SC00.SE_ORD_M": {"status": "done", "db_rows": 461,
                                  "full_ts": self._ts(days_ago=1)}}
        with mock.patch.object(mirror_mod.erp, "_ssh_sqlplus", return_value=canned):
            unchanged2, _ = mirror_mod.precheck_hot_unchanged(self._targets(), man2,
                                                              log=lambda m: None)
        self.assertEqual(unchanged2, set())
        # 上次 count_mismatch → 不跳（殘缺表必重刷）
        man3 = {"SC00.SE_ORD_M": {"status": "count_mismatch", "db_rows": 461,
                                  "last_date": "2026-07-11 09:00:00",
                                  "full_ts": self._ts(days_ago=1)}}
        with mock.patch.object(mirror_mod.erp, "_ssh_sqlplus", return_value=canned):
            unchanged3, _ = mirror_mod.precheck_hot_unchanged(self._targets(), man3,
                                                              log=lambda m: None)
        self.assertEqual(unchanged3, set())

    def test_precheck_failure_falls_back_to_full_refresh(self):
        from unittest import mock
        man = {"SC00.SE_ORD_M": {"status": "done", "db_rows": 460,
                                 "last_date": "2026-07-10 18:03:11",
                                 "full_ts": self._ts(days_ago=1)}}
        with mock.patch.object(mirror_mod.erp, "_ssh_sqlplus",
                               side_effect=ErpOracleError("ssh down")):
            unchanged, stats = mirror_mod.precheck_hot_unchanged(self._targets(), man,
                                                                 log=lambda m: None)
        self.assertEqual(unchanged, set())
        self.assertEqual(stats, {})


class MirrorRecordsWatermarkTests(unittest.TestCase):
    """mirror() 端到端：真刷完要在 manifest 記 full_ts + 變更訊號（供下晚預檢）。"""

    def test_stream_records_full_ts_and_signal(self):
        from unittest import mock
        cols_ld = [{"name": "SE_ID", "type": "VARCHAR2", "length": "20"},
                   {"name": "LAST_DATE", "type": "DATE"}]
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "erp.duckdb")
            man_path = os.path.join(d, "manifest.json")
            targets = [("SC00", "SE_ORD_M", cols_ld, 3)]
            with mock.patch.object(mirror_mod, "iter_targets", return_value=iter(targets)), \
                 mock.patch.object(mirror_mod, "count_rows", return_value=3), \
                 mock.patch.object(mirror_mod, "stream_to_gz", return_value=3), \
                 mock.patch.object(mirror_mod, "load_gz_into_duckdb", return_value=3), \
                 mock.patch.object(mirror_mod.erp, "_ssh_sqlplus",
                                   return_value="SC00.SE_ORD_M\t3\t2026-07-11 08:00:00"):
                mirror_mod.mirror(only_tables=["SC00.SE_ORD_M"], force=True,
                                  skip_unchanged=True, db_path=db, manifest_path=man_path,
                                  keep_raw=True, log=lambda m: None)
            m = load_manifest(man_path)["SC00.SE_ORD_M"]
        self.assertEqual(m["status"], "done")
        self.assertIn("full_ts", m)                        # 真刷才記 full_ts
        self.assertEqual(m["last_date"], "2026-07-11 08:00:00")  # 訊號存 last_date 鍵
        self.assertEqual(m["ts"], m["full_ts"])            # 真刷時兩者同時打點

    def test_second_run_skips_via_precheck(self):
        # 第一次真刷記下 full_ts+last_date；緊接第二次（同 COUNT+訊號）→ 預檢跳過
        from unittest import mock
        cols_ld = [{"name": "SE_ID", "type": "VARCHAR2", "length": "20"},
                   {"name": "LAST_DATE", "type": "DATE"}]
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "erp.duckdb")
            man_path = os.path.join(d, "manifest.json")
            targets = [("SC00", "SE_ORD_M", cols_ld, 3)]
            canned = "SC00.SE_ORD_M\t3\t2026-07-11 08:00:00"
            common = dict(only_tables=["SC00.SE_ORD_M"], force=True, skip_unchanged=True,
                          db_path=db, manifest_path=man_path, keep_raw=True,
                          log=lambda m: None)
            stream = mock.Mock(return_value=3)
            with mock.patch.object(mirror_mod, "iter_targets",
                                   side_effect=lambda *a, **k: iter(targets)), \
                 mock.patch.object(mirror_mod, "count_rows", return_value=3), \
                 mock.patch.object(mirror_mod, "stream_to_gz", stream), \
                 mock.patch.object(mirror_mod, "load_gz_into_duckdb", return_value=3), \
                 mock.patch.object(mirror_mod.erp, "_ssh_sqlplus", return_value=canned):
                mirror_mod.mirror(**common)               # 第一次：真刷
                first_calls = stream.call_count
                res = mirror_mod.mirror(**common)         # 第二次：應預檢跳過
        self.assertGreaterEqual(first_calls, 1)           # 第一次真的有串流
        self.assertEqual(stream.call_count, first_calls)  # 第二次沒再串流
        self.assertEqual(res["skipped_fresh"], 1)


class DuckLoadRoundTripTests(unittest.TestCase):
    def test_chr1_gz_loads_correctly(self):
        import duckdb
        with tempfile.TemporaryDirectory() as d:
            gz = os.path.join(d, "t.csv.gz")
            with gzip.open(gz, "wt", encoding="utf-8", newline="\n") as fh:
                fh.write("A001\x01Widget\x012024-01-01 00:00:00\x01100\n")
                fh.write("A002\x01\x01\x0150\n")   # 中間欄 NULL
            con = duckdb.connect()
            n = load_gz_into_duckdb(con, "T", gz, ["CODE", "NAME", "DT", "QTY"])
            self.assertEqual(n, 2)
            rows = con.execute('SELECT "CODE","NAME","QTY" FROM "T" ORDER BY "CODE"').fetchall()
            self.assertEqual(rows[0], ("A001", "Widget", "100"))
            self.assertEqual(rows[1][0], "A002")
            # 型別全 VARCHAR
            types = [r[1] for r in con.execute('DESCRIBE "T"').fetchall()]
            self.assertTrue(all(t == "VARCHAR" for t in types))
            con.close()

    def test_short_load_keeps_old_table(self):
        # 寧舊勿缺：載入列數明顯短於 db COUNT（串流短抽漏網 / read_csv 掉列）時
        # 必須 raise 並保留舊表——2026-07-11 事故：短 2/3 的表照樣蓋掉好表上線 7.5h。
        import duckdb
        with tempfile.TemporaryDirectory() as d:
            con = duckdb.connect()
            # 舊表：3 列完整資料
            con.execute('CREATE TABLE "T" AS SELECT * FROM (VALUES '
                        "('A1'),('A2'),('A3')) t(CODE)")
            gz = os.path.join(d, "t.csv.gz")
            with gzip.open(gz, "wt", encoding="utf-8", newline="\n") as fh:
                fh.write("B1\n")  # 新資料只有 1 列，但 db_rows 說應有 1000 列
            with self.assertRaises(ErpOracleError):
                load_gz_into_duckdb(con, "T", gz, ["CODE"], expected_rows=1000)
            rows = con.execute('SELECT COUNT(*) FROM "T"').fetchone()[0]
            self.assertEqual(rows, 3)  # 舊表原封不動
            # staging 表不殘留
            leftovers = con.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_name LIKE '%__stage'").fetchall()
            self.assertEqual(leftovers, [])
            # 對帳吻合時正常取代
            n = load_gz_into_duckdb(con, "T", gz, ["CODE"], expected_rows=1)
            self.assertEqual(n, 1)
            self.assertEqual(con.execute('SELECT * FROM "T"').fetchall(), [("B1",)])
            con.close()


class OrderingTests(unittest.TestCase):
    def test_history_backup_detected(self):
        self.assertTrue(mirror_mod._is_history_backup("SE_BOM_HIS_SIZE"))
        self.assertTrue(mirror_mod._is_history_backup("PO_MRP_ORD_BAK"))
        self.assertFalse(mirror_mod._is_history_backup("SE_BOM_SIZE"))

    def test_current_before_history_and_small_first(self):
        # (owner, table, cols, est_rows)
        targets = [
            ("SC00", "SE_BOM_HIS_SIZE", [], 4_000_000),  # 歷史大表 → 最後
            ("SC00", "SMALL_CUR", [], 10),               # 現行小表 → 最前
            ("SC00", "BIG_CUR", [], 900_000),            # 現行大表 → 中間
        ]
        ordered = [t[1] for t in mirror_mod._order_targets(targets, ["SC00"])]
        self.assertEqual(ordered, ["SMALL_CUR", "BIG_CUR", "SE_BOM_HIS_SIZE"])


class RefreshHotTests(unittest.TestCase):
    def test_hot_tables_nonempty_and_qualified(self):
        self.assertGreater(len(mirror_mod.HOT_TABLES), 10)
        # 皆為 OWNER.TABLE 形式
        self.assertTrue(all("." in t for t in mirror_mod.HOT_TABLES))

    def test_hot_tables_cover_view_sources(self):
        # 回歸：v_mrp 實際讀 PO_MRP_ITEM/PO_MRP_M（初版誤放 PO_MRP_ORD* 害 MRP 凍結）
        hot = set(mirror_mod.HOT_TABLES)
        for required in ("SC00.PO_MRP_ITEM", "SC00.PO_MRP_M", "SC00.PO_MRP_PO",
                         "SC00.PO_RCPT_D"):
            self.assertIn(required, hot)
        for wrong in ("SC00.PO_MRP_ORD", "SC00.PO_MRP_ORDITEM", "SC00.PO_MRP_ORDS"):
            self.assertNotIn(wrong, hot)

    def test_refresh_hot_delegates_with_force(self):
        captured = {}

        def fake_mirror(**kw):
            captured.update(kw)
            return {"total": len(kw.get("only_tables") or []), "done": 0,
                    "errors": 0, "count_mismatch": 0}

        d = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        orig = mirror_mod.mirror
        orig_alias = mirror_mod.sync_item_alias
        orig_man = mirror_mod._MANIFEST
        mirror_mod.mirror = fake_mirror
        # 對照同步走真 SSH——測試必須隔離（dev shell 若帶 ENABLED=1 會真連主機）；
        # manifest 也要隔離（refresh_hot 現在會把對照結果落帳進 _MANIFEST）
        mirror_mod.sync_item_alias = lambda *a, **kw: {"status": "stubbed"}
        mirror_mod._MANIFEST = os.path.join(d, "manifest.json")
        try:
            mirror_mod.refresh_hot(log=lambda m: None, atomic=False)
            man = load_manifest(mirror_mod._MANIFEST)
        finally:
            mirror_mod.mirror = orig
            mirror_mod.sync_item_alias = orig_alias
            mirror_mod._MANIFEST = orig_man
        self.assertIs(captured.get("only_tables"), mirror_mod.HOT_TABLES)
        self.assertTrue(captured.get("force"))
        self.assertEqual(man["_item_alias"]["status"], "stubbed")   # 對照結果有落帳

    def test_atomic_refresh_swaps_and_preserves_live_on_failure(self):
        import os
        import tempfile
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        live = os.path.join(d, "erp_full.duckdb")
        with open(live, "w") as fh:
            fh.write("ORIGINAL")
        man = os.path.join(d, "manifest.json")
        with open(man, "w") as fh:
            fh.write('{"SC00.OLD": {"status": "done", "ts": "2026-07-10 02:00:00"}}')
        orig_db, orig_mirror = mirror_mod._DB, mirror_mod.mirror
        orig_man = mirror_mod._MANIFEST
        orig_alias = mirror_mod.sync_item_alias
        mirror_mod._DB = live
        mirror_mod._MANIFEST = man
        # 對照同步走真 SSH——測試必須隔離（見 test_refresh_hot_delegates_with_force）
        mirror_mod.sync_item_alias = lambda *a, **kw: {"status": "stubbed"}
        try:
            # 成功：mirror 對 tmp 寫入 → 原子換進 live；manifest 也走 tmp→swap
            def ok_mirror(**kw):
                with open(kw["db_path"], "w") as fh:
                    fh.write("REFRESHED")
                with open(kw["manifest_path"], "w") as fh:
                    fh.write('{"SC00.NEW": {"status": "done"}}')
                return {"done": 21, "errors": 0}
            mirror_mod.mirror = ok_mirror
            mirror_mod.refresh_hot(log=lambda m: None)
            self.assertEqual(open(live).read(), "REFRESHED")
            self.assertIn("SC00.NEW", open(man).read())      # manifest 換成新
            self.assertIn("_item_alias", open(man).read())   # 對照落帳隨 manifest swap
            self.assertFalse(os.path.exists(live + ".refresh.tmp"))  # tmp 清乾淨
            self.assertFalse(os.path.exists(man + ".refresh.tmp"))

            # 失敗：mirror 拋例外 → live 與 manifest 都原封不動、tmp 清掉。
            # manifest 若被刷新（ts 變今天），staleness 告警會被自己的帳本騙過。
            with open(live, "w") as fh:
                fh.write("ORIGINAL2")
            with open(man, "w") as fh:
                fh.write('{"SC00.KEEP": {"status": "done", "ts": "2026-07-10 02:00:00"}}')

            def boom_mirror(**kw):
                with open(kw["db_path"], "w") as fh:
                    fh.write("PARTIAL")
                with open(kw["manifest_path"], "w") as fh:
                    fh.write('{"SC00.POISON": {"status": "done"}}')
                raise RuntimeError("mid-refresh crash")
            mirror_mod.mirror = boom_mirror
            with self.assertRaises(RuntimeError):
                mirror_mod.refresh_hot(log=lambda m: None)
            self.assertEqual(open(live).read(), "ORIGINAL2")  # live 未被污染
            self.assertIn("SC00.KEEP", open(man).read())      # manifest 未被污染
            self.assertNotIn("SC00.POISON", open(man).read())
            self.assertFalse(os.path.exists(live + ".refresh.tmp"))
            self.assertFalse(os.path.exists(man + ".refresh.tmp"))
        finally:
            mirror_mod._DB, mirror_mod.mirror = orig_db, orig_mirror
            mirror_mod._MANIFEST = orig_man
            mirror_mod.sync_item_alias = orig_alias


class RecordAliasManifestTests(unittest.TestCase):
    """_record_alias_manifest：對照同步結果落帳 manifest "_item_alias" 偽條目的狀態機。"""

    def setUp(self):
        d = tempfile.mkdtemp(prefix="alias_man_test_")
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        self.man = os.path.join(d, "manifest.json")

    def _entry(self):
        return load_manifest(self.man)["_item_alias"]

    def test_done_sets_last_done_and_clears_first_bad(self):
        mirror_mod._record_alias_manifest({"status": "error", "error": "ssh"}, self.man)
        e = self._entry()
        self.assertEqual(e["status"], "error")
        self.assertTrue(e["first_bad_ts"])
        self.assertNotIn("last_done_ts", e)      # 從未成功 → 不捏造
        mirror_mod._record_alias_manifest({"status": "done", "rows": 7}, self.man)
        e = self._entry()
        self.assertEqual((e["status"], e["rows"]), ("done", 7))
        self.assertEqual(e["last_done_ts"], e["ts"])
        self.assertNotIn("first_bad_ts", e)      # 復原 → 失敗定錨清掉

    def test_consecutive_failures_keep_anchors(self):
        # done → error → empty：last_done_ts 保留不動、first_bad_ts 錨在第一晚不重設
        mirror_mod._record_alias_manifest({"status": "done", "rows": 5}, self.man)
        done_ts = self._entry()["last_done_ts"]
        mirror_mod._record_alias_manifest({"status": "error", "error": "ssh"}, self.man)
        first_bad = self._entry()["first_bad_ts"]
        mirror_mod._record_alias_manifest({"status": "empty"}, self.man)
        e = self._entry()
        self.assertEqual(e["status"], "empty")
        self.assertEqual(e["last_done_ts"], done_ts)
        self.assertEqual(e["first_bad_ts"], first_bad)

    def test_preserves_table_entries(self):
        save_manifest({"SC00.T": {"status": "done", "ts": "2026-07-10 02:00:00"}},
                      self.man)
        mirror_mod._record_alias_manifest({"status": "done", "rows": 1}, self.man)
        man = load_manifest(self.man)
        self.assertIn("SC00.T", man)             # 真表條目原封不動

    def test_build_catalog_skips_pseudo_entries(self):
        # 回歸：偽條目無 "." 會讓 key.split(".", 1) 雙解包 ValueError 炸整輪 mirror
        import duckdb
        con = duckdb.connect()
        man = {"SC00.T": {"status": "done", "duck": "sc00_t", "db_rows": 1,
                          "loaded": 1, "cols": 2},
               "_item_alias": {"status": "done", "rows": 3}}
        mirror_mod._build_catalog(con, man)
        rows = con.execute("SELECT owner, tbl FROM _erp_catalog").fetchall()
        con.close()
        self.assertEqual(rows, [("SC00", "T")])


class SyncItemAliasTests(unittest.TestCase):
    """料號→庫存編號(O_ITEMNO 舊碼) 對照同步——mock SSH 串流，不連主機。"""

    def setUp(self):
        d = tempfile.mkdtemp(prefix="alias_test_")
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        self.db = os.path.join(d, "erp_full.duckdb")

    def test_alias_sql_passes_readonly_guard(self):
        captured = {}

        def fake_stream(sql):
            captured["sql"] = sql
            return iter([])

        orig = mirror_mod.ssh_sqlplus_stream
        mirror_mod.ssh_sqlplus_stream = fake_stream
        try:
            mirror_mod.sync_item_alias(self.db, log=lambda m: None)
        finally:
            mirror_mod.ssh_sqlplus_stream = orig
        guard_select_only(captured["sql"])   # 不 raise = 通過唯讀守門
        self.assertIn("GF_ITEM_O_ITEMNO", captured["sql"])

    def test_direct_read_failure_falls_back_to_function_decode(self):
        # SP_ITEM 直讀被撤權（ORA-00942 之類）→ 自動退回 GG_1001 函式法。
        calls = []

        def fake_stream(sql):
            calls.append(sql)
            if "FROM SP00.SP_ITEM " in sql:
                raise mirror_mod.ErpOracleError("ORA-00942: table or view does not exist")
            return iter(["SFIXI0500T003600400-A010\x01SF24"])

        orig = mirror_mod.ssh_sqlplus_stream
        mirror_mod.ssh_sqlplus_stream = fake_stream
        try:
            res = mirror_mod.sync_item_alias(self.db, log=lambda m: None)
        finally:
            mirror_mod.ssh_sqlplus_stream = orig
        self.assertEqual(res, {"status": "done", "rows": 1, "source": "legacy"})
        self.assertEqual(len(calls), 2)
        self.assertIn("GF_ITEM_O_ITEMNO", calls[1])

    def test_loads_pairs_skips_empty_alias_and_creates_view(self):
        lines = [
            "SFIXI0500T003600400-A010\x01SF24",
            "SF2XI05050700-A010\x01SF23.7",
            "BXDW2500T0000W000-A020\x01",     # 無舊碼 → 略過
            "",                                # 空行 → 略過
        ]
        orig = mirror_mod.ssh_sqlplus_stream
        mirror_mod.ssh_sqlplus_stream = lambda sql: iter(lines)
        try:
            res = mirror_mod.sync_item_alias(self.db, log=lambda m: None)
        finally:
            mirror_mod.ssh_sqlplus_stream = orig
        # 2026-07-30 起首選 SP_ITEM 直讀（GRANT 治本）——mock 第一發就回資料
        self.assertEqual(res, {"status": "done", "rows": 2, "source": "direct"})
        import duckdb
        con = duckdb.connect(self.db, read_only=True)
        rows = con.execute(
            'SELECT ITEM_NO, O_ITEMNO FROM "_item_alias" ORDER BY 1').fetchall()
        via_view = con.execute(
            'SELECT "料號" FROM v_item_alias WHERE "庫存編號" = \'SF24\'').fetchall()
        con.close()
        self.assertEqual(rows, [("SF2XI05050700-A010", "SF23.7"),
                                ("SFIXI0500T003600400-A010", "SF24")])
        self.assertEqual(via_view, [("SFIXI0500T003600400-A010",)])

    def test_fetch_failure_keeps_old_table(self):
        import duckdb
        con = duckdb.connect(self.db)
        con.execute('CREATE TABLE "_item_alias"(ITEM_NO VARCHAR, O_ITEMNO VARCHAR)')
        con.execute('INSERT INTO "_item_alias" VALUES (\'OLD-ITEM\', \'SF1\')')
        con.close()

        def boom(sql):
            raise ErpOracleError("ssh 斷線")
            yield  # pragma: no cover - 不可達，僅讓 boom 是 generator 形狀

        orig = mirror_mod.ssh_sqlplus_stream
        mirror_mod.ssh_sqlplus_stream = boom
        try:
            res = mirror_mod.sync_item_alias(self.db, log=lambda m: None)
        finally:
            mirror_mod.ssh_sqlplus_stream = orig
        self.assertEqual(res.get("status"), "error")
        con = duckdb.connect(self.db, read_only=True)
        rows = con.execute('SELECT * FROM "_item_alias"').fetchall()
        con.close()
        self.assertEqual(rows, [("OLD-ITEM", "SF1")])   # 舊對照未被覆蓋

    def test_zero_rows_keeps_old_table(self):
        import duckdb
        con = duckdb.connect(self.db)
        con.execute('CREATE TABLE "_item_alias"(ITEM_NO VARCHAR, O_ITEMNO VARCHAR)')
        con.execute('INSERT INTO "_item_alias" VALUES (\'OLD-ITEM\', \'SF1\')')
        con.close()
        orig = mirror_mod.ssh_sqlplus_stream
        mirror_mod.ssh_sqlplus_stream = lambda sql: iter([])
        try:
            res = mirror_mod.sync_item_alias(self.db, log=lambda m: None)
        finally:
            mirror_mod.ssh_sqlplus_stream = orig
        self.assertEqual(res.get("status"), "empty")
        con = duckdb.connect(self.db, read_only=True)
        rows = con.execute('SELECT * FROM "_item_alias"').fetchall()
        con.close()
        self.assertEqual(rows, [("OLD-ITEM", "SF1")])


class MirrorSshRetryTests(unittest.TestCase):
    """SSH 傳輸層斷線（ErpConnectionError）的單表重試：斷線重連救得回就 done；
    確定性錯誤（ORA-）不重試；RED_ERP_SSH_RETRY=0 可關。全程不連主機。"""

    _ONE_COL = [{"id": 1, "name": "SE_ID", "type": "VARCHAR2", "length": "5",
                 "nullable": True}]

    def _run(self, stream_effects, env=None):
        """跑一次 mirror（單表 SC00.T1、COUNT=3）。stream_effects 依呼叫序給
        stream_to_gz 的行為：Exception=拋出、int=寫入該列數的 gz。"""
        from unittest import mock
        import shutil
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(d, ignore_errors=True))
        raw = os.path.join(d, "raw")
        calls = {"n": 0}

        def fake_stream(sql, path):
            eff = stream_effects[min(calls["n"], len(stream_effects) - 1)]
            calls["n"] += 1
            if isinstance(eff, Exception):
                raise eff
            with gzip.open(path, "wt", encoding="utf-8") as fh:
                for r in range(eff):
                    fh.write(f"v{r}\n")
            return eff

        env_all = {"RED_ERP_SSH_RETRY_WAIT_S": "0"}
        env_all.update(env or {})
        man_path = os.path.join(d, "manifest.json")
        with mock.patch.dict(os.environ, env_all), \
             mock.patch.object(mirror_mod, "_DIR", d), \
             mock.patch.object(mirror_mod, "_RAW_DIR", raw), \
             mock.patch.object(mirror_mod, "iter_targets",
                               return_value=[("SC00", "T1", self._ONE_COL, 1)]), \
             mock.patch.object(mirror_mod, "count_rows", return_value=3), \
             mock.patch.object(mirror_mod, "stream_to_gz", side_effect=fake_stream):
            summary = mirror_mod.mirror(db_path=os.path.join(d, "t.duckdb"),
                                        manifest_path=man_path, log=lambda m: None)
        with open(man_path, encoding="utf-8") as fh:
            man = json.load(fh)
        return summary, man["SC00.T1"], calls["n"]

    def test_conn_break_retried_and_recovers(self):
        from agent_core.erp_oracle_client import ErpConnectionError
        summary, entry, n_stream = self._run(
            [ErpConnectionError("ERP 串流異常結束（exit 255）"), 3])
        self.assertEqual(summary["errors"], 0)
        self.assertEqual(entry["status"], "done")
        self.assertEqual(entry["loaded"], 3)
        self.assertEqual(entry["ssh_retries"], 1)   # 可觀測：靠重連救回
        self.assertEqual(n_stream, 2)

    def test_conn_break_exhausts_then_error(self):
        from agent_core.erp_oracle_client import ErpConnectionError
        summary, entry, n_stream = self._run(
            [ErpConnectionError("exit 255"), ErpConnectionError("exit 255")])
        self.assertEqual(summary["errors"], 1)
        self.assertEqual(entry["status"], "error")
        self.assertEqual(n_stream, 2)               # 預設 1+1 次就放棄，不無限重試

    def test_deterministic_error_not_retried(self):
        summary, entry, n_stream = self._run(
            [ErpOracleError("ERP 串流查詢錯誤：ORA-00942")])
        self.assertEqual(summary["errors"], 1)
        self.assertEqual(entry["status"], "error")
        self.assertEqual(n_stream, 1)               # ORA- 重跑必再錯，不浪費 deadline

    def test_retry_disabled_by_env(self):
        from agent_core.erp_oracle_client import ErpConnectionError
        summary, entry, n_stream = self._run(
            [ErpConnectionError("exit 255"), 3], env={"RED_ERP_SSH_RETRY": "0"})
        self.assertEqual(summary["errors"], 1)
        self.assertEqual(n_stream, 1)

    def test_no_retry_manifest_has_no_marker(self):
        _summary, entry, _n = self._run([3])
        self.assertEqual(entry["status"], "done")
        self.assertNotIn("ssh_retries", entry)      # 沒斷線就不留欄位，manifest 保持精簡


class ManifestTests(unittest.TestCase):
    def test_roundtrip_and_missing(self, ):
        with tempfile.TemporaryDirectory() as d:
            orig_dir, orig_man = mirror_mod._DIR, mirror_mod._MANIFEST
            mirror_mod._DIR = d
            mirror_mod._MANIFEST = os.path.join(d, "manifest.json")
            try:
                self.assertEqual(load_manifest(), {})     # 不存在 → 空
                save_manifest({"SC00.T": {"status": "done", "loaded": 5}})
                self.assertEqual(load_manifest()["SC00.T"]["loaded"], 5)
                self.assertTrue(os.path.exists(mirror_mod._MANIFEST))
            finally:
                mirror_mod._DIR, mirror_mod._MANIFEST = orig_dir, orig_man


if __name__ == "__main__":
    unittest.main()
