"""material_anchor：材質詞→canonical key→特寫圖庫；去重與上限。

Phase B（2026-09-01 大王「材質也要模擬出來」）：庫是 var/data/material_swatches/
的 runtime 資產——測試全部指到 tmp，空庫必須零影響（material_refs 回空）。
注意（unittest discover）：conftest fixture 不生效，隔離全在 setUp/tearDown。
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO_ROOT)


class MaterialKeyTests(unittest.TestCase):
    def test_zh_alias_maps_to_key(self):
        from agent_core import material_anchor as ma
        self.assertEqual(ma.material_key("麂皮"), "suede")
        self.assertEqual(ma.material_key("三明治網布"), "mesh")
        self.assertEqual(ma.material_key("魔鬼氈"), "velcro")

    def test_ascii_word_boundary_pu_vs_tpu(self):
        from agent_core import material_anchor as ma
        self.assertEqual(ma.material_key("TPU"), "tpu")      # 不可被 "pu" 搶走
        self.assertEqual(ma.material_key("pu/tpu"), "tpu")   # 兩料都在 → 最長別名先中
        self.assertEqual(ma.material_key("PU 皮"), "pu")

    def test_unknown_material_returns_empty(self):
        from agent_core import material_anchor as ma
        self.assertEqual(ma.material_key("水鑽"), "")
        self.assertEqual(ma.material_key(""), "")

    def test_pro_action_is_synthetic_not_nubuck(self):
        # 2026-09-02 PSS 6 案：「羊巴戈」被 LLM 望文生義成 Nubuck。羊巴戈＝客人單上
        # 的 Pro Action（南亞南通廠合成皮）；Nubuck＝牛巴戈（真牛皮磨砂），不同料。
        from agent_core import material_anchor as ma
        self.assertEqual(ma.material_key("羊巴戈"), "pro_action")
        self.assertEqual(ma.material_key("Pro Action"), "pro_action")
        self.assertEqual(ma.material_key("羊巴戈 深藍"), "pro_action")
        self.assertEqual(ma.material_key("牛巴戈"), "nubuck")

    def test_materials_in_text_for_extra_note(self):
        # 2026-09-02 PSS 6 案：修正指示「皮料改羊巴戈」不在逐部位規格裡，
        # 點名的料也要能對到特寫圖。
        from agent_core import material_anchor as ma
        got = ma.materials_in_text("皮料部位改成羊巴戈，其他不變")
        self.assertEqual([g["key"] for g in got], ["pro_action"])
        self.assertEqual(got[0]["term"], "羊巴戈")
        keys = {g["key"] for g in ma.materials_in_text("改羊巴戈拼接丹寧")}
        self.assertEqual(keys, {"pro_action", "denim"})
        self.assertEqual(ma.materials_in_text(""), [])

    def test_richtex_and_nylon_shell_fabrics(self):
        # 2026-09-02 PSS 7 案：Richtex/尼龍平織布對不上任何 key → 材質全靠文字
        # 進 prompt，被「細緻纖維」字面帶成精編絨。
        from agent_core import material_anchor as ma
        self.assertEqual(ma.material_key("Richtex"), "richtex")
        self.assertEqual(ma.material_key("全黑 Richtex 防水透氣機能面料"), "richtex")
        self.assertEqual(ma.material_key("SympaTex"), "richtex")
        self.assertEqual(ma.material_key("牛津布"), "nylon")
        self.assertEqual(ma.material_key("防水尼龍布"), "nylon")


class MaterialRefsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.lib = os.path.join(self.tmp.name, "material_swatches")
        os.makedirs(self.lib, exist_ok=True)
        self._p = mock.patch("agent_core.logging_and_paths.DATA_DIR", self.tmp.name)
        self._p.start()

    def tearDown(self):
        self._p.stop()
        self.tmp.cleanup()

    def _seed(self, key):
        from PIL import Image
        p = os.path.join(self.lib, f"{key}.png")
        Image.new("RGB", (64, 64), (120, 90, 60)).save(p)
        return p

    def test_refs_dedupe_same_material_across_parts(self):
        from agent_core import material_anchor as ma
        self._seed("suede")
        refs = ma.material_refs([
            {"part": "鞋身", "material": "麂皮"},
            {"part": "鞋舌", "material": "反毛皮"},      # 同 key、不同別名
            {"part": "大底", "material": "橡膠"},        # 庫裡沒圖 → 不附
        ])
        self.assertEqual(len(refs), 1)
        self.assertEqual(refs[0]["key"], "suede")
        self.assertEqual(refs[0]["parts"], ["鞋身", "鞋舌"])
        self.assertEqual(refs[0]["label"], "麂皮")       # 取第一個部位的原文

    def test_cap_limits_reference_count(self):
        from agent_core import material_anchor as ma
        for k in ("suede", "mesh", "rubber", "velcro"):
            self._seed(k)
        refs = ma.material_refs([
            {"part": "a", "material": "麂皮"}, {"part": "b", "material": "網布"},
            {"part": "c", "material": "橡膠"}, {"part": "d", "material": "魔鬼氈"},
        ], cap=3)
        self.assertEqual([r["key"] for r in refs], ["suede", "mesh", "rubber"])

    def test_empty_library_yields_nothing(self):
        from agent_core import material_anchor as ma
        self.assertEqual(ma.material_refs([{"part": "鞋身", "material": "麂皮"}]), [])


if __name__ == "__main__":
    unittest.main()
