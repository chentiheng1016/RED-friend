from __future__ import annotations

import os
import sys
import unittest
from unittest import mock


_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


class GeminiGenerateRetryTests(unittest.TestCase):
    def test_spending_cap_errors_are_not_retried(self):
        from agent_core import cost_tracker, gemini_client

        client = mock.MagicMock()
        client.models.generate_content.side_effect = RuntimeError(
            "429 RESOURCE_EXHAUSTED: project exceeded its monthly spending cap"
        )

        with mock.patch.object(gemini_client, "_get_gemini_client", return_value=client), \
             mock.patch.object(gemini_client, "_USES_LIBRESSL", False), \
             mock.patch.dict(os.environ, {"RED_GEMINI_FALLBACK_MODEL": ""}, clear=False), \
             mock.patch.object(cost_tracker, "record_api_error") as rec:
            with self.assertRaises(RuntimeError):
                gemini_client._gemini_generate(
                    model="gemini-3-flash-preview",
                    contents=["hello"],
                    max_attempts=5,
                )

        self.assertEqual(client.models.generate_content.call_count, 1)
        rec.assert_called_once()

    def test_prepayment_depleted_errors_are_not_retried(self):
        from agent_core import cost_tracker, gemini_client

        client = mock.MagicMock()
        client.models.generate_content.side_effect = RuntimeError(
            "429 RESOURCE_EXHAUSTED: Your prepayment credits are depleted. "
            "Please go to AI Studio to manage your project and billing."
        )

        with mock.patch.object(gemini_client, "_get_gemini_client", return_value=client), \
             mock.patch.object(gemini_client, "_USES_LIBRESSL", False), \
             mock.patch.dict(os.environ, {"RED_GEMINI_FALLBACK_MODEL": ""}, clear=False), \
             mock.patch.object(cost_tracker, "record_api_error") as rec:
            with self.assertRaises(RuntimeError):
                gemini_client._gemini_generate(
                    model="gemini-3-flash-preview",
                    contents=["hello"],
                    max_attempts=5,
                )

        # 餘額耗盡是硬錯誤：只能打一次就拋，不該重試 5 次
        self.assertEqual(client.models.generate_content.call_count, 1)
        rec.assert_called_once()

    def test_dunning_billing_block_is_not_retried(self):
        from agent_core import cost_tracker, gemini_client

        client = mock.MagicMock()
        client.models.generate_content.side_effect = RuntimeError(
            "403 PERMISSION_DENIED. {'error': {'code': 403, 'message': "
            "'Lightning dunning decision is deny for project: projects/168037703156', "
            "'status': 'PERMISSION_DENIED'}}"
        )

        with mock.patch.object(gemini_client, "_get_gemini_client", return_value=client), \
             mock.patch.object(gemini_client, "_USES_LIBRESSL", False), \
             mock.patch.dict(os.environ, {"RED_GEMINI_FALLBACK_MODEL": ""}, clear=False), \
             mock.patch.object(cost_tracker, "record_api_error") as rec:
            with self.assertRaises(RuntimeError):
                gemini_client._gemini_generate(
                    model="gemini-3-flash-preview",
                    contents=["hello"],
                    max_attempts=5,
                )

        # 帳務被擋（dunning）是硬錯誤：重試補不回付款，只打一次就拋
        self.assertEqual(client.models.generate_content.call_count, 1)
        rec.assert_called_once()


