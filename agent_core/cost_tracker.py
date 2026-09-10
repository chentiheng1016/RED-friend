"""Gemini API 成本追蹤 + 使用儀表板（Cost Dashboard）。

Why：
  現在小紅手上有 parallel sub-agents、rerank、learn_skill_from_video、
  生圖等會燒 token 的工具，但**每天實際花多少錢沒人知道**。
  不小心並行 10 個 sub-agent 跑大任務，帳單可能一天幾十刀也沒感覺。

How：
  1. 每次 `_gemini_generate` 回來後抓 response.usage_metadata
  2. 算成本 = prompt_tokens × input_rate + output_tokens × output_rate
  3. 寫一行 JSONL 到 var/data/cost/cost.jsonl
  4. 提供查詢 tool：今日花費 / 本週 / 本月 / 最燒的 tool / 趨勢

費率會變動，table 放下面 `_PRICING` 方便更新。

Tool call overhead 另計：`tool_use_prompt_token_count` 沒實際帳單費率，
但統計上算進 prompt_tokens（Gemini 計費也這樣）。
Thinking tokens（preview 模型）算 output（貴）。
"""
from __future__ import annotations

import functools
import json
import os
import time
from collections import defaultdict, Counter
from datetime import datetime, timedelta
from threading import Lock
from types import SimpleNamespace
from typing import Any

from agent_core.env_utils import env_float
from agent_core.logging_and_paths import logger


# 成本 log 位置。走 logging_and_paths.DATA_DIR（= RUNTIME_ROOT/data，吃
# RED_RUNTIME_DIR）而不是自己拼 REPO_ROOT/var/data —— 後者繞過 runtime 根，
# 造成兩個真實問題：
#   ① 測試從主 checkout 跑就直接寫進 live 成本帳（tests/__init__.py 把
#      RED_RUNTIME_DIR 導向 tmp 的防護救不到這條路）。實績：live
#      api_errors.jsonl 累積了 39 筆測試假錯誤，而那個檔正是 dashboard_alerts
#      「外部 API 錯誤率」紅線的分子。
#   ② Cloud Run 設 RED_RUNTIME_DIR=/var/lib/red，成本紀錄會落在 image 內的
#      var/ 而不是掛載的 runtime dir。
# RED_RUNTIME_DIR 沒設時（launchd 佈署）解析結果與舊值逐字相同，不需搬檔。
def _get_cost_log_path() -> str:
    try:
        from agent_core.logging_and_paths import DATA_DIR
        return os.path.join(DATA_DIR, "cost", "cost.jsonl")
    except Exception:
        return os.path.expanduser("~/RED/var/data/cost/cost.jsonl")


_COST_LOG = _get_cost_log_path()
# 外部 API 失敗事件 log（sibling of cost.jsonl）。成功呼叫記在 cost.jsonl，
# 失敗記在這裡 → dashboard_alerts 的外部 API 錯誤率紅線 = 失敗/(失敗+成功)。
_API_ERROR_LOG = os.path.join(os.path.dirname(_COST_LOG), "api_errors.jsonl")
_LOG_LOCK = Lock()
_API_ERR_LOCK = Lock()
_PG_COST_WARNING_UNTIL = 0.0


# Gemini 2.5 / 3 系列 pricing（**USD** per 1M tokens）
# 有調整時在這裡改
#
# 🚨 2026-08-05：整張表重寫成真正的美元。上一版（08-01 #328）的數字其實是
# **新台幣** —— 該帳單帳戶 currencyCode="TWD"（實查 Cloud Billing API
# billingAccounts/014C03-F57C23-F810B0），校正時把帳單上的 TWD 金額當成 USD
# 存進了名為 cost_usd 的欄位，於是每一筆都被放大約 32 倍（TWD/USD 匯率）。
# 當時「各 SKU 實收 ≈ 表訂 32×」那個怎麼看都怪的觀察，答案就是匯率本身。
#
# 本版數字**不用匯率換算**，直接取 GCP Cloud Billing Catalog API 的官方 USD
# 牌價（services/AEFD-7695-64FA「Gemini API」，2026-08-05 實查 564 條 SKU），
# 免得匯率漂移又製造第二次偏差。對照（同一條 SKU 兩種幣別）：
#   Generate content input  token count gemini 3.6 flash text   1.50 USD/M = 48.56 TWD/M（舊表 47.80）
#   Generate content output token count gemini 3.6 flash text   7.50 USD/M = 242.82 TWD/M（舊表 239.00）
#   EmbedContent input token count for gemini-embedding-001     0.15 USD/M = 4.86 TWD/M（舊表 4.75）
# 三條全部對得上 TWD、對不上 USD ⇒ 幣別誤標實錘。
#
# ⚠️ 順帶修好的第二類錯：舊表裡「帳單未見；表訂」那幾條用的是**過期的**牌價，
# 跟幣別無關但一樣低估（2.5-flash 0.075/0.30 vs 官方 0.30/2.50；
# 2.5-flash-lite 0.025/0.10 vs 官方 0.10/0.40）。等於舊表是 TWD 與 USD 混在
# 一起、還混新舊牌價。本版全部改由 Catalog API 對齊、逐條註明出處。
#
# 📌 標準層（非 batch / flex / priority）。cached input 另有官方折扣，見下方
# _CACHED_INPUT_MULTIPLIER。2.5-pro 取 short-input 檔（≤200k），長輸入檔是
# 2.50/15.00，本 fleet 幾乎不會踩到。
_PRICING = {
    # (model_prefix, input_per_M, output_per_M)  — 全部 USD
    "gemini-3-pro":          (1.25, 10.00),   # Catalog 無 3-pro text 條目；沿用表訂
    "gemini-3.6-flash":      (1.50, 7.50),    # Catalog: gemini 3.6 flash text
    "gemini-flash-latest":   (1.50, 7.50),    # fleet 別名；結在 3.6 flash SKU
    "gemini-3.1-flash":      (0.25, 1.50),    # Catalog: gemini 3.1 flash lite preview text
    "gemini-3-flash":        (1.50, 7.50),    # 未列；比照同代 3.6 flash（別再掉回舊表訂）
    "gemini-2.5-pro":        (1.25, 10.00),   # Catalog: Gemini 2.5 Pro short input/output text
    "gemini-2.5-flash-lite": (0.10, 0.40),    # Catalog: gemini 2.5 flash lite text
    "gemini-2.5-flash":      (0.30, 2.50),    # Catalog: gemini 2.5 flash text
    "gemini-2.0-flash":      (0.10, 0.40),    # Catalog 只有 batch/bidi 條目；取同級 lite 價
    "gemini-embedding":      (0.15, 0.0),     # Catalog: EmbedContent gemini-embedding-001
    # 生圖 SKU（Catalog 的 image output 條目，per-token equivalent）：
    #   nano-banana-pro / Gemini 3 Pro Image：Generate_content image output 120.00/M
    #   gemini-2.5-flash-image：Generate_content image output 30.00/M
    #   ⚠️ flash-image 這條非留不可：沒有它會被前綴 "gemini-2.5-flash" 撈走
    #     （2.50/M output），一張圖記成幾乎 0，等於還是沒記到。
    "gemini-2.5-flash-image": (0.30, 30.00),
    "nano-banana":           (1.00, 120.00),
    "imagen-4":              (0.0, 40.0),     # Imagen: $0.04/image ≈ 40/M tokens equivalent
}

