"""chroma 失敗訊息要帶真實原因 — health._check_data_stores 與 dashboard._section_rag
（red-status 走的路徑）兩條人臉路徑都鎖住。

2026-06-12 誤報：沒 export RED_CHROMA_HTTP_URL 的 shell 跑 red-smoke，
chroma_backend 防護（#112）拒開 PersistentClient → memory._get_memory_collection
吞掉例外回 None → health 只能印陳年猜測文案「Gemini embed 失敗？」
（get_or_create_collection 根本不呼叫 embed），完全誤導診斷。

2026-06-15 復發於 sibling：當初的修只落在 health.py，dashboard._section_rag
（red-status / system_status() 走的路徑）漏接，繼續印「API key？路徑？」猜測；
且人從乾淨 shell 跑 red-status 沒注入 env → 把活著的向量庫誤報「不可用」。

修法契約（本檔鎖住）：
1. memory 把最後一次 init 失敗原因存模組級 _vector_last_error，
   vector_store_last_error() 供 health / dashboard 引用；成功時清空。
2. health 的 col-is-None 訊息帶出該原因，能區分「防護拒開（env 沒帶）」
   「server 連不上」「其他」。
3. 共用 server 位址單一定義在 chroma_backend.SHARED_SERVER_URL —
   bin/inject-plist-env 與 launchd templates 不得漂移。
4. dashboard._section_rag 同樣帶出真實原因（不得回退到「API key？路徑？」）；
   system_status 在跑 section 前用 _ensure_chroma_endpoint() 把未設的
   RED_CHROMA_HTTP_URL 對齊共用 server，讓 red-status 反映 daemon 真實狀態。
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import re
import sys
import time
import unittest
from unittest import mock

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


def _reset_vector_state(memory):
    memory._chroma_client = None
    memory._chroma_collection = None
    memory._vector_ready = None
    memory._vector_last_error = None
    memory._vector_last_failure_ts = 0.0


class _VectorStateIsolation(unittest.TestCase):
    """快照/還原 memory 的向量庫單例狀態（unittest discover 下 conftest 不生效）。"""

    def setUp(self):
        from agent_core import memory

        self.memory = memory
        self._saved = (
            memory._chroma_client,
            memory._chroma_collection,
            memory._vector_ready,
            memory._vector_last_error,
            memory._vector_last_failure_ts,
        )

    def tearDown(self):
        (
            self.memory._chroma_client,
            self.memory._chroma_collection,
            self.memory._vector_ready,
            self.memory._vector_last_error,
            self.memory._vector_last_failure_ts,
        ) = self._saved


class VectorLastErrorTests(_VectorStateIsolation):
    def test_init_failure_records_reason(self):
        memory = self.memory
        _reset_vector_state(memory)
        boom = RuntimeError("共用 Chroma server 似乎正在 http://127.0.0.1:8000 運行")
        with mock.patch.object(memory, "_build_chroma_client", side_effect=boom):
            self.assertIsNone(memory._get_memory_collection())

        reason = memory.vector_store_last_error()
        self.assertIn("RuntimeError", reason)
        self.assertIn("共用 Chroma server", reason)

    def test_success_clears_stale_reason(self):
        memory = self.memory
        _reset_vector_state(memory)
        memory._vector_last_error = "RuntimeError: 上一輪的殘留"
        fake_col = mock.Mock()
        fake_client = mock.Mock(
            get_or_create_collection=mock.Mock(return_value=fake_col)
        )
        with mock.patch.object(memory, "_build_chroma_client", return_value=fake_client):
            self.assertIs(memory._get_memory_collection(), fake_col)

        self.assertIsNone(memory.vector_store_last_error())

    def test_cooldown_active_short_circuits_and_keeps_reason(self):
        # 冷卻期內不重試 init——原因維持最近一次失敗的值
        memory = self.memory
        memory._chroma_client = None
        memory._chroma_collection = None
        memory._vector_ready = False
        memory._vector_last_error = "RuntimeError: 第一次失敗的原因"
        memory._vector_last_failure_ts = time.monotonic()  # 剛失敗，冷卻中

        with mock.patch.object(
            memory, "_build_chroma_client",
            side_effect=AssertionError("冷卻期內不該重試 init"),
        ):
            self.assertIsNone(memory._get_memory_collection())
        self.assertEqual(
            memory.vector_store_last_error(), "RuntimeError: 第一次失敗的原因"
        )

    def test_cooldown_expired_retries_and_recovers(self):
        # 冷卻到期要放行重試——長壽 daemon 在 chroma server 重啟窗口撞到失敗，
        # 不該到 redeploy 前都永久失去向量記憶
        memory = self.memory
        memory._chroma_client = None
        memory._chroma_collection = None
        memory._vector_ready = False
        memory._vector_last_error = "RuntimeError: server 還沒起來"
        memory._vector_last_failure_ts = (
            time.monotonic() - memory._VECTOR_RETRY_COOLDOWN_S - 1
        )

        fake_col = mock.Mock()
        fake_client = mock.Mock(
            get_or_create_collection=mock.Mock(return_value=fake_col)
        )
        with mock.patch.object(memory, "_build_chroma_client", return_value=fake_client):
            self.assertIs(memory._get_memory_collection(), fake_col)
        self.assertTrue(memory._vector_ready)
        self.assertIsNone(memory.vector_store_last_error())

    def test_cooldown_expired_failed_retry_refreshes_reason(self):
        # 冷卻到期重試又失敗：原因刷新成這一次的、時間戳重置（再進冷卻）
        memory = self.memory
        memory._chroma_client = None
        memory._chroma_collection = None
        memory._vector_ready = False
        memory._vector_last_error = "RuntimeError: 舊原因"
        memory._vector_last_failure_ts = (
            time.monotonic() - memory._VECTOR_RETRY_COOLDOWN_S - 1
        )

        boom = ValueError("Could not connect to a Chroma server.")
        with mock.patch.object(memory, "_build_chroma_client", side_effect=boom):
            self.assertIsNone(memory._get_memory_collection())
        self.assertIn("Could not connect", memory.vector_store_last_error())
        self.assertFalse(memory._vector_ready)
        # 時間戳已重置 → 立即再叫也不會重試
        with mock.patch.object(
            memory, "_build_chroma_client",
            side_effect=AssertionError("冷卻期內不該重試 init"),
        ):
            self.assertIsNone(memory._get_memory_collection())


class HealthChromaMessageTests(_VectorStateIsolation):
    def _data_store_issues(self, init_error):
        """讓 memory init 以 init_error 失敗，跑 health._check_data_stores。"""
        from agent_core import health

        memory = self.memory
        _reset_vector_state(memory)
        with mock.patch.object(
            memory, "_build_chroma_client", side_effect=init_error
        ), mock.patch.object(
            health, "_EMAIL_LAKE_PARQUET",
            os.path.join(_REPO_ROOT, "var", "nonexistent-test.parquet"),
        ):
            issues = health._check_data_stores()
        return [i for i in issues if i["area"] == "chroma"]

    def test_guard_refusal_reason_reaches_health_message(self):
        """#112 防護拒開（env 沒帶 + server 活著）→ 訊息要含防護原文與
        export 修復提示，且不得再出現「Gemini embed」誤導。
        用 chroma_backend 真的丟出來的 RuntimeError，別自己編文案。"""
        from agent_core import chroma_backend

        # 空字串＝未設（chroma_backend 對兩個 env 都有此語意，suite 已鎖住），
        # 不必 clear=True 重建整個 os.environ
        with mock.patch.dict(
            os.environ, {"RED_CHROMA_HTTP_URL": "", "RED_CHROMA_ALLOW_DIRECT": ""}
        ), mock.patch.object(
            chroma_backend, "_shared_server_alive", return_value=True
        ):
            with self.assertRaises(RuntimeError) as ctx:
                chroma_backend.build_chroma_client("/tmp/red-test-chroma")
        guard_error = ctx.exception

        chroma_issues = self._data_store_issues(guard_error)

        self.assertEqual(len(chroma_issues), 1)
        msg = chroma_issues[0]["msg"]
        self.assertNotIn("Gemini embed", msg)
        self.assertIn("共用 Chroma server", msg)
        # 300 字截斷後，可操作的修復提示（export RED_CHROMA_HTTP_URL=…）必須還在
        self.assertIn("RED_CHROMA_HTTP_URL", msg)

    def test_server_unreachable_reason_is_distinguishable(self):
        boom = ValueError(
            "Could not connect to a Chroma server. Are you sure it is running?"
        )
        chroma_issues = self._data_store_issues(boom)

        self.assertEqual(len(chroma_issues), 1)
        msg = chroma_issues[0]["msg"]
        self.assertNotIn("Gemini embed", msg)
        self.assertIn("ValueError", msg)
        self.assertIn("Could not connect", msg)

    def test_no_recorded_reason_falls_back_without_misleading_text(self):
        from agent_core import health

        memory = self.memory
        memory._chroma_client = None
        memory._chroma_collection = None
        memory._vector_ready = False  # 冷卻短路：不會重跑 init
        memory._vector_last_error = None
        memory._vector_last_failure_ts = time.monotonic()
        with mock.patch.object(
            health, "_EMAIL_LAKE_PARQUET",
            os.path.join(_REPO_ROOT, "var", "nonexistent-test.parquet"),
        ):
            issues = health._check_data_stores()
        chroma_issues = [i for i in issues if i["area"] == "chroma"]

        self.assertEqual(len(chroma_issues), 1)
        self.assertNotIn("Gemini embed", chroma_issues[0]["msg"])

    def test_operational_db_failure_reaches_health_message(self):
        from agent_core import health

        fake_col = mock.Mock()
        fake_col.count.return_value = 1
        with mock.patch.object(health, "_get_memory_collection", return_value=fake_col), \
                mock.patch.object(
                    health,
                    "_EMAIL_LAKE_PARQUET",
                    os.path.join(_REPO_ROOT, "var", "nonexistent-test.parquet"),
                ), \
                mock.patch(
                    "agent_core.operational_db.health_status",
                    return_value={
                        "enabled": True,
                        "ok": False,
                        "error": "RuntimeError: db down",
                    },
                ):
            issues = health._check_data_stores()

        db_issues = [i for i in issues if i["area"] == "operational_db"]
        self.assertEqual(len(db_issues), 1)
        self.assertEqual(db_issues[0]["severity"], "error")
        self.assertIn("db down", db_issues[0]["msg"])


class SharedServerSingleDefinitionTests(unittest.TestCase):
    """共用 server 位址只能有一份定義（chroma_backend.SHARED_SERVER_URL），
    注入器與 launchd templates 都不得自帶 literal 漂移。"""

    def test_inject_plist_env_uses_chroma_backend_definition(self):
        from agent_core.chroma_backend import SHARED_SERVER_URL

        path = os.path.join(_REPO_ROOT, "bin", "inject-plist-env")
        loader = importlib.machinery.SourceFileLoader("_inject_plist_env_test", path)
        spec = importlib.util.spec_from_loader(loader.name, loader)
        mod = importlib.util.module_from_spec(spec)
        loader.exec_module(mod)

        self.assertEqual(mod.VAL, SHARED_SERVER_URL)

    def test_launchd_templates_pin_same_address(self):
        from agent_core.chroma_backend import SHARED_SERVER_URL

        tmpl_dir = os.path.join(_REPO_ROOT, "launchd", "templates")
        offenders = []
        seen = 0
        for name in sorted(os.listdir(tmpl_dir)):
            if not name.endswith(".plist"):
                continue
            with open(os.path.join(tmpl_dir, name), encoding="utf-8") as f:
                text = f.read()
            m = re.search(
                r"<key>RED_CHROMA_HTTP_URL</key>\s*<string>([^<]*)</string>", text
            )
            if not m:
                continue
            seen += 1
            if m.group(1) != SHARED_SERVER_URL:
                offenders.append((name, m.group(1)))

        self.assertGreater(seen, 0, "templates 裡找不到 RED_CHROMA_HTTP_URL，測試失效")
        self.assertEqual(offenders, [])


class DashboardChromaMessageTests(_VectorStateIsolation):
    """red-status 走 dashboard._section_rag（非 health._check_data_stores）。
    sibling 漏接 → 繼續印「API key？路徑？」。鎖住 dashboard 路徑也帶真實原因。"""

    def _section_rag_with_init_error(self, init_error):
        from agent_core import dashboard

        memory = self.memory
        _reset_vector_state(memory)
        with mock.patch.object(
            memory, "_build_chroma_client", side_effect=init_error
        ):
            return dashboard._section_rag()

    def test_guard_refusal_reason_reaches_dashboard(self):
        """#112 防護拒開（env 沒帶 + server 活著）→ dashboard 要帶防護原文 +
        export 修復提示，且不得再出現陳年猜測「API key？路徑？」「Gemini embed」。"""
        from agent_core import chroma_backend

        with mock.patch.dict(
            os.environ, {"RED_CHROMA_HTTP_URL": "", "RED_CHROMA_ALLOW_DIRECT": ""}
        ), mock.patch.object(
            chroma_backend, "_shared_server_alive", return_value=True
        ):
            with self.assertRaises(RuntimeError) as ctx:
                chroma_backend.build_chroma_client("/tmp/red-test-chroma")

        out = self._section_rag_with_init_error(ctx.exception)
        self.assertNotIn("API key", out)
        self.assertNotIn("Gemini embed", out)
        self.assertIn("共用 Chroma server", out)
        self.assertIn("RED_CHROMA_HTTP_URL", out)

    def test_server_unreachable_reason_is_distinguishable(self):
        boom = ValueError(
            "Could not connect to a Chroma server. Are you sure it is running?"
        )
        out = self._section_rag_with_init_error(boom)
        self.assertNotIn("API key", out)
        self.assertIn("Could not connect", out)


class EnsureChromaEndpointTests(unittest.TestCase):
    """red-status 從乾淨 shell 跑要對齊共用 server——但只在 server 活著時才切。
    測試一律 mock _shared_server_alive，不得依賴執行環境是否真有 server 在跑
    （dev 有、CI 沒有）。2026-06-15 踩過：env-set 漏這道 gate → CI 沒 server →
    切成 http 模式 → dashboard_alerts._check_chroma_mode_consistency 誤報 crit
    「共用 Chroma server 無回應」→ system_alerts 煙霧測試紅。"""

    def test_defaults_to_shared_server_when_alive_and_unset(self):
        from agent_core import dashboard, chroma_backend

        with mock.patch.dict(
            os.environ, {"RED_CHROMA_HTTP_URL": "", "RED_CHROMA_ALLOW_DIRECT": ""}
        ), mock.patch.object(
            chroma_backend, "_shared_server_alive", return_value=True
        ):
            dashboard._ensure_chroma_endpoint()
            self.assertEqual(
                os.environ["RED_CHROMA_HTTP_URL"], chroma_backend.SHARED_SERVER_URL
            )

    def test_skips_when_server_not_alive(self):
        """server 沒起來（dev / CI / 離線）→ 不切 http 模式、env 維持未設。
        這條鎖住 CI 修：切了會讓 alerts 誤報「共用 Chroma server 無回應」crit。"""
        from agent_core import dashboard, chroma_backend

        with mock.patch.dict(
            os.environ, {"RED_CHROMA_HTTP_URL": "", "RED_CHROMA_ALLOW_DIRECT": ""}
        ), mock.patch.object(
            chroma_backend, "_shared_server_alive", return_value=False
        ):
            dashboard._ensure_chroma_endpoint()
            self.assertEqual(os.environ.get("RED_CHROMA_HTTP_URL", ""), "")

    def test_respects_existing_url(self):
        from agent_core import dashboard, chroma_backend

        with mock.patch.dict(
            os.environ, {"RED_CHROMA_HTTP_URL": "http://example:9999"}
        ), mock.patch.object(
            chroma_backend, "_shared_server_alive", return_value=True
        ):
            dashboard._ensure_chroma_endpoint()
            self.assertEqual(os.environ["RED_CHROMA_HTTP_URL"], "http://example:9999")

    def test_allow_direct_is_not_overridden(self):
        """離線維運（ALLOW_DIRECT=1）即使 server 活著也不該被強指回共用 server。"""
        from agent_core import dashboard, chroma_backend

        with mock.patch.dict(
            os.environ, {"RED_CHROMA_HTTP_URL": "", "RED_CHROMA_ALLOW_DIRECT": "1"}
        ), mock.patch.object(
            chroma_backend, "_shared_server_alive", return_value=True
        ):
            dashboard._ensure_chroma_endpoint()
            self.assertEqual(os.environ.get("RED_CHROMA_HTTP_URL", ""), "")


if __name__ == "__main__":
    unittest.main()
