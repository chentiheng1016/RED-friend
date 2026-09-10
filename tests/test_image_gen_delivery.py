"""image_gen 交付面：Telegram 對話 context → [[TG_PHOTO:]]；其餘維持推送。

2026-09-02 green 聊天室 5004 案的同型修正（sample_order 見
test_sample_order_render.GenerateFromOrderDeliveryTests）：大王在任一色 bot
對話生圖時，圖要跟著回覆送回當前對話；REPL / 背景 daemon 沒有消費標記的
回覆送出點，維持 telegram_send_photo 出站推送。

注意（unittest discover）：conftest fixture 不生效，隔離全在 setUp/tearDown；
mock.patch 一律在主執行緒；路徑一律指 tmp，不碰部署本體 var/。
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO_ROOT)


class DeliverToTelegramTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.gen = os.path.join(self.tmp.name, "generated_images")
        os.makedirs(self.gen, exist_ok=True)
        self._p = mock.patch(
            "agent_core.logging_and_paths.GENERATED_IMAGES_DIR", self.gen)
        self._p.start()
        self.addCleanup(self._p.stop)
        self.addCleanup(self.tmp.cleanup)
        self.img = os.path.join(self.gen, "concept_1.png")
        with open(self.img, "wb") as f:
            f.write(b"\x89PNG fake")

    def test_no_channel_context_pushes(self):
        from skills.image_gen import _deliver_to_telegram
        with mock.patch("agent_core.telegram.telegram_send_photo") as send:
            delivery = _deliver_to_telegram([self.img], "cap")
        self.assertEqual(delivery, "")
        send.assert_called_once()

    def test_telegram_context_returns_marker_without_push(self):
        from agent_core.channel_context import channel_context
        from skills.image_gen import _deliver_to_telegram
        with mock.patch("agent_core.telegram.telegram_send_photo") as send, \
                channel_context("telegram"):
            delivery = _deliver_to_telegram([self.img], "cap")
        self.assertIn(f"[[TG_PHOTO:{self.img}]]", delivery)
        self.assertIn("原樣保留", delivery)
        send.assert_not_called()

    def test_out_of_root_path_still_pushes_in_telegram_context(self):
        """自訂 output_path 落在 generated_images 外 → 標記會被 daemon 白名單
        靜默丟棄，必須退回推送，圖才不會誰都收不到。"""
        outside = os.path.join(self.tmp.name, "elsewhere.png")
        with open(outside, "wb") as f:
            f.write(b"\x89PNG fake")
        from agent_core.channel_context import channel_context
        from skills.image_gen import _deliver_to_telegram
        with mock.patch("agent_core.telegram.telegram_send_photo") as send, \
                channel_context("telegram"):
            delivery = _deliver_to_telegram([self.img, outside], "cap")
        self.assertIn(f"[[TG_PHOTO:{self.img}]]", delivery)
        self.assertNotIn(outside, delivery)
        send.assert_called_once()
        self.assertEqual(send.call_args.args[0], outside)

    def test_push_failure_swallowed(self):
        from skills.image_gen import _deliver_to_telegram
        with mock.patch("agent_core.telegram.telegram_send_photo",
                        side_effect=RuntimeError("net down")):
            delivery = _deliver_to_telegram(self.img, "cap")
        self.assertEqual(delivery, "")


class GenerateProductConceptDeliveryTests(unittest.TestCase):
    """呼叫端要把 _deliver_to_telegram 回的標記文字接到工具回覆末尾。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.gen = os.path.join(self.tmp.name, "generated_images")
        os.makedirs(self.gen, exist_ok=True)
        self._p = mock.patch(
            "agent_core.logging_and_paths.GENERATED_IMAGES_DIR", self.gen)
        self._p.start()
        self.addCleanup(self._p.stop)
        self.addCleanup(self.tmp.cleanup)

    def _fake_image_response(self):
        import types as _t
        part = _t.SimpleNamespace(inline_data=_t.SimpleNamespace(data=b"\x89PNG fake"),
                                  text=None)
        cand = _t.SimpleNamespace(content=_t.SimpleNamespace(parts=[part]))
        return _t.SimpleNamespace(candidates=[cand])

    def test_marker_appended_in_telegram_context(self):
        from agent_core.channel_context import channel_context
        from skills import image_gen
        with mock.patch("agent_core.gemini_client.generate_content_tracked",
                        return_value=self._fake_image_response()), \
                mock.patch("agent_core.telegram.telegram_send_photo") as send, \
                channel_context("telegram"):
            out = image_gen.generate_product_concept("黑色工作靴")
        self.assertIn("[[TG_PHOTO:", out)
        send.assert_not_called()

    def test_marker_absent_outside_telegram_context(self):
        from skills import image_gen
        with mock.patch("agent_core.gemini_client.generate_content_tracked",
                        return_value=self._fake_image_response()), \
                mock.patch("agent_core.telegram.telegram_send_photo") as send:
            out = image_gen.generate_product_concept("黑色工作靴")
        self.assertNotIn("[[TG_PHOTO:", out)
        send.assert_called_once()


if __name__ == "__main__":
    unittest.main()