# 舊 TWD 口徑 → 新 USD 口徑的換算比（48.56 TWD/M ÷ 1.50 USD/M，同一條 SKU）。
# 只用來把「當初以 TWD 訂下的門檻」換算成等效美元、以及解讀歷史資料；計價本身
# 走上面的官方 USD 牌價，不經過這個數字。
_TWD_PER_USD_AT_CUTOVER = 32.37


# 計價紀元版本。**改 _PRICING 或 _CACHED_INPUT_MULTIPLIER 的語意時就 +1。**
#
# 為什麼需要這個欄位：`cost_usd` 是寫入當下用當時的牌價算好、存進 JSONL 的
# **衍生值**，而牌價至今被改對過三次，等於同一個檔案裡混著三種口徑：
#   ver 1（~2026-07-31）：過期表訂價、cached token 全額計價 → 低估
#   ver 2（08-01 #328 ~ 08-05 10:50:26）：數字其實是**新台幣** → 高估約 32×
#   ver 3（08-05 #353 起）：官方 USD 牌價 + cached 0.1 折
# 沒有版本標記時，任何跨紀元的加總都是把台幣和美元相加，數字沒有意義。
# 2026-08-05 10:50 那次假的「成本暴衝」告警就是這樣來的：redeploy 後的
# process 拿到新的 USD 門檻（$37），卻去加總當天 911 筆還是 ver 2 台幣口徑的
# 列，得到 US$77.83 就報紅了——換算回美元其實只有 $2.40。
#
# 有了這個欄位，_load_entries() 就能只對「不是當前版本」的列從 raw token 重算，
# 當前版本的列直接信任、不必重算。
_PRICING_VERSION = 3


@functools.lru_cache(maxsize=256)
def _price_for_model(model: str) -> tuple:
    """挑最匹配的 pricing 記錄。

    未知別名（如 gemini-flash-latest / gemini-pro-latest，前綴對不上任何
    具體版本）改用「家族」fallback：含 flash → flash 價、含 pro → pro 價，
    才不會像舊版那樣一律掉到最便宜的 flash-lite、把背景 fleet 的花費
    低估數倍、連帶月度上限預警失準。

    ⚠️ 有 lru_cache：`_normalize_entry_costs` 會對整個月的歷史列逐列查價
    （month_to_date_usd 一次可達 10 萬列），沒快取的話每列都要 sorted() 整張
    _PRICING。測試裡若 monkeypatch `_PRICING`，記得 `_price_for_model.cache_clear()`。
    """
    m = (model or "").lower()
    # 最長前綴優先
    matches = sorted(
        [(prefix, rates) for prefix, rates in _PRICING.items() if prefix in m],
        key=lambda x: -len(x[0]),
    )
    if matches:
        return matches[0][1]
    # 家族 fallback（flash-lite 要在 flash 之前判斷，因為它也含 "flash"）
    if "flash-lite" in m:
        return _PRICING["gemini-2.5-flash-lite"]
    if "pro" in m:
        return _PRICING["gemini-2.5-pro"]
    if "flash" in m:
        # 未知 flash 別名多半解析到最新版：帳單實錘 gemini-flash-latest 結在
        # 3.6 flash SKU，用實收價，別再掉回表訂 2.5-flash 低估數百倍。
        return _PRICING["gemini-3.6-flash"]
    return _PRICING["gemini-2.5-flash-lite"]  # 真正未知才用最保守的 lite


# 快取命中的 input token 要用幾折計價（1.0 = 全額，等同無折扣）。
#
# 預設 0.10 —— **不是猜的、也不是「業界常見值」，是 GCP Cloud Billing Catalog
# API 上同一個模型兩條 SKU 的比值**（2026-08-04 用 owner@ 的 token 實查
# services/AEFD-7695-64FA「Gemini API」）：
#   Generate content input token count gemini 3.6 flash text          48.56 TWD/M
#   Generate content cached input token count gemini 3.6 flash text    4.86 TWD/M
#   → 4.86 / 48.56 = 0.1001，正好十分之一。
#
# 也就是說 _PRICING 裡的 47.80 就是**未快取**那條 SKU 的價，折扣完全沒有被
# 吸收進去 —— 帳本一直把 cached token 以全額計價，對「對話類 caller」是純粹
# 高估（embedding 0% cached 不受影響，所以「telegram_chat 最貴」這個排名本身
# 就是被這個 bug 撐出來的）。
# 影響量級（實測 8/3-8/4 全部 telegram_chat）：432 萬 prompt tokens 其中
# 331 萬（77%）是 cached；套 0.10 後那兩天的對話成本從 222.90 降到 80.51。
#
# ⚠️ 另一個**尚未在本 PR 修**的獨立錯誤：上面整張 _PRICING 的數字其實是
# **新台幣**。該帳單帳戶 currencyCode=TWD（實查 billingAccounts/
# 014C03-F57C23-F810B0），08-01 校正時把帳單上的 TWD 金額當成 USD 存進了名為
# cost_usd 的欄位。官方牌價對照：3.6-flash text input 1.50 USD/M = 48.56 TWD/M
# （表內 47.80）、output 7.50 USD/M = 242.82 TWD/M（表內 239.00）、
# embedding-001 0.15 USD/M = 4.86 TWD/M（表內 4.75）—— 三條都對得上 TWD。
# 所以帳本數字要除以 ~32.37 才是美元。修這個要連同所有門檻（cost_alert
# 700/日、dashboard warn/crit 800/1200、月 cap 25000）一起換算，是獨立的一刀。
_CACHED_INPUT_MULTIPLIER = env_float(
    "RED_COST_CACHED_INPUT_MULTIPLIER", 0.10, min_value=0.0, max_value=1.0
)


def compute_cost_usd(
    model: str,
    prompt_tokens: int,
    output_tokens: int,
    cached_tokens: int = 0,
) -> float:
    """估算一次 call 的 USD 成本。

    cached_tokens：prompt_tokens 之中命中快取的部分，依
    `RED_COST_CACHED_INPUT_MULTIPLIER` 打折（預設 1.0＝與舊行為逐位元相同）。
    傳 0 / 不傳 → 完全等同舊簽名，既有呼叫端不受影響。
    """
    input_rate, output_rate = _price_for_model(model)
    prompt_tokens = max(0, int(prompt_tokens or 0))
    # cached 夾在 [0, prompt]：API 偶爾回報 cached > prompt（或負值）時
    # 不能讓 fresh 變負數把成本算成負的。
    cached = max(0, min(int(cached_tokens or 0), prompt_tokens))
    fresh = prompt_tokens - cached
    cost = (
        (fresh / 1_000_000) * input_rate
        + (cached / 1_000_000) * input_rate * _CACHED_INPUT_MULTIPLIER
        + (max(0, int(output_tokens or 0)) / 1_000_000) * output_rate
    )
    return round(cost, 6)


