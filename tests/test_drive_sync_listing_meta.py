"""listing metadata 重用：欄位齊全的 listing dict 直接餵 _prepare_file，
跳過每檔一發 files().get（20k 檔夜跑 = 20k 個多餘 RPC）。

契約：
  - _listing_meta_usable：id/name/mimeType/modifiedTime 必備；非 Google 原生
    檔還要有 size（缺 size 的 listing 會讓 too_large 下載閘拿到 0 而靜默失效）。
  - 齊全 → _prepare_file 用 listing meta、不打 GET。
  - 不齊 / 直呼 sync_file 沒帶 → 照舊 GET（行為不變）。
  - 序列路徑 _sync_file_with_optional_prefetch 只在齊全時把 listing_meta kwarg
    傳給 sync_file（既有測試/呼叫端的 sync_file fake 不必認得新 kwarg）。
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest import mock

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from agent_core.ingest import drive_sync  # noqa: E402


def _binary_meta(**over):
    meta = {
        "id": "fid", "name": "x.pdf", "mimeType": "application/pdf",
        "modifiedTime": "2026-05-07T09:00:00.000Z", "parents": ["p1"],
        "size": "123",
    }
    meta.update(over)
    return meta


class ListingMetaUsableTests(unittest.TestCase):
    def test_binary_with_size_is_usable(self):
        self.assertTrue(drive_sync._listing_meta_usable(_binary_meta()))

    def test_binary_without_size_is_not_usable(self):
        meta = _binary_meta()
        del meta["size"]
        self.assertFalse(drive_sync._listing_meta_usable(meta))

    def test_google_native_without_size_is_usable(self):
        # Docs/Sheets 的 API 回應永遠沒有 size — 不能因此退回 GET（GET 也不會有）。
        meta = _binary_meta(mimeType="application/vnd.google-apps.document")
        del meta["size"]
        self.assertTrue(drive_sync._listing_meta_usable(meta))

    def test_missing_required_key_is_not_usable(self):
        for key in ("id", "name", "mimeType", "modifiedTime"):
            meta = _binary_meta()
            del meta[key]
            self.assertFalse(drive_sync._listing_meta_usable(meta), key)

    def test_none_is_not_usable(self):
        self.assertFalse(drive_sync._listing_meta_usable(None))


class SyncFileListingMetaTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._patch = mock.patch.object(
            drive_sync, "_SKIP_STATE_FILE",
            os.path.join(self._tmp.name, "drive_sync_skip_state.json"),
        )
        self._patch.start()
        drive_sync._SKIP_STATE_CACHE = None

    def tearDown(self):
        drive_sync._SKIP_STATE_CACHE = None
        self._patch.stop()
        self._tmp.cleanup()

    @staticmethod
    def _store():
        store = mock.MagicMock()
        store.get_doc_metadata.return_value = {
            "synced_at": "", "folder_id": "", "drive_id": "",
            "content_hash": "", "title": "", "sync_complete": True,
        }
        return store

    @staticmethod
    def _service():
        files_obj = mock.MagicMock()
        service = mock.MagicMock()
        service.files.return_value = files_obj
        return service, files_obj

    def test_usable_listing_meta_skips_files_get(self):
        service, files_obj = self._service()
        listing_meta = _binary_meta(
            id="fid", name="x.bin", mimeType="application/x-not-supported",
            size="5",
        )
        with mock.patch.object(drive_sync, "get_store", return_value=self._store()), \
             mock.patch("agent_core.google_auth.get_service", return_value=service):
            r = drive_sync.sync_file(
                "fid", folder_id="p1",
                modified_time="2026-05-07T09:00:00.000Z",
                listing_meta=listing_meta,
            )
        # unsupported mime 的判定完全來自 listing meta；GET 從未發生。
        self.assertEqual(r["reason"], "unsupported mime_type")
        files_obj.get.assert_not_called()

    def test_incomplete_listing_meta_falls_back_to_get(self):
        service, files_obj = self._service()
        files_obj.get.return_value.execute.return_value = {
            "id": "fid", "name": "x.bin",
            "mimeType": "application/x-not-supported", "parents": ["p1"],
            "modifiedTime": "2026-05-07T09:00:00.000Z",
        }
        incomplete = {"id": "fid", "name": "x.bin",
                      "mimeType": "application/x-not-supported",
                      "modifiedTime": "2026-05-07T09:00:00.000Z"}  # 缺 size
        with mock.patch.object(drive_sync, "get_store", return_value=self._store()), \
             mock.patch("agent_core.google_auth.get_service", return_value=service):
            r = drive_sync.sync_file(
                "fid", folder_id="p1",
                modified_time="2026-05-07T09:00:00.000Z",
                listing_meta=incomplete,
            )
        self.assertEqual(r["reason"], "unsupported mime_type")
        files_obj.get.assert_called_once()

    def test_direct_call_without_listing_meta_keeps_get(self):
        service, files_obj = self._service()
        files_obj.get.return_value.execute.return_value = {
            "id": "fid", "name": "x.bin",
            "mimeType": "application/x-not-supported", "parents": ["p1"],
            "modifiedTime": "2026-05-07T09:00:00.000Z",
        }
        with mock.patch.object(drive_sync, "get_store", return_value=self._store()), \
             mock.patch("agent_core.google_auth.get_service", return_value=service):
            drive_sync.sync_file("fid", folder_id="p1")
        files_obj.get.assert_called_once()

    def test_too_large_gate_uses_listing_size(self):
        """listing 的 size 要真的餵進 too_large 閘 — 不是只省 GET。"""
        service, files_obj = self._service()
        listing_meta = _binary_meta(size="99")
        with mock.patch.object(drive_sync, "get_store", return_value=self._store()), \
             mock.patch("agent_core.google_auth.get_service", return_value=service), \
             mock.patch.object(drive_sync, "_DRIVE_MAX_DOWNLOAD_BYTES", 10), \
             mock.patch.object(drive_sync, "_export_file_text_for_sync") as export_mock:
            r = drive_sync.sync_file(
                "fid", folder_id="p1",
                modified_time="2026-05-07T09:00:00.000Z",
                listing_meta=listing_meta,
            )
        self.assertIn("too_large", r["reason"])
        files_obj.get.assert_not_called()
        export_mock.assert_not_called()


class SerialPathThreadsListingMetaTests(unittest.TestCase):
    """_process_file_list 序列路徑：listing dict 齊全才傳 listing_meta kwarg。"""

    def _run(self, files):
        captured: list[dict] = []

        def fake_sync_file(file_id, **kw):
            captured.append(kw)
            return {"file_id": file_id, "chunks": 1}

        with mock.patch.object(drive_sync, "get_store", return_value=mock.MagicMock()), \
             mock.patch.object(drive_sync, "_defer_skip_state_writes"), \
             mock.patch.object(drive_sync, "sync_file", side_effect=fake_sync_file):
            drive_sync._process_file_list(
                files, folder_id_fn=lambda f: "F",
                prefetched={}, used_prefetch=False, fetch_workers=1,
            )
        return captured

    def test_complete_listing_dict_is_forwarded(self):
        f = _binary_meta(id="fa", name="A.pdf")
        captured = self._run([f])
        self.assertEqual(captured[0].get("listing_meta"), f)

    def test_incomplete_listing_dict_is_not_forwarded(self):
        f = {"id": "fb", "name": "B.pdf", "mimeType": "application/pdf",
             "modifiedTime": "t"}  # 缺 size → 舊 sync_file fake 契約不變
        captured = self._run([f])
        self.assertNotIn("listing_meta", captured[0])


class WorkerPathThreadsListingMetaTests(unittest.TestCase):
    def test_safe_prepare_file_forwards_listing_dict(self):
        f = _binary_meta(id="fa", name="A.pdf")
        with mock.patch.object(drive_sync, "_prepare_file") as prep:
            prep.return_value = drive_sync._FilePlan("fa", {"file_id": "fa"})
            drive_sync._safe_prepare_file(
                f, folder_id="F", drive_id="", prefetched={}, used_prefetch=False,
                service=mock.MagicMock(), store=mock.MagicMock(),
            )
        self.assertIs(prep.call_args.kwargs.get("listing_meta"), f)


if __name__ == "__main__":
    unittest.main()
