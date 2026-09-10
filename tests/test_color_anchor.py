"""color_anchor：Pantone 查表、文字色解析（含客戶色彙）、色票條、ΔE 驗證。

2026-09-01 大王三個要求的地基：材質可模擬、Pantone 要正確、文字敘述顏色要準。
顏色錨定的解析順序（resolve_color docstring）：Pantone 色號 > 客戶色彙 > 內建色名，
都沒有才輪到呼叫端的 LLM 推估——測試釘死這個優先序，避免之後誰改了讓色又開始漂。

注意（unittest discover）：conftest fixture 不生效，隔離全在 setUp/tearDown。
"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO_ROOT)


class ResolveColorTests(unittest.TestCase):
    """解析優先序：Pantone > 客戶色彙 > 內建色名 > None。"""

    def setUp(self):
        # 客戶色彙表指到 tmp，別讀到（未來會存在的）live 檔
        self.tmp = tempfile.TemporaryDirectory()
        self._p = mock.patch("agent_core.logging_and_paths.STATE_DIR", self.tmp.name)
        self._p.start()

    def tearDown(self):
        self._p.stop()
        self.tmp.cleanup()

    def _write_glossary(self, data):
        with open(os.path.join(self.tmp.name, "customer_colors.json"), "w",
                  encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)

    def test_pantone_code_hits_table(self):
        from agent_core import color_anchor as ca
        got = ca.resolve_color("PANTONE 19-4052 TCX")
        self.assertEqual(got["hex"], "0f4c81")          # classic blue 公認近似值
        self.assertEqual(got["source"], "pantone")

    def test_tpg_suffix_same_number_same_anchor(self):
        from agent_core import color_anchor as ca
        self.assertEqual(ca.resolve_color("18-3838 TPG")["hex"],
                         ca.resolve_color("18-3838 TCX")["hex"])

    def test_pantone_beats_color_word_in_same_text(self):
        from agent_core import color_anchor as ca
        got = ca.resolve_color("深藍 PANTONE 19-4052 TCX")
        self.assertEqual(got["source"], "pantone")

    # ── PMS（印刷系）色號 ──────────────────────────────────────────
    def test_pms_coated_code_with_and_without_space(self):
        from agent_core import color_anchor as ca
        for text in ("PANTONE 286C", "pantone 286 C", "286C"):
            got = ca.resolve_color(text)
            self.assertEqual((got["hex"], got["source"]), ("0033a0", "pantone"), text)
        self.assertEqual(ca.resolve_color("286C")["label"], "Pantone 286 C")

    def test_pms_uncoated_differs_from_coated(self):
        from agent_core import color_anchor as ca
        self.assertEqual(ca.resolve_color("PANTONE 2925 U")["hex"], "4097db")
        self.assertEqual(ca.resolve_color("PANTONE 2925 C")["hex"], "009cde")

    def test_pms_prefixed_without_suffix_defaults_to_coated(self):
        from agent_core import color_anchor as ca
        self.assertEqual(ca.resolve_color("PMS 485")["hex"], "da291c")

    def test_pms_bare_number_without_suffix_not_matched(self):
        from agent_core import color_anchor as ca
        self.assertIsNone(ca.resolve_color("貨號 485"))       # 沒後綴沒 PANTONE 字樣＝不是色號
        self.assertIsNone(ca.resolve_color("105cm 鞋帶"))     # cm 不可誤當 coated

    def test_pms_named_color_requires_pantone_context(self):
        from agent_core import color_anchor as ca
        got = ca.resolve_color("PANTONE Cool Gray 9C")
        self.assertEqual((got["hex"], got["label"]), ("75787b", "Pantone Cool Gray 9 C"))
        # 「black 2」要贏過「black」
        self.assertEqual(ca.resolve_color("PANTONE Black 2 C")["hex"], "332f21")
        # 沒點名 pantone/pms：常見色詞落到內建色名表，不吃 PMS 命名色
        self.assertEqual(ca.resolve_color("black leather")["source"], "builtin")

    def test_pms_2016_series_from_supplement(self):
        from agent_core import color_anchor as ca
        got = ca.resolve_color("PANTONE 2172 C")             # 主來源沒有、2024 補缺才有
        self.assertEqual((got["hex"], got["source"]), ("007ed1", "pantone"))
        # 補缺只有 coated：指名 U 退 coated 錨定、label 照實講用了 C
        self.assertEqual(ca.resolve_color("PANTONE 2172 U")["label"], "Pantone 2172 C")

    def test_tcx_wins_over_pms_when_both_present(self):
        from agent_core import color_anchor as ca
        got = ca.resolve_color("PANTONE 19-4052 TCX / PANTONE 286C")
        self.assertEqual(got["hex"], "0f4c81")               # TCX 表先查

    def test_longest_zh_name_wins(self):
        from agent_core import color_anchor as ca
        self.assertEqual(ca.resolve_color("深寶藍")["hex"], "27408b")   # 不是「寶藍」「藍」
        self.assertEqual(ca.resolve_color("藍")["hex"], "2456b0")

    def test_ascii_needs_word_boundary(self):
        from agent_core import color_anchor as ca
        self.assertIsNone(ca.resolve_color("bordered"))                 # 不可命中 red
        self.assertEqual(ca.resolve_color("dark red trim")["hex"], "8b1a1a")

    def test_customer_glossary_beats_builtin(self):
        from agent_core import color_anchor as ca
        self._write_glossary({"richter": {"nude": "e3bc9a"}})
        got = ca.resolve_color("nude", customer="Richter")
        self.assertEqual((got["hex"], got["source"]), ("e3bc9a", "glossary"))
        # 其他客戶（或沒認出客戶）不吃 richter 的定義 → 落到內建表
        self.assertEqual(ca.resolve_color("nude")["source"], "builtin")

    def test_glossary_star_scope_applies_to_all(self):
        from agent_core import color_anchor as ca
        self._write_glossary({"*": {"廠標綠": "0a8f4d"}})
        self.assertEqual(ca.resolve_color("廠標綠", customer="lurchi")["hex"], "0a8f4d")

    def test_bare_suffixed_code_loses_to_any_color_word(self):
        # 2026-09-08 review：3~5 位數＋C/U 也可能是貨號/尺寸——「黑色 貨號 485C」
        # 曾被解成 Pantone 485 C 大紅還掛 pantone source 進色票+ΔE。
        from agent_core import color_anchor as ca
        got = ca.resolve_color("黑色 貨號 485C")
        self.assertEqual((got["hex"], got["source"]), ("1a1a1a", "builtin"))
        self.assertEqual(ca.resolve_color("鞋帶 120U 白色")["source"], "builtin")
        # 全欄只有裸碼、無任何色詞 → 仍認 PMS（#468 原設計不變）
        self.assertEqual(ca.resolve_color("286C")["hex"], "0033a0")

    def test_glossary_beats_pms_named_but_not_explicit_code(self):
        # 客戶色彙要能更正 green/black 這類 PMS 弱命名色；但客人明寫數字色號
        # 時照碼辦事（色號最權威）。
        from agent_core import color_anchor as ca
        self._write_glossary({"lurchi": {"green": "0a8f4d"}})
        got = ca.resolve_color("Lurchi green (PMS TBC)", customer="Lurchi")
        self.assertEqual((got["hex"], got["source"]), ("0a8f4d", "glossary"))
        self.assertEqual(
            ca.resolve_color("green PANTONE 286C", customer="Lurchi")["hex"], "0033a0")

    def test_multiword_named_with_suffix_needs_no_context(self):
        # 2026-09-08 review：規格欄常只寫「REFLEX BLUE C」（無 PANTONE 字樣）——
        # 多字 base＋明確後綴夠獨特，不該落到內建通用藍（ΔE 34.7 的錯錨）。
        from agent_core import color_anchor as ca
        got = ca.resolve_color("REFLEX BLUE C")
        self.assertEqual(got["source"], "pantone")
        self.assertEqual(got["hex"], ca._pms_table()["reflex-blue-c"])
        # 單字 base 無語境仍不認（black/green 是常用詞，#468 原則不變）
        self.assertEqual(ca.resolve_color("black leather")["source"], "builtin")

    def test_unknown_returns_none_and_parse_hex_guards_llm(self):
        from agent_core import color_anchor as ca
        self.assertIsNone(ca.resolve_color("看起來很高級的那種顏色"))
        self.assertEqual(ca.parse_hex("#0F4C81"), "0f4c81")
        self.assertEqual(ca.parse_hex("0f4c81"), "0f4c81")
        self.assertEqual(ca.parse_hex("藍色"), "")
        self.assertEqual(ca.parse_hex("#0f4c8"), "")


class ColorMathTests(unittest.TestCase):
    def test_lab_sanity(self):
        from agent_core import color_anchor as ca
        ell, a, b = ca.rgb_to_lab((255, 255, 255))
        self.assertAlmostEqual(ell, 100.0, delta=0.5)
        self.assertAlmostEqual(a, 0.0, delta=1.0)
        self.assertAlmostEqual(b, 0.0, delta=1.0)
        self.assertGreater(ca.delta_e(ca.rgb_to_lab((0, 0, 0)),
                                      ca.rgb_to_lab((255, 255, 255))), 90)
        lab = ca.rgb_to_lab((32, 80, 160))
        self.assertEqual(ca.delta_e(lab, lab), 0.0)


class VerifyColorsTests(unittest.TestCase):
    """成品照 ΔE 閘：對的色放行、明顯錯的色抓出來、讀不了圖不擋件。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        from PIL import Image, ImageDraw
        im = Image.new("RGB", (200, 200), (255, 255, 255))   # 純白背景
        ImageDraw.Draw(im).rectangle((40, 60, 160, 150), fill=(210, 43, 43))  # 紅鞋身
        self.photo = os.path.join(self.tmp.name, "render.png")
        im.save(self.photo)

    def tearDown(self):
        self.tmp.cleanup()

    def test_present_color_passes_absent_color_flagged(self):
        from agent_core import color_anchor as ca
        misses = ca.find_color_misses(
            self.photo,
            [{"part": "鞋身", "hex": "d22b2b"}, {"part": "大底", "hex": "2456b0"}],
            de_max=28.0)
        self.assertEqual([m["part"] for m in misses], ["大底"])
        self.assertGreater(misses[0]["delta_e"], 28.0)

    def test_whitish_target_skipped_against_white_bg(self):
        from agent_core import color_anchor as ca
        misses = ca.find_color_misses(self.photo, [{"part": "包邊", "hex": "f5f0e1"}],
                                      de_max=28.0)
        self.assertEqual(misses, [])

    def test_small_accent_seen_even_when_dominant_palette_is_blind(self):
        """2026-09-08 review：12 色 MEDIANCUT 把 1–4% 點綴併進大叢集 → 正確渲染
        也報偏色。小色塊補盤（_chroma_clusters）要能證明點綴色在場；真的不在
        圖上的色照樣要抓。"""
        from PIL import Image, ImageDraw
        from agent_core import color_anchor as ca
        im = Image.new("RGB", (200, 200), (255, 255, 255))
        d = ImageDraw.Draw(im)
        d.rectangle((10, 40, 150, 190), fill=(43, 48, 66))     # 深藍鞋身
        d.rectangle((160, 40, 195, 70), fill=(25, 63, 143))    # 寶藍點綴 ~2.8%
        p = os.path.join(self.tmp.name, "accented.png")
        im.save(p)
        body_only = [(ca.rgb_to_lab((43, 48, 66)), 0.5)]       # 模擬主色盤失明
        with mock.patch.object(ca, "dominant_lab_colors", return_value=body_only):
            self.assertEqual(
                ca.find_color_misses(p, [{"part": "後套", "hex": "193f8f"}], 28.0), [])
            misses = ca.find_color_misses(p, [{"part": "大底", "hex": "d22b2b"}], 28.0)
        self.assertEqual([m["part"] for m in misses], ["大底"])

    def test_unreadable_image_never_blocks(self):
        from agent_core import color_anchor as ca
        bad = os.path.join(self.tmp.name, "not_an_image.png")
        with open(bad, "wb") as f:
            f.write(b"\x89PNG fake")
        self.assertEqual(
            ca.find_color_misses(bad, [{"part": "鞋身", "hex": "d22b2b"}], 28.0), [])


