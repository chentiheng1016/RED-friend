"""回覆附圖機制測試（[[TG_PHOTO:...]] 標記 → sendPhoto 回當前對話）。

2026-07-28 UserA 案：員工要鞋款照片本體，員工 chat 不在出站推送授權名單，
工具端推不出去 → 照片跟著回覆走。covers：標記抽取與路徑白名單 fail-closed、
tg_send_with_photos 的文字/照片分流、fetch_shoe_photos 工具（mock Drive/ERP）。

注意（unittest discover）：conftest fixture 不生效，隔離全在 setUp/tearDown；
mock.patch 一律在主執行緒。
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO_ROOT)


class ExtractReplyPhotosTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.realpath(self.tmp.name)
        self._roots_patch = mock.patch(
            "agent_core.daemon_telegram._reply_photo_allowed_roots",
            return_value=(self.root,),
        )
        self._roots_patch.start()

    def tearDown(self):
        self._roots_patch.stop()
        self.tmp.cleanup()

    def _photo(self, name: str, size: int = 10) -> str:
        path = os.path.join(self.root, name)
        with open(path, "wb") as f:
            f.write(b"x" * size)
        return path

    def test_no_marker_passthrough(self):
        from agent_core.daemon_telegram import _extract_reply_photos
        clean, photos = _extract_reply_photos("一般回覆，沒有標記")
        self.assertEqual(clean, "一般回覆，沒有標記")
        self.assertEqual(photos, [])

    def test_valid_marker_extracted_and_stripped(self):
        from agent_core.daemon_telegram import _extract_reply_photos
        p = self._photo("a.jpg")
        clean, photos = _extract_reply_photos(f"找到鞋圖：\n\n[[TG_PHOTO:{p}]]\n")
        self.assertEqual(photos, [p])
        self.assertNotIn("TG_PHOTO", clean)
        self.assertIn("找到鞋圖", clean)

    def test_path_outside_allowed_roots_rejected(self):
        from agent_core.daemon_telegram import _extract_reply_photos
        outside = os.path.join(os.path.dirname(self.root), "evil.jpg")
        with open(outside, "wb") as f:
            f.write(b"x")
        try:
            clean, photos = _extract_reply_photos(f"[[TG_PHOTO:{outside}]]")
            self.assertEqual(photos, [])
            self.assertNotIn("TG_PHOTO", clean)
        finally:
            os.remove(outside)

    def test_traversal_into_root_prefix_sibling_rejected(self):
        # /tmp/xxx-evil 不能因為字串前綴 startswith(/tmp/xxx) 被誤放行
        from agent_core.daemon_telegram import _extract_reply_photos
        sibling_dir = self.root + "-evil"
        os.makedirs(sibling_dir, exist_ok=True)
        evil = os.path.join(sibling_dir, "b.jpg")
        with open(evil, "wb") as f:
            f.write(b"x")
        try:
            _clean, photos = _extract_reply_photos(f"[[TG_PHOTO:{evil}]]")
            self.assertEqual(photos, [])
        finally:
            evil_dir = sibling_dir
            os.remove(evil)
            os.rmdir(evil_dir)

    def test_non_image_ext_rejected(self):
        from agent_core.daemon_telegram import _extract_reply_photos
        p = self._photo("secret.pdf")
        _clean, photos = _extract_reply_photos(f"[[TG_PHOTO:{p}]]")
        self.assertEqual(photos, [])

    def test_missing_file_rejected(self):
        from agent_core.daemon_telegram import _extract_reply_photos
        ghost = os.path.join(self.root, "ghost.jpg")
        _clean, photos = _extract_reply_photos(f"[[TG_PHOTO:{ghost}]]")
        self.assertEqual(photos, [])

    def test_oversize_rejected(self):
        from agent_core import daemon_telegram as dt
        p = self._photo("big.jpg")
        with mock.patch.object(dt, "_TG_REPLY_PHOTO_MAX_BYTES", 5):
            _clean, photos = dt._extract_reply_photos(f"[[TG_PHOTO:{p}]]")
        self.assertEqual(photos, [])

    def test_cap_and_dedup(self):
        from agent_core import daemon_telegram as dt
        paths = [self._photo(f"p{i}.jpg") for i in range(7)]
        markers = "\n".join(f"[[TG_PHOTO:{p}]]" for p in paths + [paths[0]])
        _clean, photos = dt._extract_reply_photos(markers)
        self.assertEqual(len(photos), dt._TG_REPLY_PHOTO_MAX)
        self.assertEqual(len(set(photos)), len(photos))


class TgSendWithPhotosTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.realpath(self.tmp.name)
        self._roots_patch = mock.patch(
            "agent_core.daemon_telegram._reply_photo_allowed_roots",
            return_value=(self.root,),
        )
        self._roots_patch.start()
        self.photo = os.path.join(self.root, "shoe.jpg")
        with open(self.photo, "wb") as f:
            f.write(b"jpg")

    def tearDown(self):
        self._roots_patch.stop()
        self.tmp.cleanup()

    def test_text_and_photo_sent_with_same_token_and_chat(self):
        from agent_core import daemon_telegram as dt
        sent = {}
        with mock.patch.object(dt, "tg_send", return_value=True) as m_send, \
                mock.patch("agent_core.telegram._send_photo_with_retries",
                           return_value=({"ok": True}, None)) as m_photo:
            ok = dt.tg_send_with_photos(
                "TOKEN", "9990000005", f"鞋圖來了\n[[TG_PHOTO:{self.photo}]]")
        self.assertTrue(ok)
        sent_text = m_send.call_args[0][2]
        self.assertNotIn("TG_PHOTO", sent_text)
        self.assertIn("鞋圖來了", sent_text)
        kwargs = m_photo.call_args.kwargs
        self.assertEqual(kwargs["target"], "9990000005")
        self.assertEqual(kwargs["token"], "TOKEN")
        self.assertEqual(kwargs["abs_path"], self.photo)
        _ = sent  # noqa: F841

    def test_photo_only_reply_skips_empty_text_send(self):
        from agent_core import daemon_telegram as dt
        with mock.patch.object(dt, "tg_send", return_value=True) as m_send, \
                mock.patch("agent_core.telegram._send_photo_with_retries",
                           return_value=({"ok": True}, None)):
            ok = dt.tg_send_with_photos("T", "1", f"[[TG_PHOTO:{self.photo}]]")
        self.assertTrue(ok)
        m_send.assert_not_called()

    def test_photo_failure_does_not_break_text_delivery(self):
        from agent_core import daemon_telegram as dt
        with mock.patch.object(dt, "tg_send", return_value=True), \
                mock.patch("agent_core.telegram._send_photo_with_retries",
                           side_effect=RuntimeError("boom")):
            ok = dt.tg_send_with_photos(
                "T", "1", f"文字\n[[TG_PHOTO:{self.photo}]]")
        self.assertTrue(ok)

    def test_plain_text_goes_through_tg_send_unchanged(self):
        from agent_core import daemon_telegram as dt
        with mock.patch.object(dt, "tg_send", return_value=True) as m_send, \
                mock.patch("agent_core.telegram._send_photo_with_retries") as m_photo:
            ok = dt.tg_send_with_photos("T", "1", "純文字回覆")
        self.assertTrue(ok)
        self.assertEqual(m_send.call_args[0][2], "純文字回覆")
        m_photo.assert_not_called()

    def test_all_markers_rejected_still_sends_clean_text(self):
        # Codex P2（PR #305）：標記全數被拒也不能把原始標記丟給使用者
        from agent_core import daemon_telegram as dt
        ghost = os.path.join(self.root, "ghost.jpg")   # 不存在 → 標記被拒
        with mock.patch.object(dt, "tg_send", return_value=True) as m_send:
            ok = dt.tg_send_with_photos(
                "T", "1", f"查到鞋圖如下\n[[TG_PHOTO:{ghost}]]")
        self.assertTrue(ok)
        sent = m_send.call_args[0][2]
        self.assertNotIn("TG_PHOTO", sent)
        self.assertIn("查到鞋圖如下", sent)

    def test_marker_only_reply_all_rejected_sends_notice(self):
        from agent_core import daemon_telegram as dt
        ghost = os.path.join(self.root, "ghost.jpg")
        with mock.patch.object(dt, "tg_send", return_value=True) as m_send:
            ok = dt.tg_send_with_photos("T", "1", f"[[TG_PHOTO:{ghost}]]")
        self.assertTrue(ok)
        sent = m_send.call_args[0][2]
        self.assertNotIn("TG_PHOTO", sent)
        self.assertIn("未通過驗證", sent)


class _FakeDriveFiles:
    """files().list().execute() / files().get_media() 的最小假物件。"""

    def __init__(self, hits):
        self._hits = hits
        self.list_calls: list[dict] = []

    def list(self, **kwargs):
        self.list_calls.append(kwargs)
        q = kwargs.get("q", "")
        matched = [f for f in self._hits if f["_kw"] in q]
        return mock.Mock(execute=lambda: {"files": [
            {k: v for k, v in f.items() if k != "_kw"} for f in matched]})

    def get_media(self, fileId="", **kwargs):
        return mock.Mock(_file_id=fileId)


class FetchShoePhotosTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        import skills.product_photo_search as pps
        self.pps = pps
        self._fetch_patch = mock.patch.object(
            pps, "_FETCH_DIR", os.path.join(self.tmp.name, "fetched"))
        self._fetch_patch.start()

    def tearDown(self):
        self._fetch_patch.stop()
        self.tmp.cleanup()

    def _run(self, query, hits, erp_codes=None, top_k=3, erp_lots=None):
        pps = self.pps
        fake_svc = mock.Mock()
        self.fake_files = _FakeDriveFiles(hits)
        fake_svc.files.return_value = self.fake_files

        class _FakeDownloader:
            def __init__(self, buf, req):
                buf.write(b"IMAGEBYTES-" + req._file_id.encode())
                self._done = False

            def next_chunk(self):
                return None, True

        with mock.patch.object(pps, "_erp_style_codes",
                               side_effect=lambda kw: list(erp_codes or [])), \
                mock.patch.object(pps, "_erp_cust_lots",
                                  side_effect=lambda kw: list(erp_lots or [])), \
                mock.patch.object(pps, "_load", side_effect=FileNotFoundError), \
                mock.patch("agent_core.google_auth.get_service",
                           return_value=fake_svc), \
                mock.patch("googleapiclient.http.MediaIoBaseDownload",
                           _FakeDownloader):
            return pps.fetch_shoe_photos(query, top_k=top_k)

    def test_empty_query_returns_usage(self):
        out = self.pps.fetch_shoe_photos("")
        self.assertIn("款號", out)

    def test_erp_resolution_and_download_markers(self):
        hits = [
            {"_kw": "DJS336195", "id": "fid00001", "name": "DJS336195L1.jpg",
             "mimeType": "image/jpeg", "size": "146374"},
            {"_kw": "DJS336195", "id": "fid00002", "name": "DJS336195.jpg",
             "mimeType": "image/jpeg", "size": "59123"},
        ]
        out = self._run("#8916447", hits, erp_codes=["DJS336195-01GREEN", "DJS336195"])
        self.assertIn("[[TG_PHOTO:", out)
        self.assertIn("8916447→DJS336195", out)
        # 非線稿排前（彩圖案）：L1 是線稿命名慣例 → 沉底
        self.assertLess(out.index("DJS336195.jpg"), out.index("DJS336195L1.jpg"))
        # 檔案真的落在暫存區
        fetched = os.listdir(self.pps._FETCH_DIR)
        self.assertEqual(len(fetched), 2)

    def test_no_hits_returns_hint_not_marker(self):
        out = self._run("99999999", [], erp_codes=[])
        self.assertNotIn("[[TG_PHOTO:", out)
        self.assertIn("找不到", out)

    def test_real_photo_via_numeric_core_beats_lineart(self):
        # 2026-07-28 彩圖案：查型體 DJS336195，真實照檔名是「336195-8916447 …」
        # （不帶 DJS 前綴）——要靠數字核心展開才搜得到，且排序要壓過大檔線稿。
        hits = [
            {"_kw": "DJS336195", "id": "fidL1", "name": "DJS336195L1.jpg",
             "mimeType": "image/jpeg", "size": "146374"},
            {"_kw": "DJS336195", "id": "fidPT", "name": "DJS336195 pt (A005).jpg",
             "mimeType": "image/jpeg", "size": "15350"},
            {"_kw": "336195", "id": "fidREAL", "name": "336195-8916447 GREEN綠.jpg",
             "mimeType": "image/jpeg", "size": "108573"},
        ]
        out = self._run("DJS336195", hits, erp_codes=[],
                        erp_lots=["8916447", "8916446"], top_k=2)
        # 真實照第一、線稿沉底（top_k=2 → pt(A005) 15KB 不該出現）
        self.assertLess(out.index("336195-8916447 GREEN綠.jpg"),
                        out.index("DJS336195L1.jpg"))
        self.assertNotIn("pt (A005)", out)
        self.assertIn("｜照片｜", out)
        self.assertIn("｜線稿/標註圖｜", out)

    def test_input_lot_exact_hit_excludes_sibling_styles(self):
        # 2026-07-29 UserA 案：查 #8916447 被回整個型體 336195 所有 STYLE——
        # 指定款號有檔名命中時只回該款，兄弟款/線稿不得混入
        hits = [
            {"_kw": "8916447", "id": "fid47", "name": "336195-8916447 GREEN綠.jpg",
             "mimeType": "image/jpeg", "size": "108573"},
            {"_kw": "336195", "id": "fid46", "name": "336195-8916446 #N06A GREY.jpg",
             "mimeType": "image/jpeg", "size": "134218"},
            {"_kw": "336195", "id": "fid04", "name": "336195-9026204.jpg",
             "mimeType": "image/jpeg", "size": "120000"},
            {"_kw": "DJS336195", "id": "fidL1", "name": "DJS336195L1.jpg",
             "mimeType": "image/jpeg", "size": "146374"},
        ]
        out = self._run("8916447", hits, erp_codes=["DJS336195"],
                        erp_lots=["8916446", "8916447", "9026204"], top_k=3)
        self.assertIn("336195-8916447 GREEN綠.jpg", out)
        self.assertNotIn("8916446", out)
        self.assertNotIn("9026204", out)
        self.assertNotIn("DJS336195L1.jpg", out)
        self.assertEqual(out.count("[[TG_PHOTO:/"), 1)   # 只傳正主一張
        self.assertNotIn("非該款本尊", out)               # 有正主就不是備援模式

    def test_input_lot_no_exact_falls_back_to_family_with_notice(self):
        # 指定款號查無正主 → 退同型體參考，但必須明講「非該款本尊」
        hits = [
            {"_kw": "336195", "id": "fid46", "name": "336195-8916446 #N06A GREY.jpg",
             "mimeType": "image/jpeg", "size": "134218"},
            {"_kw": "DJS336195", "id": "fidL1", "name": "DJS336195L1.jpg",
             "mimeType": "image/jpeg", "size": "146374"},
        ]
        out = self._run("8916447", hits, erp_codes=["DJS336195"],
                        erp_lots=["8916446", "8916447"], top_k=3)
        self.assertIn("336195-8916446 #N06A GREY.jpg", out)
        self.assertIn("查無檔名含款號 8916447", out)
        self.assertIn("非該款本尊", out)
        # 備援仍維持真實照在前、線稿沉底
        self.assertLess(out.index("336195-8916446 #N06A GREY.jpg"),
                        out.index("DJS336195L1.jpg"))

    def test_drive_search_scoped_to_allowed_drives(self):
        # Codex P1（PR #305）：不能用 corpora=allDrives 掃全部共用硬碟
        self._run("MH100", [])
        self.assertTrue(self.fake_files.list_calls)
        allowed = self.pps._allowed_photo_drive_ids()
        for call in self.fake_files.list_calls:
            self.assertEqual(call.get("corpora"), "drive")
            self.assertIn(call.get("driveId"), allowed)

    def test_env_overrides_allowed_drives(self):
        with mock.patch.dict(os.environ,
                             {"RED_SHOE_PHOTO_DRIVE_IDS": "driveA, driveB"}):
            self.assertEqual(self.pps._allowed_photo_drive_ids(),
                             ("driveA", "driveB"))

    def test_long_filename_truncation_keeps_extension(self):
        # Codex P2（PR #305）：截長檔名不能截掉副檔名（閘門以副檔名放行）
        hits = [{"_kw": "MH100", "id": "fidlong01",
                 "name": "MH100_" + "很長的檔名" * 40 + ".jpg",
                 "mimeType": "image/jpeg", "size": "1234"}]
        out = self._run("MH100", hits, top_k=1)
        self.assertIn("[[TG_PHOTO:/", out)
        fetched = os.listdir(self.pps._FETCH_DIR)
        self.assertEqual(len(fetched), 1)
        self.assertTrue(fetched[0].endswith(".jpg"))

    def test_top_k_caps_downloads(self):
        hits = [
            {"_kw": "MH100", "id": f"fid{i:05d}", "name": f"MH100_{i}.jpg",
             "mimeType": "image/jpeg", "size": str(1000 + i)}
            for i in range(8)
        ]
        out = self._run("MH100", hits, top_k=2)
        # 只數真正的標記行（帶絕對路徑）；說明文字裡的 [[TG_PHOTO:...]] 不算
        self.assertEqual(out.count("[[TG_PHOTO:/"), 2)


if __name__ == "__main__":
    unittest.main()
