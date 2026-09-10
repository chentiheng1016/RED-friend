"""skills/erp_kitting 齊套引擎測試 —— 臨時 DuckDB 假鏡像端到端 + 純 Python 引擎單元測。

不碰真 DB / Gemini。固定 today=2026-07-24 保證判定可重現。
"""
import datetime
import os
import shutil
import tempfile
import unittest
from unittest import mock

import skills.erp_kitting as ek

_TODAY = datetime.date(2026, 7, 24)


def _build_db(path, *, orders=(), materials=(), stock=(), po=(), mrp=(),
              proc=(), itemsche=(), wo=(), disp=(), prod=(), stock_lot=None):
    """建立只含引擎會摸到的欄位的假鏡像。

    stock_lot=None 時從 stock 衍生（全掛 '0' 批＝可用）——既有測試語意不變；
    要測 M01/預購批就顯式給 [(倉, 料號, 批號, 量), ...]。"""
    import duckdb
    con = duckdb.connect(path)
    con.execute("CREATE TABLE v_orders(單號 VARCHAR, 客戶 VARCHAR, 客戶PO VARCHAR, "
                "單據日 VARCHAR, 鞋款 VARCHAR, 數量 BIGINT, 狀態 VARCHAR, 交期 VARCHAR)")
    con.execute("CREATE TABLE v_order_materials(單號 VARCHAR, 料號 VARCHAR, 需求 DOUBLE, "
                "訂購 DOUBLE, 點收 DOUBLE, 發料 DOUBLE, 材料類型 VARCHAR, 品名描述 VARCHAR)")
    con.execute("CREATE TABLE v_stock(倉庫代碼 VARCHAR, 料號 VARCHAR, 單位 VARCHAR, "
                "結存數量 DOUBLE)")
    con.execute("CREATE TABLE v_stock_lot(倉庫代碼 VARCHAR, 料號 VARCHAR, "
                "批號 VARCHAR, 結存數量 DOUBLE)")
    con.execute("CREATE TABLE v_purchase_orders(採購單號 VARCHAR, 料號 VARCHAR, "
                "採購單位 VARCHAR, 訂購數量 DOUBLE, 已收數量 DOUBLE, "
                "計劃到貨日 VARCHAR, 明細狀態 VARCHAR, 供應商簡稱 VARCHAR, "
                "下單日期 VARCHAR)")
    con.execute("CREATE TABLE SC00__PO_MRP_PO(SE_ID VARCHAR, ITEM_NO VARCHAR, "
                "ORDER_NO VARCHAR, ORDER_QTY VARCHAR)")
    con.execute("CREATE TABLE SC00__PO_PROC_D(PROC_NO VARCHAR, ITEM_NO VARCHAR, "
                "PROC_QTY VARCHAR, RCPT_QTY VARCHAR, PLAN_DATE VARCHAR, STATUS VARCHAR)")
    con.execute("CREATE TABLE SC00__SE_ITEMSCHE_M(SE_ID VARCHAR, ITEM_NO VARCHAR, "
                "PR_TYPE VARCHAR, IS_PR VARCHAR)")
    con.execute("CREATE TABLE v_work_orders(工單號 VARCHAR, 訂單號 VARCHAR)")
    con.execute("CREATE TABLE v_dispatch(工單號 VARCHAR)")
    con.execute("CREATE TABLE v_production(訂單號 VARCHAR, 生產日期 VARCHAR, "
                "站別 VARCHAR, 異動類型 VARCHAR, 生產數量 BIGINT)")
    if stock_lot is None:
        stock_lot = [(wh, item, "0", qty) for wh, item, _u, qty in stock]
    # v_purchase_orders 後面補了 供應商簡稱/下單日期 兩欄（見底料的採購狀態要用）。
    # 既有測試的 po tuple 是 7 欄位置式，這裡補空值讓它們不用改。
    po = [tuple(r) + ("",) * (9 - len(r)) for r in po]
    ins = {"v_orders": orders, "v_order_materials": materials, "v_stock": stock,
           "v_stock_lot": stock_lot,
           "v_purchase_orders": po, "SC00__PO_MRP_PO": mrp, "SC00__PO_PROC_D": proc,
           "SC00__SE_ITEMSCHE_M": itemsche, "v_work_orders": wo, "v_dispatch": disp,
           "v_production": prod}
    for table, rows in ins.items():
        for r in rows:
            ph = ", ".join("?" for _ in r)
            con.execute(f"INSERT INTO {table} VALUES ({ph})", list(r))
    con.close()


def _patch_env(cls, dbpath):
    for p in (mock.patch.object(ek, "_db_path", return_value=dbpath),
              mock.patch.object(ek, "_today", return_value=_TODAY)):
        p.start()
        cls.addClassCleanup(p.stop)