class GeminiModelResolutionTests(unittest.TestCase):
    """GEMINI_MODEL is RED_GEMINI_MODEL-overridable (resolved at import)."""

    def setUp(self):
        # Back up the caller's real env value so the reloads below never
        # clobber a developer who actually has RED_GEMINI_MODEL set.
        self._orig_env = os.environ.get("RED_GEMINI_MODEL")

    def tearDown(self):
        # Restore env + reload the module to its default-env state so the
        # override doesn't leak into other tests.
        import importlib
        from agent_core import gemini_client
        if self._orig_env is None:
            os.environ.pop("RED_GEMINI_MODEL", None)
        else:
            os.environ["RED_GEMINI_MODEL"] = self._orig_env
        importlib.reload(gemini_client)

    def _reload_with(self, value):
        """Reload gemini_client with RED_GEMINI_MODEL=value, or absent if None."""
        import importlib
        from agent_core import gemini_client
        if value is None:
            env = dict(os.environ)
            env.pop("RED_GEMINI_MODEL", None)
            patcher = mock.patch.dict(os.environ, env, clear=True)
        else:
            patcher = mock.patch.dict(os.environ, {"RED_GEMINI_MODEL": value}, clear=False)
        with patcher:
            return importlib.reload(gemini_client)

    def test_default_model_when_env_unset(self):
        mod = self._reload_with(None)
        self.assertEqual(mod.GEMINI_MODEL, "gemini-3-flash-preview")

    def test_env_overrides_model(self):
        mod = self._reload_with("gemini-flash-latest")
        self.assertEqual(mod.GEMINI_MODEL, "gemini-flash-latest")

    def test_blank_env_falls_back_to_default(self):
        mod = self._reload_with("   ")
        self.assertEqual(mod.GEMINI_MODEL, "gemini-3-flash-preview")


class RagGenModelResolutionTests(unittest.TestCase):
    """RAG_GEN_MODEL: independent RED_RAG_GEN_MODEL override, falls back to GEMINI_MODEL."""

    def setUp(self):
        self._orig_rag = os.environ.get("RED_RAG_GEN_MODEL")
        self._orig_gem = os.environ.get("RED_GEMINI_MODEL")

    def tearDown(self):
        import importlib
        from agent_core import gemini_client
        for key, val in (("RED_RAG_GEN_MODEL", self._orig_rag),
                         ("RED_GEMINI_MODEL", self._orig_gem)):
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val
        importlib.reload(gemini_client)

    def _reload_with(self, env_overrides):
        """Reload gemini_client with env keys set; keys mapped to None are removed."""
        import importlib
        from agent_core import gemini_client
        env = dict(os.environ)
        for key, val in env_overrides.items():
            if val is None:
                env.pop(key, None)
            else:
                env[key] = val
        with mock.patch.dict(os.environ, env, clear=True):
            return importlib.reload(gemini_client)

    def test_falls_back_to_gemini_model_when_unset(self):
        mod = self._reload_with({"RED_RAG_GEN_MODEL": None,
                                 "RED_GEMINI_MODEL": "gemini-flash-latest"})
        self.assertEqual(mod.RAG_GEN_MODEL, "gemini-flash-latest")

    def test_blank_env_falls_back_to_gemini_model(self):
        mod = self._reload_with({"RED_RAG_GEN_MODEL": "  ",
                                 "RED_GEMINI_MODEL": "gemini-flash-latest"})
        self.assertEqual(mod.RAG_GEN_MODEL, "gemini-flash-latest")

    def test_override_is_independent_of_gemini_model(self):
        mod = self._reload_with({"RED_RAG_GEN_MODEL": "gemini-2.5-pro",
                                 "RED_GEMINI_MODEL": "gemini-flash-latest"})
        self.assertEqual(mod.RAG_GEN_MODEL, "gemini-2.5-pro")
        self.assertEqual(mod.GEMINI_MODEL, "gemini-flash-latest")


