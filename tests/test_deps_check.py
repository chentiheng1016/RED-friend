"""venv 實裝版本 vs requirements 釘版的漂移偵測。

2026-08-12 一天內踩到兩次「改了釘版但沒生效」：pypdf 以為升好其實沒升、ruff 因為
`requirements.txt` 不含 `-dev` 而停在舊版（而 ruff 版本決定 lint 規則集，差一版
差 4,014 個錯）。當時沒有任何東西在檢查這件事。

本檔守的核心不變量是**判準要窄**：只有「已安裝但版本不符」算漂移，「沒安裝」不算
—— gui/market/oracle/box-ocr 是選配依賴，把沒裝的也算進來會天天假警報，而假警報
會讓人學會忽略告警，比沒有告警更糟。
"""
from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


class ParseRequirementsTests(unittest.TestCase):
    def setUp(self):
        from agent_core import deps_check
        self.dc = deps_check

    def test_pins_and_floors(self):
        got = self.dc.parse_requirements(
            "google-genai==2.17.0\nPillow>=12.3.0\nuvicorn>=0.52.1\n")
        self.assertEqual(got["google-genai"], ("==", "2.17.0"))
        self.assertEqual(got["pillow"], (">=", "12.3.0"))

    def test_skips_comments_includes_and_blanks(self):
        got = self.dc.parse_requirements(
            "# 註解\n\n-r requirements-core.txt\n--index-url https://x\npypdf==6.15.0\n")
        self.assertEqual(list(got), ["pypdf"])

    def test_strips_extras_and_env_markers(self):
        got = self.dc.parse_requirements(
            'uvicorn[standard]>=0.52.1\nfoo==1.2.3 ; python_version < "3.13"\n')
        self.assertEqual(got["uvicorn"], (">=", "0.52.1"))
        self.assertEqual(got["foo"], ("==", "1.2.3"))

    def test_name_normalisation_pep503(self):
        """`Pillow` / `google_genai` 要對得上 metadata 回的名字。"""
        got = self.dc.parse_requirements("Pillow>=12.3.0\ngoogle_cloud_bigquery==3.43.0\n")
        self.assertIn("pillow", got)
        self.assertIn("google-cloud-bigquery", got)


class DriftDetectionTests(unittest.TestCase):
    def setUp(self):
        from agent_core import deps_check
        self.dc = deps_check

    def test_exact_pin_mismatch_is_drift(self):
        d = self.dc.find_drift({"pypdf": ("==", "6.15.0")}, {"pypdf": "6.14.2"})
        self.assertEqual(len(d), 1)
        self.assertEqual(d[0]["installed"], "6.14.2")

    def test_exact_pin_match_is_clean(self):
        self.assertEqual(
            self.dc.find_drift({"pypdf": ("==", "6.15.0")}, {"pypdf": "6.15.0"}), [])

    def test_floor_satisfied_by_newer(self):
        self.assertEqual(
            self.dc.find_drift({"ruff": (">=", "0.16.2")}, {"ruff": "0.17.0"}), [])

    def test_floor_violated_by_older(self):
        d = self.dc.find_drift({"ruff": (">=", "0.16.2")}, {"ruff": "0.15.18"})
        self.assertEqual(len(d), 1)

    def test_floor_compares_numerically_not_lexically(self):
        """字串比較會說 '0.9' > '0.16' —— 版本必須逐段數字比。"""
        self.assertEqual(
            self.dc.find_drift({"x": (">=", "0.9.0")}, {"x": "0.16.0"}), [])

    def test_differing_segment_counts(self):
        """2.17 vs 2.17.0 不該被判成不符（>= 情形）。"""
        self.assertEqual(
            self.dc.find_drift({"x": (">=", "2.17")}, {"x": "2.17.0"}), [])

    def test_missing_package_is_not_drift(self):
        """本檔最重要的一條：選配依賴沒裝不算漂移，否則天天假警報。"""
        self.assertEqual(
            self.dc.find_drift({"paddleocr": ("==", "3.0.0")}, {}), [])

    def test_prerelease_segment_does_not_crash(self):
        self.dc.find_drift({"x": (">=", "1.2.0rc1")}, {"x": "1.2.0"})
        self.dc.find_drift({"x": (">=", "1.2.0")}, {"x": "1.2.0.dev3"})


