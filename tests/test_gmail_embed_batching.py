"""gmail_sync.sync_query batches embeds across threads.

Syncing threads one-by-one spends ~1 Gemini embed request per 1-3 chunks,
saturating the embedding RPM cap. sync_query now buffers whole threads and
flushes one combined upsert (which embeds the whole batch in one request),
finalizing each thread's delete_stale individually. These tests pin that
behavior: fewer/bigger embed calls, correct skip/empty handling, per-thread
finalize, and error isolation.
"""
from __future__ import annotations

import os
import sys
import unittest
from unittest import mock


_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

_MB = "boss@example.com"


def _service_for(threads):
    list_req = mock.MagicMock()
    list_req.execute.return_value = {"threads": threads}
    threads_obj = mock.MagicMock()
    threads_obj.list.return_value = list_req
    profile_req = mock.MagicMock()
    profile_req.execute.return_value = {"emailAddress": _MB}
    users = mock.MagicMock()
    users.threads.return_value = threads_obj
    users.getProfile.return_value = profile_req
    svc = mock.MagicMock()
    svc.users.return_value = users
    return svc


def _text_for(_service, thread_id):
    # _thread_text(service, thread_id) — first positional is the service.
    # one short chunk per thread, subject tags it
    return (f"body of {thread_id}", {
        "subject": f"S-{thread_id}", "sender": "a@x.com",
        "date": "today", "history_id": "h-" + thread_id,
    })


class GmailEmbedBatchingTests(unittest.TestCase):
    def _store(self):
        store = mock.MagicMock()
        store.bulk_get_doc_metadata.return_value = {}  # nothing prefetched → no skips
        return store

    def test_many_threads_collapse_into_one_embed_batch(self):
        from agent_core.ingest import gmail_sync
        svc = _service_for([{"id": f"t{i}", "historyId": f"h{i}"} for i in range(3)])
        store = self._store()
        with mock.patch.object(gmail_sync, "get_store", return_value=store), \
             mock.patch.object(gmail_sync, "_thread_text", side_effect=_text_for):
            res = gmail_sync.sync_query("q", max_threads=3, service=svc, mailbox_email=_MB)

        self.assertEqual(res["synced"], 3)
        # 3 threads → ONE upsert (one embed request), not three
        self.assertEqual(store.upsert_batch.call_count, 1)
        ids, docs, metas = store.upsert_batch.call_args.args[:3]
        self.assertEqual(len(metas), 3)  # one chunk per thread, all batched
        self.assertEqual({m["doc_id"] for m in metas}, {"t0", "t1", "t2"})
        # delete_stale still runs once per thread
        self.assertEqual(store.delete_stale_chunks.call_count, 3)

    def test_flush_triggers_at_threshold(self):
        from agent_core.ingest import gmail_sync
        svc = _service_for([{"id": f"t{i}", "historyId": f"h{i}"} for i in range(3)])
        store = self._store()
        # threshold of 2 chunks → flush after t0+t1, then remainder flush for t2
        with mock.patch.object(gmail_sync, "_EMBED_FLUSH_CHUNKS", 2), \
             mock.patch.object(gmail_sync, "get_store", return_value=store), \
             mock.patch.object(gmail_sync, "_thread_text", side_effect=_text_for):
            res = gmail_sync.sync_query("q", max_threads=3, service=svc, mailbox_email=_MB)

        self.assertEqual(res["synced"], 3)
        self.assertEqual(store.upsert_batch.call_count, 2)

    def test_skipped_and_empty_threads_not_embedded(self):
        from agent_core.ingest import gmail_sync
        from agent_core.rag_gateway import metadata_access_fields
        svc = _service_for([
            {"id": "skip1", "historyId": "h1"},
            {"id": "empty1", "historyId": "h2"},
            {"id": "good1", "historyId": "h3"},
        ])
        store = self._store()
        # prefetch makes skip1 a history-id match → unchanged skip
        af = metadata_access_fields("gmail", mailbox_email=_MB)
        store.bulk_get_doc_metadata.return_value = {
            "skip1": {"history_id": "h1", **af},
        }

        def _text(_svc, tid):
            if tid == "empty1":
                return ("", {})  # empty → delete + skip, never embedded
            return _text_for(_svc, tid)

        with mock.patch.object(gmail_sync, "get_store", return_value=store), \
             mock.patch.object(gmail_sync, "_thread_text", side_effect=_text):
            res = gmail_sync.sync_query("q", max_threads=3, service=svc, mailbox_email=_MB)

        self.assertEqual(res["synced"], 1)
        self.assertEqual(res["skipped"], 2)
        self.assertEqual(store.upsert_batch.call_count, 1)
        metas = store.upsert_batch.call_args.args[2]
        self.assertEqual([m["doc_id"] for m in metas], ["good1"])  # only the live one
        store.delete_by_doc_id.assert_called_once_with("empty1")

    def test_flush_failure_marks_only_buffered_threads(self):
        from agent_core.ingest import gmail_sync
        svc = _service_for([{"id": f"t{i}", "historyId": f"h{i}"} for i in range(2)])
        store = self._store()
        store.upsert_batch.side_effect = RuntimeError("chroma down")
        with mock.patch.object(gmail_sync, "get_store", return_value=store), \
             mock.patch.object(gmail_sync, "_thread_text", side_effect=_text_for):
            res = gmail_sync.sync_query("q", max_threads=2, service=svc, mailbox_email=_MB)

        self.assertEqual(res["synced"], 0)
        self.assertEqual(res["skipped"], 2)  # both buffered threads → error

    def test_delete_stale_failure_keeps_thread_synced(self):
        # A delete_stale_chunks failure must NOT flip an already-upserted
        # thread to error (would force a needless re-embed) nor abort the rest.
        from agent_core.ingest import gmail_sync
        svc = _service_for([{"id": f"t{i}", "historyId": f"h{i}"} for i in range(3)])
        store = self._store()
        store.delete_stale_chunks.side_effect = RuntimeError("stale delete boom")
        with mock.patch.object(gmail_sync, "get_store", return_value=store), \
             mock.patch.object(gmail_sync, "_thread_text", side_effect=_text_for):
            res = gmail_sync.sync_query("q", max_threads=3, service=svc, mailbox_email=_MB)

        self.assertEqual(res["synced"], 3)   # upsert landed → all synced
        self.assertEqual(res["skipped"], 0)
        self.assertEqual(store.upsert_batch.call_count, 1)
        self.assertEqual(store.delete_stale_chunks.call_count, 3)  # attempted each


