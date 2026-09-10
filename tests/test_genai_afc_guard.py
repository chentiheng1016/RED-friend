"""AFC 未知工具護欄：模型呼叫不存在/被拔掉的工具不該炸掉整輪。

回歸自正式事故 ``❌ 背景任務最後失敗（347s）：KeyError: 'run_shell'`` ——
非 owner actor 被拔掉 owner-only 工具後，模型仍去叫 run_shell，genai SDK 的
``function_map[func_name]`` 在 try/except 外 KeyError，整輪任務死。
"""
import unittest

from agent_core.genai_afc_guard import (
    _GuardedFunctionMap,
    install_afc_unknown_tool_guard,
)


class GuardedFunctionMapTests(unittest.TestCase):
    def test_known_key_returned_unchanged(self):
        sentinel = lambda: "real"  # noqa: E731
        m = _GuardedFunctionMap({"a": sentinel})
        self.assertIs(m["a"], sentinel)

    def test_missing_key_returns_raising_stub(self):
        m = _GuardedFunctionMap({})
        stub = m["run_shell"]  # 不可 KeyError
        self.assertTrue(callable(stub))
        self.assertEqual(stub.__name__, "run_shell")
        with self.assertRaises(ValueError) as ctx:
            stub(command="rm -rf /")  # 任何參數都該被吞掉再拋
        self.assertIn("run_shell", str(ctx.exception))

    def test_contains_still_reports_missing(self):
        # __missing__ 只影響 __getitem__；in / get 行為不變（SDK 內部別處可能用到）。
        m = _GuardedFunctionMap({"a": lambda: 1})
        self.assertIn("a", m)
        self.assertNotIn("nope", m)
        self.assertIsNone(m.get("nope"))


class InstallGuardTests(unittest.TestCase):
    def test_install_is_idempotent_and_sets_flag(self):
        from google.genai import _extra_utils

        self.assertTrue(install_afc_unknown_tool_guard())
        self.assertTrue(install_afc_unknown_tool_guard())  # 第二次也 True
        self.assertTrue(getattr(_extra_utils, "_red_unknown_tool_guard", False))

    def test_sdk_seam_still_exists(self):
        # 不變量：SDK 升級若把派發函式搬走，這裡先紅，提醒更新護欄。
        from google.genai import _extra_utils

        self.assertTrue(hasattr(_extra_utils, "get_function_response_parts"))


class RealSdkDispatchTests(unittest.TestCase):
    """跑真正的 SDK 派發路徑，確認護欄把未知工具變成 error part 而非崩潰。"""

    def setUp(self):
        install_afc_unknown_tool_guard()
        from google.genai import types

        self._types = types

    def _response_calling(self, tool_name, args):
        types = self._types
        return types.GenerateContentResponse(
            candidates=[
                types.Candidate(
                    content=types.Content(
                        role="model",
                        parts=[
                            types.Part(
                                function_call=types.FunctionCall(
                                    name=tool_name, args=args
                                )
                            )
                        ],
                    )
                )
            ]
        )

    def test_unknown_tool_does_not_crash_and_returns_error_part(self):
        from google.genai import _extra_utils

        resp = self._response_calling("run_shell", {"command": "ls"})
        # 空 function_map → 一定查不到；護欄前這行會 KeyError 炸掉。
        parts = _extra_utils.get_function_response_parts(resp, {})
        self.assertEqual(len(parts), 1)
        payload = parts[0].function_response.response
        self.assertIn("error", payload)
        self.assertIn("run_shell", payload["error"])

    def test_known_tool_still_dispatched(self):
        from google.genai import _extra_utils

        resp = self._response_calling("echo_tool", {})
        parts = _extra_utils.get_function_response_parts(
            resp, {"echo_tool": lambda: "ok-result"}
        )
        self.assertEqual(parts[0].function_response.response, {"result": "ok-result"})


if __name__ == "__main__":
    unittest.main()