class LoadRequiredTests(unittest.TestCase):
    def setUp(self):
        from agent_core import deps_check
        self.dc = deps_check

    def test_reads_every_requirements_file_but_skips_lock(self):
        """`requirements.venv.lock` 早已漂移且沒人維護 —— 納入會製造整片假警報。"""
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            for name, body in (("requirements-core.txt", "a==1.0\n"),
                               ("requirements-gui.txt", "b>=2.0\n"),
                               ("requirements.venv.lock", "c==0.0.1\n")):
                with open(os.path.join(d, name), "w", encoding="utf-8") as fh:
                    fh.write(body)
            got = self.dc.load_required(d)
        self.assertEqual(sorted(got), ["a", "b"])

    def test_separate_venv_marker_excludes_whole_file(self):
        """標了 separate-venv 的檔整份跳過 —— 那是別的 venv 的清單。

        真實案例：requirements-box-ocr.txt 裝的是 var/venvs/box_ocr，它釘
        onnxruntime==1.28.0；主 venv 剛好也有 onnxruntime（chromadb 拉的傳遞
        依賴），版本自然不同 → 天天報一筆無意義的漂移。
        """
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            for name, body in (
                ("requirements-core.txt", "a==1.0\n"),
                ("requirements-box-ocr.txt",
                 "# 專用 venv\n# deps-check: separate-venv\nonnxruntime==1.28.0\n"),
            ):
                with open(os.path.join(d, name), "w", encoding="utf-8") as fh:
                    fh.write(body)
            got = self.dc.load_required(d)
        self.assertEqual(sorted(got), ["a"])
        self.assertNotIn("onnxruntime", got)

    def test_real_box_ocr_file_carries_the_marker(self):
        """釘死本 repo 的實際檔案有標記**且具名** —— 否則假警報會回來，
        或是退化成「跳過但沒人檢查」（Dependabot bump 沒人裝的那個洞）。"""
        path = os.path.join(_REPO_ROOT, "requirements-box-ocr.txt")
        with open(path, "r", encoding="utf-8") as fh:
            marked, venv = self.dc.parse_separate_venv_marker(fh.read())
        self.assertTrue(marked)
        self.assertEqual(venv, "box_ocr")
        self.assertNotIn("onnxruntime", self.dc.load_required(_REPO_ROOT))
        self.assertIn("box_ocr", self.dc.separate_venv_requirements(_REPO_ROOT))

    def test_unreadable_dir_returns_empty_not_raises(self):
        self.assertEqual(self.dc.load_required("/nonexistent/zzz"), {})


