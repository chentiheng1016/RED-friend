"""影片學習評測集 — 量測「教學影片 → 知識」管線的理解率。

「提高影片學習精度」的每一步優化（本機 ASR、glossary 修正、關鍵幀 gating、
skill cards 分級）都需要一把尺：沒有評測集，「理解率 100%」只是宣稱。
本模組提供這把尺：

1. `draft_eval_questions()`：從一支影片既有的分析文字（watch_teaching_video
   輸出或 SOP）自動草擬 10–20 題問答（欄位值/步驟順序/術語/因果四類），
   狀態標 `draft` — **草稿不算數**，要人工核對過（CLI `verify`）才進評分母體。
2. `run_eval()`：拿「管線最新產出的分析文字」對著已驗證的題目讓 LLM 批改，
   算出理解分數，逐次留檔（var/data/video_eval/runs/）供跨版本比較。
3. `format_eval_report()`：看一支影片歷次評測的分數走勢 — 管線改動有沒有變好，
   看數字不看感覺。

評分規則：correct=1、partial=0.5、wrong/not_covered=0；score = 加權和/題數。
批改用便宜的 flash 模型（caller=video_eval.judge 歸戶成本）。

儲存：var/data/video_eval/<video_id>.json（題庫）、runs/（歷次評測）。
跟 mistakes.json 同款 JSON+原子寫檔，不進 ChromaDB — 題庫要能直接開檔人工編修。
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from typing import Any

from agent_core.logging_and_paths import DATA_DIR, _atomic_write_text, logger

_EVAL_DIR = os.path.join(DATA_DIR, "video_eval")
_RUNS_SUBDIR = "runs"

_QUESTION_KINDS = ("param", "step", "term", "causal", "other")
_VERDICTS = ("correct", "partial", "wrong", "not_covered")
# 一次評測/草擬餵給 LLM 的素材上限 — 教學分析文字通常幾千字，超長截斷保護 token。
_MAX_SOURCE_CHARS = 60_000


# ────────────────────────────────────────────────────────────────────
# 儲存層
# ────────────────────────────────────────────────────────────────────

def _safe_id(video_id: str) -> str:
    """video_id 轉安全檔名（Drive file id 本來就只有 [A-Za-z0-9_-]，防衛其他來源）。"""
    return re.sub(r"[^A-Za-z0-9_\-]", "_", (video_id or "").strip())[:80]


def _eval_path(video_id: str) -> str:
    return os.path.join(_EVAL_DIR, f"{_safe_id(video_id)}.json")


def _runs_dir() -> str:
    return os.path.join(_EVAL_DIR, _RUNS_SUBDIR)


def load_eval_set(video_id: str) -> dict[str, Any] | None:
    path = _eval_path(video_id)
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except Exception as exc:
        logger.warning("評測題庫 %s 讀取失敗（%s）", path, exc)
        return None


def save_eval_set(eval_set: dict[str, Any]) -> str:
    vid = str(eval_set.get("video_id", "")).strip()
    if not vid:
        raise ValueError("eval_set 缺 video_id")
    os.makedirs(_EVAL_DIR, exist_ok=True)
    eval_set["updated_at"] = datetime.now(timezone.utc).isoformat()
    path = _eval_path(vid)
    _atomic_write_text(path, json.dumps(eval_set, ensure_ascii=False, indent=2))
    return path


def list_eval_sets() -> list[dict[str, Any]]:
    """列出所有題庫的摘要（video_id、名稱、題數、已驗證題數）。"""
    if not os.path.isdir(_EVAL_DIR):
        return []
    out: list[dict[str, Any]] = []
    for fn in sorted(os.listdir(_EVAL_DIR)):
        if not fn.endswith(".json"):
            continue
        try:
            with open(os.path.join(_EVAL_DIR, fn), encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            continue
        if not isinstance(data, dict) or "questions" not in data:
            continue
        qs = data.get("questions") or []
        out.append({
            "video_id": data.get("video_id", fn[:-5]),
            "video_name": data.get("video_name", ""),
            "questions": len(qs),
            "verified": sum(1 for q in qs if q.get("status") == "verified"),
        })
    return out


def upsert_questions(
    video_id: str,
    video_name: str,
    questions: list[dict[str, Any]],
    *,
    source: str,
    status: str = "draft",
) -> dict[str, Any]:
    """把題目合併進題庫（以問句文字去重），回傳更新後的 eval_set。"""
    es = load_eval_set(video_id) or {
        "video_id": video_id.strip(),
        "video_name": (video_name or "").strip(),
        "questions": [],
    }
    if video_name and not es.get("video_name"):
        es["video_name"] = video_name.strip()
    existing = {str(q.get("question", "")).strip() for q in es["questions"]}
    # qid 取既有編號最大值+1（不是 len+1）：題庫明文開放人工開檔刪爛題，
    # len+1 會在刪題後撞號 → judge 的 by_id dict 讓兩題共用同一 verdict。
    next_n = 0
    for q in es["questions"]:
        m = re.fullmatch(r"q(\d+)", str(q.get("id", "")))
        if m:
            next_n = max(next_n, int(m.group(1)))
    added = 0
    for q in questions:
        text = str(q.get("question", "")).strip()
        expected = str(q.get("expected", "")).strip()
        if not text or not expected or text in existing:
            continue
        kind = str(q.get("kind", "other")).strip()
        next_n += 1
        es["questions"].append({
            "id": f"q{next_n:02d}",
            "kind": kind if kind in _QUESTION_KINDS else "other",
            "question": text,
            "expected": expected,
            "status": status,
            "source": source,
        })
        existing.add(text)
        added += 1
    save_eval_set(es)
    return {"ok": True, "added": added, "total": len(es["questions"]),
            "video_id": es["video_id"]}


def verify_questions(video_id: str, qids: list[str] | None = None) -> dict[str, Any]:
    """把題目標成 verified（人工核對過才算數）。qids=None 表示全部 draft 轉正。"""
    es = load_eval_set(video_id)
    if not es:
        return {"ok": False, "reason": "no_eval_set"}
    wanted = set(qids) if qids else None
    changed = 0
    for q in es.get("questions", []):
        if wanted is not None and q.get("id") not in wanted:
            continue
        if q.get("status") != "verified":
            q["status"] = "verified"
            changed += 1
    save_eval_set(es)
    return {"ok": True, "verified": changed,
            "total_verified": sum(1 for q in es["questions"]
                                  if q.get("status") == "verified")}


# ────────────────────────────────────────────────────────────────────
# LLM：草擬題目 / 批改
# ────────────────────────────────────────────────────────────────────

def _parse_json_array(text: str) -> list | None:
    text = (text or "").strip()
    try:
        obj = json.loads(text)
        if isinstance(obj, list):
            return obj
    except (ValueError, TypeError):
        pass
    m = re.search(r"\[.*\]", text, re.DOTALL)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
        return obj if isinstance(obj, list) else None
    except Exception:
        return None


def _json_config():
    from google.genai import types

    return types.GenerateContentConfig(response_mime_type="application/json")


def _draft_prompt(n: int, source_text: str) -> str:
    return (
        "你是出題者。下面是一支工廠教學影片整理出的教學內容，"
        f"請出 {n} 題「看過這支影片的人應該答得出」的問答題，用來評測另一套"
        "影片理解系統的產出品質。\n"
        "題型盡量平均涵蓋四類：\n"
        "- param：具體欄位值/參數/數字（例：某欄位要填什麼值、數量是多少）\n"
        "- step：步驟順序（例：A 和 B 哪個先做、某步驟的下一步）\n"
        "- term：專有名詞（例：畫面代碼、機台/料號/術語指什麼）\n"
        "- causal：因果與注意事項（例：為什麼要先做 X、哪裡容易做錯）\n"
        "只輸出 JSON array，每題："
        '{"kind": "param|step|term|causal", "question": "…", "expected": "標準答案（精簡）"}\n'
        "鐵則：題目與答案都只根據教學內容本身，不要編造；"
        "答案要具體可比對（有數值寫數值、有代碼寫代碼）。\n\n"
        "【教學內容】\n" + source_text
    )


def draft_eval_questions(
    video_id: str,
    video_name: str,
    source_text: str,
    n: int = 12,
) -> dict[str, Any]:
    """從影片的分析文字自動草擬 n 題問答，存成 draft 待人工核對。

    草稿題**不進評分母體**（run_eval 預設只算 verified）——出題和受測是同一類
    模型，未核對的題目會把系統性錯誤變成「標準答案」，評了等於沒評。
    """
    vid = (video_id or "").strip()
    text = (source_text or "").strip()
    if not vid or not text:
        return {"ok": False, "reason": "empty_video_id_or_text"}
    n = max(3, min(30, int(n)))

    from agent_core.gemini_client import GEMINI_MODEL, _gemini_generate
    from agent_core.prompt_injection import sanitize_for_llm, wrap_as_untrusted

    material = wrap_as_untrusted(
        sanitize_for_llm(text[:_MAX_SOURCE_CHARS]), "video_analysis")
    resp = _gemini_generate(
        model=GEMINI_MODEL,
        contents=[_draft_prompt(n, material)],
        config=_json_config(),
        caller="video_eval.draft",
    )
    items = _parse_json_array(resp.text or "")
    if not items:
        return {"ok": False, "reason": "llm_output_unparseable"}
    result = upsert_questions(vid, video_name, [i for i in items if isinstance(i, dict)],
                              source="auto_draft", status="draft")
    result["note"] = "草稿題需人工核對（bin/red-video-eval verify）後才進評分母體"
    return result


def _judge_prompt(questions: list[dict], candidate_text: str) -> str:
    qlines = []
    for q in questions:
        qlines.append(
            f'- id={q["id"]} [{q.get("kind", "other")}] 問：{q["question"]}\n'
            f'  標準答案：{q["expected"]}'
        )
    return (
        "你是評測批改者。下面有一份「影片理解系統的產出文字」和一組已驗證的問答題。\n"
        "逐題判斷：**只根據產出文字**（不要用你自己的知識補答案），"
        "這題能不能答對？\n"
        "- correct：產出文字包含的資訊足以完整答對（數值/代碼要一致）\n"
        "- partial：方向對但缺關鍵細節，或數值/代碼不完整\n"
        "- wrong：產出文字會導出錯誤答案（與標準答案矛盾）\n"
        "- not_covered：產出文字完全沒涵蓋這題\n"
        "只輸出 JSON array，每題："
        '{"id": "…", "verdict": "correct|partial|wrong|not_covered", "note": "一句話依據"}\n\n'
        "【題目】\n" + "\n".join(qlines) + "\n\n"
        "【產出文字】\n" + candidate_text
    )


def judge_answers(
    eval_set: dict[str, Any],
    candidate_text: str,
    *,
    only_verified: bool = True,
) -> dict[str, Any]:
    """用 LLM 批改：candidate_text 對題庫的每一題給 verdict，回逐題結果與總分。"""
    questions = [
        q for q in (eval_set.get("questions") or [])
        if (not only_verified) or q.get("status") == "verified"
    ]
    if not questions:
        return {"ok": False, "reason": "no_verified_questions",
                "hint": "先 draft 再人工 verify，才有評分母體"}
    text = (candidate_text or "").strip()
    if not text:
        return {"ok": False, "reason": "empty_candidate_text"}

    from agent_core.gemini_client import GEMINI_MODEL, _gemini_generate
    from agent_core.prompt_injection import sanitize_for_llm, wrap_as_untrusted

    material = wrap_as_untrusted(
        sanitize_for_llm(text[:_MAX_SOURCE_CHARS]), "candidate_output")
    resp = _gemini_generate(
        model=GEMINI_MODEL,
        contents=[_judge_prompt(questions, material)],
        config=_json_config(),
        caller="video_eval.judge",
    )
    items = _parse_json_array(resp.text or "")
    if not items:
        # 批改輸出解析失敗（或空清單）≠ 全題 not_covered — 默默給 score=0
        # 落 runs/ 檔會讓走勢圖看起來像管線大退化（其實是 judge 壞了）。
        # 回 ok=False，run_eval 不留檔。
        return {"ok": False, "reason": "judge_output_unparseable"}
    by_id = {str(i.get("id", "")): i for i in items if isinstance(i, dict)}

    results = []
    weights = {"correct": 1.0, "partial": 0.5, "wrong": 0.0, "not_covered": 0.0}
    score = 0.0
    covered = 0
    for q in questions:
        item = by_id.get(q["id"], {})
        verdict = str(item.get("verdict", "not_covered")).strip()
        if verdict not in _VERDICTS:
            verdict = "not_covered"
        score += weights[verdict]
        if verdict != "not_covered":
            covered += 1
        results.append({
            "id": q["id"], "kind": q.get("kind", "other"),
            "question": q["question"], "verdict": verdict,
            "note": str(item.get("note", ""))[:200],
        })
    n = len(questions)
    return {
        "ok": True,
        "questions": n,
        "score": round(score / n, 4),
        "coverage": round(covered / n, 4),
        "verdicts": {v: sum(1 for r in results if r["verdict"] == v)
                     for v in _VERDICTS},
        "results": results,
    }


def run_eval(
    video_id: str,
    candidate_text: str,
    *,
    label: str = "",
    only_verified: bool = True,
) -> dict[str, Any]:
    """跑一次評測並留檔（runs/），回傳含分數的摘要。label 標註這次跑的管線版本。"""
    es = load_eval_set(video_id)
    if not es:
        return {"ok": False, "reason": "no_eval_set",
                "hint": "先用 draft_eval_questions() 建題庫"}
    graded = judge_answers(es, candidate_text, only_verified=only_verified)
    if not graded.get("ok"):
        return graded

    now = datetime.now(timezone.utc)
    record = {
        "video_id": es["video_id"],
        "video_name": es.get("video_name", ""),
        "label": (label or "").strip(),
        "ran_at": now.isoformat(),
        **graded,
    }
    runs = _runs_dir()
    os.makedirs(runs, exist_ok=True)
    fname = f"{_safe_id(video_id)}__{now.strftime('%Y%m%dT%H%M%SZ')}.json"
    _atomic_write_text(os.path.join(runs, fname),
                       json.dumps(record, ensure_ascii=False, indent=2))
    record["run_file"] = fname
    return record


def _load_runs(video_id: str) -> list[dict[str, Any]]:
    runs = _runs_dir()
    if not os.path.isdir(runs):
        return []
    prefix = _safe_id(video_id) + "__"
    out = []
    for fn in sorted(os.listdir(runs)):
        if not (fn.startswith(prefix) and fn.endswith(".json")):
            continue
        try:
            with open(os.path.join(runs, fn), encoding="utf-8") as f:
                out.append(json.load(f))
        except Exception:
            continue
    return out


def format_eval_report(video_id: str, last_n: int = 10) -> str:
    """一支影片歷次評測的分數走勢（管線改動前後對照用）。"""
    es = load_eval_set(video_id)
    if not es:
        return f"沒有 video_id={video_id} 的評測題庫。"
    runs = _load_runs(video_id)[-max(1, int(last_n)):]
    qs = es.get("questions", [])
    lines = [
        f"評測題庫：{es.get('video_name') or es['video_id']}",
        f"題數 {len(qs)}（verified {sum(1 for q in qs if q.get('status') == 'verified')}"
        f" / draft {sum(1 for q in qs if q.get('status') == 'draft')}）",
        "",
    ]
    if not runs:
        lines.append("還沒有評測紀錄 — 用 run_eval() 跑第一次基準。")
        return "\n".join(lines)
    lines.append(f"最近 {len(runs)} 次評測：")
    for r in runs:
        v = r.get("verdicts", {})
        lines.append(
            f"  {str(r.get('ran_at', ''))[:19]}  score={r.get('score', 0):.2f}"
            f"  coverage={r.get('coverage', 0):.2f}"
            f"  (✓{v.get('correct', 0)} ±{v.get('partial', 0)}"
            f" ✗{v.get('wrong', 0)} ∅{v.get('not_covered', 0)})"
            + (f"  [{r['label']}]" if r.get("label") else "")
        )
    return "\n".join(lines)