class KittingEndToEndTests(unittest.TestCase):
    """端到端：跑真 SQL（假資料），驗 kitting_check / kitting_alert 全流程。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="erp_kitting_test_")
        cls.db = os.path.join(cls.tmp, "fake.duckdb")
        _build_db(
            cls.db,
            orders=[
                # A：乾淨單（齊/在途/庫存可補/收貨流水齊/1項無法判定=20%<30%）→ ✅
                ("JFA001", "DECA", "PO-A", "2026-06-01", "STYLE-A", 1000, "生效", "2026-08-23"),
                # B：M3 庫存被 A/H 先搶走 → 缺料無來源 → 🔴
                ("JFB002", "DECA", "PO-B", "2026-06-01", "STYLE-B", 800, "生效", "2026-09-02"),
                # C：交期已過未上線 → 殭屍/催排（不搶 FCFS 庫存）
                ("JFC003", "DECA", "PO-C", "2026-05-01", "STYLE-C", 500, "生效", "2026-07-01"),
                # E：在途 ETA 已逾期 → 🟠
                ("JFE005", "LURCHI", "PO-E", "2026-06-01", "STYLE-E", 300, "生效", "2026-08-10"),
                # U：庫存單位 ≠ 採購單位 → 單位待換算 🟠
                ("JFU009", "DECA", "PO-U", "2026-06-01", "STYLE-U", 200, "生效", "2026-08-18"),
                # F：結構性無 BOM 客戶 → 白名單排除
                ("JFF006", "Khai Hoan 3", "PO-F", "2026-06-01", "STYLE-F", 400, "生效", "2026-08-15"),
                # G：完工單 → 完全不進分析
                ("JFG007", "DECA", "PO-G", "2026-06-01", "STYLE-G", 100, "完工", "2026-08-20"),
                # H：已上線（有入站）→ 進 FCFS pool、但不進 alert scope
                ("JFH008", "DECA", "PO-H", "2026-06-01", "STYLE-H", 600, "生效", "2026-08-25"),
                # 校準用歷史完工單
                ("DONE1", "DECA", "PO-D1", "2025-05-01", "STYLE-D", 100, "完工", "2025-08-01"),
            ],
            materials=[
                ("JFA001", "M1", 100, 100, 100, 0, "織帶", "尼龍織帶"),      # 點收 → 齊
                ("JFA001", "M2", 100, 100, 0, 0, "織帶", "反光織帶"),        # PO在途(ETA未來)
                ("JFA001", "M3", 50, 0, 0, 0, "織帶", "共用鬆緊帶"),         # 庫存可補(FCFS首位)
                ("JFA001", "M4", 80, 0, 0, 0, "膠水", "PU接著劑"),           # 不可信料類 → 無法判定
                ("JFA001", "M5", 0.5, 0, 0, 0, "車線", "縫線殘渣"),          # < min_need → 濾掉
                ("JFA001", "M12", 25, 25, 0, 0, "織帶", "委外收貨織帶"),     # DJ已收 → 齊(收貨流水)
                ("JFB002", "M3", 30, 0, 0, 0, "織帶", "共用鬆緊帶"),         # 庫存被搶光 → 🔴
                ("JFB002", "M11", 25, 25, 0, 0, "織帶", "委外加工帶"),       # DJ在途(ETA未來)
                ("JFB002", "M13", 10, 0, 0, 0, "織帶", "廢料倉誘餌"),        # SW倉不可用 → 🔴
                ("JFC003", "M9", 500, 0, 0, 0, "織帶", "殭屍需求帶"),        # 殭屍：不搶庫存
                ("JFE005", "M8", 70, 70, 0, 0, "織帶", "逾期在途帶"),        # ETA已過 → 在途逾期
                ("JFE005", "M14", 15, 15, 0, 0, "織帶", "孤兒委外帶"),       # MRP指向不在鏡像的DJ單
                ("JFU009", "M7", 30, 0, 0, 0, "織帶", "碼裝織帶"),           # 單位 M vs Y
                ("JFH008", "M3", 20, 0, 0, 0, "織帶", "共用鬆緊帶"),         # FCFS 第二位
                # 料類可信度校準：織帶發料足額(可信)、膠水零發料(不可信)
                ("DONE1", "X1", 100, 100, 100, 100, "織帶", "歷史織帶"),
                ("DONE1", "X2", 100, 0, 0, 0, "膠水", "歷史膠水"),
            ],
            stock=[
                ("RFW", "M3", "PCS", 60),    # A 拿 50、H 拿 10、B 落空
                ("RFW", "M7", "M", 30),      # 單位 M ≠ 採購單位 Y
                ("RFW", "M9", "PCS", 100),   # 殭屍 C 不可扣（驗 FCFS 排除）
                ("SW", "M13", "PCS", 999),   # 廢料倉 → 不算可用
            ],
            po=[
                ("J0P1", "M2", "PCS", 100, 0, "2026-08-10", "生效"),
                ("J0P2", "M8", "PCS", 70, 0, "2026-07-01", "生效"),   # ETA 已過
                ("J0P4", "M7", "Y", 50, 50, "2026-06-01", "結案"),    # 只為建立單位對照
            ],
            mrp=[
                ("JFA001", "M2", "J0P1", "100"),
                ("JFE005", "M8", "J0P2", "70"),
                ("JFB002", "M11", "DJ01", "25"),
                ("JFA001", "M12", "DJ02", "25"),
                ("JFE005", "M14", "DJMISS", "15"),   # DJMISS 不在 proc → 孤兒配額
            ],
            proc=[
                ("DJ01", "M11", "25", "0", "2026-08-20", "0"),     # 委外在途
                ("DJ02", "M12", "25", "25", "2026-06-15", "99"),   # 委外已收結案
            ],
            itemsche=[("JFA001", "M6", "", "N")],   # 客供（本測試 A 沒這料項，驗不誤爆）
            wo=[("W1", "JFA001")],
            disp=[("W1",)],
            prod=[("JFH008", "2026-07-10", "裁斷", "入站", 100)],
        )
        _patch_env(cls, cls.db)

    # ---- kitting_alert ----
    def test_alert_red_orange_scope(self):
        out = ek.kitting_alert()
        self.assertIn("🧩", out)
        self.assertIn("JFB002", out)                    # 🔴 缺料無來源
        self.assertIn("共用鬆緊帶", out)
        self.assertIn("JFE005", out)                    # 🟠 在途逾期
        self.assertNotIn("JFU009", out)                 # 單位待換算=🟡資訊層，不洗版
        self.assertIn("🟡1", out)                       # 但計入 header 統計
        self.assertIn("卡單料", out)                    # 料號聚合視圖
        self.assertNotIn("JFA001", out)                 # ✅ 不洗版
        self.assertNotIn("JFH008", out)                 # 已上線不進 alert scope
        self.assertNotIn("JFG007", out)                 # 完工單不存在於分析
        self.assertIn("Khai Hoan 3", out)               # 白名單說明

    def test_alert_overdue_unstarted_list(self):
        out = ek.kitting_alert()
        self.assertIn("催排", out)
        self.assertIn("JFC003", out)

    def test_alert_accepts_float_params(self):
        # 模型常傳 5.0 這種 float——不可整次炸掉
        out = ek.kitting_alert(top=5.0, min_need=1)
        self.assertIn("🧩", out)
        self.assertIn("🔴", ek.kitting_check("JFB002", top=3.0))

    def test_orphan_mrp_quota_becomes_overdue_transit(self):
        # MRP 配額指向不在鏡像的委外單：不可靜默消失，要當「在途但單據不可考」催辦
        out = ek.kitting_check("JFE005")
        self.assertIn("M14", out)
        self.assertIn("在途逾期", out)

    def test_alert_conflict_table(self):
        out = ek.kitting_alert()
        self.assertIn("共用料衝突", out)
        self.assertIn("M3", out)
        # 殭屍單的 M9 不得進衝突（殭屍不在 FCFS pool）
        self.assertNotIn("M9", out.split("共用料衝突")[1].split("📋")[0])

    def test_alert_window_excludes_far_orders(self):
        # 窗縮到 5 天：紅橘全在窗外 → 只剩殭屍催排
        out = ek.kitting_alert(days_ahead=5)
        self.assertNotIn("JFB002", out)
        self.assertIn("JFC003", out)

    # ---- kitting_check ----
    def test_check_by_order_red_detail(self):
        out = ek.kitting_check("JFB002")
        self.assertIn("🔴", out)
        self.assertIn("缺料無來源", out)
        self.assertIn("M3", out)
        self.assertIn("在途1", out)          # M11 委外在途 → bucket 計數
        self.assertNotIn("M11", out)         # 但不進缺料明細表

    def test_check_green_order_buckets(self):
        out = ek.kitting_check("JFA001")
        self.assertTrue(out.startswith("✅"))
        self.assertIn("齊", out)
        self.assertIn("庫存可補", out)
        self.assertIn("無法判定", out)          # 膠水零訊號誠實標示
        self.assertNotIn("M5", out)             # 微量列被 min_need 濾掉
        self.assertNotIn("缺料無來源", out)     # M12 靠委外收貨份額判齊、不誤紅

    def test_check_by_customer_sorted(self):
        out = ek.kitting_check("DECA")
        self.assertIn("JFA001", out)
        self.assertIn("JFB002", out)
        self.assertNotIn("JFG007", out)         # 完工單不出現

    def test_check_unit_mismatch_is_yellow_info(self):
        out = ek.kitting_check("JFU009")
        self.assertTrue(out.startswith("🟡"))
        self.assertIn("單位待換算", out)
        self.assertNotIn("🔴無來源", out)

    def test_check_no_bom_customer(self):
        out = ek.kitting_check("Khai Hoan")
        self.assertIn("結構性無 BOM", out)

    # ---- item 聚焦（2026-07-29 UserA SF24 案）----
    def test_check_item_focus_shows_only_matched_material(self):
        out = ek.kitting_check("JFB002", item="M3")
        self.assertIn("M3", out)
        self.assertIn("缺料無來源", out)
        self.assertNotIn("M11", out)          # 其他料不列
        self.assertIn("JFB002", out)

    def test_check_item_focus_includes_non_shortage_buckets(self):
        # 齊/可補的料也要給判定（UserA 案：料不在缺料表 top 就被腦補成短少 0）
        out = ek.kitting_check("JFA001", item="M1")
        self.assertIn("M1", out)
        self.assertIn("齊", out)

    def test_check_item_focus_no_match_says_so(self):
        out = ek.kitting_check("JFA001", item="不存在料")
        self.assertIn("無符合", out)
        self.assertIn("JFA001", out)

    def test_check_item_focus_alias_expands_old_code(self):
        import duckdb
        con = duckdb.connect(self.db)
        con.execute("CREATE TABLE IF NOT EXISTS _item_alias("
                    "ITEM_NO VARCHAR, O_ITEMNO VARCHAR)")
        con.execute("INSERT INTO _item_alias VALUES ('M3', 'SF99')")
        con.close()
        try:
            import agent_core.erp_stock_query as esq
            with mock.patch.object(esq, "_db_path", return_value=self.db):
                out = ek.kitting_check("JFB002", item="SF99")
            self.assertIn("庫存編號", out)     # ℹ️ 對照註記
            self.assertIn("M3", out)
            self.assertIn("缺料無來源", out)
        finally:
            con = duckdb.connect(self.db)
            con.execute("DROP TABLE _item_alias")
            con.close()

    def test_kitting_check_in_employee_whitelists(self):
        from agent_core.dept_tool_scope import allowed_tool_names_for_color
        self.assertIn("kitting_check", allowed_tool_names_for_color("yellow"))
        self.assertIn("kitting_check", allowed_tool_names_for_color("indigo"))

    def test_check_not_found_and_empty(self):
        self.assertIn("查無符合", ek.kitting_check("NOPE999"))
        self.assertIn("訂單單號", ek.kitting_check(""))


class KittingQuietPathTests(unittest.TestCase):
    """全綠世界 → alert 回 (無新發現)，daemon 不推播。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="erp_kitting_quiet_")
        cls.db = os.path.join(cls.tmp, "fake.duckdb")
        _build_db(
            cls.db,
            orders=[("JFA001", "DECA", "PO-A", "2026-06-01", "S", 100, "生效", "2026-08-23"),
                    ("DONE1", "DECA", "PO-D", "2025-05-01", "S", 100, "完工", "2025-08-01")],
            materials=[("JFA001", "M1", 100, 100, 100, 0, "織帶", "帶"),
                       ("DONE1", "X1", 100, 100, 100, 100, "織帶", "帶")],
        )
        _patch_env(cls, cls.db)

    def test_quiet(self):
        self.assertEqual(ek.kitting_alert(), "(無新發現)")