class SeparateVenvTests(unittest.TestCase):
    """跳過≠檢查完 —— 具名的專用 venv 要真的被比對到。

    這條防的是 2026-08-14 那個洞：Dependabot 07-31 bump 了
    requirements-box-ocr.txt 的 onnxruntime，但沒有任何自動流程會安裝那份檔，
    var/venvs/box_ocr 停在舊版兩週沒人發現。
    """

    def setUp(self):
        from agent_core import deps_check
        self.dc = deps_check

    def test_marker_parsing_named_and_bare(self):
        self.assertEqual(self.dc.parse_separate_venv_marker(
            "# deps-check: separate-venv=box_ocr\n"), (True, "box_ocr"))
        self.assertEqual(self.dc.parse_separate_venv_marker(
            "# deps-check: separate-venv box_ocr\n"), (True, "box_ocr"))
        # 不具名 = 只跳過，無從檢查
        self.assertEqual(self.dc.parse_separate_venv_marker(
            "# deps-check: separate-venv\nx==1\n"), (True, ""))
        self.assertEqual(self.dc.parse_separate_venv_marker("x==1\n"), (False, ""))

    def test_named_file_is_grouped_by_venv_not_merged_into_main(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            for name, body in (
                ("requirements-core.txt", "a==1.0\n"),
                ("requirements-box-ocr.txt",
                 "# deps-check: separate-venv=box_ocr\nonnxruntime==1.28.0\n"),
                ("requirements-bare.txt",
                 "# deps-check: separate-venv\nzzz==9.9\n"),
            ):
                with open(os.path.join(d, name), "w", encoding="utf-8") as fh:
                    fh.write(body)
            self.assertEqual(sorted(self.dc.load_required(d)), ["a"])
            sep = self.dc.separate_venv_requirements(d)
        self.assertEqual(sorted(sep), ["box_ocr"])          # 不具名的不收
        self.assertEqual(sep["box_ocr"]["onnxruntime"], ("==", "1.28.0"))

    def test_venv_python_honours_runtime_root(self):
        got = self.dc.venv_python("box_ocr", runtime_root="/rt")
        self.assertEqual(got, os.path.join("/rt", "venvs", "box_ocr", "bin", "python"))

    def test_missing_venv_probes_to_empty_not_raises(self):
        self.assertEqual(self.dc.load_installed_from_venv("/nonexistent/bin/python"), {})
        self.assertEqual(self.dc.load_installed_from_venv(""), {})

    def test_probe_reads_a_real_interpreter(self):
        """探測邏輯本身要真的能問出版本 —— 拿現在這個直譯器當實體測試。

        （不釘特定套件名，免得換依賴就紅；問得到東西、且名字已正規化就夠。）
        """
        got = self.dc.load_installed_from_venv(sys.executable)
        self.assertTrue(got, "應該問得到至少一個套件")
        self.assertTrue(all(isinstance(v, str) for v in got.values()))
        self.assertTrue(all(k == self.dc.normalize(k) for k in got),
                        "回傳的套件名要已經正規化")

    def test_drift_entries_carry_venv_and_separate_venv_is_compared(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "requirements-box-ocr.txt"), "w", encoding="utf-8") as fh:
                fh.write("# deps-check: separate-venv=box_ocr\nonnxruntime==1.28.0\n")
            with mock.patch.object(self.dc, "load_installed", return_value={}), \
                    mock.patch.object(self.dc, "load_installed_from_venv",
                                      return_value={"onnxruntime": "1.27.0"}):
                drift = self.dc.check_dependency_drift(d)
        self.assertEqual(len(drift), 1)
        self.assertEqual(drift[0]["venv"], "box_ocr")
        self.assertEqual(drift[0]["installed"], "1.27.0")

    def test_venv_not_installed_is_not_drift(self):
        """專用 venv 沒建起來 = 沒裝這個選配元件，與「沒裝不算」同一判準。"""
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "requirements-box-ocr.txt"), "w", encoding="utf-8") as fh:
                fh.write("# deps-check: separate-venv=box_ocr\nonnxruntime==1.28.0\n")
            with mock.patch.object(self.dc, "load_installed", return_value={}), \
                    mock.patch.object(self.dc, "load_installed_from_venv", return_value={}):
                self.assertEqual(self.dc.check_dependency_drift(d), [])

    def test_fix_hint_points_at_the_setup_script_only_if_it_exists(self):
        self.assertIn("pip install", self.dc.fix_hint(self.dc.MAIN_VENV))
        # 真實 repo 有 bin/setup-box-ocr
        self.assertEqual(self.dc.fix_hint("box_ocr", _REPO_ROOT), "./bin/setup-box-ocr")
        # 沒有對應腳本就別亂給指令
        self.assertNotIn("./bin/", self.dc.fix_hint("no_such_venv", _REPO_ROOT))

    def test_format_groups_by_venv_with_its_own_fix(self):
        out = self.dc.format_drift([
            {"name": "ruff", "op": "==", "wanted": "1.0", "installed": "0.9",
             "venv": self.dc.MAIN_VENV},
            {"name": "onnxruntime", "op": "==", "wanted": "1.28.0",
             "installed": "1.27.0", "venv": "box_ocr"},
        ], root=_REPO_ROOT)
        self.assertIn("主 .venv", out)
        self.assertIn("專用 venv：box_ocr", out)
        self.assertIn("pip install", out)
        self.assertIn("./bin/setup-box-ocr", out)


