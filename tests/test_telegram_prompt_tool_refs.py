"""前台 Telegram bot 的系統提示點名的工具，必須真的存在、而且分級跟提示講的一致。

這是「白名單 / 提示與現實脫節 → 靜默失效」那一類的守門，跟
`tests/test_dispatcher_task_tool_reachability.py`（排程任務那條路）成對。

實際踩到的：2026-06-16（a1a6312f）提示的「免確認直接做」清單裡混進了
``update_calendar_event``，那支工具**從來沒有存在過** —— 真正註冊的只有
create_calendar_event / delete_calendar_event / list_calendar_events。於是模型被
告知有一支可以直接改行程的免確認工具，而它在工具表裡根本看不到這個名字。躺了
兩個半月沒人發現，因為：

  - tool_tiers.get_tier() 對不存在的名字一律回 "safe"（未知預設 safe），
    所以分級系統本身抓不到幽靈；
  - tg_auth._SENSITIVE_TOOLS / tool_budgets / intent_router / task_queue 裡也都
    有這個名字，看起來「到處都登記過」，更像真的；
  - 提示是一大串中文字串常量，沒有任何東西在比對它跟註冊表。

治法：把提示裡明列的工具名抽成 _NO_CONFIRM_TOOL_NAMES / _CONFIRM_REQUIRED_
TOOL_NAMES 兩個常數（提示由常數組出來），這支測試再拿它們去對註冊表與 tier。

後續：``update_calendar_event`` 本身已於 2026-08-28 補上實作（google_suite），
所以它現在**合法**出現在提示裡 —— 幽靈是被做出來、不是被刪掉的。這支測試不管
走哪條，只問「提示說得出口的工具，註冊表裡找不找得到、tier 對不對」。
"""
from __future__ import annotations

import os
import re
import sys
import unittest

os.environ.setdefault("AGENT_DAEMON_MODE", "1")

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from agent_core import daemon_telegram as dt  # noqa: E402
from agent_core.tool_tiers import get_tier  # noqa: E402


def _real_tool_names() -> set[str]:
    from agent_core.tool_registry import tools_list
    return {getattr(t, "__name__", "") for t in tools_list}


# 提示裡出現、但**確認過不是工具**的識別字：變數名、模式名、外部程式名，以及
# 中文散文裡剛好接了全形括號的字（_CALL_FORM 的 `（` 也算呼叫形式）。
# 這份清單就是「有人看過了」的簽名檔——名字掉進來又不在註冊表時，測試會逼人
# 明確表態，而不是靜靜地放過下一個 update_calendar_event。
KNOWN_NON_TOOL_IDENTIFIERS = frozenset({
    # 模組內部函式 / 變數（提示在解釋自己怎麼運作）
    "_gemini_generate", "_in_long_poll", "_tg_chat_state",
    "caller", "ignore_paths", "last_msg_ts", "payload",
    # 模式名 / 外部程式
    "ffmpeg", "meeting", "music", "requests", "requests_module",
    # 中文散文 + 全形括號（不是呼叫）
    "dict", "phrase",
})


class NoConfirmListTests(unittest.TestCase):
    def setUp(self):
        self.real = _real_tool_names()
        self.prompt = dt._TG_BOT_SYSTEM_INSTRUCTION_APPEND

    def test_every_no_confirm_tool_exists(self):
        """幽靈名字守門：提示說得出口的工具，註冊表裡要找得到。"""
        for name in dt._NO_CONFIRM_TOOL_NAMES:
            with self.subTest(tool=name):
                self.assertIn(
                    name, self.real,
                    f"提示告訴模型 `{name}` 可以直接呼叫，但註冊表裡沒有這支工具",
                )

    def test_every_no_confirm_tool_is_actually_safe(self):
        """提示宣稱「免確認」，tier 就必須真的是 safe，否則模型會被引導去撞閘門。"""
        for name in dt._NO_CONFIRM_TOOL_NAMES:
            with self.subTest(tool=name):
                self.assertEqual(
                    get_tier(name), "safe",
                    f"提示說 `{name}` 免確認，但 tool_tiers 判它是 {get_tier(name)}",
                )

    def test_confirm_required_tools_exist_and_are_not_safe(self):
        """反方向也要釘：提示說「這些才要確認」，它們就不能是 safe。"""
        for name in dt._CONFIRM_REQUIRED_TOOL_NAMES:
            with self.subTest(tool=name):
                self.assertIn(name, self.real)
                self.assertNotEqual(
                    get_tier(name), "safe",
                    f"提示說 `{name}` 需要確認，但 tool_tiers 判它是 safe",
                )

    def test_constants_actually_reach_the_rendered_prompt(self):
        """常數不能跟提示正文脫鉤（否則改了常數、提示還印舊的）。"""
        for name in (*dt._NO_CONFIRM_TOOL_NAMES, *dt._CONFIRM_REQUIRED_TOOL_NAMES):
            with self.subTest(tool=name):
                self.assertIn(name, self.prompt)

    def test_update_calendar_event_is_backed_by_a_real_tool(self):
        """回歸（原「幽靈工具」案）：提示留著這個名字，就必須真的有這支工具。

        2026-06-16~08-28 之間它只存在於提示與各份閘門清單裡，函式本體不存在。
        現在補上了實作，所以斷言方向是「名字在提示裡 ⇒ 註冊表裡也要有」，
        而不是「不准出現」。
        """
        self.assertIn("update_calendar_event", self.prompt)
        self.assertIn("update_calendar_event", self.real)
        self.assertEqual(get_tier("update_calendar_event"), "safe")