class GmailParallelFetchTests(unittest.TestCase):
    def _store(self):
        store = mock.MagicMock()
        store.bulk_get_doc_metadata.return_value = {}
        return store

    def test_parallel_fetch_same_result_and_uses_worker_services(self):
        from agent_core.ingest import gmail_sync
        svc = _service_for([{"id": f"t{i}", "historyId": f"h{i}"} for i in range(6)])
        store = self._store()
        builder = mock.MagicMock(side_effect=lambda: mock.MagicMock(name="worker_svc"))

        with mock.patch.object(gmail_sync, "get_store", return_value=store), \
             mock.patch.object(gmail_sync, "_thread_text", side_effect=_text_for):
            res = gmail_sync.sync_query(
                "q", max_threads=6, service=svc, mailbox_email=_MB,
                service_builder=builder, fetch_workers=4,
            )

        self.assertEqual(res["synced"], 6)
        self.assertEqual(res["skipped"], 0)
        # workers each built their own service (transport not thread-safe)
        self.assertGreaterEqual(builder.call_count, 1)
        self.assertLessEqual(builder.call_count, 4)
        # embed still batched on the main thread, finalize per thread
        self.assertGreaterEqual(store.upsert_batch.call_count, 1)
        self.assertEqual(store.delete_stale_chunks.call_count, 6)

    def test_parallel_one_fetch_error_isolated(self):
        from agent_core.ingest import gmail_sync
        svc = _service_for([{"id": f"t{i}", "historyId": f"h{i}"} for i in range(5)])
        store = self._store()

        def _text(_service, tid):
            if tid == "t2":
                raise RuntimeError("gmail fetch boom")
            return _text_for(_service, tid)

        with mock.patch.object(gmail_sync, "get_store", return_value=store), \
             mock.patch.object(gmail_sync, "_thread_text", side_effect=_text):
            res = gmail_sync.sync_query(
                "q", max_threads=5, service=svc, mailbox_email=_MB,
                service_builder=lambda: mock.MagicMock(), fetch_workers=3,
            )

        self.assertEqual(res["synced"], 4)   # the other four still sync
        self.assertEqual(res["skipped"], 1)  # t2 isolated as error

    def test_builder_failure_does_not_crash_run(self):
        from agent_core.ingest import gmail_sync
        svc = _service_for([{"id": "t0", "historyId": "h0"}])
        store = self._store()

        def _bad_builder():
            raise RuntimeError("bad service account file")

        with mock.patch.object(gmail_sync, "get_store", return_value=store), \
             mock.patch.object(gmail_sync, "_thread_text", side_effect=_text_for):
            res = gmail_sync.sync_query(
                "q", max_threads=1, service=svc, mailbox_email=_MB,
                service_builder=_bad_builder, fetch_workers=2,
            )

        self.assertEqual(res["synced"], 0)
        self.assertEqual(res["skipped"], 1)  # builder failure → thread errored, no crash


if __name__ == "__main__":
    unittest.main()