class YellowUrgentGateTests(unittest.TestCase):
    """交期逼近但大半料項無資料可判的 🟡 單，不可被「(無新發現)」閘門吞掉。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="erp_kitting_yellow_")
        cls.db = os.path.join(cls.tmp, "fake.duckdb")
        _build_db(
            cls.db,
            orders=[("JFY001", "DECA", "PO-Y", "2026-06-01", "S", 100, "生效", "2026-07-27"),
                    ("DONE1", "DECA", "PO-D", "2025-05-01", "S", 100, "完工", "2025-08-01")],
            materials=[("JFY001", "G1", 40, 0, 0, 0, "膠水", "膠A"),
                       ("JFY001", "G2", 40, 0, 0, 0, "膠水", "膠B"),
                       ("DONE1", "X2", 100, 0, 0, 0, "膠水", "歷史膠水")],
        )
        _patch_env(cls, cls.db)

    def test_yellow_urgent_fires_alert(self):
        out = ek.kitting_alert()
        self.assertNotEqual(out, "(無新發現)")
        self.assertIn("🟡", out)
        self.assertIn("JFY001", out)
        self.assertIn("不可驗項數", out)


class BatchSemanticsTests(unittest.TestCase):
    """可用庫存＝0 批（2026-07-29 UserA 案）：M01/預購批不進 FCFS 池、聚焦表列待確認。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="erp_kitting_batch_")
        cls.db = os.path.join(cls.tmp, "fake.duckdb")
        _build_db(
            cls.db,
            orders=[("JFM001", "DECA", "PO-M", "2026-06-01", "STYLE-M",
                     100, "生效", "2026-08-23"),
                    # 料類可信度校準：織帶完工單發料足額 → 可信
                    ("DONE1", "DECA", "PO-D1", "2025-05-01", "STYLE-D",
                     100, "完工", "2025-08-01")],
            materials=[("JFM001", "MM1", 40, 0, 0, 0, "織帶", "M01批織帶"),
                       ("DONE1", "X1", 100, 100, 100, 100, "織帶", "歷史織帶")],
            stock=[("RFW", "MM1", "PCS", 50)],
            # 全倉結存 50，但可用(0批)只有 3——M01 的 47 要倉庫確認調撥
            stock_lot=[("RFW", "MM1", "0", 3), ("RFW", "MM1", "M01", 47)],
        )
        _patch_env(cls, cls.db)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_m01_not_in_fcfs_pool(self):
        # 全批口徑會給「庫存可補」（50≥40）；0 批口徑 3<40 → 缺料無來源
        out = ek.kitting_check("JFM001")
        self.assertIn("缺料無來源", out)
        self.assertNotIn("庫存可補1", out)

    def test_item_focus_shows_pending_column(self):
        out = ek.kitting_check("JFM001", item="MM1")
        self.assertIn("待確認批", out)
        self.assertIn("47", out)                  # M01 量列在待確認欄
        self.assertIn("37", out)                  # 缺 = 40 − 可用 3
        self.assertIn("要倉庫實地確認調撥到 0 批", out)

    def test_pooled_verdict_transfer_sufficient_not_purchase(self):
        # 池(47) ≥ 缺(37)：料在廠內 M01，不催開採購單、也不給 ⛔ 採購面行——
        # 但要講明「調撥後可補 ≠ 已可發料」
        out = ek.kitting_check("JFM001", item="MM1")
        self.assertIn("調撥到 0 批後可補", out)
        self.assertIn("**非**已可發料", out)
        self.assertNotIn("需開 ERP 採購單", out)
        self.assertNotIn("⛔ 採購面", out)

    def test_fallback_to_full_stock_when_lot_view_missing(self):
        import duckdb
        con = duckdb.connect(self.db)
        con.execute("ALTER TABLE v_stock_lot RENAME TO v_stock_lot_bak")
        con.close()
        try:
            out = ek.kitting_check("JFM001")
            self.assertIn("庫存可補", out)        # 舊鏡像退回全批口徑（50≥40）
        finally:
            con = duckdb.connect(self.db)
            con.execute("ALTER TABLE v_stock_lot_bak RENAME TO v_stock_lot")
            con.close()