def _api_key_fingerprint() -> str:
    """目前這把 Gemini key 的短指紋，取不到就回空字串。

    **lazy import**：`gemini_client` 在模組層 import 本檔（record_call），頂層
    反向 import 會變成循環相依。記帳是 best-effort，取指紋失敗絕不能拖垮呼叫。
    """
    try:
        from agent_core.gemini_client import api_key_fingerprint

        return api_key_fingerprint()
    except Exception:
        return ""


def _warn_pg_cost_fallback(exc: Exception) -> None:
    global _PG_COST_WARNING_UNTIL
    now = time.monotonic()
    if now < _PG_COST_WARNING_UNTIL:
        return
    _PG_COST_WARNING_UNTIL = now + 30
    logger.warning("Postgres cost tracker failed; falling back to JSONL: %s", exc)


def _pg_cost_store():
    try:
        from agent_core import operational_cost_tracker as store

        if store.enabled():
            return store
    except Exception as exc:  # noqa: BLE001 - accounting must stay best-effort
        _warn_pg_cost_fallback(exc)
    return None


def _infer_caller_tool() -> str:
    """從 call stack 推哪個 tool 引發這次 Gemini 呼叫。

    以「模組」為單位跳過 cost_tracker / gemini_client 自己的 frame。
    歷史教訓：舊版用函式名 skip 清單 ("_gemini_generate", "record_call")，
    2026-06-25 把 _gemini_generate 拆出 _gemini_generate_once 時清單沒同步，
    所有未顯式傳 caller 的呼叫全被記成 gemini_client._gemini_generate_once
    （live cost.jsonl 實錘 119/200 筆）。模組級 skip 之後再改名也不會斷。"""
    import sys
    try:
        frame = sys._getframe(2)  # 跳 1 層到 record_call 的 caller
        for _ in range(15):
            if frame is None:
                break
            mod = frame.f_globals.get("__name__", "?")
            fn = frame.f_code.co_name
            if (mod == "agent_core.gemini_client"
                    or mod.startswith("agent_core.cost_tracker")):
                frame = frame.f_back
                continue
            return f"{mod.rsplit('.',1)[-1]}.{fn}"
    except Exception:
        pass
    return "?"


