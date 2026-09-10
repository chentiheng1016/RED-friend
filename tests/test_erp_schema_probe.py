"""agent_core/erp_schema_probe 單元測試 —— 不需 ERP 主機、不需 oracledb。

以依賴注入的假 fetch 取代 erp_oracle_client.fetch_table（不 patch 全域，避免跨測試洩漏），
所以完全不會觸發 SSH / is_enabled 閘門。最關鍵的一測：把每一條產生的 SQL 丟進**真正的**
guard_select_only，證明探勘查詢確實被唯讀守門放行（防止不小心用到 comment 之類禁字）。
"""
import tempfile
import unittest
from pathlib import Path

from agent_core import erp_schema_probe as probe_mod
from agent_core.erp_oracle_client import ErpOracleError, guard_select_only
from agent_core.erp_schema_probe import (
    extract_source,
    logic_verdict,
    probe,
    probe_source,
    render_markdown,
    sql_columns,
    sql_constraints,
    sql_detect_owner,
    sql_logic_inventory,
    sql_owners,
    sql_sequence_count,
    sql_source_stream,
    sql_tables,
    sql_trigger_inventory,
    sql_views,
    validate_owner,
    write_source_files,
)


def _fake_fetch(canned):
    """回一個依 SQL 內容派發罐頭列的假 fetch(sql, headers, limit=...)。"""
    def fetch(sql, headers, limit=None):
        if "table_name IN (" in sql and "all_tables" in sql:
            key = "detect"
        elif "all_tab_columns" in sql:
            key = "columns"
        elif "all_tables t" in sql:
            key = "tables"
        elif "all_tables GROUP BY owner" in sql:
            key = "owners"
        elif "all_constraints" in sql:
            key = "constraints"
        elif "all_views" in sql:
            key = "views"
        elif "all_source" in sql:
            key = "logic"
        elif "all_triggers" in sql:
            key = "triggers"
        elif "all_sequences" in sql:
            key = "sequences"
        else:  # pragma: no cover - 測試若走到這代表派發漏了
            raise AssertionError(f"未預期的 SQL：{sql[:80]}")
        return {"columns": headers, "rows": canned.get(key, []), "truncated": False}
    return fetch


_CANNED = {
    "detect": [["APP", "2"]],
    "tables": [["BQ_SE_ORDITEM", "2306", "訂單主檔"],
               ["BQ_SE_ITEMSCHE", "35618", ""]],
    "columns": [["BQ_SE_ORDITEM", "9", "SE_ID", "VARCHAR2", "20", "N", "訂單單號"],
                ["BQ_SE_ORDITEM", "40", "SE_QTY", "NUMBER", "8", "Y", ""],
                ["BQ_SE_ITEMSCHE", "11", "ITEM_NO", "VARCHAR2", "30", "N", "物料編號"]],
    "constraints": [["BQ_SE_ORDITEM", "P", "SE_ID", ""],
                    ["BQ_SE_ITEMSCHE", "R", "SE_ID", "PK_ORDITEM"]],
    "views": [["V_ORDER_SUMMARY"], ["V_MATERIAL_GAP"]],
    "logic": [["PACKAGE BODY", "12", "3400"], ["TRIGGER", "5", "220"]],
    "triggers": [["5", "3"]],
    "sequences": [["7"]],
}


class GuardComplianceTests(unittest.TestCase):
    """每條探勘 SQL 都必須過真正的 guard_select_only（唯讀守門）。"""

    def test_all_generated_sql_passes_guard(self):
        sqls = [
            sql_detect_owner(), sql_owners(), sql_tables("APP"), sql_columns("APP"),
            sql_constraints("APP"), sql_views("APP"), sql_logic_inventory("APP"),
            sql_trigger_inventory("APP"), sql_sequence_count("APP"),
            sql_source_stream("APP"),
        ]
        for sql in sqls:
            with self.subTest(sql=sql[:50]):
                # 不 raise 即通過；回傳應為非空 SELECT
                self.assertTrue(guard_select_only(sql).lower().startswith("select"))

    def test_owner_is_inlined(self):
        self.assertIn("'APP'", sql_tables("APP"))
        self.assertIn("'APP'", sql_logic_inventory("APP"))
        self.assertIn("all_source", sql_logic_inventory("APP"))