class PurchaseSideVerdictTests(unittest.TestCase):
    """採購面判死（2026-07-29 UserA 案二，仿真實 SF24 數字）：兩張新單訂購=0
    （手工 Excel 下單、ERP 沒單），合計缺 88.5 ＞ M01 池 26.43——不能講「調撥後
    即可發料」，要判「採購面缺料、調撥仍缺 62.1 需開 ERP 採購」。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="erp_kitting_purchase_")
        cls.db = os.path.join(cls.tmp, "fake.duckdb")
        _build_db(
            cls.db,
            orders=[
                ("JFC26490", "JALAS", "PO-1", "2026-07-01", "JA1055",
                 1260, "生效", "2026-10-12"),
                ("JFC26491", "JALAS", "PO-2", "2026-07-01", "JA1065",
                 1220, "生效", "2026-10-12"),
                # 料類可信度校準：織帶完工單發料足額 → 可信
                ("DONE1", "DECA", "PO-D1", "2025-05-01", "STYLE-D",
                 100, "完工", "2025-08-01"),
            ],
            materials=[
                ("JFC26490", "SFX1", 46.51, 0, 0, 0, "織帶", "BIAG 3.6MM 中底板"),
                ("JFC26491", "SFX1", 45.35, 0, 0, 0, "織帶", "BIAG 3.6MM 中底板"),
                ("DONE1", "X1", 100, 100, 100, 100, "織帶", "歷史織帶"),
            ],
            stock=[("RFW", "SFX1", "M2", 29.78)],
            # 0 批只有 3.35 可用；26.43 卡在 M01 待倉庫調撥（全料號一池）
            stock_lot=[("RFW", "SFX1", "0", 3.35), ("RFW", "SFX1", "M01", 26.43)],
        )
        _patch_env(cls, cls.db)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_focus_table_has_order_qty_column(self):
        out = ek.kitting_check("JFC26490", item="SFX1")
        self.assertIn("訂購", out)                 # ERP 採購面欄位直接可對
        self.assertIn("缺料無來源", out)

    def test_per_order_purchase_line_fires_when_no_erp_po(self):
        out = ek.kitting_check("JFC26490", item="SFX1")
        self.assertIn("⛔ 採購面：", out)
        self.assertIn("未開(足) ERP 採購單", out)
        self.assertIn("手工下單", out)

    def test_pooled_verdict_requires_erp_po_when_transfer_insufficient(self):
        out = ek.kitting_check("JFC264", item="SFX1")   # 兩張單都命中
        self.assertIn("採購面判定 SFX1", out)
        self.assertIn("2 張單合計缺 88.5", out)         # 43.2 + 45.4（扣 0批 3.35）
        self.assertIn("即使全數調撥仍缺 62.1", out)     # 88.5 − 26.43
        self.assertIn("需開 ERP 採購單補量", out)
        self.assertIn("跨單不可加總", out)               # M01 池不可逐單重複計

    def test_pending_pool_not_double_counted_per_order(self):
        # 兩張單的聚焦表各列待確認 26.4（同一池）——池判定只能用 26.43 一次
        out = ek.kitting_check("JFC264", item="SFX1")
        self.assertNotIn("52.9", out)                   # 26.43×2 的影子不得出現


class UnitMismatchPurchaseVerdictTests(unittest.TestCase):
    """實案 SF24 真正走的分支：stock 單位 'M' vs 採購單位 '59/M2' → 判定=單位待換算。
    需求/訂購同表同單位恆可比——採購面判定不得因單位字串不一致而沉默；
    庫存側數字掛「如同單位」保留、不做無 hedge 跨單位結論。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="erp_kitting_unitpv_")
        cls.db = os.path.join(cls.tmp, "fake.duckdb")
        _build_db(
            cls.db,
            orders=[
                ("JFC26490", "JALAS", "PO-1", "2026-07-01", "JA1055",
                 1260, "生效", "2026-10-12"),
                ("JFC26491", "JALAS", "PO-2", "2026-07-01", "JA1065",
                 1220, "生效", "2026-10-12"),
                ("DONE1", "DECA", "PO-D1", "2025-05-01", "STYLE-D",
                 100, "完工", "2025-08-01"),
            ],
            materials=[
                ("JFC26490", "SFX1", 46.51, 0, 0, 0, "織帶", "BIAG 3.6MM 中底板"),
                ("JFC26491", "SFX1", 45.35, 0, 0, 0, "織帶", "BIAG 3.6MM 中底板"),
                ("DONE1", "X1", 100, 100, 100, 100, "織帶", "歷史織帶"),
            ],
            stock=[("RFW", "SFX1", "M", 29.78)],
            stock_lot=[("RFW", "SFX1", "0", 3.35), ("RFW", "SFX1", "M01", 26.43)],
            # 舊單 PO（已結案、無 MRP 配額掛到新單）——單位 '59/M2' ≠ stock 'M'
            po=[("J0M26030077", "SFX1", "59/M2", 67, 67, "2026-03-01", "結案")],
        )
        _patch_env(cls, cls.db)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_unit_mismatch_purchase_line_still_fires(self):
        out = ek.kitting_check("JFC26490", item="SFX1")
        self.assertIn("單位待換算", out)
        self.assertIn("⛔ 採購面：", out)
        self.assertIn("同表同單位可直接比", out)
        self.assertIn("人工核對", out)

    def test_unit_mismatch_pooled_verdict_hedged_arithmetic(self):
        out = ek.kitting_check("JFC264", item="SFX1")
        self.assertIn("採購面判定 SFX1", out)
        self.assertIn("91.9", out)                       # 缺合計 46.51+45.35
        self.assertIn("0批 3.4＋待確認批 26.4＝29.8", out)
        self.assertIn("全數調撥仍缺 62.1", out)          # 91.86 − 29.78
        self.assertIn("如同單位", out)                   # 跨單位減法必掛 hedge
        self.assertIn("需開 ERP 採購單", out)


