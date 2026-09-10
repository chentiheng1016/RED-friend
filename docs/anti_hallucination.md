# Anti-Hallucination Defense Stack

How 小紅 stops fabricating "facts" about customers, materials, BOMs, and
prices. Written for the next person to touch this code so they understand
which layer to extend instead of bolting on a sixth one.

---

## Why this exists — the Jalas 防水膜 incident

**2026-05-19/20.** 大王 asked "jalas這個客戶有用防水膜嗎". The bot:

1. Pulled Jalas-related email snippets via `recall()` — got back a mix of
   Jalas + 華峰 PU emails (real) plus an unrelated Sympatex stock report
   (Lurchi/Richter, ~0.42 cosine similarity).
2. Confidently synthesised "**Jalas 確實有在使用防水相關材料**, 主要材料是
   華峰 (Huafon) 的 PU468" — conflating PU coating (撥水, water-repellent)
   with waterproof membrane (Sympatex/Gore-Tex/eVent class).
3. When 大王 pushed back ("我查過 jalas 沒有用防水膜, 你再確認一下"), the
   bot flipped 180° with "**您的判斷是對的**" — **without re-querying**.

Two failure modes in one turn:
- **Fabrication**: synthesised a material spec from co-occurrence, not evidence.
- **Sycophancy**: capitulated to user pushback without verifying.

The defense stack below is engineered to catch both.

---

## The five layers, in the order they engage

Each layer is a separate module; they compose. When a turn fires multiple,
each line shows up in `var/logs/daemon-telegram.log` so the failure surface
is observable.

### Layer 1 — Inbound classifiers (per-turn prompt reinforcement)

**Files:** [`agent_core/correction_detector.py`](../agent_core/correction_detector.py),
[`agent_core/extractive_mode.py`](../agent_core/extractive_mode.py)

Run on the raw user message **before** anything else. They don't reject
the turn — they prepend a context block onto the wrapped envelope so the
LLM sees explicit guidance.

