"""Drive 的 mimeType 會說謊 —— 標成 media 的文字檔別上傳給 Gemini。

2026-08-15 實例（是今晚 red-status 那條「Gemini 錯誤率 24%」追出來的）：

    zh_TW.ini / zh_CN.ini   mimeType=audio/mpeg   size≈5.4KB   ext=ini

兩個 UTF-16 LE 的 INI 文字檔，在 Drive 上被標成 `audio/mpeg`，於是每晚被當音訊
上傳給 Gemini、每晚回一次 `400 INVALID_ARGUMENT`，兩個檔從來沒進過索引。誤判的
成因就寫在 bytes 裡：UTF-16 LE 的 BOM 是 `\xff\xfe`，跟 MP3 的 frame sync
`\xff\xfb` 只差**一個 byte**。

守的兩個不變量：
  1. bytes 不像它自稱的容器 → 不要送 Gemini，改走文字路徑（省一次必失敗的呼叫，
     而且檔案真的能被索引）
  2. **真的 media 一個都不能被誤判** —— 誤判的代價是丟掉整支影片的轉錄內容，
     比多傳一次貴得多。所以判準保守：只有確定不符才改路。

第三條（`_decode_bom_text`）：改路之後還得抽得出東西。UTF-16 走 strings 抽取器
只會得到碎片（實測 5,364 bytes → 171 字亂碼），**亂碼進索引比抽不到更糟**。
"""
from __future__ import annotations

import os
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

os.environ.setdefault("AGENT_DAEMON_MODE", "1")

from agent_core.ingest import drive_sync as ds

# 真檔開頭（UTF-16 LE BOM + "[Language String]"）
_REAL_INI_HEAD = b"\xff\xfe[\x00L\x00a\x00n\x00g\x00u\x00a\x00g\x00e\x00"


class MediaBytesMismatchTests(unittest.TestCase):
    def test_the_real_mislabelled_ini_is_detected(self):
        self.assertTrue(ds._media_bytes_mismatch(_REAL_INI_HEAD + b"\x00" * 64,
                                                 "audio/mpeg"))

    def test_genuine_media_signatures_are_never_flagged(self):
        """誤判真 media 的代價 = 丟掉整支影片的轉錄，比多傳一次貴得多。"""
        for label, blob, mime in (
            ("mp3 ID3", b"ID3\x04\x00\x00\x00\x00\x00\x00" + b"x" * 32, "audio/mpeg"),
            ("mp3 sync", b"\xff\xfb\x90\x00" * 8, "audio/mpeg"),
            ("mp3 sync fa", b"\xff\xfa\x90\x00" * 8, "audio/mp3"),
            ("wav", b"RIFF\x00\x00\x00\x00WAVE" + b"x" * 16, "audio/wav"),
            ("avi", b"RIFF\x00\x00\x00\x00AVI " + b"x" * 16, "video/x-msvideo"),
            ("mp4", b"\x00\x00\x00\x20ftypisom" + b"x" * 32, "video/mp4"),
            ("m4a", b"\x00\x00\x00\x20ftypM4A " + b"x" * 32, "audio/x-m4a"),
            ("mov moov", b"\x00\x00\x00\x08moov" + b"x" * 32, "video/quicktime"),
            ("mpeg-ts", b"\x47" + b"x" * 32, "video/mp2t"),
        ):
            self.assertFalse(ds._media_bytes_mismatch(blob, mime), label)

    def test_unknown_or_unjudgeable_input_keeps_the_old_route(self):
        """無從判斷時一律不改路 —— 寧可白傳一次。"""
        self.assertFalse(ds._media_bytes_mismatch(b"", "audio/mpeg"))
        self.assertFalse(ds._media_bytes_mismatch(b"\xff", "audio/mpeg"))
        self.assertFalse(ds._media_bytes_mismatch(b"short", "video/mp4"))
        # magic 沒收錄的 mime → 不表態
        self.assertFalse(ds._media_bytes_mismatch(b"whatever" * 8, "audio/ogg"))

    def test_every_media_mime_either_has_magic_or_is_explicitly_unjudged(self):
        """新增 _MEDIA_MIMES 時提醒補 magic —— 沒補就是靜默退回舊行為。"""
        known = (set(ds._MEDIA_MAGIC) | set(ds._MEDIA_MAGIC_AT_4)
                 | set(ds._MPEG_AUDIO_MIMES))
        missing = set(ds._MEDIA_MIMES) - known
        self.assertEqual(missing, set(), f"這些 media mime 還沒有 magic 判準：{missing}")


