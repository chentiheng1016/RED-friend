"""pip-audit 的 chromadb CVE 豁免不准活得比它的理由久。

背景：chromadb 1.5.9（＝最新版，上游對這四個 CVE 都還沒修）在 pip-audit 底下會
讓 `make security-audit` 紅。Makefile 用 scoped `--ignore-vuln` 壓掉，理由是每個
CVE 的前提條件在這套部署都不成立（server 只綁 127.0.0.1、沒有任何 auth provider、
單一 tenant、沒用 trust_remote_code）。

豁免的危險在於它會活得比理由久 —— Makefile 自己的 npm 註解就記著這個教訓
（brace-expansion 那次：上游修好後豁免留著，會遮住下一次回歸）。這裡放兩道守門：

  1. chromadb 的 pin 一動，測試就紅 —— 逼你在升版當下回去看豁免還需不需要，
     而不是讓一份為 1.5.9 寫的理由默默套用到之後的版本；
  2. 豁免清單裡的每個 CVE 編號都必須在上面的註解區塊裡有交代，不准無聲加一條。
"""
from __future__ import annotations

import os
import re
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from agent_core.path_safety import _REPO_ROOT as REPO_ROOT  # noqa: E402

# 豁免理由是針對這個版本查證的（2026-08-25）。動了 pin 就必須重新查證。
JUSTIFIED_CHROMADB_VERSION = "1.5.9"

_MAKEFILE = os.path.join(REPO_ROOT, "Makefile")
_REQ_RAG = os.path.join(REPO_ROOT, "requirements-rag.txt")


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


def _ignore_block(makefile: str) -> str:
    """CHROMADB_CVE_IGNORE 的賦值，含反斜線續行。"""
    lines = makefile.splitlines()
    start = next(i for i, ln in enumerate(lines)
                 if ln.startswith("CHROMADB_CVE_IGNORE"))
    end = start
    while lines[end].rstrip().endswith("\\"):
        end += 1
    return "\n".join(lines[start:end + 1])


def _justification_comment(makefile: str) -> str:
    """緊貼在賦值上方的那段註解。"""
    lines = makefile.splitlines()
    start = next(i for i, ln in enumerate(lines)
                 if ln.startswith("CHROMADB_CVE_IGNORE"))
    out = []
    i = start - 1
    while i >= 0 and lines[i].startswith("#"):
        out.append(lines[i])
        i -= 1
    return "\n".join(reversed(out))


class ChromadbCveIgnoreScopeTests(unittest.TestCase):
    def test_pin_still_matches_the_version_the_ignores_were_justified_for(self):
        pinned = re.search(r"^chromadb==(\S+)", _read(_REQ_RAG), re.M)
        self.assertIsNotNone(pinned, "requirements-rag.txt 找不到 chromadb 的 pin")
        self.assertEqual(
            pinned.group(1), JUSTIFIED_CHROMADB_VERSION,
            "chromadb 的 pin 動了，但 Makefile 的 CHROMADB_CVE_IGNORE 還是為 "
            f"{JUSTIFIED_CHROMADB_VERSION} 寫的理由。請回去確認：上游是不是已經修掉"
            "其中幾個 CVE（修掉的就從豁免清單刪掉，留著會遮住下一次回歸），"
            "然後把這個測試的 JUSTIFIED_CHROMADB_VERSION 一起更新。",
        )

    def test_every_suppressed_cve_is_justified_in_the_comment(self):
        makefile = _read(_MAKEFILE)
        suppressed = re.findall(r"--ignore-vuln\s+(\S+)", _ignore_block(makefile))
        self.assertTrue(suppressed, "CHROMADB_CVE_IGNORE 解析不到任何 CVE")
        comment = _justification_comment(makefile)
        for cve in suppressed:
            self.assertIn(
                cve, comment,
                f"{cve} 被壓掉了卻沒在上面的註解裡交代為什麼安全 —— "
                "無聲的豁免正是稽核訊號失效的起點。",
            )

    def test_loopback_only_premise_is_recorded(self):
        """整套理由都建立在『chroma 只綁 loopback』上，這句話不能從註解裡消失。"""
        comment = _justification_comment(_read(_MAKEFILE))
        self.assertIn("127.0.0.1", comment)


if __name__ == "__main__":
    unittest.main()