class MaterialCapacityEndToEndTests(unittest.TestCase):
    """形體剩餘產能（material_capacity_by_style）端到端：跑真 SQL、假資料。

    形體 ST-A（三張單取樣：生效 1000 + 逾期生效 500 + 完工 400 = 1900 雙）
      P1 織帶 每雙 2.0（3800/1900），池 0批1000+M01 3000（J0C 5000 綁單不計、
         SW 9999 廢料倉不計、M01 -500 負值歸零）= 4000；在手單佔 3000（含逾期單）
         → 淨 1000/2.0 = 500 雙、毛 4000/2.0 = 2000 雙
      P2 織帶 每雙 0.5（700/1400，只出現在 2/3 張單 → 覆蓋 0.67 仍列入），池 600、
         在手單佔 500 → 淨 100/0.5 = 200 雙、毛 1200 雙  ← 最緊，形體答案取這個
      P3 只在 1/3 張單 → 低覆蓋不列；PU 單位待換算不列；PX 庫存表查無不列
    形體 ST-B（只有完工單，沒有在手單）：P4 池 0 → 見底、淨可做 0
    """

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="erp_capacity_test_")
        cls.db = os.path.join(cls.tmp, "fake.duckdb")
        _build_db(
            cls.db,
            orders=[
                ("S1", "DECA.", "PO1", "2026-06-01", "ST-A", 1000, "生效", "2026-08-20"),
                ("S2", "DECA.", "PO2", "2026-06-01", "ST-A", 500, "生效", "2026-07-01"),
                ("S5", "DECA.", "PO5", "2026-06-10", "ST-A", 400, "完工", "2026-07-10"),
                ("S4", "DECA.", "PO4", "2026-06-20", "ST-A", 300, "取消", "2026-08-30"),
                ("S3", "DECA.", "PO3", "2026-06-15", "ST-B", 200, "完工", "2026-07-15"),
                ("OTH", "LURCHI", "PO9", "2026-06-01", "ST-Z", 100, "生效", "2026-08-20"),
                ("DONE1", "DECA.", "PO-D1", "2025-05-01", "ST-H", 100, "完工", "2025-08-01"),
            ],
            materials=[
                ("S1", "P1", 2000, 0, 0, 0, "織帶", "主織帶"),
                ("S2", "P1", 1000, 0, 0, 0, "織帶", "主織帶"),
                ("S1", "P2", 500, 0, 0, 0, "織帶", "副織帶"),
                ("S1", "P3", 100, 0, 0, 0, "織帶", "變體專用帶"),
                ("S1", "PU", 300, 0, 0, 0, "織帶", "碼裝織帶"),
                ("S2", "PU", 150, 0, 0, 0, "織帶", "碼裝織帶"),
                ("S1", "PX", 100, 0, 0, 0, "織帶", "帳外管理料"),
                ("S2", "PX", 50, 0, 0, 0, "織帶", "帳外管理料"),
                # 完工單的發料要足額，否則會把「織帶」這個料類的可信度拉到門檻以下
                # （cat_fill 就是拿完工單的發料達成率算的），連帶讓需求不被承認。
                ("S5", "P1", 800, 0, 0, 800, "織帶", "主織帶"),
                ("S5", "P2", 200, 0, 0, 200, "織帶", "副織帶"),
                ("S5", "PU", 120, 0, 0, 120, "織帶", "碼裝織帶"),
                ("S5", "PX", 40, 0, 0, 40, "織帶", "帳外管理料"),
                ("S3", "P4", 200, 0, 0, 200, "織帶", "ST-B 專用帶"),
                # 料類可信度校準：織帶發料足額 → 可信
                ("DONE1", "X1", 100, 100, 100, 100, "織帶", "歷史織帶"),
            ],
            stock=[("RFW", "P1", "PCS", 4000), ("RFW", "P2", "PCS", 600),
                   ("RFW", "PU", "M", 900), ("RFW", "P4", "PCS", 0),
                   ("SW", "P1", "PCS", 9999)],
            stock_lot=[
                ("RFW", "P1", "0", 1000),
                ("RFW", "P1", "M01", 3000),
                ("RFW", "P1", "M01", -500),          # 負結存 → 歸零，不倒扣
                ("RFW", "P1", "J0C26010001", 5000),  # 預購批綁單 → 不計
                ("SW", "P1", "0", 9999),             # 廢料倉 → 不計
                ("RFW", "P2", "0", 600),
                ("RFW", "PU", "0", 900),
                ("RFW", "P4", "0", 0),
            ],
            po=[
                ("J0P9", "PU", "Y", 50, 50, "2026-06-01", "結案", "布商A", "2026-06-01"),
                # P4（ST-B 見底料）已開採購單、未收完 → 報表要講「已開單、ETA」
                ("J0P4", "P4", "PCS", 500, 100, "2026-08-30", "生效", "帶廠B", "2026-07-20"),
                ("J0P4b", "P4", "PCS", 100, 100, "2026-07-01", "結案", "帶廠C", "2026-05-01"),
                # P1 只有結案單 → 報表要講「目前沒有未結採購單，最近一次…」
                ("J0P1", "P1", "PCS", 900, 900, "2026-06-20", "結案", "帶廠D", "2026-06-10"),
            ],
        )
        _patch_env(cls, cls.db)
        # 週產速度來自 Drive 的生管日報 —— 測試不連網，固定注入。
        p = mock.patch.object(ek, "_weekly_rates", return_value={"ST-A": 100.0})
        p.start()
        cls.addClassCleanup(p.stop)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _rows(self, weekly_rates=None):
        snap = ek._load_snapshot(_TODAY)
        res = ek._analyze(snap, _TODAY)
        since = (_TODAY - datetime.timedelta(days=30 * ek._CAP_BOM_MONTHS)).isoformat()
        extras = ek._load_capacity_extras("DECA.", since)
        return {r["鞋款"]: r
                for r in ek._capacity_by_style(snap, res, extras,
                                               weekly_rates=weekly_rates)}

    def test_per_pair_usage_and_net_gross(self):
        a = self._rows()["ST-A"]
        self.assertEqual(a["取樣單數"], 3)              # 取消單不進取樣
        self.assertAlmostEqual(a["淨可做"], 200.0)      # 卡在 P2
        self.assertAlmostEqual(a["毛可做"], 1200.0)
        self.assertEqual(a["瓶頸"][0]["料號"], "P2")
        p1 = next(x for x in a["全部"] if x["料號"] == "P1")
        self.assertAlmostEqual(p1["每雙"], 2.0)
        self.assertAlmostEqual(p1["庫存"], 4000.0)      # 0批+M01、負值歸零、排除 J0C/SW
        self.assertAlmostEqual(p1["在手單佔用"], 3000.0)  # 含逾期單 S2
        self.assertAlmostEqual(p1["淨可做"], 500.0)

    def test_coverage_and_unjudgeable_counts(self):
        a = self._rows()["ST-A"]
        self.assertEqual(a["料項"], 5)                 # P1 P2 P3 PU PX
        self.assertEqual(a["核心料"], 4)               # P3 覆蓋 1/3 → 出局
        self.assertEqual(a["低覆蓋"], 1)
        self.assertEqual(a["可判定"], 2)               # P1 P2
        self.assertEqual(a["單位待換算"], 1)           # PU
        self.assertEqual(a["庫存表查無"], 1)           # PX
        self.assertNotIn("P3", [x["料號"] for x in a["全部"]])

    def test_style_without_live_orders_still_reported(self):
        """只有完工單的形體照樣要出現 —— 那正是「還能不能再接一張」的問題。"""
        b = self._rows()["ST-B"]
        self.assertAlmostEqual(b["淨可做"], 0.0)
        self.assertEqual([x["料號"] for x in b["見底"]], ["P4"])
        self.assertEqual(b["瓶頸"], [])

    def test_other_customer_not_included(self):
        self.assertNotIn("ST-Z", self._rows())

    def test_weeks_conversion_from_weekly_rate(self):
        """ST-A 淨可做 200 雙、週產 100 → 約可再做 2 週；沒給速度的形體是 None。"""
        rows = self._rows(weekly_rates={"ST-A": 100.0})
        self.assertAlmostEqual(rows["ST-A"]["可做週數"], 2.0)
        self.assertIsNone(rows["ST-B"]["可做週數"])       # 近期沒生產 → 不硬換算

    def test_exhausted_material_carries_purchase_status(self):
        b = self._rows()["ST-B"]
        po = b["見底"][0]["採購"]
        self.assertTrue(po["已開單"])
        self.assertEqual(po["供應商"], "帶廠B")            # 取未收量最大的那家
        self.assertAlmostEqual(po["在途"], 400.0)          # 500-100，結案單不算
        self.assertEqual(po["ETA"], "2026-08-30")

    def test_purchase_status_without_open_po(self):
        since = (_TODAY - datetime.timedelta(days=365)).isoformat()
        status = ek._purchase_status(ek._load_capacity_extras("DECA.", since)["po"])
        self.assertFalse(status["P1"]["已開單"])          # 只有結案單
        self.assertEqual(status["P1"]["供應商"], "帶廠D")
        self.assertEqual(status["P1"]["最後下單"], "2026-06-10")

    def test_render_and_tool_output(self):
        out = ek.material_capacity_by_style("DECA.")
        self.assertIn("ST-A", out)
        self.assertIn("ST-B", out)
        self.assertIn("200", out)                       # ST-A 淨可做
        self.assertIn("已見底", out)                     # ST-B 的 P4
        self.assertIn("2.0 週", out)                     # ST-A 200 雙 ÷ 週產 100
        self.assertIn("已開單 帶廠B", out)                # 見底料的採購狀態
        self.assertIn("2026-08-30", out)                 # 該採購單 ETA
        self.assertIn("M01", out)                        # 口徑說明
        self.assertIn("不能相加", out)                   # 共用料重複計的警語
        self.assertIn("單位待換算 1", out)
        # 最緊的形體排最前面（ST-B 0 雙 → ST-A 200 雙）
        self.assertLess(out.index("ST-B"), out.index("ST-A"))

    def test_unknown_customer_says_so(self):
        self.assertIn("查無", ek.material_capacity_by_style("NOSUCH"))