class PromptCallFormRefsTests(unittest.TestCase):
    """提示裡以「呼叫形式」出現的名字（`name(`）也要對得到工具。

    複用 daemon_dispatcher.extract_tool_refs —— 抽取規則單一真相，排程那條路
    跟前台這條路用同一把尺。
    """

    def test_call_form_refs_all_resolve(self):
        from agent_core.daemon_dispatcher import extract_tool_refs
        real = _real_tool_names()
        refs = extract_tool_refs(dt._TG_BOT_SYSTEM_INSTRUCTION_APPEND, real)
        missing = sorted(r for r in refs
                         if r not in real and r not in KNOWN_NON_TOOL_IDENTIFIERS)
        self.assertEqual(
            missing, [],
            f"系統提示以呼叫形式點名了不存在的工具：{missing}。"
            "若是工具→把它做出來或改名；若只是散文→加進 KNOWN_NON_TOOL_IDENTIFIERS。",
        )


class BacktickedToolNameDriftTests(unittest.TestCase):
    """提示裡用反引號括起來、且**看起來像工具名**的識別字，抽出來人工對帳。

    不能一律要求「反引號＝工具」——提示裡也用反引號括變數名、模式名、路徑。
    所以這裡只鎖住一份已知清單：新增的反引號識別字若不在清單也不在註冊表，
    就要有人來看一眼（多半就是下一個 update_calendar_event）。
    """

    def test_no_unreviewed_backticked_identifiers(self):
        real = _real_tool_names()
        found = set(re.findall(r"`([a-z_][a-z0-9_]{3,})`",
                               dt._TG_BOT_SYSTEM_INSTRUCTION_APPEND))
        unknown = sorted(found - real - KNOWN_NON_TOOL_IDENTIFIERS)
        self.assertEqual(
            unknown, [],
            "系統提示用反引號點名了既不是註冊工具、也不在 KNOWN_NON_TOOL_IDENTIFIERS "
            f"的識別字：{unknown}。若是工具→把它做出來或改名；若不是→加進清單。",
        )


class ConfirmShortCodeClaimsTests(unittest.TestCase):
    """提示對 +確認 短碼的每個宣稱，都要對得上 tg_auth 的真實行為。

    同本檔其他測試的原則：提示是寫給模型看的**承諾**，承諾跟實作漂開時沒有任何
    東西會響。這裡鎖住三句話：

      1.「一則訊息寫『c cc』就同時授權兩層」
      2.「單獨的『cc』在沒先打過 c 的情況下無效」
      3.「一般 +確認 是 c」

    背景：2026-08-28 大王連著送 c 和 cc 要取消兩場會議，因為 `c` 那一輪跑了約
    40 秒（兩次被擋的嘗試 + LLM 來回），它的「請補 cc」比大王自己送出的 cc 還晚
    到，看起來像被忽略 —— 於是多打了兩次。tg_auth 早就支援一則『c cc』一次授權
    兩層，但提示沒寫，所以模型從來不會這樣教。
    """

    # 從未確認過的 chat id：拿來驗「裸 cc 不得預先武裝第二層」。
    UNCONFIRMED_CHAT = "999999999"

    def setUp(self):
        from agent_core import tg_auth
        self.auth = tg_auth
        self.prompt = dt._TG_BOT_SYSTEM_INSTRUCTION_APPEND

    def test_combined_c_cc_grants_both_layers(self):
        for text in ("c cc", "cc c"):
            with self.subTest(text=text):
                self.assertTrue(
                    self.auth.message_grants_confirmation(text),
                    f"提示說一則『{text}』能授權第一層，實作沒有")
                self.assertTrue(
                    self.auth.message_grants_dangerous_confirmation(
                        text, chat_id=self.UNCONFIRMED_CHAT),
                    f"提示說一則『{text}』能授權第二層，實作沒有")

    def test_bare_cc_alone_does_not_arm_the_second_layer(self):
        """提示明講單獨 cc 無效 —— 這是防注入的刻意設計，不可被悄悄放寬。"""
        self.assertFalse(
            self.auth.message_grants_dangerous_confirmation(
                "cc", chat_id=self.UNCONFIRMED_CHAT))
        self.assertFalse(self.auth.message_grants_confirmation("cc"))

    def test_bare_c_grants_only_the_first_layer(self):
        self.assertTrue(self.auth.message_grants_confirmation("c"))
        self.assertFalse(
            self.auth.message_grants_dangerous_confirmation(
                "c", chat_id=self.UNCONFIRMED_CHAT))

    def test_prompt_actually_teaches_the_combined_form(self):
        """常數/正則對了但提示沒寫，模型還是不會用 —— 兩邊都要在。"""
        self.assertIn("c cc", self.prompt)
        self.assertIn("one-shot", self.prompt)


if __name__ == "__main__":
    unittest.main()