def record_call(model: str, usage_metadata: Any, duration_ms: float = 0.0,
                caller: str = "") -> None:
    """記錄一次 Gemini call。失敗不拖累主流程。"""
    if usage_metadata is None:
        return
    try:
        prompt = int(usage_metadata.prompt_token_count or 0)
        output = int(usage_metadata.candidates_token_count or 0)
        cached = int(usage_metadata.cached_content_token_count or 0)
        thinking = int(getattr(usage_metadata, "thoughts_token_count", 0) or 0)
        tool_prompt = int(getattr(usage_metadata, "tool_use_prompt_token_count", 0) or 0)
        total = int(usage_metadata.total_token_count or 0)
    except Exception as e:
        logger.debug("cost_tracker 解析 usage_metadata 失敗：%s", e)
        return

    # cached 一併傳進去：預設係數 1.0 時結果與舊版逐位元相同，係數一改就
    # 立刻對所有新記錄生效（歷史行不會回溯，跨期比較要記得這個斷層）。
    cost = compute_cost_usd(model, prompt, output + thinking, cached_tokens=cached)
    entry = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "model": model,
        "prompt_tokens": prompt,
        "output_tokens": output,
        "thinking_tokens": thinking,
        "cached_tokens": cached,
        "tool_use_tokens": tool_prompt,
        "total_tokens": total,
        "cost_usd": cost,
        "pricing_ver": _PRICING_VERSION,  # 讀取端據此判斷要不要重算，見該常數註解
        "key_fp": _api_key_fingerprint(),  # 哪一把 key 打的（雜湊，非金鑰本身）
        "duration_ms": round(duration_ms, 1),
        "caller": caller or _infer_caller_tool(),
    }

    store = _pg_cost_store()
    if store is not None:
        try:
            store.write_cost_entry(entry)
            return
        except Exception as e:  # noqa: BLE001 - fall back to local accounting
            _warn_pg_cost_fallback(e)

    try:
        os.makedirs(os.path.dirname(_COST_LOG), exist_ok=True)
        with _LOG_LOCK:
            with open(_COST_LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.debug("cost_tracker 寫 log 失敗：%s", e)


# gemini-embedding-001 每筆輸入的 token 截斷上限；超過的部分不會處理也不計費，
# 估算同樣封頂，避免長文高估。
_EMBED_MAX_TOKENS_PER_INPUT = 2048

# 每個字元折算幾個 token。**這兩個數字是量出來的，不是猜的**，改之前請照下面的
# 方法重量一次。
#
# 2026-08-12 校準：拿 `client.models.count_tokens(model="gemini-embedding-001")`
# 當真值（免費、就是斷詞本身），對 ChromaDB 裡的**真實語料**抽樣 —— drive_docs_768
# / gmail_threads_768 / google_chat_messages_768 / operation_sops_768 各 120 篇、
# 每 10 篇一批共 48 批，對 actual ≈ A×CJK字 + B×其他字 做最小平方擬合：
#
#     A（CJK） = 0.821    B（其他） = 0.501  → 1 token ≈ 2.0 個非 CJK 字元
#
# 用三組**獨立**種子（不是擬合用的那批）驗收，看的是總量而非單批 —— 記帳要的是
# 加總對，不是每批都準：
#
#     seed  99 / 123 / 555   舊公式 估/實 0.562 / 0.565 / 0.557（穩定低估 1.8×）
#                            新公式 估/實 1.021 / 1.010 / 1.017（±2% 以內）
#
# ⚠️ 單批誤差仍有 ~23%（擬合當下 17%，out-of-sample 退到 23%＝有一點過擬合），
# 而且不同 collection 方向不同（drive 偏低估、gmail/chat 偏高估）。一組全域係數
# 抓不住這個差異，但**總量**才是計價要的，那一項三組獨立樣本都落在 ±2%。
#
# 舊公式（CJK 1.0／其他 0.25，即 4 字元 1 token）錯在它的前提：docstring 寫
# 「語料以 zh-TW + 英文混排為主」，但實際語料**只有 4–6% 是 CJK 字元**
# （drive 4.0%、gmail 6.2%、chat 6.3%；只有 SOP 那個小 collection 是 52%）。
# 剩下九成多不是乾淨英文散文，而是料號、單號、數字、表格、越南文帶聲調符號 ——
# 這些的斷詞密度遠高於「4 字元 1 token」，實測 drive 1.51、gmail 2.26 字元/token。
# 結果是整體**低估**：drive 估/實 0.424、gmail 0.683。
#
# 獨立佐證：GCP Cloud Monitoring 的 embed token quota 用量（08-03→08-10 視窗，
# 記得 paid_tier 與 paid_tier_3 兩個桶都要加）顯示帳本低估約 2.19×，與這裡量到
# 的同向、量級相符（差額來自那週 drive 佔比特別高）。
#
# ⚠️ 這一改會讓帳本的 embedding token 數（與成本）較舊列**跳升約 1.8 倍**。那是
# 修正不是暴衝：舊列少記。以目前水位（embedding 約佔日成本 15%、日燒 ~US$9）
# 換算，日成本會多記約 US$1.3，離 crit 門檻 US$37.5 還很遠，不會誤觸告警。
#
# 🚨 **下次要動這兩個係數的人請先看這裡。** 改係數會在帳本留下一個永久的階梯，
# 而且**不可回溯**——舊列存的 token 是用舊係數估的，`_normalize_entry_costs`
# 只能重算價錢、不能重算 token（詳見該函式 docstring 的警告）。後果：
#   1. 接下來 7 天，`dashboard_alerts._check_cost` 的 `today / 7d_avg` 跨在階梯
#      上，ratio 告警**不可信**，別急著追。
#   2. 跨越改動日的任何 embedding token/成本比較都要註明口徑，不能直接對比。
# 2026-08-14 評估過要不要加機制自動處理（例如用版本號切窗），結論是不加：計價表
# 那半邊已由 _PRICING_VERSION + _normalize_entry_costs 完整處理，剩下的估算這半
# 邊實測沒有觸發過（當時 ratio 0.49×、warn 門檻 2.5×），不值得為它加一層機制。
_EMBED_TOKENS_PER_CJK_CHAR = 0.82
_EMBED_TOKENS_PER_OTHER_CHAR = 0.50


def estimate_embed_tokens(texts: Any) -> int:
    """粗估一批 embedding 輸入的 token 總數（給 record_embed_call 用）。

    為什麼要估：公開 Gemini API 的 EmbedContentResponse **不帶任何 usage/token
    數**（ContentEmbedding.statistics 與 metadata.billable_character_count 都是
    Enterprise/Vertex 平台限定），但帳單照 input tokens 收錢——2026-08 稽核實錘
    embedding 單月 27.4 億 tokens、$12,929 完全沒進帳。估比不記好。

    啟發式：CJK（中日韓表意/假名/全形）與其他字元各自乘上實測係數（見
    `_EMBED_TOKENS_PER_CJK_CHAR` / `_EMBED_TOKENS_PER_OTHER_CHAR`，那裡寫了
    係數怎麼量出來的），每筆封頂 `_EMBED_MAX_TOKENS_PER_INPUT`。殘餘誤差約
    17%，量級對帳足夠；精確對帳仍以 GCP 帳單為準。

    ⚠️ 想要精確值時可以直接打 `count_tokens`（免費、就是真值），但那等於每批
    embedding 多一次 API round-trip 與一份 rate limit 配額，背景夜跑扛不住 ——
    所以這裡用離線校準過的啟發式，不在熱路徑上打那一槍。
    """
    if isinstance(texts, str):
        texts = [texts]
    total = 0
    for t in texts or []:
        s = t if isinstance(t, str) else str(t)
        if not s:
            continue
        cjk = 0
        other = 0
        for ch in s:
            o = ord(ch)
            if (0x2E80 <= o <= 0x9FFF or 0xF900 <= o <= 0xFAFF
                    or 0xFF00 <= o <= 0xFFEF or 0x20000 <= o <= 0x2FA1F):
                cjk += 1
            else:
                other += 1
        est = int(cjk * _EMBED_TOKENS_PER_CJK_CHAR
                  + other * _EMBED_TOKENS_PER_OTHER_CHAR + 0.5)
        total += min(max(est, 1), _EMBED_MAX_TOKENS_PER_INPUT)
    return total


def record_embed_call(model: str, texts: Any, duration_ms: float = 0.0,
                      caller: str = "") -> None:
    """記錄一次 embedding 呼叫（走既有 record_call，同一份 cost.jsonl 帳本）。

    2026-08-01 稽核前 vector_store / memory 的 embed_content 完全沒記帳，
    月燒最大宗（embedding $8k–13k/月）在 cost_today / month_to_date_usd 全隱形。
    token 數用 estimate_embed_tokens 本地估（API 回應不帶 usage，見該函式
    docstring）；全記在 prompt_tokens、output=0。失敗不拖累主流程。
    """
    try:
        tokens = estimate_embed_tokens(texts)
        if tokens <= 0:
            return
        usage = SimpleNamespace(
            prompt_token_count=tokens,
            candidates_token_count=0,
            cached_content_token_count=0,
            total_token_count=tokens,
        )
        record_call(model=model, usage_metadata=usage,
                    duration_ms=duration_ms, caller=caller)
    except Exception as e:
        logger.debug("record_embed_call 失敗：%s", e)


def record_api_error(service: str, status: str = "", *, model: str = "",
                     requested_model: str = "", detail: str = "") -> None:
    """記錄一次外部 API「最終失敗」（給 dashboard_alerts 的外部 API 錯誤率紅線用）。

    成功呼叫由 record_call 寫 cost.jsonl；失敗沒有 usage_metadata，改記這裡。
    _gemini_generate 只在最終放棄（不可重試 / retries 用盡）時呼叫一次，所以
    「錯誤率」＝真實任務失敗率，retry 後成功的暫時性 503 不計入。失敗不拖累主流程。

    `model` 要填**解析後**的具體型號，與 cost.jsonl 的成功列同一個口徑，
    by_model 才可比（呼叫端 `_record_gemini_api_error` 會查別名對照表）；查不到
    對照時退回請求別名。原始別名放 `requested_model`，只在兩者不同時才寫，
    留給事後追查用。
    """
    entry = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "service": service or "?",
        "status": str(status or "")[:40],
        "model": model or "",
        "detail": (detail or "")[:200],
    }
    if requested_model and requested_model != (model or ""):
        entry["requested_model"] = requested_model
    store = _pg_cost_store()
    if store is not None:
        try:
            store.write_api_error(entry)
            return
        except Exception as e:  # noqa: BLE001 - fall back to local accounting
            _warn_pg_cost_fallback(e)

    try:
        os.makedirs(os.path.dirname(_API_ERROR_LOG), exist_ok=True)
        with _API_ERR_LOCK:
            with open(_API_ERROR_LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.debug("record_api_error 寫 log 失敗：%s", e)


def record_chat_response(resp: Any, *, caller: str = "") -> None:
    """Best-effort cost logging for the interactive chat path.

    chat.send_message() goes through the SDK's internal generate_content and
    therefore bypasses _gemini_generate's record_call — so without this the
    most expensive calls (full ~300-tool schema + multimodal attachments)
    never reach cost tracking, and month_to_date_usd / the monthly-cap alert
    stay quiet while real spend climbs. Never raises (accounting must not
    break a reply).

    Note: usage_metadata reflects the FINAL response; with automatic function
    calling the intermediate tool rounds may be under-counted — still far
    better than recording nothing.
    """
    try:
        um = getattr(resp, "usage_metadata", None)
        if um is None:
            return
        model = getattr(resp, "model_version", "") or ""
        if not model:
            try:
                from agent_core.gemini_client import GEMINI_MODEL
                model = GEMINI_MODEL
            except Exception:
                model = "gemini-2.5-pro"
        record_call(model=model, usage_metadata=um, caller=caller or "chat.send_message")
    except Exception:
        pass


# ────────────────────────────────────────────────────────────────────
# 查詢 tools — 暴露給 agent
# ────────────────────────────────────────────────────────────────────
def _entry_ts(e: dict) -> datetime | None:
    """解析一筆 cost entry 的 ts；壞行回 None。

    _load_jsonl_window 刻意保留無法解析 ts 的行（與舊前向讀行為一致），
    所以下游任何 `datetime.fromisoformat(e["ts"])` 裸呼叫都會被單獨一行
    損毀資料打炸 — cost_alert 跑在 daemon 裡就是持續紅。全部走這個 helper
    （對齊 month_to_date_usd 的 try/except continue 寫法）。"""
    try:
        return datetime.fromisoformat(e.get("ts", ""))
    except Exception:
        return None


def _read_lines_reverse(path: str, block_size: int = 65536):
    """Yield complete lines of a text file newest-first, without loading it all.
    Lets a bounded-window reader stop early on an append-ordered log instead of
    scanning the whole (multi-MB) history. Bytes are reassembled at line
    boundaries via `tail`, so multi-byte UTF-8 chars are never split."""
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        pos = f.tell()
        tail = b""
        while pos > 0:
            read = min(block_size, pos)
            pos -= read
            f.seek(pos)
            buf = f.read(read) + tail
            parts = buf.split(b"\n")
            tail = parts[0]  # possibly-incomplete first line; carry into next block
            for part in reversed(parts[1:]):
                yield part.decode("utf-8", "replace")
        if tail:
            yield tail.decode("utf-8", "replace")


def _load_jsonl_window(path: str, hours: int | None, *, grace_min: int = 10) -> list:
    """讀 append-ordered JSONL log。hours=None → 全量前向讀；給定 hours → 反向讀、
    一過 cutoff（含 grace 分鐘容忍些微亂序）就停，把掃描收斂到視窗而非整個歷史。
    （健檢 Medium：cost.jsonl 25MB/89K 行被 alert daemon 每 5 分鐘全掃 3-4 次。）"""
    if not os.path.isfile(path):
        return []
    if hours is None:
        out = []
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        out.append(json.loads(line))
                    except (ValueError, TypeError):
                        continue
        except Exception as exc:
            logger.warning("讀 %s 失敗：%s", path, exc)
        return out
    cutoff = datetime.now() - timedelta(hours=hours)
    stop_before = cutoff - timedelta(minutes=grace_min)
    out = []
    try:
        for line in _read_lines_reverse(path):
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except (ValueError, TypeError):
                continue
            try:
                t = datetime.fromisoformat(e.get("ts", ""))
            except Exception:
                out.append(e)  # 無法解析 ts → 保留（與舊前向行為一致）
                continue
            if t < stop_before:
                break  # append-ordered：更早的只會更舊 → 停止掃描
            if t >= cutoff:
                out.append(e)
    except Exception as exc:
        logger.warning("讀 %s 失敗：%s", path, exc)
    out.reverse()  # 還原時間順序（與舊前向讀一致）
    return out


def _normalize_entry_costs(entries: list) -> list:
    """把非當前計價紀元的列，用 raw token 重算 `cost_usd`，就地覆蓋。

    為什麼在讀取端做、而不是回填 cost.jsonl：
      1. **併發安全。** 寫入是 `open(_COST_LOG, "a")` 每次開關檔，只有 process
         內的 _LOG_LOCK，沒有跨 process 鎖；6+ 支 daemon 同時 append。rename +
         重寫有資料遺失窗口，而 RAG 夜跑一輪 7–15h，也沒有乾淨的停機窗。
      2. **這裡是唯一咽喉點。** cost_today / cost_last_7_days / cost_by_tool /
         cost_stats / month_to_date_usd，以及 dashboard_alerts._recent_cost_usd
         全部走 _load_entries，改一處就全部一致（含 Postgres backend 那條分支）。
      3. **raw token 才是真相。** cost_usd 是衍生值且三個紀元都算錯過，
         prompt/output/thinking/cached 則逐列完整保存 → 永遠可以重算回來。

    重算不動檔案，純記憶體。缺 token 欄位或算出 0 的列保留原值（寧可沿用舊數字
    也不要把它變成 0 而低報）。

    ⚠️ **第 3 點對 embedding 的列不成立。** 那些列的 `prompt_tokens` 不是 API 回
    傳的真值，是 `estimate_embed_tokens` 本地估的（embedding API 不回 usage）。
    所以「改係數」跟「改計價表」的可回溯性完全不同：

      改 _PRICING（計價表）    → bump _PRICING_VERSION → 這裡拿舊列的 raw token
                                重算，歷史自動對齊，**沒有紀元混算問題**
      改 estimate_embed_tokens → 舊列存的 token 數本身就是用舊係數算的，**無法**
        的係數                    回溯修正；歷史帳會永久停在舊口徑

    後者的實際後果：係數一改，帳本的 embedding token 數會出現一個階梯（#388 是
    1.8×），而 `dashboard_alerts._check_cost` 的 `today / 7d_avg` 在接下來 7 天會
    跨在那個階梯上。2026-08-14 實測那次沒有觸發（embedding 只佔日成本一部分，
    當時 ratio 0.49×、離 warn 門檻 2.5× 很遠），所以**刻意沒有為它加機制**——
    真要再改係數，記得那 7 天的 ratio 告警要當作不可信，別急著追。
    """
    for e in entries:
        try:
            if e.get("pricing_ver") == _PRICING_VERSION:
                continue
            prompt = int(e.get("prompt_tokens") or 0)
            output = int(e.get("output_tokens") or 0)
            thinking = int(e.get("thinking_tokens") or 0)
            if prompt <= 0 and output <= 0 and thinking <= 0:
                continue  # 沒有 raw token 可依據 → 不動它
            fixed = compute_cost_usd(
                e.get("model", ""), prompt, output + thinking,
                cached_tokens=int(e.get("cached_tokens") or 0),
            )
            if fixed > 0:
                e["cost_usd"] = fixed
        except (ValueError, TypeError):
            continue  # 單列壞掉不能拖垮整份帳
    return entries


def _load_entries(hours: int | None = None):
    """讀 cost log，可選只取最近 N 小時（bounded window 走反向早停）。

    回傳前一律過 `_normalize_entry_costs`，確保跨紀元的加總是同一個幣別/口徑。
    """
    store = _pg_cost_store()
    if store is not None:
        try:
            return _normalize_entry_costs(store.load_cost_entries(hours=hours))
        except Exception as e:  # noqa: BLE001 - fall back to local accounting
            _warn_pg_cost_fallback(e)
    return _normalize_entry_costs(_load_jsonl_window(_COST_LOG, hours))


def _load_api_errors(hours: int | None = None) -> list:
    """讀 api_errors log，可選只取最近 N 小時（bounded window 走反向早停）。"""
    store = _pg_cost_store()
    if store is not None:
        try:
            return store.load_api_errors(hours=hours)
        except Exception as e:  # noqa: BLE001 - fall back to local accounting
            _warn_pg_cost_fallback(e)
    return _load_jsonl_window(_API_ERROR_LOG, hours)


def _is_embedding_entry(entry: dict) -> bool:
    """這列 cost.jsonl 是不是 embedding 呼叫。

    用 model 名判定（gemini-embedding-001 / text-embedding-*）而不是 caller：
    caller（vector_store.embed_document…）會隨模組改名漂移，model 是計價欄位、
    穩定得多。
    """
    return "embed" in str(entry.get("model") or "").lower()


def api_error_stats(hours: float = 6.0) -> dict:
    """最近 N 小時的外部 API 錯誤統計（目前全量＝Gemini：cost.jsonl 與
    api_errors.jsonl 唯一來源都是 Gemini）。

    error_rate_pct = errors / (errors + successes)。errors 是「最終失敗」（retries
    用盡或不可重試），不含 retry 後成功的暫時性錯誤；successes 取自 cost.jsonl
    每次成功 call。給 dashboard_alerts 的外部 API 錯誤率紅線用。

    🔑 **分母只算生成路徑，embedding 列一律排除**（2026-08-14 假警報普查 E）：
    分子只有 `gemini_client._gemini_generate` 最終放棄時會寫（實測 30 天 476 筆
    錯誤，model 全是生成模型、**零筆 embedding**），分母卻是 cost.jsonl 全部列
    —— 而 embedding 佔絕大多數：實測 24h 949 筆裡 718 筆（75.7%）是 embedding，
    錯誤率被稀釋 4.1×（7 天 3.1×）。夜跑背填那種日子更極端（2026-08-02 是
    10,774 embedding vs 133 生成 = 82×），生成路徑整條掛掉也稀釋到看不見。
    這是**漏報**方向的病：不修的話門檻形同虛設。
    """
    h = max(1, int(round(hours)))
    # 分子也只收生成路徑：embedding 的最終失敗記在 service="gemini_embed"
    # （vector_store._record_embed_api_error），有自己成對的分子分母，見
    # embed_error_stats。混進來就變成「分子含 embedding、分母不含」＝同一個母體
    # 錯配的鏡像版（改成高估）。歷史列都是 service="gemini"，不受影響。
    errs = [e for e in _load_api_errors(hours=h)
            if (e.get("service") or "gemini") == "gemini"]
    all_oks = _load_entries(hours=h)
    oks = [e for e in all_oks if not _is_embedding_entry(e)]
    n_err = len(errs)
    n_ok = len(oks)
    n_embedding = len(all_oks) - n_ok
    total = n_err + n_ok
    rate = (n_err / total * 100.0) if total else 0.0
    by_status: dict = {}
    by_model: dict = defaultdict(lambda: {"errors": 0, "successes": 0})
    for e in errs:
        k = e.get("status") or "?"
        by_status[k] = by_status.get(k, 0) + 1
        model = e.get("model") or "?"
        by_model[model]["errors"] += 1
    for e in oks:
        model = e.get("model") or "?"
        by_model[model]["successes"] += 1
    # 錯誤在時間上的分佈：整批擠在幾秒內 = 瞬時爆發，攤開 = 持續故障
    err_ts = []
    for e in errs:
        try:
            err_ts.append(datetime.fromisoformat(str(e.get("ts", ""))))
        except (ValueError, TypeError):
            continue
    span_sec = ((max(err_ts) - min(err_ts)).total_seconds()
                if len(err_ts) >= 2 else None)
    by_model_out = {}
    for model, counts in by_model.items():
        model_total = counts["errors"] + counts["successes"]
        model_rate = (counts["errors"] / model_total * 100.0) if model_total else 0.0
        by_model_out[model] = {
            "errors": counts["errors"],
            "successes": counts["successes"],
            "total": model_total,
            "error_rate_pct": round(model_rate, 1),
        }
    return {
        "window_hours": h,
        "errors": n_err,
        "successes": n_ok,
        "total": total,
        "error_rate_pct": round(rate, 1),
        "by_status": by_status,
        "by_model": by_model_out,
        # 排除在分母外的 embedding 成功數。留著是為了讓面板/診斷看得出「這個
        # 視窗的分母為什麼比 cost.jsonl 的列數少」，不是拿來算率的。
        "embedding_successes": n_embedding,
        # 第一筆到最後一筆錯誤的秒差。**持續故障**會攤在整個視窗上（span≈視窗長），
        # **瞬時爆發**（DNS 斷線、睡眠喚醒空窗）則整批擠在幾秒內 —— 兩者的處置
        # 完全不同，但只看「N 失敗 / M 呼叫 = X%」分不出來。None = 不足 2 筆。
        "error_span_sec": span_sec,
    }


def embed_error_stats(hours: float = 6.0) -> dict:
    """embedding 路徑自己的錯誤率（跟 api_error_stats 是兩條獨立的統計）。

    為什麼要分開算：embedding 走 `embed_content`、不經 `_gemini_generate`，跟生成
    路徑是兩套配額、兩套失敗模式（429 突發 / DSQ 403 / 傳輸逾時）。假警報普查 E
    已經把 embedding 的成功從生成路徑的分母剔掉，這裡把它們配上**自己的**分子：

        分子 = api_errors.jsonl 裡 service="gemini_embed" 的列
               （vector_store._record_embed_api_error，只在重試用盡／不可重試時記）
        分母 = cost.jsonl 裡 _is_embedding_entry 的列（每次成功 embed）

    為什麼值得單獨看：embedding 是最大宗 Gemini 消費者（實測某日 783 次呼叫裡
    625 次），而它失敗在此之前**完全沒有結構化紀錄** —— 只 print 到 rag_sync log。
    災難級（硬配額燒乾）會中止夜跑、由 daemon 健康紅線接手；但「持續失敗但沒到
    中止門檻」那一段是純盲區，只能事後從覆蓋率（rag_gap_report）反推。
    """
    h = max(1, int(round(hours)))
    errs = [e for e in _load_api_errors(hours=h)
            if (e.get("service") or "") == "gemini_embed"]
    oks = [e for e in _load_entries(hours=h) if _is_embedding_entry(e)]
    n_err, n_ok = len(errs), len(oks)
    total = n_err + n_ok
    by_status: dict = {}
    for e in errs:
        k = e.get("status") or "?"
        by_status[k] = by_status.get(k, 0) + 1
    return {
        "window_hours": h,
        "errors": n_err,
        "successes": n_ok,
        "total": total,
        "error_rate_pct": round((n_err / total * 100.0) if total else 0.0, 1),
        "by_status": by_status,
    }


def month_to_date_usd(now: datetime | None = None) -> float:
    """本日曆月（自當月 1 號 00:00 起）累計的 Gemini 花費（USD）。

    對應 Google AI Studio 的 *monthly spend cap*（撞到會回 429
    RESOURCE_EXHAUSTED）。給 dashboard_alerts 的月度上限預警用。

    Args:
        now: 測試可注入；預設取系統當下時間。
    """
    now = (now or datetime.now()).replace(tzinfo=None)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    # _load_entries filters by (system) datetime.now() - hours, so size the
    # window to actually reach month_start (≥32 days) — otherwise an injected
    # past `now`, or a run early on the 1st, could miss this month's entries.
    span_hours = int((datetime.now() - month_start).total_seconds() / 3600) + 1
    entries = _load_entries(hours=max(32 * 24, span_hours))
    total = 0.0
    for e in entries:
        try:
            ts = datetime.fromisoformat(e["ts"]).replace(tzinfo=None)
            if ts >= month_start:
                total += float(e.get("cost_usd") or 0.0)
        except (ValueError, TypeError):
            continue
    return total


def cost_today() -> str:
    """今天（自今日 00:00 起）的 Gemini 總花費、call 數、top 5 燒錢的 tool。"""
    now = datetime.now()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    hours_since_midnight = int((now - midnight).total_seconds() / 3600) + 1
    entries = _load_entries(hours=hours_since_midnight + 1)
    entries = [e for e in entries
               if (t := _entry_ts(e)) is not None and t >= midnight]

    if not entries:
        return "💰 今天還沒有任何 Gemini call（0 USD）"

    total_cost = sum(e["cost_usd"] for e in entries)
    total_prompt = sum(e["prompt_tokens"] for e in entries)
    total_output = sum(e["output_tokens"] for e in entries)
    n_calls = len(entries)

    by_tool = defaultdict(lambda: {"cost": 0.0, "calls": 0, "tokens": 0})
    by_model = defaultdict(lambda: {"cost": 0.0, "calls": 0})
    for e in entries:
        by_tool[e.get("caller", "?")]["cost"] += e["cost_usd"]
        by_tool[e.get("caller", "?")]["calls"] += 1
        by_tool[e.get("caller", "?")]["tokens"] += e["total_tokens"]
        by_model[e.get("model", "?")]["cost"] += e["cost_usd"]
        by_model[e.get("model", "?")]["calls"] += 1

    lines = [
        f"💰 今日 Gemini 花費（{now.strftime('%Y-%m-%d %H:%M')}）",
        "─" * 60,
        f"  總花費:     ${total_cost:.4f} USD",
        f"  呼叫次數:   {n_calls:,}",
        f"  prompt 總: {total_prompt:,} tokens",
        f"  output 總: {total_output:,} tokens",
        "",
        "📊 Top 5 最燒錢的 caller:",
    ]
    top_callers = sorted(by_tool.items(), key=lambda kv: -kv[1]["cost"])[:5]
    for caller, stats in top_callers:
        lines.append(
            f"  ${stats['cost']:.4f}  {caller:35s}  "
            f"({stats['calls']} calls, {stats['tokens']:,} tokens)"
        )

    lines.append("")
    lines.append("🤖 依模型分布:")
    for model, stats in sorted(by_model.items(), key=lambda kv: -kv[1]["cost"])[:5]:
        lines.append(f"  ${stats['cost']:.4f}  {model:40s}  ({stats['calls']} calls)")

    return "\n".join(lines)


def cost_last_7_days() -> str:
    """最近 7 天的 Gemini 花費（每日摘要 + 總計）。"""
    entries = _load_entries(hours=24 * 7)
    if not entries:
        return "💰 最近 7 天沒有任何 Gemini call"

    by_day: dict = defaultdict(lambda: {"cost": 0.0, "calls": 0, "tokens": 0})
    by_tool_overall = defaultdict(lambda: {"cost": 0.0, "calls": 0})
    for e in entries:
        day = e["ts"][:10]
        by_day[day]["cost"] += e["cost_usd"]
        by_day[day]["calls"] += 1
        by_day[day]["tokens"] += e["total_tokens"]
        by_tool_overall[e.get("caller", "?")]["cost"] += e["cost_usd"]
        by_tool_overall[e.get("caller", "?")]["calls"] += 1

    total_cost = sum(s["cost"] for s in by_day.values())
    total_calls = sum(s["calls"] for s in by_day.values())
    days_with_data = len(by_day)
    avg_daily = total_cost / max(1, days_with_data)

    lines = [
        "💰 最近 7 天 Gemini 花費",
        "─" * 60,
        f"  總花費:       ${total_cost:.4f} USD",
        f"  總呼叫:       {total_calls:,} calls",
        f"  日均:         ${avg_daily:.4f}（月化 ≈ ${avg_daily*30:.2f}）",
        "",
        "📅 逐日:",
    ]
    for day in sorted(by_day.keys(), reverse=True):
        s = by_day[day]
        bar_len = int((s["cost"] / max(s0["cost"] for s0 in by_day.values())) * 30) if max(s0["cost"] for s0 in by_day.values()) > 0 else 0
        lines.append(
            f"  {day}  ${s['cost']:7.4f}  "
            f"{s['calls']:4d} calls  "
            f"{'█' * bar_len}"
        )

    lines.append("")
    lines.append("🔥 最近 7 天 top 10 最燒錢的 caller:")
    top10 = sorted(by_tool_overall.items(), key=lambda kv: -kv[1]["cost"])[:10]
    for caller, stats in top10:
        lines.append(f"  ${stats['cost']:.4f}  {caller:35s}  ({stats['calls']} calls)")

    return "\n".join(lines)


def cost_by_tool(hours: int = 168, min_cost_usd: float = 0.001) -> str:
    """按呼叫 tool 彙總成本（哪個 tool 是預算殺手）。

    Args:
        hours: 看最近幾小時（預設 168 = 一週）。
        min_cost_usd: 低於這個成本的 tool 不列（避免洗版）。預設 0.001 USD。
    """
    entries = _load_entries(hours=hours)
    if not entries:
        return f"💰 最近 {hours}h 沒有資料"

    by_tool = defaultdict(lambda: {
        "cost": 0.0, "calls": 0, "prompt": 0, "output": 0, "avg_ms": []
    })
    for e in entries:
        k = e.get("caller", "?")
        by_tool[k]["cost"] += e["cost_usd"]
        by_tool[k]["calls"] += 1
        by_tool[k]["prompt"] += e["prompt_tokens"]
        by_tool[k]["output"] += e["output_tokens"]
        by_tool[k]["avg_ms"].append(e.get("duration_ms", 0))

    filtered = [(k, v) for k, v in by_tool.items() if v["cost"] >= min_cost_usd]
    filtered.sort(key=lambda kv: -kv[1]["cost"])

    if not filtered:
        return f"💰 最近 {hours}h 沒有 cost ≥ ${min_cost_usd} 的 call"

    lines = [f"💰 最近 {hours}h 按 tool 的成本（≥ ${min_cost_usd}）"]
    lines.append("─" * 70)
    lines.append(f"{'tool':<32s}  {'cost$':>9s}  {'calls':>7s}  {'avg_ms':>8s}  {'tokens':>10s}")
    for k, v in filtered[:30]:
        avg_ms = sum(v["avg_ms"]) / max(1, len(v["avg_ms"]))
        tokens = v["prompt"] + v["output"]
        lines.append(
            f"{k:<32s}  ${v['cost']:>8.4f}  {v['calls']:>7d}  {avg_ms:>7.0f}ms  {tokens:>10,d}"
        )
    return "\n".join(lines)


def cost_by_key(hours: int = 168) -> str:
    """按 API key 拆帳。多把 key 分屬不同 GCP project、帳單也照 project 出，
    這支就是本機端唯一能對上那個維度的視角。

    key 以 `key_fp`（sha256 前 8 碼）標示，不是金鑰本身。要知道某個指紋是哪一把，
    在該 key 生效的環境下呼叫 `gemini_client.api_key_fingerprint()` 比對即可。

    ⚠️ 本功能上線（#378，2026-08-12 部署）之前寫的列沒有 `key_fp` 欄位，會歸到
    "(未記錄)" —— 那段時間本機無從得知是哪把 key 打的，別把它當成「某把 key 沒
    用量」。歷史列補不回來：當初用哪把 key，本機沒有任何地方留下紀錄。
    """
    entries = _load_entries(hours=hours)
    if not entries:
        return f"💰 最近 {hours}h 沒有任何 Gemini call"

    by_key: dict = defaultdict(lambda: {"cost": 0.0, "calls": 0, "tokens": 0,
                                        "models": Counter(), "callers": Counter()})
    for e in entries:
        k = e.get("key_fp") or "(未記錄)"
        cell = by_key[k]
        cell["cost"] += e.get("cost_usd") or 0.0
        cell["calls"] += 1
        cell["tokens"] += e.get("total_tokens") or 0
        cell["models"][e.get("model", "?")] += e.get("cost_usd") or 0.0
        cell["callers"][e.get("caller", "?")] += e.get("cost_usd") or 0.0

    total = sum(v["cost"] for v in by_key.values())
    ordered = sorted(by_key.items(), key=lambda kv: -kv[1]["cost"])
    current = _api_key_fingerprint()

    lines = [f"💰 最近 {hours}h 按 API key 拆帳（總計 ${total:.4f} USD）", "─" * 68]
    for k, v in ordered:
        mark = "  ← 目前這把" if k and k == current else ""
        pct = 100 * v["cost"] / total if total else 0.0
        lines.append(f"🔑 {k}{mark}")
        lines.append(f"   ${v['cost']:.4f}（{pct:.1f}%）  {v['calls']:,} calls  {v['tokens']:,} tokens")
        top_m = "、".join(f"{m} ${c:.2f}" for m, c in v["models"].most_common(3))
        top_c = "、".join(f"{c} ${x:.2f}" for c, x in v["callers"].most_common(3))
        lines.append(f"   模型：{top_m}")
        lines.append(f"   大戶：{top_c}")
    if len(ordered) == 1 and ordered[0][0] == "(未記錄)":
        lines.append("")
        # 刻意**不寫死日期**：上一版寫了「2026-08-07」，但實際部署是 08-12，
        # 那個數字一寫就開始走鐘、還會誤導日後對帳的人。改成描述機制本身。
        lines.append(
            "ℹ️ 這段期間的列都沒有 key 指紋 —— 表示它們寫入時 key_fp 欄位還沒上線，"
            "不是「某把 key 沒用量」。歷史列補不回來。"
        )
    return "\n".join(lines)


def cost_alert(daily_budget_usd: float = 22.0) -> str:
    """檢查今天花費是否超出預算。給 daemon / 提醒用。

    Args:
        daily_budget_usd: 每日預算（USD）。預設 $22 — 🚨 2026-08-05 改用真美元
            刻度。上一版的 700 其實是**新台幣**（08-01 那次校正把帳單 TWD 金額
            當 USD 存進 _PRICING，門檻跟著訂在 TWD 刻度），700/32.37 ≈ US$21.6，
            這裡取 22 維持等效觸發點。實際日燒 ≈ US$13。
    """
    now = datetime.now()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    entries = _load_entries(hours=25)
    entries = [e for e in entries
               if (t := _entry_ts(e)) is not None and t >= midnight]
    today_cost = sum(e["cost_usd"] for e in entries)
    pct = (today_cost / daily_budget_usd) * 100 if daily_budget_usd > 0 else 0

    icon = "✅" if pct < 80 else ("⚠️" if pct < 100 else "🚨")
    lines = [
        f"{icon} 今日 Gemini 花費 vs 預算",
        f"  今日已花: ${today_cost:.4f}",
        f"  預算:     ${daily_budget_usd:.2f}",
        f"  使用率:   {pct:.1f}%",
    ]
    if pct >= 100:
        lines.append(
            "  🚨 **超出預算**！建議：\n"
            "    - 檢查 cost_by_tool() 找兇手\n"
            "    - 啟動 dry-run 避免進一步支出\n"
            "    - 關閉背景 ponder daemon 若不急用"
        )
    elif pct >= 80:
        lines.append("  ⚠️  接近預算上限，小心後續呼叫")
    return "\n".join(lines)


def cost_stats() -> str:
    """全量累計統計（從 cost.jsonl 第一天到現在）。"""
    entries = _load_entries(hours=None)  # 不限
    if not entries:
        return "💰 cost log 是空的。先跑幾次 Gemini 呼叫就會開始累積。"

    total_cost = sum(e["cost_usd"] for e in entries)
    total_calls = len(entries)
    total_prompt = sum(e["prompt_tokens"] for e in entries)
    total_output = sum(e["output_tokens"] for e in entries)

    # 日期範圍
    dates = sorted({e["ts"][:10] for e in entries})
    first, last = dates[0], dates[-1]

    # 模型分布
    by_model = Counter(e.get("model", "?") for e in entries)

    lines = [
        f"📊 累計 Gemini 使用 ({first} ~ {last}, {len(dates)} 天有資料)",
        "─" * 60,
        f"  總花費:     ${total_cost:.4f} USD",
        f"  總呼叫:     {total_calls:,}",
        f"  總 prompt:  {total_prompt:,} tokens",
        f"  總 output:  {total_output:,} tokens",
        f"  平均每 call: ${total_cost/max(1,total_calls):.5f} / "
        f"{(total_prompt+total_output)/max(1,total_calls):.0f} tokens",
        "",
        "🤖 模型分布:",
    ]
    for model, count in by_model.most_common(10):
        lines.append(f"  {count:6d}x  {model}")
    return "\n".join(lines)