class DominantHexColorsTests(unittest.TestCase):
    """完稿主色抽取：平色塊抽得準、白底/背景剔除。"""

    def test_flat_blocks_extracted_white_excluded(self):
        import tempfile
        from PIL import Image, ImageDraw
        from agent_core import color_anchor as ca
        with tempfile.TemporaryDirectory() as td:
            im = Image.new("RGB", (200, 200), (255, 255, 255))
            d = ImageDraw.Draw(im)
            d.rectangle((10, 10, 120, 190), fill=(121, 100, 81))
            d.rectangle((130, 10, 190, 190), fill=(236, 122, 45))
            p = os.path.join(td, "art.png")
            im.save(p)
            got = ca.dominant_hex_colors(p)
        hexes = [h for h, _ in got]
        self.assertIn("796451", hexes)
        self.assertIn("ec7a2d", hexes)
        self.assertNotIn("ffffff", hexes)              # 白底不是「主色」


class AccentHexColorsTests(unittest.TestCase):
    """點綴色抽取：主色路線看不見的小面積高彩色塊要抓到；低彩度/白底/過小不收。

    2026-09-03 PSS 2 案：後套寶藍只佔完稿 2.3%，MEDIANCUT 把它併進深藍鞋身
    叢集 → dominant_hex_colors 看不見、ΔE 驗證無目標，偏色渲染照樣通關。
    """

    def _art(self, td, extras=()):
        from PIL import Image, ImageDraw
        im = Image.new("RGB", (200, 200), (255, 255, 255))
        d = ImageDraw.Draw(im)
        d.rectangle((10, 40, 150, 190), fill=(43, 48, 66))       # 深藍鞋身（chroma≈12）
        d.rectangle((160, 100, 195, 130), fill=(188, 196, 201))  # 灰反光條（chroma≈4）
        for box, fill in extras:
            d.rectangle(box, fill=fill)
        p = os.path.join(td, "art.png")
        im.save(p)
        return p

    def test_small_saturated_patch_found_low_chroma_ignored(self):
        from agent_core import color_anchor as ca
        with tempfile.TemporaryDirectory() as td:
            p = self._art(td, extras=[((160, 40, 195, 70), (25, 63, 143))])  # 寶藍 ~2.8%
            got = ca.accent_hex_colors(p)
        self.assertEqual([h for h, _ in got], ["193f8f"])         # 只收高彩度那塊

    def test_below_min_frac_dropped(self):
        from agent_core import color_anchor as ca
        with tempfile.TemporaryDirectory() as td:
            p = self._art(td, extras=[((160, 40, 168, 48), (25, 63, 143))])  # 寶藍 ~0.2%
            self.assertEqual(ca.accent_hex_colors(p), [])

    def test_unreadable_image_never_raises(self):
        from agent_core import color_anchor as ca
        with tempfile.TemporaryDirectory() as td:
            bad = os.path.join(td, "bad.png")
            with open(bad, "wb") as f:
                f.write(b"\x89PNG fake")
            self.assertEqual(ca.accent_hex_colors(bad), [])


class SwatchStripTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_strip_renders_exact_block_colors(self):
        from PIL import Image
        from agent_core import color_anchor as ca
        out = ca.render_swatch_strip(
            [{"part": "鞋身", "hex": "0f4c81", "label": "Pantone 19-4052"},
             {"part": "大底", "hex": "d22b2b", "label": "紅"}],
            os.path.join(self.tmp.name, "sw.png"))
        self.assertTrue(out and os.path.exists(out))
        im = Image.open(out)
        self.assertEqual(im.getpixel((115, 60)), (15, 76, 129))    # 第一格中心＝色票值
        self.assertEqual(im.getpixel((345, 60)), (210, 43, 43))    # 第二格

    def test_no_valid_hex_returns_empty(self):
        from agent_core import color_anchor as ca
        self.assertEqual(ca.render_swatch_strip(
            [{"part": "鞋身", "hex": "", "label": ""}],
            os.path.join(self.tmp.name, "sw.png")), "")


if __name__ == "__main__":
    unittest.main()