- **`detect_correction(user_text)`** — regex for four phrasings of "you're
  wrong": `user_independent_check` ("我查過 X 不是 Y"), `direct_error_
  assertion` ("你搞錯了"), `demand_recheck` ("再確認一下"), and
  `short_denial_with_fact` ("報價不對"). When matched, the daemon
  prepends the 反翻供守則 workflow steps so the model is told to
  re-query before conceding.

- **`is_fact_lookup(user_text)`** / **`extractive_addendum(user_text)`**
  — regex for pure factual-lookup questions ("X 用什麼", "報價多少", "X
  的 BOM", English equivalents). When matched, the daemon prepends a
  "quote tool output verbatim, never paraphrase your way to an answer"
  instruction. Triggers on the structural shape of the question, not its
  topic.

**Log signature when fired:**

```
[correction] 偵測到糾錯訊號：user_independent_check → 「我查過jalas沒有」
[extractive] 偵測到純查詢類問題 → 啟用萃取模式
```

### Layer 2 — Structured data sources

**Files:** [`agent_core/agents/orange_sales/bom.py`](../agent_core/agents/orange_sales/bom.py),
[`agent_core/email_lake.py`](../agent_core/email_lake.py),
[`agent_core/agents/orange_sales/quote.py`](../agent_core/agents/orange_sales/quote.py),
[`agent_core/memory.py`](../agent_core/memory.py)

The hallucination root cause was the LLM having only fuzzy RAG snippets
to answer a question that the Pricing BOM xlsx could have answered exactly.
These tools surface the structured truth:

| Tool | Source of truth | Citation key |
|---|---|---|
| `query_bom(customer, sku, category)` | Pricing BOM xlsx (synced via `sync_bom_from_drive` from the 業務部門 Shared Drive) | `source_file` (xlsx filename) |
| `query_email_lake(entity, doc_type, days)` | `var/data/data_lake/emails_master.parquet` | `message_id` |
| `query_quote_history(customer, sku)` | `var/data/quote_history/auto_extracted.csv` | `email_date` + sender |
| `recall(query, min_score=...)` | ChromaDB vector store + BM25 | `thread_id` + 🟢/🟡/🔴 tier |

**Crucial design detail of `recall()`**: every hit is tagged 🟢 (cosine
≥ 0.75), 🟡 (0.5–0.75), or 🔴 (< 0.5). The 0.42 Sympatex snippet from the
Jalas incident is now visibly 🔴, with a header warning that 🔴 ≠ evidence.
Callers who want a hard floor pass `min_score=0.5`. For a daemon-wide
hard floor, set `RED_RECALL_DEFAULT_MIN_SCORE=0.5` in the environment —
🔴 hits never reach the LLM at all.

**Crucial design detail of `query_bom()`**: 12-category material classifier
(`防水膜 / 撥水 / 微纖維 / 皮料 / 襯裡 / 緩衝補強 / 扣件 / 縫線 / 標籤 / 鞋頭/鞋尾 /
成型件 / 其他`) so `query_bom(customer="Jalas", category="防水膜")` returns
either real membrane rows or an explicit "0 筆 — 沒有用的直接證據" message.

### Layer 3 — LLM self-check (`verify_claim`)

**File:** [`agent_core/citation_guard.py`](../agent_core/citation_guard.py)

Tool the agent calls on its own draft before sending:

```python
verify_claim(
    facts_to_verify=["Jalas", "CASPER 005", "IDROREPELLEN", "1.8528 EUR"],
    evidence=<raw query_bom output>,
)
```

Normalised substring check — case-insensitive, em-dash → hyphen, whitespace
collapsed. Returns `✅ PASS — N/N` or `❌ FAIL` with the specific terms
that did not appear. The persona instructs the agent to run this for any
material/spec/price reply.

The Jalas-membrane bad-reply (`"...Jalas 確實有在使用防水相關材料, ... PU468..."`)
fails verify_claim against the real BOM output (which only mentions CASPER
IDROREPELLEN), so the agent has a chance to catch itself before sending.

### Layer 4 — Outbound guard (`citation_guard.check_citation`)

**File:** [`agent_core/citation_guard.py`](../agent_core/citation_guard.py),
hooked in [`agent_core/daemon_telegram.py`](../agent_core/daemon_telegram.py)
right between the Gemini response and `tg_send`.

Hard guard, runs on every outgoing reply. Two trigger conditions:

1. Customer name (from the canonical list — Jalas, Lurchi, Richter,
   Blaklader, Decathlon, Isco, etc.) **AND** a material/spec/price
   keyword (防水膜, 撥水, BOM, 料號, EUR/USD price patterns, PU468, ...).
2. A standalone price+BOM/spec assertion ("1155 報價 25 EUR/PAR") even
   without a customer name, because that's unambiguously a factual claim.

If either trigger fires and the reply has **no** `[證據：…]` /
`[查不到直接證據]` marker (and isn't a structured tool output that
already carries provenance like 📁 來源檔案 or `id=<message_id>`), the
daemon prepends a visible warning banner:

```
⚠️ 未通過引用檢查：偵測到「Jalas」+「防水膜」相關宣稱，但沒附 [證據：…]
   引用。如果是關鍵業務事實，建議回「重查」讓小紅用 query_bom /
   query_email_lake 重新確認。