class GeminiApiErrorRecordingTests(unittest.TestCase):
    """_gemini_generate records ONLY final failures (retries exhausted or
    non-retryable) to cost_tracker — recoverable transients that retry-succeed
    must NOT be counted, so the error-rate red-line reflects true task failure
    rate (gemini-code-assist false-positive finding on PR #136)."""

    def _run(self, side_effect, max_attempts=3):
        from agent_core import cost_tracker, gemini_client
        client = mock.MagicMock()
        client.models.generate_content.side_effect = side_effect
        with mock.patch.object(gemini_client, "_get_gemini_client", return_value=client), \
             mock.patch.object(gemini_client, "_USES_LIBRESSL", False), \
             mock.patch.object(cost_tracker, "record_api_error") as rec, \
             mock.patch.dict(os.environ, {"RED_GEMINI_FALLBACK_MODEL": ""}, clear=False), \
             mock.patch("time.sleep"):
            try:
                gemini_client._gemini_generate(
                    model="gemini-3-flash-preview",
                    contents=["hi"],
                    max_attempts=max_attempts,
                )
            except Exception:
                pass
        return rec, client

    def test_quota_error_recorded_once(self):
        rec, client = self._run(RuntimeError("429 prepayment credits are depleted"))
        rec.assert_called_once()
        self.assertEqual(rec.call_args.args[0], "gemini")
        self.assertEqual(rec.call_args.args[1], "quota_depleted")
        self.assertEqual(rec.call_args.kwargs.get("model"), "gemini-3-flash-preview")
        self.assertEqual(client.models.generate_content.call_count, 1)

    def test_transient_final_failure_recorded_once(self):
        rec, client = self._run(RuntimeError("503 UNAVAILABLE high demand"),
                                max_attempts=3)
        # retried 3×, but only the final give-up is recorded
        self.assertEqual(client.models.generate_content.call_count, 3)
        rec.assert_called_once()
        self.assertEqual(rec.call_args.args[1], "503")

    def test_transient_then_success_records_nothing(self):
        from agent_core import cost_tracker, gemini_client
        resp = mock.MagicMock(usage_metadata=None)
        client = mock.MagicMock()
        client.models.generate_content.side_effect = [
            RuntimeError("503 unavailable"), resp,
        ]
        with mock.patch.object(gemini_client, "_get_gemini_client", return_value=client), \
             mock.patch.object(gemini_client, "_USES_LIBRESSL", False), \
             mock.patch.object(cost_tracker, "record_api_error") as rec, \
             mock.patch.dict(os.environ, {"RED_GEMINI_FALLBACK_MODEL": ""}, clear=False), \
             mock.patch("time.sleep"):
            out = gemini_client._gemini_generate(model="m", contents=["hi"],
                                                 max_attempts=3)
        self.assertIs(out, resp)
        rec.assert_not_called()  # recoverable transient → no error recorded

    def test_local_network_failures_get_their_own_status(self):
        """本機 DNS／連線斷掉不是 Gemini 的問題，不能跟 400 那種一起塞進 "other"。

        2026-08-14 實例：reflection daemon 一輪 12 個群組全掛在
        `[Errno 8] nodename nor servname`（睡眠喚醒空窗），面板只顯示
        「status: other×12」，advice 給的三條沒一條對得上，得翻十分鐘 log。
        """
        from agent_core import gemini_client as gc
        for msg in (
            "[Errno 8] nodename nor servname provided, or not known",
            "Failed to resolve 'generativelanguage.googleapis.com'",
            "NameResolutionError: Failed to resolve host",
            "[Errno 51] Network is unreachable",
            "[Errno 61] Connection refused",
            "Temporary failure in name resolution",
        ):
            self.assertEqual(gc._classify_api_error(msg), "network_unreachable", msg)

    def test_http_status_still_wins_over_the_network_bucket(self):
        """帶了 5xx 的訊息就算夾雜連線字眼，也要歸到狀態碼那類。"""
        from agent_core import gemini_client as gc
        self.assertEqual(
            gc._classify_api_error("503 unavailable: connection reset by peer"), "503")

    def test_classify_api_error(self):
        from agent_core import gemini_client as gc
        self.assertEqual(gc._classify_api_error("429 rate limit"), "429")
        self.assertEqual(gc._classify_api_error("503 unavailable"), "503")
        self.assertEqual(gc._classify_api_error("deadline exceeded"), "timeout")
        self.assertEqual(gc._classify_api_error("400 bad arg"), "other")
        self.assertEqual(
            gc._classify_api_error("prepayment credits are depleted"),
            "quota_depleted",
        )
        # 帳務催收封鎖（dunning）也歸 quota_depleted → billing 紅線抓得到，
        # 不再掉進 "other" 隱形。
        self.assertEqual(
            gc._classify_api_error(
                "403 PERMISSION_DENIED ... Lightning dunning decision is deny "
                "for project: projects/168037703156"
            ),
            "quota_depleted",
        )
        self.assertEqual(gc._classify_api_error("weird boom"), "other")


