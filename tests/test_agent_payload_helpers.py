"""pstr / pint 抽取的行為等價回歸測試。

證明共用 helper 對各種邊界輸入與原本散落各 agent 的 inline idiom 逐一等價，
讓「抽 helper」這個 refactor 站得住腳。"""
from __future__ import annotations

import os
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from agent_core.agents._payload import pint, pstr


def _old_pstr(payload, *keys):
    """原 idiom：str(payload.get(k1) or payload.get(k2) or ... or "")。"""
    acc = ""
    for k in keys:
        acc = acc or payload.get(k)
    return str(acc or "")


def _old_pint(payload, *keys, default):
    """原 idiom：int(payload.get(k1, payload.get(k2, default)) or default)（巢狀）。"""
    sentinel = object()
    val = default
    for k in keys:
        v = payload.get(k, sentinel)
        if v is not sentinel:
            val = v
            break
    return int(val or default)


class PstrEquivalenceTests(unittest.TestCase):
    def test_matches_old_idiom(self):
        payloads = [
            {},
            {"counterparty": "ACME"},
            {"customer": "DECA"},
            {"counterparty": "", "customer": "DECA"},   # 空字串 → 退下一個
            {"counterparty": 0, "customer": "X"},        # falsy → 退下一個
            {"po_number": 12345},                         # 非字串 → str()
            {"keyword": "鞋", "query": "ignored"},
        ]
        for p in payloads:
            self.assertEqual(
                pstr(p, "counterparty", "customer", "supplier"),
                _old_pstr(p, "counterparty", "customer", "supplier"),
                f"counterparty 鏈不等價: {p}",
            )
            self.assertEqual(pstr(p, "keyword", "query"), _old_pstr(p, "keyword", "query"), p)

    def test_default(self):
        self.assertEqual(pstr({}, "a", "b"), "")
        self.assertEqual(pstr({}, "a", default="fallback"), "fallback")


class PintEquivalenceTests(unittest.TestCase):
    def test_matches_nested_default_idiom(self):
        payloads = [
            {},
            {"days_back": 30},
            {"days": 90},
            {"days_back": 7, "days": 90},                 # 第一個存在的 key 勝
            {"days_back": 0, "days": 90},                 # 0 存在 → 落 default（非 90）
            {"days": 0},                                   # 0 → default
            {"days_back": "45"},                          # 數字字串 → int
            {"limit": 50},
        ]
        for p in payloads:
            self.assertEqual(
                pint(p, "days_back", "days", default=365),
                _old_pint(p, "days_back", "days", default=365),
                f"days_back 巢狀預設不等價: {p}",
            )
            self.assertEqual(pint(p, "limit", default=20), _old_pint(p, "limit", default=20), p)

    def test_zero_present_key_falls_to_default_not_next(self):
        # 這是 nested-default 與 first-truthy 的關鍵差異，必須是前者
        self.assertEqual(pint({"days_back": 0, "days": 90}, "days_back", "days", default=365), 365)

    def test_non_numeric_returns_default(self):
        # 比原 idiom 多的保險：非數字不拋例外
        self.assertEqual(pint({"days_back": "abc"}, "days_back", default=365), 365)


if __name__ == "__main__":
    unittest.main()