class MaterialCapacityEngineUnitTests(unittest.TestCase):
    """純 Python：餵 snap/res/extras dict，驗覆蓋門檻、扣抵與捨去。"""

    @staticmethod
    def _res(items, *, excluded=()):
        """items: [(單號, 料號, 料類, gap, 已到, 旗標dict)]"""
        orders = {}
        for se_id, item, cat, gap, arrived, flags in items:
            rec = orders.setdefault(se_id, {"單號": se_id, "items": []})
            rec["items"].append({
                "料號": item, "料類": cat, "gap_after_transit": gap, "已到": arrived,
                "客供": flags.get("客供", False), "無追蹤": flags.get("無追蹤", False),
                "單位待換算": flags.get("單位待換算", False),
            })
        return {"orders": orders, "trusted_cats": {"織帶"},
                "excluded": [{"單號": s} for s in excluded]}

    @staticmethod
    def _extras(pool, bom):
        return {"pool": pool, "bom": bom}

    def test_committed_skips_same_cases_as_fcfs(self):
        res = self._res([
            ("A", "M1", "織帶", 100, 0, {}),
            ("A", "M2", "織帶", 100, 0, {"客供": True}),
            ("A", "M3", "織帶", 100, 0, {"單位待換算": True}),
            ("A", "M4", "織帶", 100, 0, {"無追蹤": True}),
            ("A", "M5", "膠水", 100, 0, {}),        # 不可信料類 + 零到料
            ("A", "M6", "膠水", 100, 5, {}),        # 不可信但有到料訊號 → 算
            ("B", "M1", "織帶", 50, 0, {}),
        ])
        committed = ek._committed_by_item(res)
        self.assertEqual(committed, {"M1": 150.0, "M6": 100.0})

    def test_excluded_customer_orders_do_not_hold_stock(self):
        res = self._res([("A", "M1", "織帶", 100, 0, {}), ("K", "M1", "織帶", 900, 0, {})],
                        excluded=["K"])
        self.assertEqual(ek._committed_by_item(res), {"M1": 100.0})

    def test_coverage_threshold_excludes_variant_only_material(self):
        snap = {"unit_mismatch": [], "stock_items": [("M1",), ("M2",)]}
        res = self._res([])
        # M2 只出現在 4 張單裡的 1 張（0.25 < 0.5）→ 不列入判定
        extras = self._extras(
            [("M1", 100.0), ("M2", 0.0)],
            [("ST", "M1", "主料", "織帶", 400.0, 400.0, 4, 4),
             ("ST", "M2", "變體料", "織帶", 10.0, 100.0, 1, 4)])
        row = ek._capacity_by_style(snap, res, extras)[0]
        self.assertEqual(row["可判定"], 1)
        self.assertEqual(row["低覆蓋"], 1)
        self.assertAlmostEqual(row["淨可做"], 100.0)   # 只看 M1：100 / 1.0

    def test_purchase_status_picks_biggest_open_supplier_and_earliest_eta(self):
        po = ek._purchase_status([
            ("M1", "小廠", 50.0, "2026-08-01", "2026-07-01"),
            ("M1", "大廠", 500.0, "2026-08-20", "2026-07-10"),
            ("M1", "舊廠", 0.0, None, "2026-09-99"),      # 沒未收量：只更新最後下單
        ])["M1"]
        self.assertTrue(po["已開單"])
        self.assertEqual(po["供應商"], "大廠")
        self.assertAlmostEqual(po["在途"], 550.0)
        self.assertEqual(po["ETA"], "2026-08-01")         # 最早的那張
        self.assertEqual(po["最後下單"], "2026-09-99")

    def test_purchase_line_wording(self):
        self.assertIn("查無", ek._po_line(None))
        self.assertIn("已開單 甲", ek._po_line(
            {"已開單": True, "供應商": "甲", "在途": 10.0, "ETA": "2026-08-01"}))
        self.assertIn("沒有未結採購單", ek._po_line(
            {"已開單": False, "供應商": "乙", "最後下單": "2026-06-01"}))

    def test_weeks_wording(self):
        self.assertEqual(ek._weeks(None), "—")            # 無速度＝不換算
        self.assertEqual(ek._weeks(0.0), "0 週")
        self.assertEqual(ek._weeks(0.04), "不足 0.1 週")
        self.assertEqual(ek._weeks(3.44), "3.4 週")

    def test_pairs_floor_not_round(self):
        """0.9 雙做不出來 —— 顯示一律無條件捨去，不能四捨五入多承諾。"""
        self.assertEqual(ek._pairs(0.9), "0")
        self.assertEqual(ek._pairs(1999.99), "1,999")
        self.assertEqual(ek._pairs(-5), "0")

    def test_material_label_not_duplicated(self):
        self.assertEqual(ek._mat_label({"料類": "熱熔膠", "品名": "熱熔膠 981", "料號": "X"}),
                         "熱熔膠 981")
        self.assertEqual(ek._mat_label({"料類": "織帶", "品名": "主帶", "料號": "X"}),
                         "織帶 主帶")
        self.assertEqual(ek._mat_label({"料類": "", "品名": "", "料號": "X"}), "X")

    def test_empty_bom_renders_hint(self):
        self.assertIn("查無", ek._render_capacity([], "DECA.", ""))