class MpegAudioFrameSyncTests(unittest.TestCase):
    """MPEG audio 的合法 header 是 11 bits sync，不是可列舉的前綴。

    byte1 = AAA BB CC D：AAA=sync(111)、BB=version(00=2.5/10=MPEG2/11=MPEG1)、
    CC=layer(01=III/10=II/11=I)。原本只列 Layer III 的 FB/F3/F2/FA，其餘合法
    組合都被誤判成「mime 說謊」→ 丟去文字抽取 → 轉錄整個掉掉。
    """

    def test_non_layer3_mpeg_audio_is_not_flagged(self):
        for label, byte1 in (
            ("MPEG2.5 LayerIII（低位元率語音：錄音筆／通訊軟體語音）", 0xE3),
            ("MPEG1 LayerII", 0xFD),
            ("MPEG2 LayerII", 0xF4),
            ("MPEG1 LayerI（不與 BOM 衝突的那個 header）", 0xFF),
            ("MPEG2.5 LayerIII 無 CRC", 0xE2),
        ):
            blob = bytes([0xFF, byte1, 0x90, 0x00]) * 8
            self.assertFalse(ds._media_bytes_mismatch(blob, "audio/mpeg"),
                             f"{label}（byte1={byte1:#04x}）被誤判成文字")

    def test_bom_beats_frame_sync(self):
        """關鍵順序：UTF-16 LE 的 BOM（FF FE）本身就是合法的 MPEG1 LayerI
        sync，只看 sync 會把真實那支誤標 INI 放行、400 照吃。BOM 必須先判。"""
        self.assertTrue(ds._starts_with_text_bom(_REAL_INI_HEAD))
        # 只看 sync 的話這支會過（0xFE 的高 3 bits 全 1）
        self.assertEqual(_REAL_INI_HEAD[0], 0xFF)
        self.assertEqual(_REAL_INI_HEAD[1] & 0xE0, 0xE0)
        # 但 BOM 先判，所以照樣擋下
        self.assertTrue(ds._media_bytes_mismatch(_REAL_INI_HEAD + b"\x00" * 64,
                                                 "audio/mpeg"))

    def test_ff_fe_collision_resolves_to_bom_deliberately(self):
        """`FF FE` 同時是 UTF-16 LE BOM 與合法 MPEG1 LayerI sync，光看檔頭
        無法分辨、只能擇一。**刻意選 BOM**：MPEG1 LayerI（.mp1）現實中幾近
        絕跡，而誤標成 audio/mpeg 的 UTF-16 文字檔這個語料庫現在就有兩支。

        代價寫在這裡當文件：真的 `FF FE` 開頭 MP1 會被判成文字。哪天真的撞到，
        改法是往後多看幾個 byte（UTF-16 LE 的 ASCII 內容每隔一 byte 是 NUL），
        不是把 BOM 那層拿掉。"""
        mp1_head = b"\xff\xfe\x90\x00" * 8
        self.assertTrue(ds._media_bytes_mismatch(mp1_head, "audio/mpeg"))

    def test_other_boms_also_beat_frame_sync(self):
        for bom in (b"\xef\xbb\xbf", b"\xfe\xff", b"\xff\xfe\x00\x00",
                    b"\x00\x00\xfe\xff"):
            self.assertTrue(
                ds._media_bytes_mismatch(bom + b"key=value" * 8, "audio/mpeg"),
                f"BOM {bom.hex()} 沒被擋下")

    def test_genuinely_non_audio_still_flagged(self):
        """放寬 sync 判準不能把「真的不是音訊」也放行。"""
        for blob in (b"<html><body>not audio</body></html>",
                     b"PK\x03\x04" + b"x" * 32,          # zip
                     b"%PDF-1.7\n" + b"x" * 32):
            self.assertTrue(ds._media_bytes_mismatch(blob, "audio/mpeg"),
                            f"{blob[:8]!r} 該被擋下")


class BomTextDecodingTests(unittest.TestCase):
    def test_utf16_le_is_decoded_not_strings_scraped(self):
        """回歸：修前 5,364 bytes 只抽出 171 字亂碼（'Bf@S\\n2QX['…）。"""
        payload = "[Language String]\r\nFront Video=前端鏡頭\r\n"
        data = b"\xff\xfe" + payload.encode("utf-16-le")
        out = ds._extract_textlike_binary(data)
        self.assertIn("前端鏡頭", out)
        self.assertIn("[Language String]", out)
        self.assertNotIn("\x00", out)

    def test_utf16_be_and_utf8_bom(self):
        self.assertIn("測試", ds._decode_bom_text(b"\xfe\xff" + "測試".encode("utf-16-be")))
        self.assertIn("測試", ds._decode_bom_text(b"\xef\xbb\xbf" + "測試".encode("utf-8")))

    def test_utf32_le_not_mistaken_for_utf16(self):
        """`\\xff\\xfe\\x00\\x00` 同時是 utf-32-le BOM 與 utf-16-le BOM 的前綴。"""
        self.assertIn("A", ds._decode_bom_text(b"\xff\xfe\x00\x00" + "A".encode("utf-32-le")))

    def test_no_bom_returns_empty_so_old_paths_still_run(self):
        self.assertEqual(ds._decode_bom_text(b"plain ascii text"), "")
        self.assertEqual(ds._decode_bom_text(b""), "")

    def test_plain_text_without_bom_is_unaffected(self):
        out = ds._extract_textlike_binary(b"key=value\nother=thing\n")
        self.assertIn("key=value", out)


if __name__ == "__main__":
    unittest.main()
