"""clear_drive_skip_markers 一次性維運腳本：reason 精確篩選、dry-run 不動檔、
--apply 原子寫回且不誤刪其他 reason 的 marker。"""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest


_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import scripts.clear_drive_skip_markers as cdsm  # noqa: E402


XLSX_REASON = "extract_error: Excel xlsx file; not supported"


def _state() -> dict:
    return {
        "version": 1,
        "files": {
            "keep-encrypted": {
                "reason": "extract_error: FileNotDecryptedError",
                "modified_time": "2026-05-07T09:00:00.000Z",
                "title": "secret.pdf",
            },
            "gone-invoice": {
                "reason": XLSX_REASON,
                "modified_time": "2026-06-01T10:00:00.000Z",
                "title": "INVOICE --LOT 208-2026 (EXCEL).xls",
            },
            "gone-packing": {
                "reason": XLSX_REASON,
                "modified_time": "2026-06-02T10:00:00.000Z",
                "title": "PACKING LIST (EXCEL).xls",
            },
        },
    }


class RemoveMarkersByReasonTests(unittest.TestCase):
    def test_removes_only_matching_reason(self):
        cleaned, removed = cdsm.remove_markers_by_reason(_state(), {XLSX_REASON})
        self.assertEqual(
            sorted(fid for fid, _ in removed), ["gone-invoice", "gone-packing"]
        )
        self.assertEqual(set(cleaned["files"]), {"keep-encrypted"})

    def test_reason_match_is_exact_not_substring(self):
        cleaned, removed = cdsm.remove_markers_by_reason(_state(), {"Excel xlsx file"})
        self.assertEqual(removed, [])
        self.assertEqual(len(cleaned["files"]), 3)

    def test_original_state_untouched_and_version_preserved(self):
        state = _state()
        cleaned, _ = cdsm.remove_markers_by_reason(state, {XLSX_REASON})
        self.assertEqual(len(state["files"]), 3)  # 原 dict 不被就地修改
        self.assertEqual(cleaned["version"], 1)

    def test_tolerates_missing_or_malformed_files_key(self):
        for state in ({}, {"files": "not-a-dict"}):
            cleaned, removed = cdsm.remove_markers_by_reason(state, {XLSX_REASON})
            self.assertEqual(removed, [])
            self.assertEqual(cleaned, state)

    def test_ids_only_removes_listed_ids_any_reason(self):
        cleaned, removed = cdsm.remove_markers_by_reason(
            _state(), None, ids={"keep-encrypted", "gone-invoice"}
        )
        self.assertEqual(
            sorted(fid for fid, _ in removed), ["gone-invoice", "keep-encrypted"]
        )
        self.assertEqual(set(cleaned["files"]), {"gone-packing"})

    def test_reason_and_ids_intersect(self):
        cleaned, removed = cdsm.remove_markers_by_reason(
            _state(), {XLSX_REASON}, ids={"gone-invoice", "keep-encrypted"}
        )
        # keep-encrypted 在白名單但 reason 不符；gone-packing reason 符但不在白名單
        self.assertEqual([fid for fid, _ in removed], ["gone-invoice"])
        self.assertEqual(set(cleaned["files"]), {"keep-encrypted", "gone-packing"})

    def test_requires_reasons_or_ids(self):
        for reasons, ids in ((None, None), (set(), None), (None, set())):
            with self.assertRaises(ValueError):
                cdsm.remove_markers_by_reason(_state(), reasons, ids)


class MainTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state_file = os.path.join(self._tmp.name, "drive_sync_skip_state.json")
        with open(self.state_file, "w", encoding="utf-8") as f:
            json.dump(_state(), f)

    def _read_state(self) -> dict:
        with open(self.state_file, encoding="utf-8") as f:
            return json.load(f)

    @staticmethod
    def _run(argv: list[str]) -> tuple[int, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cdsm.main(argv)
        return rc, out.getvalue()

    def test_dry_run_lists_but_does_not_write(self):
        rc, out = self._run(["--reason", XLSX_REASON, "--state-file", self.state_file])
        self.assertEqual(rc, 0)
        self.assertIn("gone-invoice", out)
        self.assertIn("INVOICE --LOT 208-2026 (EXCEL).xls", out)
        self.assertEqual(len(self._read_state()["files"]), 3)

    def test_apply_removes_matching_and_keeps_rest(self):
        rc, _ = self._run(
            ["--reason", XLSX_REASON, "--state-file", self.state_file, "--apply"]
        )
        self.assertEqual(rc, 0)
        state = self._read_state()
        self.assertEqual(set(state["files"]), {"keep-encrypted"})
        self.assertEqual(state["version"], 1)

    def test_missing_state_file_returns_error(self):
        rc, _ = self._run(
            ["--reason", XLSX_REASON, "--state-file", self.state_file + ".nope"]
        )
        self.assertEqual(rc, 1)

    def _write_ids_file(self, content: str) -> str:
        path = os.path.join(self._tmp.name, "ids.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return path

    def test_ids_file_apply_removes_only_listed(self):
        ids_path = self._write_ids_file(
            "# 已入庫確認的白名單\ngone-invoice\n\n"
        )
        rc, out = self._run(
            ["--ids-file", ids_path, "--state-file", self.state_file, "--apply"]
        )
        self.assertEqual(rc, 0)
        self.assertIn("gone-invoice", out)
        self.assertEqual(
            set(self._read_state()["files"]), {"keep-encrypted", "gone-packing"}
        )

    def test_ids_file_intersects_with_reason(self):
        ids_path = self._write_ids_file("gone-invoice\nkeep-encrypted\n")
        rc, out = self._run(
            [
                "--reason", XLSX_REASON, "--ids-file", ids_path,
                "--state-file", self.state_file, "--apply",
            ]
        )
        self.assertEqual(rc, 0)
        self.assertNotIn("keep-encrypted", out)  # reason 不符 → 白名單也不清
        self.assertEqual(
            set(self._read_state()["files"]), {"keep-encrypted", "gone-packing"}
        )

    def test_empty_or_missing_ids_file_returns_error(self):
        empty = self._write_ids_file("# 只有註解\n\n")
        for path in (empty, os.path.join(self._tmp.name, "nope.txt")):
            rc, _ = self._run(["--ids-file", path, "--state-file", self.state_file])
            self.assertEqual(rc, 1)
        self.assertEqual(len(self._read_state()["files"]), 3)

    def test_neither_reason_nor_ids_is_usage_error(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as ctx:
                cdsm.main(["--state-file", self.state_file])
        self.assertNotEqual(ctx.exception.code, 0)


if __name__ == "__main__":
    unittest.main()
