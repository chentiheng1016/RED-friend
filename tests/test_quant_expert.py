"""Contract tests for the Economics / Calculus / Statistics expert upgrade.

Three things are locked in here so the behavior can't silently regress:

  1. persona  — the always-on 【量化分析素養】 block stays in 小紅's system prompt
                with its non-negotiable rigor rules (verify-by-tool, correlation≠
                causation, correct p-value framing, the business formulas).
  2. quant mode — the opt-in deep-dive work mode is registered in BOTH
                persona_profiles (what set_work_mode validates against) AND
                mode_policy (tool/tier rules), matching the security-mode contract.
  3. skill    — skills/quant_tools.py computes the headline business formulas
                correctly (so the agent verifies numbers instead of guessing).

Pure / offline by design: build_persona_text takes its memory as an argument and
the mode lookups + skill are pure functions, so there is no live var/ state to
isolate (see the "tests immune to live runtime state" house rule).
"""
import importlib.util
import os
import unittest

# Match how the suite runs (make test sets this); keeps imports non-interactive.
os.environ.setdefault("AGENT_DAEMON_MODE", "1")

from agent_core import path_safety
from agent_core.persona import build_persona_text
from agent_core.persona_profiles import list_known_modes, persona_for
from agent_core.mode_policy import known_modes, get_mode_rules


def _load_quant_skill():
    """Load skills/quant_tools.py by path (skills/ is not an importable package)."""
    skill_path = os.path.join(path_safety._REPO_ROOT, "skills", "quant_tools.py")
    spec = importlib.util.spec_from_file_location("quant_tools_under_test", skill_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class PersonaQuantExpertiseTests(unittest.TestCase):
    """The always-on expert block must stay in the base system prompt."""

    def setUp(self):
        self.persona = build_persona_text("(測試用啟動記憶)")

    def test_block_header_present(self):
        self.assertIn("量化分析素養", self.persona)

    def test_verify_by_tool_rule(self):
        # The agent must be told to compute with run_python_code, not mental math.
        self.assertIn("run_python_code", self.persona)
        self.assertIn("quant_tools", self.persona)

    def test_statistics_honesty_rules(self):
        # p-value framing + correlation≠causation are the easiest things to get
        # wrong; both must be spelled out.
        self.assertIn("p 值", self.persona)
        self.assertIn("相關 ≠ 因果", self.persona)

    def test_business_formulas_present(self):
        for needle in ("損益兩平", "貢獻邊際", "需求彈性", "MR=邊際成本 MC"):
            self.assertIn(needle, self.persona)

    def test_current_data_must_be_fetched(self):
        # "Changing" numbers (rates/fx) must be looked up, not recalled.
        self.assertIn("search_the_web", self.persona)

    def test_persona_still_substantive(self):
        # Mirror the smoke-test contract: persona stays a long string about 小紅.
        self.assertGreater(len(self.persona), 1000)
        self.assertIn("小紅", self.persona)


class QuantModeRegistrationTests(unittest.TestCase):
    """The quant work mode must be registered everywhere a mode needs to be."""

    def test_registered_in_persona_profiles(self):
        # This is exactly what set_work_mode() validates an incoming mode against.
        self.assertIn("quant", list_known_modes())

    def test_addendum_describes_quant_role(self):
        addendum = persona_for("quant")
        self.assertTrue(addendum.strip(), "quant addendum should be non-empty")
        self.assertIn("量化深度模式", addendum)
        self.assertIn("step-by-step", addendum)

    def test_registered_in_mode_policy(self):
        self.assertIn("quant", known_modes())

    def test_quant_mode_keeps_full_tool_set(self):
        # Analysis may need any tool (run_python_code / search_the_web / recall…),
        # so quant must not narrow the tool set, like dev.
        rules = get_mode_rules("quant")
        self.assertIsNone(rules["tool_names"])
        self.assertEqual(rules["blocked_tiers"], [])


class QuantSkillTests(unittest.TestCase):
    """skills/quant_tools.py must compute the headline formulas correctly."""

    @classmethod
    def setUpClass(cls):
        cls.q = _load_quant_skill()

    # ---- break-even ---------------------------------------------------
    def test_break_even_basic(self):
        out = self.q.break_even_analysis(1000, 10, 6)
        self.assertNotIn("❌", out)
        self.assertIn("250", out)        # 1000 / (10-6) = 250 units
        self.assertIn("貢獻邊際", out)

    def test_break_even_nonpositive_margin(self):
        out = self.q.break_even_analysis(1000, 10, 12)  # CM = -2
        self.assertIn("❌", out)

    # ---- profit -------------------------------------------------------
    def test_profit_at_quantity(self):
        out = self.q.profit_at_quantity(300, 1000, 10, 6)
        # rev 3,000 ; total cost 2,800 ; profit 200
        self.assertIn("200", out)
        self.assertIn("賺錢", out)

    # ---- elasticity ---------------------------------------------------
    def test_price_elasticity_elastic(self):
        out = self.q.price_elasticity(10, 100, 8, 140)  # Ed = -1.5
        self.assertIn("1.5", out)
        self.assertIn("有彈性", out)

    # ---- NPV ----------------------------------------------------------
    def test_npv_positive(self):
        out = self.q.npv_analysis(10, "-100, 60, 60")  # NPV ≈ +4.13
        self.assertIn("4.13", out)
        self.assertIn("值得", out)

    def test_npv_bad_input(self):
        self.assertIn("❌", self.q.npv_analysis(10, "abc"))

    # ---- CAGR ---------------------------------------------------------
    def test_cagr(self):
        out = self.q.cagr(100, 200, 3)  # 2^(1/3)-1 ≈ 25.99%
        self.assertIn("25.99", out)

    def test_cagr_bad_input(self):
        self.assertIn("❌", self.q.cagr(0, 200, 3))

    # ---- descriptive stats -------------------------------------------
    def test_descriptive_stats(self):
        out = self.q.descriptive_stats("2, 4, 4, 4, 5, 5, 7, 9")
        self.assertIn("n = 8", out)
        self.assertIn("Mean = 5", out)
        self.assertIn("Median = 4.5", out)

    def test_descriptive_stats_empty(self):
        self.assertIn("❌", self.q.descriptive_stats(""))

    # ---- linear regression -------------------------------------------
    def test_regression_perfect_line(self):
        out = self.q.simple_linear_regression("1,2,3,4,5", "2,4,6,8,10")
        self.assertIn("b1 = 2", out)     # slope 2
        self.assertIn("R² = 1", out)     # perfect fit
        self.assertIn("相關 ≠ 因果", out)  # causation caveat always shown

    def test_regression_length_mismatch(self):
        self.assertIn("❌", self.q.simple_linear_regression("1,2,3", "1,2"))


if __name__ == "__main__":
    unittest.main()