class GeminiFallbackModelTests(unittest.TestCase):
    def tearDown(self):
        from agent_core import gemini_client

        gemini_client._reset_gemini_circuit_for_tests()

    def test_transient_final_failure_tries_fallback_model(self):
        from agent_core import cost_tracker, gemini_client

        resp = mock.MagicMock(usage_metadata=None)
        client = mock.MagicMock()
        client.models.generate_content.side_effect = [
            RuntimeError("503 UNAVAILABLE high demand"),
            resp,
        ]
        with mock.patch.dict(
            os.environ,
            {"RED_GEMINI_FALLBACK_MODEL": "gemini-2.5-flash-lite"},
            clear=False,
        ), \
             mock.patch.object(gemini_client, "_get_gemini_client", return_value=client), \
             mock.patch.object(gemini_client, "_USES_LIBRESSL", False), \
             mock.patch.object(cost_tracker, "record_api_error") as record_error, \
             mock.patch("time.sleep"):
            out = gemini_client._gemini_generate(
                model="gemini-flash-latest",
                contents=["hi"],
                max_attempts=1,
            )

        self.assertIs(out, resp)
        models = [
            call.kwargs["model"]
            for call in client.models.generate_content.call_args_list
        ]
        self.assertEqual(models, ["gemini-flash-latest", "gemini-2.5-flash-lite"])
        # 2026-07 深檢 #3：fallback 全救回＝任務沒失敗 → 不記 api_error
        # （舊行為 primary 記一筆，把 dashboard 錯誤率推近 50% 造成告警疲勞）。
        # fallback 也失敗時才補記 primary — 見 test_deep_audit_cost_state_fixes。
        record_error.assert_not_called()

    def test_non_retryable_quota_does_not_try_fallback_model(self):
        from agent_core import cost_tracker, gemini_client

        client = mock.MagicMock()
        client.models.generate_content.side_effect = RuntimeError(
            "429 RESOURCE_EXHAUSTED: prepayment credits are depleted"
        )
        with mock.patch.dict(
            os.environ,
            {"RED_GEMINI_FALLBACK_MODEL": "gemini-2.5-flash-lite"},
            clear=False,
        ), \
             mock.patch.object(gemini_client, "_get_gemini_client", return_value=client), \
             mock.patch.object(gemini_client, "_USES_LIBRESSL", False), \
             mock.patch.object(cost_tracker, "record_api_error"):
            with self.assertRaises(RuntimeError):
                gemini_client._gemini_generate(
                    model="gemini-flash-latest",
                    contents=["hi"],
                    max_attempts=3,
                )

        self.assertEqual(client.models.generate_content.call_count, 1)

    def test_open_circuit_uses_fallback_without_primary_client_call(self):
        from agent_core import cost_tracker, gemini_client

        resp = mock.MagicMock(usage_metadata=None)
        client = mock.MagicMock()
        client.models.generate_content.return_value = resp

        def check_circuit(model):
            if model == "gemini-flash-latest":
                raise gemini_client.GeminiCircuitOpenError("open")

        with mock.patch.dict(
            os.environ,
            {"RED_GEMINI_FALLBACK_MODEL": "gemini-2.5-flash-lite"},
            clear=False,
        ), \
             mock.patch.object(gemini_client, "_check_gemini_circuit", side_effect=check_circuit), \
             mock.patch.object(gemini_client, "_get_gemini_client", return_value=client), \
             mock.patch.object(gemini_client, "_USES_LIBRESSL", False), \
             mock.patch.object(cost_tracker, "record_api_error") as record_error:
            out = gemini_client._gemini_generate(
                model="gemini-flash-latest",
                contents=["hi"],
                max_attempts=1,
            )

        self.assertIs(out, resp)
        client.models.generate_content.assert_called_once()
        self.assertEqual(
            client.models.generate_content.call_args.kwargs["model"],
            "gemini-2.5-flash-lite",
        )
        record_error.assert_not_called()