class FormatTests(unittest.TestCase):
    def setUp(self):
        from agent_core import deps_check
        self.dc = deps_check

    def test_clean_message(self):
        self.assertIn("一致", self.dc.format_drift([]))

    def test_drift_lists_package_and_fix_command(self):
        out = self.dc.format_drift(
            [{"name": "pypdf", "op": "==", "wanted": "6.15.0", "installed": "6.14.2"}])
        self.assertIn("pypdf", out)
        self.assertIn("6.14.2", out)
        self.assertIn("pip install", out)


class AlertIntegrationTests(unittest.TestCase):
    def setUp(self):
        from agent_core import dashboard_alerts
        self.da = dashboard_alerts

    def test_registered_in_all_checks(self):
        """沒掛進 _ALL_CHECKS 就永遠不會跑 —— 正是這批要修的靜默失效。"""
        self.assertIn(self.da._check_dependency_drift, self.da._ALL_CHECKS)

    def test_clean_env_produces_no_alert(self):
        from agent_core import deps_check
        with mock.patch.object(deps_check, "check_dependency_drift", return_value=[]):
            self.assertEqual(self.da._check_dependency_drift(), [])

    def test_drift_produces_warn_with_actionable_advice(self):
        from agent_core import deps_check
        drift = [{"name": "pypdf", "op": "==", "wanted": "6.15.0", "installed": "6.14.2"}]
        with mock.patch.object(deps_check, "check_dependency_drift", return_value=drift):
            alerts = self.da._check_dependency_drift()
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["level"], "warn")
        self.assertIn("pypdf", alerts[0]["detail"])
        self.assertIn("pip install", alerts[0]["advice"])

    def test_separate_venv_drift_gets_its_own_fix_command(self):
        """對 box_ocr 叫人跑 `pip install -r requirements.txt` 是錯的建議 ——
        advice 要依實際漂到的 venv 給對應指令。"""
        from agent_core import deps_check
        drift = [{"name": "onnxruntime", "op": "==", "wanted": "1.28.0",
                  "installed": "1.27.0", "venv": "box_ocr"}]
        with mock.patch.object(deps_check, "check_dependency_drift", return_value=drift):
            alerts = self.da._check_dependency_drift()
        self.assertEqual(len(alerts), 1)
        self.assertIn("box_ocr", alerts[0]["detail"])          # 標明是哪個 venv
        self.assertIn("setup-box-ocr", alerts[0]["advice"])
        self.assertNotIn("pip install -r requirements.txt", alerts[0]["advice"])
        self.assertEqual(alerts[0]["metric"]["venvs"], ["box_ocr"])

    def test_check_failure_does_not_raise(self):
        from agent_core import deps_check
        with mock.patch.object(deps_check, "check_dependency_drift",
                               side_effect=RuntimeError("boom")):
            self.assertEqual(self.da._check_dependency_drift(), [])


class DashboardSectionTests(unittest.TestCase):
    def setUp(self):
        from agent_core import dashboard
        self.d = dashboard

    def test_section_renders_drift(self):
        from agent_core import deps_check
        drift = [{"name": "onnxruntime", "op": "==", "wanted": "1.28.0",
                  "installed": "1.27.0"}]
        with mock.patch.object(deps_check, "check_dependency_drift", return_value=drift):
            out = self.d._section_deps()
        self.assertIn("onnxruntime", out)

    def test_section_survives_check_failure(self):
        from agent_core import deps_check
        with mock.patch.object(deps_check, "check_dependency_drift",
                               side_effect=OSError("nope")):
            self.assertIn("⚠️", self.d._section_deps())


if __name__ == "__main__":
    unittest.main()