class KittingGracefulTests(unittest.TestCase):
    def test_registered_in_skill_tools(self):
        self.assertIn(ek.kitting_check, ek.SKILL_TOOLS)
        self.assertIn(ek.kitting_alert, ek.SKILL_TOOLS)
        self.assertIn(ek.material_capacity_by_style, ek.SKILL_TOOLS)
        self.assertIn(ek.production_capacity_brief, ek.SKILL_TOOLS)

    def test_background_safe_markers(self):
        """排程要用的三顆都得標 background_safe，否則 safe_tools() 會整顆濾掉。"""
        self.assertTrue(ek.kitting_alert.background_safe)
        self.assertTrue(ek.material_capacity_by_style.background_safe)
        self.assertTrue(ek.production_capacity_brief.background_safe)

    def test_capacity_missing_db(self):
        with mock.patch.object(ek, "_db_ready", return_value=False):
            self.assertIn("不存在", ek.material_capacity_by_style())

    def test_capacity_query_error_wrapped(self):
        with mock.patch.object(ek, "_db_ready", return_value=True), \
             mock.patch.object(ek, "_load_snapshot", side_effect=RuntimeError("boom")):
            self.assertIn("boom", ek.material_capacity_by_style())

    def test_brief_survives_one_section_failing(self):
        """一段掛掉不該讓整封信發不出去（另一段照常，錯誤寫在信裡看得見）。"""
        with mock.patch.object(ek, "material_capacity_by_style",
                               side_effect=RuntimeError("材料段爆炸")), \
             mock.patch("agent_core.factory_production_report.station_capacity_report",
                        return_value="📈 產能表"):
            out = ek.production_capacity_brief()
        self.assertIn("材料段爆炸", out)
        self.assertIn("📈 產能表", out)
        self.assertIn("生產管理每日簡報", out)

    def test_brief_combines_both_sections(self):
        with mock.patch.object(ek, "material_capacity_by_style", return_value="🧮 材料表"), \
             mock.patch("agent_core.factory_production_report.station_capacity_report",
                        return_value="📈 產能表\n[[MAIL_FILE:/x/y.png]]") as chart:
            out = ek.production_capacity_brief()
        chart.assert_called_once_with(days=ek._BRIEF_CHART_DAYS)
        self.assertIn("🧮 材料表", out)
        self.assertIn("[[MAIL_FILE:/x/y.png]]", out)

    def test_missing_db(self):
        with mock.patch.object(ek, "_db_ready", return_value=False):
            self.assertIn("不存在", ek.kitting_alert())
            self.assertIn("不存在", ek.kitting_check("X"))

    def test_query_error_wrapped(self):
        with mock.patch.object(ek, "_db_ready", return_value=True), \
             mock.patch.object(ek, "_load_snapshot", side_effect=RuntimeError("boom")):
            self.assertIn("boom", ek.kitting_alert())
            self.assertIn("boom", ek.kitting_check("X"))