class ValidateOwnerTests(unittest.TestCase):
    def test_uppercases_valid(self):
        self.assertEqual(validate_owner("app"), "APP")
        self.assertEqual(validate_owner("  Scott_1  "), "SCOTT_1")

    def test_rejects_injection_and_bad(self):
        for bad in ["APP; DROP TABLE X", "APP'--", "1ABC", "", "a" * 31, "APP OR 1=1", "a b"]:
            with self.subTest(bad=bad):
                with self.assertRaises(ErpOracleError):
                    validate_owner(bad)


class ProbeAssemblyTests(unittest.TestCase):
    def test_probe_autodetects_and_assembles(self):
        report = probe(None, fetch=_fake_fetch(_CANNED))
        self.assertEqual(report["owner"], "APP")
        self.assertEqual(report["summary"]["tables"], 2)
        self.assertEqual(report["summary"]["columns"], 3)
        self.assertEqual(report["summary"]["views"], 2)

    def test_logic_totals(self):
        report = probe("APP", fetch=_fake_fetch(_CANNED))
        logic = report["logic"]
        self.assertEqual(logic["total_lines"], 3620)
        self.assertEqual(logic["total_objects"], 17)
        self.assertEqual(logic["triggers"], 5)
        self.assertEqual(logic["trigger_tables"], 3)
        self.assertEqual(logic["sequences"], 7)

    def test_keys_attached_to_tables(self):
        report = probe("APP", fetch=_fake_fetch(_CANNED))
        by_name = {t["name"]: t for t in report["tables"]}
        self.assertEqual(by_name["BQ_SE_ORDITEM"]["pk"], ["SE_ID"])
        self.assertEqual(by_name["BQ_SE_ITEMSCHE"]["fk"],
                         [{"column": "SE_ID", "references": "PK_ORDITEM"}])
        self.assertEqual(by_name["BQ_SE_ORDITEM"]["est_rows"], 2306)

    def test_columns_grouped_by_table(self):
        report = probe("APP", fetch=_fake_fetch(_CANNED))
        by_name = {t["name"]: t for t in report["tables"]}
        self.assertEqual(len(by_name["BQ_SE_ORDITEM"]["columns"]), 2)
        c0 = by_name["BQ_SE_ORDITEM"]["columns"][0]
        self.assertEqual(c0["name"], "SE_ID")
        self.assertFalse(c0["nullable"])
        self.assertEqual(c0["comment"], "訂單單號")

    def test_detect_none_raises(self):
        empty = _fake_fetch({"detect": []})
        with self.assertRaises(ErpOracleError):
            probe(None, fetch=empty)

    def test_markdown_has_sections(self):
        report = probe("APP", fetch=_fake_fetch(_CANNED))
        md = render_markdown(report)
        for token in ["邏輯落點量測", "BQ_SE_ORDITEM", "訂單單號", "V_ORDER_SUMMARY", "主鍵：SE_ID"]:
            self.assertIn(token, md)


class LogicVerdictTests(unittest.TestCase):
    def test_empty_means_forms(self):
        self.assertIn("Forms", logic_verdict({"total_lines": 0, "triggers": 0}))

    def test_small_is_partial(self):
        self.assertIn("部分", logic_verdict({"total_lines": 500, "triggers": 2}))

    def test_substantial_is_worth_it(self):
        v = logic_verdict({"total_lines": 5000, "triggers": 5, "total_objects": 20})
        self.assertIn("實質材料", v)


def _fake_source_stream(lines):
    """回一個 stream(sql)→iterator 的假 all_source 串流（忽略 sql、吐罐頭行）。"""
    def stream(_sql):
        return iter(lines)
    return stream


