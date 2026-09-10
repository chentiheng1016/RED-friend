"""工具函式字串 annotations 解析（fix: read_drive_file isinstance TypeError）。

`from __future__ import annotations` 模組裡的工具，__annotations__ 是字串。
google-genai 自動 function calling 在派發參數時對 annotation 做
isinstance(value, annotation)，字串會炸
`TypeError: isinstance() arg 2 must be a type, a tuple of types, or a union`。
陰險之處：FunctionDeclaration.from_callable 吃字串沒事 — 工具上得了場、
一被 LLM 帶參數呼叫就死（2026-06-12 read_drive_file 實際踩雷）。
"""
import unittest

from agent_core.tool_registry import _resolve_string_annotations, tools_list


def _make_string_annotated(name="fake_tool"):
    def fn(file_id, max_chars=8000):
        return "ok"

    fn.__name__ = name
    fn.__annotations__ = {"file_id": "str", "max_chars": "int", "return": "str"}
    return fn


class ResolveStringAnnotationsTests(unittest.TestCase):
    def test_string_annotations_resolved_to_types(self):
        fn = _make_string_annotated()
        _resolve_string_annotations([fn])
        self.assertIs(fn.__annotations__["file_id"], str)
        self.assertIs(fn.__annotations__["max_chars"], int)

    def test_real_type_annotations_untouched(self):
        def fn(q: str, n: int = 5) -> str:
            return q

        before = dict(fn.__annotations__)
        _resolve_string_annotations([fn])
        self.assertEqual(fn.__annotations__, before)

    def test_unresolvable_annotation_left_alone(self):
        # 前向引用到不存在的名稱：get_type_hints 會 NameError — 必須吞掉、
        # 留原樣，不能把 registry 組裝炸掉。
        def fn(x):
            return x

        fn.__annotations__ = {"x": "NoSuchTypeAnywhere123"}
        _resolve_string_annotations([fn])  # 不噴例外即過
        self.assertEqual(fn.__annotations__, {"x": "NoSuchTypeAnywhere123"})

    def test_no_annotations_ok(self):
        _resolve_string_annotations([lambda: None])  # 不噴例外即過


class RegistryInvariantTests(unittest.TestCase):
    def test_no_tool_in_registry_has_string_annotations(self):
        """全 catalog 不變量：任何進到 tools_list 的工具都不得帶字串 annotation。

        新模組若用 `from __future__ import annotations` 又沒被組裝點解析，
        這裡會抓到（修復前全 catalog 有 139 個）。
        """
        offenders = []
        for t in tools_list:
            ann = getattr(t, "__annotations__", {}) or {}
            if any(isinstance(v, str) for v in ann.values()):
                offenders.append(getattr(t, "__name__", repr(t)))
        self.assertEqual(
            offenders, [],
            f"這些工具帶字串 annotations，Gemini 一帶參數呼叫就會 TypeError：{offenders}",
        )

    def test_no_tool_signature_has_string_annotations(self):
        """更強的不變量：genai AFC 派發走 `inspect.signature(fn)`，它會沿
        `__wrapped__` unwrap 到**最內層**函式讀 annotation。只比對 `__annotations__`
        dict（上面那個測試）會漏看「進 resolve 前就被包過一層」的工具 —— 外層 dict
        修好了、inspect.signature 仍讀到內層字串。2026-06-16 add_task 等 45 個工具
        正是這樣靜默壞掉而舊不變量測試全綠。守住 signature 這條才是真的守住派發路徑。
        """
        import inspect

        offenders = []
        for t in tools_list:
            try:
                sig = inspect.signature(t)
            except (TypeError, ValueError):
                continue
            strs = [
                p.name for p in sig.parameters.values()
                if isinstance(p.annotation, str)
            ]
            if strs:
                offenders.append((getattr(t, "__name__", repr(t)), strs))
        self.assertEqual(
            offenders, [],
            f"這些工具的 inspect.signature 仍帶字串 annotation，Gemini 帶參數呼叫"
            f"就會 TypeError：{offenders}",
        )


class GenaiDispatchRegressionTests(unittest.TestCase):
    def test_genai_arg_conversion_accepts_read_drive_file(self):
        """直接走 google-genai 的參數轉換（出事的那條路）：

        修復前：annotation 是 'str' → isinstance(value, 'str') → TypeError。
        修復後：轉換成功、值原樣通過。不實際呼叫工具（不打 Drive API）。
        """
        try:
            from google.genai._extra_utils import convert_argument_from_function
        except Exception:  # SDK 私有 API — 版本變動時跳過而不是紅
            self.skipTest("google.genai._extra_utils 不可用")

        from agent_core.ingest.drive_search import read_drive_file

        converted = convert_argument_from_function(
            {"file_id": "fake-id-123", "max_chars": 600}, read_drive_file
        )
        self.assertEqual(converted["file_id"], "fake-id-123")
        self.assertEqual(converted["max_chars"], 600)

    def test_genai_arg_conversion_blows_up_on_string_annotation(self):
        """反向佐證：字串 annotation 確實會讓 SDK 派發炸 TypeError —
        證明 RegistryInvariantTests 守的就是這個雷。"""
        try:
            from google.genai._extra_utils import convert_argument_from_function
        except Exception:
            self.skipTest("google.genai._extra_utils 不可用")

        fn = _make_string_annotated()
        with self.assertRaises(TypeError):
            convert_argument_from_function({"file_id": "x"}, fn)

    def test_genai_arg_conversion_accepts_prewrapped_tool(self):
        """回歸：add_task 是「進 resolve 前就被包過一層（有 __wrapped__）」的工具。
        修復前 resolve 只解外層 __annotations__ dict，但 SDK 走 inspect.signature
        會 unwrap 到內層字串 → 帶參數呼叫炸 TypeError（小紅 2026-06-16 在 Telegram
        上「無法登記提醒」的真因）。釘住 __signature__ 後這條路也得通。"""
        try:
            from google.genai._extra_utils import convert_argument_from_function
        except Exception:
            self.skipTest("google.genai._extra_utils 不可用")

        at = [t for t in tools_list if getattr(t, "__name__", "") == "add_task"]
        self.assertTrue(at, "add_task 不在 tools_list")
        converted = convert_argument_from_function(
            {"title": "看牙周病", "priority": 1}, at[0]
        )
        self.assertEqual(converted["title"], "看牙周病")
        self.assertEqual(converted["priority"], 1)


if __name__ == "__main__":
    unittest.main()
