"""agent_core.telegram_format.markdown_to_telegram_html 的密封測試。

重點：沒 fence 零行為改變、有 fence 轉 <pre>、& < > 轉義、" 不可被轉成 &quot;
（Telegram HTML 不解 &quot;）、未閉合 fence 安全退回純文字。
"""
import unittest

from agent_core.telegram_format import markdown_to_telegram_html as m2h


class MarkdownToTelegramHtmlTests(unittest.TestCase):
    def test_no_fence_passthrough(self):
        text = "大王你好，**粗體**不處理，| 表格 | 也照舊。"
        self.assertEqual(m2h(text), (text, None))  # 零行為改變

    def test_fence_becomes_pre(self):
        rendered, pm = m2h("看表：\n```\nA  1\nB  2\n```\n完畢")
        self.assertEqual(pm, "HTML")
        self.assertIn("<pre>A  1\nB  2</pre>", rendered)
        self.assertTrue(rendered.startswith("看表：\n"))
        self.assertTrue(rendered.endswith("\n完畢"))

    def test_html_special_chars_escaped(self):
        rendered, pm = m2h("a < b & c > d\n```\nx<y & z>w\n```")
        self.assertEqual(pm, "HTML")
        self.assertIn("a &lt; b &amp; c &gt; d", rendered)      # fence 外也轉義
        self.assertIn("<pre>x&lt;y &amp; z&gt;w</pre>", rendered)  # fence 內轉義
        self.assertNotIn("<y", rendered.replace("<pre>", ""))   # 沒有裸 < 殘留

    def test_quotes_not_escaped(self):
        # Telegram HTML 不解 &quot;/&#x27;，務必保留字面引號
        rendered, _pm = m2h('say "hi" it\'s ok\n```\n"q" \'s\'\n```')
        self.assertNotIn("&quot;", rendered)
        self.assertNotIn("&#x27;", rendered)
        self.assertIn('"hi"', rendered)

    def test_unterminated_fence_falls_back_to_plain(self):
        text = "開了沒關\n```\nA  1\nB  2"  # 只有一個 ```
        self.assertEqual(m2h(text), (text, None))  # 不冒險送半截 <pre>

    def test_fence_with_language_tag(self):
        rendered, pm = m2h("```text\nhello\n```")
        self.assertEqual(pm, "HTML")
        self.assertEqual(rendered, "<pre>hello</pre>")

    def test_two_fences(self):
        rendered, pm = m2h("```\nA\n```\n中間\n```\nB\n```")
        self.assertEqual(pm, "HTML")
        self.assertEqual(rendered, "<pre>A</pre>\n中間\n<pre>B</pre>")

    def test_empty_and_none(self):
        self.assertEqual(m2h(""), ("", None))
        self.assertEqual(m2h(None), ("", None))


class TelegramTextChunksTests(unittest.TestCase):
    """telegram_text_chunks：UTF-16 code unit 計長 + 多段預留 [i/n]\\n 前綴空間。

    健檢 High regression：舊分段用 code point 切滿 4096，(a) 加前綴後爆上限、
    (b) emoji 佔 2 個 UTF-16 unit，4096 個 code point 的 emoji 訊息實際是 8192
    unit，Telegram 會 400 打回、整段丟失。
    """

    def _chunks(self, *a, **k):
        from agent_core.telegram_format import telegram_text_chunks
        return telegram_text_chunks(*a, **k)

    def _u16(self, s):
        from agent_core.telegram_format import utf16_len
        return utf16_len(s)

    def test_utf16_len_counts_astral_as_two(self):
        self.assertEqual(self._u16("abc"), 3)
        self.assertEqual(self._u16("中文"), 2)      # BMP：1 unit
        self.assertEqual(self._u16("😀"), 2)         # astral：2 units
        self.assertEqual(self._u16("a😀b"), 4)

    def test_short_message_single_chunk_unchanged(self):
        self.assertEqual(self._chunks("hello 大王"), ["hello 大王"])

    def test_empty_returns_empty_list(self):
        self.assertEqual(self._chunks(""), [])
        self.assertEqual(self._chunks(None), [])

    def test_exactly_at_limit_stays_single_chunk_no_reserve(self):
        # 剛好 4096 → 單段（單段不加前綴，不需預留 reserve）
        text = "A" * 4096
        self.assertEqual(self._chunks(text), [text])

    def test_over_limit_reserves_prefix_room_in_every_chunk(self):
        text = "A" * 4097
        chunks = self._chunks(text)
        self.assertGreater(len(chunks), 1)
        self.assertEqual("".join(chunks), text)  # 無損重組
        for i, c in enumerate(chunks, 1):
            prefix = f"[{i}/{len(chunks)}]\n"
            self.assertLessEqual(
                self._u16(prefix + c), 4096,
                f"chunk {i} 加前綴後爆 4096（舊 bug：切滿 4096 再加前綴）",
            )

    def test_emoji_counted_as_utf16_units(self):
        # 4096 個 emoji code point = 8192 UTF-16 units → 必須切段，
        # 舊 code-point 邏輯會當成剛好一段、被 Telegram 打回。
        text = "😀" * 4096
        chunks = self._chunks(text)
        self.assertGreater(len(chunks), 1)
        self.assertEqual("".join(chunks), text)
        for i, c in enumerate(chunks, 1):
            prefix = f"[{i}/{len(chunks)}]\n"
            self.assertLessEqual(self._u16(prefix + c), 4096)

    def test_astral_chars_never_split(self):
        # 邊界剛好落在 emoji 中間也不能切半：每段自己 re-encode UTF-16 必須
        # round-trip（半個 surrogate pair 無法編碼）。
        text = ("x" + "😀") * 3000  # 每組 3 units，邊界會落在各種位置
        chunks = self._chunks(text)
        self.assertEqual("".join(chunks), text)
        for c in chunks:
            # 若 astral 被切半，Python 字串不可能產生 lone surrogate（切片以
            # code point 為單位），這裡驗證每段可獨立編碼且長度守約。
            c.encode("utf-16-le")  # 不應 raise
            self.assertLessEqual(self._u16(c), 4096 - 16)

    def test_custom_limit_and_reserve(self):
        chunks = self._chunks("A" * 25, limit=10, reserve=2)
        self.assertEqual(["".join(chunks)], ["A" * 25])
        for c in chunks:
            self.assertLessEqual(self._u16(c), 8)


if __name__ == "__main__":
    unittest.main()