class GeminiCircuitBreakerTests(unittest.TestCase):
    def tearDown(self):
        from agent_core import gemini_client

        gemini_client._reset_gemini_circuit_for_tests()

    def _env(self):
        return mock.patch.dict(os.environ, {
            "RED_GEMINI_CIRCUIT_BREAKER": "1",
            "RED_GEMINI_CIRCUIT_FAILURES": "2",
            "RED_GEMINI_CIRCUIT_WINDOW_S": "60",
            "RED_GEMINI_CIRCUIT_OPEN_S": "120",
            "RED_GEMINI_FALLBACK_MODEL": "",
        }, clear=False)

    def test_transient_final_failures_open_circuit(self):
        from agent_core import cost_tracker, gemini_client

        client = mock.MagicMock()
        client.models.generate_content.side_effect = RuntimeError(
            "503 UNAVAILABLE high demand"
        )
        now = [1000.0]

        with self._env(), \
             mock.patch.object(gemini_client, "_get_gemini_client", return_value=client), \
             mock.patch.object(gemini_client, "_USES_LIBRESSL", False), \
             mock.patch.object(gemini_client, "_monotonic", side_effect=lambda: now[0]), \
             mock.patch.object(cost_tracker, "record_api_error") as rec, \
             mock.patch("time.sleep"):
            for _ in range(2):
                with self.assertRaises(RuntimeError):
                    gemini_client._gemini_generate(
                        model="gemini-flash-latest",
                        contents=["hi"],
                        max_attempts=1,
                    )

            with self.assertRaises(gemini_client.GeminiCircuitOpenError):
                gemini_client._gemini_generate(
                    model="gemini-flash-latest",
                    contents=["hi"],
                    max_attempts=1,
                )

        self.assertEqual(client.models.generate_content.call_count, 2)
        self.assertEqual(rec.call_count, 2)

    def test_circuit_open_call_is_not_recorded_as_api_error(self):
        from agent_core import cost_tracker, gemini_client

        client = mock.MagicMock()
        client.models.generate_content.side_effect = RuntimeError(
            "503 UNAVAILABLE high demand"
        )
        with self._env(), \
             mock.patch.object(gemini_client, "_get_gemini_client", return_value=client), \
             mock.patch.object(gemini_client, "_USES_LIBRESSL", False), \
             mock.patch.object(gemini_client, "_monotonic", return_value=1000.0), \
             mock.patch.object(cost_tracker, "record_api_error") as rec, \
             mock.patch("time.sleep"):
            for _ in range(2):
                with self.assertRaises(RuntimeError):
                    gemini_client._gemini_generate(
                        model="gemini-flash-latest",
                        contents=["hi"],
                        max_attempts=1,
                    )
            rec.reset_mock()

            with self.assertRaises(gemini_client.GeminiCircuitOpenError):
                gemini_client._gemini_generate(
                    model="gemini-flash-latest",
                    contents=["hi"],
                    max_attempts=1,
                )

        rec.assert_not_called()

    def test_success_clears_prior_failure_count(self):
        from agent_core import cost_tracker, gemini_client

        resp = mock.MagicMock(usage_metadata=None)
        client = mock.MagicMock()
        client.models.generate_content.side_effect = [
            RuntimeError("503 UNAVAILABLE high demand"),
            resp,
            RuntimeError("503 UNAVAILABLE high demand"),
        ]
        with self._env(), \
             mock.patch.object(gemini_client, "_get_gemini_client", return_value=client), \
             mock.patch.object(gemini_client, "_USES_LIBRESSL", False), \
             mock.patch.object(gemini_client, "_monotonic", return_value=1000.0), \
             mock.patch.object(cost_tracker, "record_api_error"), \
             mock.patch("time.sleep"):
            with self.assertRaises(RuntimeError):
                gemini_client._gemini_generate(
                    model="gemini-flash-latest",
                    contents=["hi"],
                    max_attempts=1,
                )
            self.assertIs(
                gemini_client._gemini_generate(
                    model="gemini-flash-latest",
                    contents=["hi"],
                    max_attempts=1,
                ),
                resp,
            )
            with self.assertRaises(RuntimeError):
                gemini_client._gemini_generate(
                    model="gemini-flash-latest",
                    contents=["hi"],
                    max_attempts=1,
                )

        self.assertEqual(client.models.generate_content.call_count, 3)

    def test_postgres_open_circuit_blocks_before_client_call(self):
        from agent_core import cost_tracker, gemini_client, operational_gemini_circuit

        client = mock.MagicMock()
        with self._env(), \
             mock.patch.dict(os.environ, {
                 "RED_OPERATIONAL_DB_URL": "postgresql://red",
                 "RED_GEMINI_CIRCUIT_BACKEND": "postgres",
             }, clear=False), \
             mock.patch.object(operational_gemini_circuit, "enabled", return_value=True), \
             mock.patch.object(
                 operational_gemini_circuit,
                 "open_state",
                 return_value={"opened_until": 220.0, "reason": "503"},
             ) as open_state, \
             mock.patch.object(gemini_client, "_epoch_time", return_value=100.0), \
             mock.patch.object(gemini_client, "_get_gemini_client", return_value=client), \
             mock.patch.object(cost_tracker, "record_api_error") as rec:
            with self.assertRaises(gemini_client.GeminiCircuitOpenError):
                gemini_client._gemini_generate(
                    model="gemini-flash-latest",
                    contents=["hi"],
                    max_attempts=1,
                )

        open_state.assert_called_once_with("gemini-flash-latest", now=100.0)
        client.models.generate_content.assert_not_called()
        rec.assert_not_called()

    def test_transient_failure_records_postgres_circuit_state(self):
        from agent_core import cost_tracker, gemini_client, operational_gemini_circuit

        client = mock.MagicMock()
        client.models.generate_content.side_effect = RuntimeError(
            "503 UNAVAILABLE high demand"
        )
        with self._env(), \
             mock.patch.dict(os.environ, {
                 "RED_OPERATIONAL_DB_URL": "postgresql://red",
                 "RED_GEMINI_CIRCUIT_BACKEND": "postgres",
             }, clear=False), \
             mock.patch.object(operational_gemini_circuit, "enabled", return_value=True), \
             mock.patch.object(operational_gemini_circuit, "open_state", return_value=None), \
             mock.patch.object(operational_gemini_circuit, "record_failure") as record_failure, \
             mock.patch.object(gemini_client, "_get_gemini_client", return_value=client), \
             mock.patch.object(gemini_client, "_USES_LIBRESSL", False), \
             mock.patch.object(gemini_client, "_epoch_time", return_value=100.0), \
             mock.patch.object(cost_tracker, "record_api_error"), \
             mock.patch("time.sleep"):
            with self.assertRaises(RuntimeError):
                gemini_client._gemini_generate(
                    model="gemini-flash-latest",
                    contents=["hi"],
                    max_attempts=1,
                )

        record_failure.assert_called_once_with(
            "gemini-flash-latest",
            reason="503",
            now=100.0,
            threshold=2,
            window_s=60,
            open_s=120,
        )


if __name__ == "__main__":
    unittest.main()