class AnalyzeEngineUnitTests(unittest.TestCase):
    """純 Python 引擎單元測：直接餵 snapshot dict，驗分攤/cap/FCFS 細節。"""

    @staticmethod
    def _snap(**over):
        base = {"orders": [], "materials": [], "transit_lines": [], "stock": [],
                "unit_mismatch": [], "pr_items": [], "cat_fill": [("織帶", 1.0)]}
        base.update(over)
        return base

    @staticmethod
    def _order_row(se_id, due, *, cust="DECA", started=False):
        return (se_id, cust, "PO", "STYLE", 100, due, started, True, True)

    def test_proportional_split_no_double_count(self):
        # 一條 PO open=100，配額 A=100/B=100（tot=200）→ 各分 50，總和不膨脹
        snap = self._snap(
            orders=[self._order_row("A", "2026-08-01"), self._order_row("B", "2026-08-05")],
            materials=[("A", "M", "織帶", "帶", 60, 100, 0, 0),
                       ("B", "M", "織帶", "帶", 60, 100, 0, 0)],
            transit_lines=[("A", "M", 100.0, 200.0, 100.0, 0.0, 0.0, "2026-08-10", False),
                           ("B", "M", 100.0, 200.0, 100.0, 0.0, 0.0, "2026-08-10", False)],
        )
        res = ek._analyze(snap, _TODAY)
        it_a = res["orders"]["A"]["items"][0]
        it_b = res["orders"]["B"]["items"][0]
        self.assertAlmostEqual(it_a["在途"], 50.0)
        self.assertAlmostEqual(it_b["在途"], 50.0)
        # 需求 60、已到 0、在途 50 → 還缺 10 無來源
        self.assertEqual(it_a["判定"], "缺料無來源")
        self.assertAlmostEqual(it_a["缺"], 10.0)

    def test_transit_capped_at_quota_with_scale(self):
        # 配額只有 10，行上 future/overdue 各 100 → 等比例縮到合計 10
        snap = self._snap(
            orders=[self._order_row("A", "2026-08-01")],
            materials=[("A", "M", "織帶", "帶", 100, 10, 0, 0)],
            transit_lines=[("A", "M", 10.0, 10.0, 100.0, 100.0, 0.0, "2026-08-10", False)],
        )
        res = ek._analyze(snap, _TODAY)
        it = res["orders"]["A"]["items"][0]
        self.assertAlmostEqual(it["在途"] + it["在途逾期"], 10.0)
        self.assertAlmostEqual(it["在途"], 5.0)

    def test_rcpt_share_counts_as_arrived(self):
        # 已收 30×配額比 1.0 → 已到 30（收貨流水訊號）
        snap = self._snap(
            orders=[self._order_row("A", "2026-08-01")],
            materials=[("A", "M", "織帶", "帶", 30, 30, 0, 0)],
            transit_lines=[("A", "M", 30.0, 30.0, 0.0, 0.0, 30.0, None, False)],
        )
        res = ek._analyze(snap, _TODAY)
        it = res["orders"]["A"]["items"][0]
        self.assertEqual(it["判定"], "齊")
        self.assertAlmostEqual(it["已到"], 30.0)

    def test_fcfs_by_due_date_and_zombie_excluded(self):
        # 庫存 60：交期早的 A(50) 先拿、B(30) 只拿到 10；殭屍 Z 不參與
        snap = self._snap(
            orders=[self._order_row("B", "2026-09-01"), self._order_row("A", "2026-08-01"),
                    self._order_row("Z", "2026-07-01")],
            materials=[("A", "M", "織帶", "帶", 50, 0, 0, 0),
                       ("B", "M", "織帶", "帶", 30, 0, 0, 0),
                       ("Z", "M", "織帶", "帶", 500, 0, 0, 0)],
            stock=[("M", 60.0)],
        )
        res = ek._analyze(snap, _TODAY)
        self.assertAlmostEqual(res["orders"]["A"]["items"][0]["庫存可補"], 50.0)
        self.assertAlmostEqual(res["orders"]["B"]["items"][0]["庫存可補"], 10.0)
        self.assertAlmostEqual(res["orders"]["Z"]["items"][0]["庫存可補"], 0.0)
        self.assertEqual([z["單號"] for z in res["zombies"]], ["Z"])
        # 衝突以 pool（非殭屍）計：A50+B30=80 > 60
        self.assertEqual(len(res["conflicts"]), 1)
        self.assertAlmostEqual(res["conflicts"][0]["總缺口"], 80.0)

    def test_zero_footprint_is_untracked_not_short(self):
        # 全系統零足跡（無庫存行、無 MRP、零到料）→ 無追蹤資料，不掛紅、不進衝突
        snap = self._snap(
            orders=[self._order_row("A", "2026-08-01")],
            materials=[("A", "GHOST", "織帶", "帳外化工料", 40, 30, 0, 0)],
        )
        res = ek._analyze(snap, _TODAY)
        self.assertEqual(res["orders"]["A"]["items"][0]["判定"], "無追蹤資料")
        self.assertEqual(res["conflicts"], [])

    def test_scrap_only_stock_still_counts_as_tracked(self):
        # 只在排除倉(SW)有貨：足跡存在 → 不是無追蹤，但庫存不可用 → 缺料無來源
        snap = self._snap(
            orders=[self._order_row("A", "2026-08-01")],
            materials=[("A", "M", "織帶", "帶", 40, 0, 0, 0)],
            stock=[], stock_items=[("M",)],
        )
        res = ek._analyze(snap, _TODAY)
        self.assertEqual(res["orders"]["A"]["items"][0]["判定"], "缺料無來源")

    def test_tail_residue_counts_as_complete(self):
        # 需求 25.8、已到 25.5：缺 0.3 < min_need → 齊（損耗率小數殘渣）
        snap = self._snap(
            orders=[self._order_row("A", "2026-08-01")],
            materials=[("A", "M", "織帶", "帶", 25.8, 0, 25.5, 0)],
            stock=[("M", 0.0)],
        )
        res = ek._analyze(snap, _TODAY)
        self.assertEqual(res["orders"]["A"]["items"][0]["判定"], "齊")

    def test_untrusted_zero_signal_is_unjudgeable_not_short(self):
        # 無法判定不算缺料：不進衝突分子、不搶庫存（裡外一致）
        snap = self._snap(
            orders=[self._order_row("A", "2026-08-01"), self._order_row("B", "2026-08-05")],
            materials=[("A", "M", "膠水", "膠", 40, 0, 0, 0),
                       ("B", "M", "織帶", "帶", 10, 0, 10, 0)],
            stock=[("M", 5.0)],
            cat_fill=[("織帶", 1.0), ("膠水", 0.05)],
        )
        res = ek._analyze(snap, _TODAY)
        self.assertEqual(res["orders"]["A"]["items"][0]["判定"], "無法判定")
        self.assertEqual(res["conflicts"], [])   # A 的 40 幻影缺口不得灌入衝突

    def test_orphan_quota_kept_as_overdue(self):
        # 孤兒配額（單據不在鏡像）→ 全額歸在途逾期、足跡保留
        snap = self._snap(
            orders=[self._order_row("A", "2026-08-01")],
            materials=[("A", "M", "織帶", "帶", 20, 20, 0, 0)],
            transit_lines=[("A", "M", 20.0, 20.0, 0.0, 0.0, 0.0, None, True)],
        )
        res = ek._analyze(snap, _TODAY)
        it = res["orders"]["A"]["items"][0]
        self.assertEqual(it["判定"], "在途逾期")
        self.assertAlmostEqual(it["在途逾期"], 20.0)

    def test_rcpt_plus_open_capped_at_quota(self):
        # 同條 PO 已收 30 + 在途 30、配額只有 30 → 在途被 cap 到 0（不可雙重計）
        snap = self._snap(
            orders=[self._order_row("A", "2026-08-01")],
            materials=[("A", "M", "織帶", "帶", 60, 30, 0, 0)],
            transit_lines=[("A", "M", 30.0, 30.0, 30.0, 0.0, 30.0, "2026-08-10", False)],
        )
        res = ek._analyze(snap, _TODAY)
        it = res["orders"]["A"]["items"][0]
        self.assertAlmostEqual(it["已到"], 30.0)
        self.assertAlmostEqual(it["在途"], 0.0)

    def test_zombie_with_ample_stock_marked_maybe_covered(self):
        # 殭屍單不搶庫存，但庫存毛量蓋得住 → 「庫存或可補」而非假紅
        snap = self._snap(
            orders=[self._order_row("Z", "2026-07-01")],
            materials=[("Z", "M", "織帶", "帶", 100, 0, 0, 0)],
            stock=[("M", 5000.0)],
        )
        res = ek._analyze(snap, _TODAY)
        it = res["orders"]["Z"]["items"][0]
        self.assertEqual(it["判定"], "庫存或可補")
        self.assertNotEqual(ek._order_light(res["orders"]["Z"]), "🔴")

    def test_supplied_by_customer_not_short(self):
        snap = self._snap(
            orders=[self._order_row("A", "2026-08-01")],
            materials=[("A", "M", "織帶", "帶", 40, 0, 0, 0)],
            pr_items=[("A", "M")],
        )
        res = ek._analyze(snap, _TODAY)
        self.assertEqual(res["orders"]["A"]["items"][0]["判定"], "客供")

    def test_unit_mismatch_stock_not_applied(self):
        snap = self._snap(
            orders=[self._order_row("A", "2026-08-01")],
            materials=[("A", "M", "織帶", "帶", 40, 0, 0, 0)],
            stock=[("M", 100.0)],
            unit_mismatch=[("M",)],
        )
        res = ek._analyze(snap, _TODAY)
        it = res["orders"]["A"]["items"][0]
        self.assertEqual(it["判定"], "單位待換算")
        self.assertAlmostEqual(it["庫存可補"], 0.0)
        self.assertEqual(res["conflicts"], [])   # 單位不可比 → 不入衝突分子

    def test_light_yellow_when_unjudgeable_ratio_high(self):
        snap = self._snap(
            orders=[self._order_row("A", "2026-08-01")],
            materials=[("A", "M1", "織帶", "帶", 40, 0, 40, 0),
                       ("A", "M2", "膠水", "膠", 40, 0, 0, 0)],
            cat_fill=[("織帶", 1.0), ("膠水", 0.0)],
        )
        res = ek._analyze(snap, _TODAY)
        self.assertEqual(ek._order_light(res["orders"]["A"]), "🟡")


if __name__ == "__main__":
    unittest.main()
