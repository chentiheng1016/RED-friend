"""agent_core/env_utils 共用 env 解析 helper 的行為合約。

這是全 repo 12+ 份 _env_int/_env_float 重複實作收斂後的唯一真相：
壞值回 default、default 一律原樣回傳（不 clamp）、clamp 只套在解析
成功的值上、min/max 為 None 表示該側無界（cloud_run 的「刻意不
clamp」就靠這個）。
"""
from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from agent_core.env_utils import env_float, env_int  # noqa: E402

_VAR = "RED_TEST_ENV_UTILS_VAR"


class EnvIntTests(unittest.TestCase):
    def setUp(self):
        # make test-quiet 走 unittest discover，conftest 的 pytest fixture
        # 不會生效 — 環境隔離必須放在 setUp/tearDown。
        os.environ.pop(_VAR, None)

    tearDown = setUp

    def test_unset_returns_default(self):
        self.assertEqual(env_int(_VAR, 7), 7)

    def test_empty_string_returns_default(self):
        with mock.patch.dict(os.environ, {_VAR: ""}):
            self.assertEqual(env_int(_VAR, 7), 7)

    def test_whitespace_only_returns_default(self):
        with mock.patch.dict(os.environ, {_VAR: "   "}):
            self.assertEqual(env_int(_VAR, 7), 7)

    def test_non_numeric_returns_default(self):
        with mock.patch.dict(os.environ, {_VAR: "abc"}):
            self.assertEqual(env_int(_VAR, 7), 7)

    def test_float_string_is_not_an_int(self):
        with mock.patch.dict(os.environ, {_VAR: "12.5"}):
            self.assertEqual(env_int(_VAR, 7), 7)

    def test_valid_value_parsed(self):
        with mock.patch.dict(os.environ, {_VAR: "42"}):
            self.assertEqual(env_int(_VAR, 7), 42)

    def test_surrounding_whitespace_tolerated(self):
        with mock.patch.dict(os.environ, {_VAR: " 42 "}):
            self.assertEqual(env_int(_VAR, 7), 42)

    def test_negative_value_without_bounds(self):
        with mock.patch.dict(os.environ, {_VAR: "-5"}):
            self.assertEqual(env_int(_VAR, 7), -5)

    def test_clamp_lower_bound(self):
        with mock.patch.dict(os.environ, {_VAR: "1"}):
            self.assertEqual(env_int(_VAR, 7, min_value=5, max_value=10), 5)

    def test_clamp_upper_bound(self):
        with mock.patch.dict(os.environ, {_VAR: "999"}):
            self.assertEqual(env_int(_VAR, 7, min_value=5, max_value=10), 10)

    def test_within_bounds_untouched(self):
        with mock.patch.dict(os.environ, {_VAR: "8"}):
            self.assertEqual(env_int(_VAR, 7, min_value=5, max_value=10), 8)

    def test_max_value_none_means_unbounded(self):
        # telegram.py 的 maximum=None 變體：有下限、無上限
        with mock.patch.dict(os.environ, {_VAR: "999999999"}):
            self.assertEqual(env_int(_VAR, 7, min_value=1), 999999999)

    def test_min_value_none_means_unbounded(self):
        with mock.patch.dict(os.environ, {_VAR: "-999"}):
            self.assertEqual(env_int(_VAR, 7, max_value=10), -999)

    def test_no_bounds_means_no_clamp(self):
        # cloud_run_entrypoint 的「刻意不 clamp」語意
        with mock.patch.dict(os.environ, {_VAR: "70000"}):
            self.assertEqual(env_int(_VAR, 8080), 70000)

    def test_default_is_not_clamped(self):
        # default 由呼叫端負責，一律原樣回傳
        self.assertEqual(env_int(_VAR, 7, min_value=10, max_value=20), 7)

    def test_bad_value_default_is_not_clamped(self):
        with mock.patch.dict(os.environ, {_VAR: "abc"}):
            self.assertEqual(env_int(_VAR, 7, min_value=10, max_value=20), 7)


class EnvFloatTests(unittest.TestCase):
    def setUp(self):
        os.environ.pop(_VAR, None)

    tearDown = setUp

    def test_unset_returns_default(self):
        self.assertEqual(env_float(_VAR, 2.5), 2.5)

    def test_empty_string_returns_default(self):
        with mock.patch.dict(os.environ, {_VAR: ""}):
            self.assertEqual(env_float(_VAR, 2.5), 2.5)

    def test_non_numeric_returns_default(self):
        with mock.patch.dict(os.environ, {_VAR: "soon"}):
            self.assertEqual(env_float(_VAR, 2.5), 2.5)

    def test_valid_value_parsed(self):
        with mock.patch.dict(os.environ, {_VAR: "3.75"}):
            self.assertEqual(env_float(_VAR, 2.5), 3.75)

    def test_int_string_parses_as_float(self):
        with mock.patch.dict(os.environ, {_VAR: "4"}):
            self.assertEqual(env_float(_VAR, 2.5), 4.0)

    def test_nan_returns_default(self):
        with mock.patch.dict(os.environ, {_VAR: "nan"}):
            self.assertEqual(env_float(_VAR, 2.5), 2.5)

    def test_positive_infinity_returns_default(self):
        with mock.patch.dict(os.environ, {_VAR: "inf"}):
            self.assertEqual(env_float(_VAR, 2.5), 2.5)

    def test_negative_infinity_returns_default(self):
        with mock.patch.dict(os.environ, {_VAR: "-inf"}):
            self.assertEqual(env_float(_VAR, 2.5), 2.5)

    def test_clamp_lower_bound(self):
        with mock.patch.dict(os.environ, {_VAR: "0.1"}):
            self.assertEqual(env_float(_VAR, 2.5, min_value=1.0, max_value=60.0), 1.0)

    def test_clamp_upper_bound(self):
        with mock.patch.dict(os.environ, {_VAR: "100.0"}):
            self.assertEqual(env_float(_VAR, 2.5, min_value=1.0, max_value=60.0), 60.0)

    def test_max_value_none_means_unbounded(self):
        with mock.patch.dict(os.environ, {_VAR: "1e12"}):
            self.assertEqual(env_float(_VAR, 2.5, min_value=0.0), 1e12)

    def test_no_bounds_means_no_clamp(self):
        with mock.patch.dict(os.environ, {_VAR: "-3.5"}):
            self.assertEqual(env_float(_VAR, 2.5), -3.5)

    def test_default_is_not_clamped(self):
        self.assertEqual(env_float(_VAR, 0.5, min_value=1.0, max_value=60.0), 0.5)

    def test_default_coerced_to_float(self):
        value = env_float(_VAR, 25)
        self.assertIsInstance(value, float)
        self.assertEqual(value, 25.0)


if __name__ == "__main__":
    unittest.main()