─────
<the LLM's original reply>
```

The reply still goes out — the cost of full blocking would hurt UX too
much — but 大王 sees exactly what slipped through. **Opt-in retry mode**:
set `RED_CITATION_RETRY=1` to make the daemon spend one extra LLM round
asking the model to rewrite with proper citations before sending; if
the retry passes the guard it goes out clean, otherwise the daemon
falls back to banner-and-send on the retry text. Costs ~1 extra
inference per flagged turn, so off by default.

**Log signature when fired:**

```
[citation_guard] flagged outgoing reply: customers=['Jalas'], facts=['防水膜'], no evidence marker, not a structured tool output
```

### Layer 5 (meta) — Regression suite

**File:** [`tests/test_hallucination_regression.py`](../tests/test_hallucination_regression.py)

Golden incident corpus: each entry is `(user_q, bad_reply, good_reply,
user_correction)` drawn from a real or representative failure. Tests
assert:

- `citation_guard.check_citation(bad_reply).ok == False` for every incident
- `citation_guard.check_citation(good_reply).ok == True` for every incident
- `correction_detector.detect_correction(user_correction).is_correction == True`
- `extractive_mode.is_fact_lookup(user_q) == True`
- `verify_claim` rejects fabricated facts against real evidence
- **Meta assertion**: at least one layer fires on every incident — even
  if one detector regresses, others must still cover

The suite is deterministic (no live Gemini call) so it runs in every CI
pass. A regression shows up as `REGRESSION: <layer> no longer flags
<incident>` in the test output, naming which historical bug just came
back.

---

## How to read production logs

When a turn fires defenses, look for these markers in `var/logs/daemon-telegram.log`:

```
[correction] 偵測到糾錯訊號：<pattern> → 「<matched text>」
[extractive] 偵測到純查詢類問題 → 啟用萃取模式
[citation_guard] flagged outgoing reply: <reason>
```

A turn that doesn't fire any marker but produced a hallucination is the
loudest signal that a new attack vector has been found — add it to
`HALLUCINATION_INCIDENTS` and tighten the matching layer.

---

## Adding a new known-bad incident

When 大王 catches a new hallucination flavour:

1. Open [`tests/test_hallucination_regression.py`](../tests/test_hallucination_regression.py).
2. Add an entry to `HALLUCINATION_INCIDENTS` with the actual question,
   the bad reply, an example good reply that should have been said, and
   the correction phrasing 大王 used (if any).
3. Run `pytest tests/test_hallucination_regression.py -v`.
4. If any detector misses the new entry, **fix the detector** — don't
   water down the incident text to make the test pass. The whole point
   is the corpus stays faithful to real failures.

---

## What to do when a hallucination still slips through

1. **Grep the daemon log** for `[correction]`, `[extractive]`, and
   `[citation_guard]` markers around the timestamp. Which layers fired?
2. **If no layer fired** — that's a coverage gap. Add the new failure
   to the regression corpus (step above) and extend the relevant detector
   to catch the pattern. Run the suite to confirm coverage.
3. **If the banner fired but 大王 still believed the answer** — the
   banner wording is too soft, or the persona didn't recover. Check
   `agent_core/citation_guard.py::CitationCheckResult.banner` for the
   warning text; consider blocking-and-retry instead of warn-and-send.
4. **If `recall` returned a 🔴 hit and the LLM still cited it** — the
   model is ignoring the tier emoji. Tighten the persona's 🔴-is-not-
   evidence rule, or set `min_score=0.5` as the daemon-level default.
5. **If `verify_claim` was not called** — the LLM forgot. Strengthen the
   persona instruction in [`agent_core/persona.py`](../agent_core/persona.py)
   under 事實準確守則, or add an extractive-mode rule that requires it.

---

## File map (quick reference)

```
agent_core/
├── persona.py                                 — soft rules (事實準確守則)
├── correction_detector.py                     — Layer 1 inbound: corrections
├── extractive_mode.py                         — Layer 1 inbound: fact lookups
├── citation_guard.py                          — Layer 3 verify_claim + Layer 4 banner
├── memory_ops.py / memory.py                  — Layer 2 recall() with tiers
├── email_lake.py                              — Layer 2 query_email_lake
├── agents/orange_sales/
│   ├── bom.py                                 — Layer 2 query_bom + Drive sync
│   └── quote.py                               — Layer 2 query_quote_history
└── daemon_telegram.py                         — hooks all layers into the chat loop

tests/
├── test_citation_guard.py                     — unit tests for Layers 3+4
├── test_correction_detector.py                — unit tests for Layer 1 (corrections)
├── test_extractive_mode.py                    — unit tests for Layer 1 (lookups)
├── test_recall_confidence.py                  — unit tests for Layer 2 (recall tiers)
├── test_bom_history.py                        — unit tests for Layer 2 (BOM)
└── test_hallucination_regression.py           — Layer 5 golden corpus
```