# type\x01name\x01line\x01text —— 兩個 package body（行序刻意打亂驗排序）+ 一個 trigger
_SRC_LINES = [
    "PACKAGE BODY\x01PKG_ORDER\x012\x01  x := 1;",
    "PACKAGE BODY\x01PKG_ORDER\x011\x01PROCEDURE do_it IS",
    "PACKAGE BODY\x01PKG_ORDER\x013\x01END;",
    "TRIGGER\x01TRG_AUDIT\x011\x01BEGIN NULL; END;",
    "",  # 空行應被跳過
    "PACKAGE BODY\x01PKG_MISC\x011\x01BEGIN NULL; END;",
]


class ExtractSourceTests(unittest.TestCase):
    def test_source_stream_sql_passes_guard(self):
        self.assertTrue(
            guard_select_only(sql_source_stream("APP")).lower().startswith("select")
        )
        self.assertIn("all_source", sql_source_stream("APP"))
        self.assertIn("'APP'", sql_source_stream("APP"))

    def test_reassembles_body_in_line_order(self):
        bodies = extract_source("APP", stream=_fake_source_stream(_SRC_LINES))
        self.assertEqual(
            bodies[("PACKAGE BODY", "PKG_ORDER")],
            "PROCEDURE do_it IS\n  x := 1;\nEND;",  # 依 line 1,2,3 重組、tab 縮排保留
        )
        self.assertEqual(bodies[("TRIGGER", "TRG_AUDIT")], "BEGIN NULL; END;")
        self.assertIn(("PACKAGE BODY", "PKG_MISC"), bodies)
        self.assertEqual(len(bodies), 3)  # 空行不成物件

    def test_malformed_line_skipped(self):
        bad = ["PACKAGE\x01ONLY_THREE\x011", "PACKAGE BODY\x01OK\x011\x01BEGIN"]
        bodies = extract_source("APP", stream=_fake_source_stream(bad))
        self.assertEqual(set(bodies), {("PACKAGE BODY", "OK")})

    def test_write_source_files_splits_by_type(self):
        bodies = extract_source("APP", stream=_fake_source_stream(_SRC_LINES))
        with tempfile.TemporaryDirectory() as tmp:
            counts = write_source_files(bodies, "APP", out_dir=tmp)
            base = Path(tmp) / "source" / "APP"
            pkg = base / "package_bodies" / "PKG_ORDER.sql"
            self.assertTrue(pkg.exists())
            self.assertIn("PROCEDURE do_it IS", pkg.read_text(encoding="utf-8"))
            self.assertTrue((base / "triggers" / "TRG_AUDIT.sql").exists())
            self.assertEqual(counts["PACKAGE BODY"], 2)
            self.assertEqual(counts["TRIGGER"], 1)

    def test_probe_source_autodetects_and_reports(self):
        report = probe_source(
            None,
            fetch=_fake_fetch(_CANNED),  # 只為 detect_owner
            stream=_fake_source_stream(_SRC_LINES),
            out_dir=tempfile.mkdtemp(),
        )
        self.assertEqual(report["owner"], "APP")
        self.assertEqual(report["objects"], 3)
        self.assertEqual(report["total_files"], 3)

    def test_filename_sanitizes_path_traversal(self):
        bodies = {("PROCEDURE", "../../etc/passwd"): "SELECT 1"}
        with tempfile.TemporaryDirectory() as tmp:
            write_source_files(bodies, "APP", out_dir=tmp)
            # 不得逃出 out_dir；名字裡的 / .. 應被替成 _
            escaped = list((Path(tmp) / "source" / "APP" / "procedures").glob("*.sql"))
            self.assertEqual(len(escaped), 1)
            self.assertNotIn("/", escaped[0].name.replace(".sql", ""))


class ModuleWiringTests(unittest.TestCase):
    def test_default_fetch_is_real_client(self):
        # 預設 fetch 應綁到真正的 erp_oracle_client.fetch_table（生產走 SSH+sqlplus）。
        from agent_core import erp_oracle_client
        self.assertIs(probe_mod.fetch_table, erp_oracle_client.fetch_table)

    def test_default_stream_is_real_client(self):
        from agent_core import erp_oracle_client
        self.assertIs(probe_mod.ssh_sqlplus_stream, erp_oracle_client.ssh_sqlplus_stream)


if __name__ == "__main__":
    unittest.main()
