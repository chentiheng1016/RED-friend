"""本機高精度語音轉錄 — whisper.cpp large-v3（Metal）＋幻覺過濾＋統一繁體。

影片學習管線的第一級轉錄引擎：本機免費、不吃 Gemini 預算、不撞 503。

引擎選型（2026-07，兩輪）：
- faster-whisper 出局：CTranslate2 在 Apple Silicon 無 Metal 支援、只能 CPU。
- mlx-whisper 出局：硬依賴 numba，而 numba 全系列要求 numpy<2.2，與本 repo
  釘死的 numpy==2.5.0 直接衝突（CI 同款 Mac 實測 pip 解析炸裂）。
- **whisper.cpp**（`brew install whisper-cpp`）：Metal + Core ML、large-v3 約
  10x realtime、零 Python 依賴 — 跟 ffmpeg 同款「系統工具、沒裝就優雅降級」。
  模型檔另外下載（見 _MODEL_PATH_DEFAULT 註解）。

幻覺防線（Whisper 對機台噪音/音樂/靜音的結構性幻覺）：
1. `--no-context`：不把前一段轉錄當 context — 防一段幻覺污染整片（重複迴圈）。
2. 輸出側逐段過濾：compression_ratio 過高（本模組以 zlib 計算，同 openai
   whisper 公式）= 重複迴圈直接丟；avg_logprob 過低（由 token 機率換算）保留
   但標低信度。被丟的段留在 segments 標 drop_reason，可追溯不憑空消失。
3. 輸出側跨段過濾：連續 ≥_REPEAT_RUN_MIN 段正規化後文字相同 = 跨段複讀迴圈
   （最典型幻覺，如「謝謝大家收看」×40）整串丟 — 每段各自壓縮率正常，
   逐段的 compression_ratio 抓不到，只有跨段視角看得見。
4. 非語音窗口由 whisper.cpp 內建 no-speech 偵測處理（引擎層跳過；JSON 輸出
   拿不到 no_speech_prob 分數，_classify_segment 的 non_speech 分支只在未來
   引擎提供該欄位時生效 — 欄位缺省 0.0 不觸發）。

不可用（沒裝 whisper-cli / 沒模型檔 / RED_ASR_DISABLE=1）一律回 None，
呼叫端 fallback 到既有 Gemini 多模態路徑 —— 跟 ocrmac 同款「本機優先、雲端兜底」。

Module-level import 保持輕量（os + env_utils + logging_and_paths）：
subprocess / json / opencc 都 lazy。
"""
from __future__ import annotations

import os
from typing import Any

from agent_core.env_utils import env_bool, env_float, env_int
from agent_core.logging_and_paths import logger

# ggml 模型檔預設位置。下載（一次、~3GB）：
#   mkdir -p ~/.cache/whisper-cpp && curl -L -o ~/.cache/whisper-cpp/ggml-large-v3.bin \
#     https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3.bin
_MODEL_PATH_DEFAULT = os.path.expanduser("~/.cache/whisper-cpp/ggml-large-v3.bin")

# 幻覺過濾門檻 — 沿用 whisper 官方 fallback 判定的慣例值。
_LOGPROB_MIN = env_float("RED_ASR_LOGPROB_MIN", -1.0)
_COMPRESSION_MAX = env_float("RED_ASR_COMPRESSION_MAX", 2.4, min_value=1.0)
_NO_SPEECH_MAX = env_float("RED_ASR_NO_SPEECH_MAX", 0.6, min_value=0.0, max_value=1.0)
# 跨段複讀迴圈門檻：連續 ≥N 段正規化後文字相同 → 整串標幻覺丟棄。
_REPEAT_RUN_MIN = env_int("RED_ASR_REPEAT_RUN_MIN", 3, min_value=2)
# initial_prompt 的詞彙表只放最高頻前 N 個 — 只吃最後 224 token，且配
# --carry-initial-prompt 後**每個窗口**都會帶著它（token 預算跟著每窗口吃緊，
# 別調太大）；asr_glossary.correct_transcript 後修層仍是全量 glossary 的主力。
_PROMPT_TERMS_MAX = env_int("RED_ASR_PROMPT_TERMS", 30, min_value=0, max_value=100)
# 整輪 subprocess 上限（長片轉錄是分鐘級工作；這是防呆天花板，不是調速旋鈕）。
_TIMEOUT_S = env_int("RED_ASR_TIMEOUT_S", 14_400, min_value=60)

# 種子 prompt：把輸出偏向繁體中文＋工廠語域（Whisper 中文預設常冒簡體）。
_PROMPT_SEED = (
    os.environ.get("RED_ASR_PROMPT_SEED", "").strip()
    or "以下是台灣製鞋工廠的教育訓練影片旁白，使用繁體中文。"
)


def _find_binary() -> str | None:
    """whisper.cpp CLI 的路徑：RED_ASR_BIN 優先，否則 PATH 上找 brew 裝的名字。"""
    explicit = os.environ.get("RED_ASR_BIN", "").strip()
    if explicit:
        return explicit if os.path.exists(explicit) else None
    import shutil

    for name in ("whisper-cli", "whisper-cpp"):
        found = shutil.which(name)
        if found:
            return found
    return None


def _model_path() -> str:
    return (os.environ.get("RED_ASR_MODEL_PATH", "").strip()
            or _MODEL_PATH_DEFAULT)


def is_available() -> bool:
    """本機 ASR 可不可用：裝了 whisper.cpp＋模型檔在＋沒被 kill switch 關掉。"""
    if env_bool("RED_ASR_DISABLE", False):
        return False
    if _find_binary() is None:
        return False
    return os.path.isfile(_model_path())


def _get_opencc():
    """OpenCC 轉繁器（lazy；沒裝回 None = 原樣輸出）。預設 s2twp（含台灣用語），
    設定檔不存在退 s2t。"""
    try:
        from opencc import OpenCC  # type: ignore
    except Exception:
        return None
    cfg = os.environ.get("RED_ASR_OPENCC_CONFIG", "").strip() or "s2twp"
    try:
        return OpenCC(cfg)
    except Exception:
        try:
            return OpenCC("s2t")
        except Exception:
            return None


def _to_traditional(text: str, cc) -> str:
    if not cc or not text:
        return text
    try:
        return cc.convert(text)
    except Exception:
        return text


def build_initial_prompt(glossary_terms: list[str] | None = None) -> str:
    """種子 prompt＋高頻專有名詞。Whisper 只保留 initial_prompt 的**最後** 224
    token，詞彙表接在種子後面（尾端）確保留在有效窗口內。

    配 --carry-initial-prompt 這段 prompt 會 prime **每個** 30 秒窗口（全片
    術語與繁體偏置都吃得到）；opencc 轉繁＋asr_glossary 後修仍是兜底。"""
    terms = [t.strip() for t in (glossary_terms or []) if t and str(t).strip()]
    if len(terms) > _PROMPT_TERMS_MAX:
        terms = terms[:_PROMPT_TERMS_MAX]
    prompt = _PROMPT_SEED
    if terms:
        prompt += "常出現的專有名詞：" + "、".join(terms) + "。"
    return prompt


def _compression_ratio(text: str) -> float:
    """openai whisper 的重複迴圈指標：原文 bytes / zlib 壓縮後 bytes。
    高度重複的幻覺文字壓縮率極高 → ratio 飆高。"""
    data = (text or "").encode("utf-8")
    if not data:
        return 0.0
    import zlib

    return len(data) / max(1, len(zlib.compress(data)))


def _seg_float(seg: dict, key: str, default: float = 0.0) -> float:
    try:
        return float(seg.get(key, default))
    except (TypeError, ValueError):
        return default


def _classify_segment(seg: dict) -> tuple[bool, str]:
    """逐段幻覺判定，回 (keep, drop_reason)。"""
    if not (seg.get("text") or "").strip():
        return False, "empty"
    if _seg_float(seg, "compression_ratio") > _COMPRESSION_MAX:
        return False, "repetition_loop"  # 重複迴圈（幻覺的典型特徵）
    if (_seg_float(seg, "no_speech_prob") > _NO_SPEECH_MAX
            and _seg_float(seg, "avg_logprob") < _LOGPROB_MIN):
        return False, "non_speech"  # 引擎有給 no_speech 分數時才可能觸發
    return True, ""


def _normalize_repeat_text(text: str) -> str:
    """跨段複讀比對鍵：去空白/標點/大小寫 —「謝謝大家收看。」≡「謝謝大家收看」。"""
    import re

    return re.sub(r"[\s\W_]+", "", (text or "").lower())


def _repeat_run_indexes(segments: list[dict]) -> set[int]:
    """輸出側跨段幻覺判定：連續 ≥_REPEAT_RUN_MIN 段正規化後文字相同 →
    整串（含第一段）標丟棄。逐段 compression_ratio 抓的是「段內」重複；
    Whisper 最典型的幻覺是同一短句跨段複讀（「謝謝大家收看」×40），每段
    各自壓縮率完全正常 — 只有跨段視角抓得到。回丟棄段的 index 集合。"""
    doomed: set[int] = set()
    run_start = 0
    prev = ""
    for i in range(len(segments) + 1):  # +1：哨兵收尾最後一串
        norm = (_normalize_repeat_text(str(segments[i].get("text", "")))
                if i < len(segments) else None)
        if norm is not None and norm and norm == prev:
            continue  # 同一串延續中
        if prev and i - run_start >= _REPEAT_RUN_MIN:
            doomed.update(range(run_start, i))
        run_start = i
        prev = norm or ""
    return doomed


# ────────────────────────────────────────────────────────────────────
# whisper.cpp 引擎層
# ────────────────────────────────────────────────────────────────────

def _to_wav16k(path: str, out_dir: str) -> str | None:
    """ffmpeg 轉 16kHz 單聲道 WAV（whisper.cpp 的輸入格式）。失敗回 None。"""
    import shutil
    import subprocess

    if not shutil.which("ffmpeg"):
        return None
    wav = os.path.join(out_dir, "asr_input.wav")
    try:
        proc = subprocess.run(
            ["ffmpeg", "-hide_banner", "-nostdin", "-y", "-i", path,
             "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", wav],
            capture_output=True, timeout=1800,
        )
    except Exception as exc:
        logger.warning("ASR 音軌轉檔失敗（%s）", exc)
        return None
    if proc.returncode != 0 or not os.path.isfile(wav):
        logger.warning("ASR 音軌轉檔失敗（ffmpeg rc=%s）", proc.returncode)
        return None
    return wav


def _engine_args(binary: str, model: str, wav: str, lang: str,
                 prompt: str, out_prefix: str) -> list[str]:
    """組 whisper-cli 參數（獨立函式方便測旗標，不用真跑 subprocess）。"""
    return [
        binary, "-m", model, "-l", lang,
        "--prompt", prompt,
        # 鐵則：不帶前文 context — 教學影片的機台噪音/音樂段是幻覺重複迴圈的
        # 典型觸發點，帶著會讓一段幻覺污染整片。whisper-cli 沒有 --no-context
        # 旗標，等價做法是 --max-context 0（跨窗口 context token 存量歸零）。
        "--max-context", "0",
        # 但 initial_prompt（繁中種子＋glossary 詞彙表）要**每個窗口**都帶 —
        # 沒這個旗標它只 prime 第一個 30 秒窗口，後段術語全靠後修。
        "--carry-initial-prompt",
        "--temperature", "0",
        "--output-json", "--output-json-full",
        "--output-file", out_prefix,
        "--no-prints",
        wav,
    ]


def _run_engine(binary: str, model: str, wav: str, lang: str,
                prompt: str, out_dir: str) -> dict | None:
    """跑 whisper-cli、讀回 full JSON。失敗回 None。"""
    import json
    import subprocess

    out_prefix = os.path.join(out_dir, "asr_out")
    try:
        proc = subprocess.run(
            _engine_args(binary, model, wav, lang, prompt, out_prefix),
            capture_output=True, timeout=_TIMEOUT_S,
        )
    except Exception as exc:
        logger.warning("whisper.cpp 執行失敗（%s: %s）", type(exc).__name__, exc)
        return None
    json_path = out_prefix + ".json"
    if proc.returncode != 0 or not os.path.isfile(json_path):
        tail = (proc.stderr or b"")[-300:].decode("utf-8", "replace")
        logger.warning("whisper.cpp rc=%s，無 JSON 輸出（%s）", proc.returncode, tail)
        return None
    try:
        with open(json_path, encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:
        logger.warning("whisper.cpp JSON 解析失敗（%s）", exc)
        return None


def _segments_from_engine(raw: dict) -> list[dict[str, Any]]:
    """whisper.cpp full JSON → 內部 segment 格式。
    avg_logprob 由 token 機率取 ln 平均（跳過 [_...] 特殊 token）；
    compression_ratio 本模組以 zlib 計算；no_speech_prob 引擎不提供、缺省 0.0。"""
    import math

    segments: list[dict[str, Any]] = []
    for item in (raw or {}).get("transcription") or []:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        offsets = item.get("offsets") or {}
        logps: list[float] = []
        for tok in item.get("tokens") or []:
            t = str(tok.get("text") or "")
            if t.startswith("[_"):
                continue  # timing/special token
            try:
                p = float(tok.get("p", 0.0))
            except (TypeError, ValueError):
                continue
            if p > 0:
                logps.append(math.log(p))
        segments.append({
            "start": round(_seg_float(offsets, "from") / 1000.0, 2),
            "end": round(_seg_float(offsets, "to") / 1000.0, 2),
            "text": text,
            "avg_logprob": round(sum(logps) / len(logps), 4) if logps else 0.0,
            "no_speech_prob": 0.0,
            "compression_ratio": round(_compression_ratio(text), 4),
        })
    return segments


def transcribe_local(
    path: str,
    *,
    language: str | None = None,
    glossary_terms: list[str] | None = None,
    model: str | None = None,
) -> dict[str, Any] | None:
    """本機轉錄一個影音檔（先 ffmpeg 抽 16k WAV，再餵 whisper.cpp）。

    回 None = 不可用或失敗，呼叫端走雲端 fallback。成功回：
    {"engine", "model", "language", "text"（僅保留段、已轉繁）,
     "segments": [{"start","end","text","avg_logprob","no_speech_prob",
                   "compression_ratio","kept",("drop_reason"|"low_confidence")}],
     "dropped": 被過濾段數}

    注意：長片轉錄是分鐘級工作，subprocess 有 RED_ASR_TIMEOUT_S 防呆天花板；
    整輪 wall-clock 仍由呼叫端的 run_with_deadline 之類看門狗把關。
    """
    if not is_available():
        return None
    if not path or not os.path.isfile(path):
        return None
    binary = _find_binary()
    model_path = (model or "").strip() or _model_path()
    lang = (language or "").strip() or (
        os.environ.get("RED_ASR_LANGUAGE", "").strip() or "zh")

    import tempfile

    tmp_dir = tempfile.mkdtemp(prefix="red_asr_")
    try:
        wav = _to_wav16k(path, tmp_dir)
        if not wav:
            return None
        raw = _run_engine(binary, model_path, wav, lang,
                          build_initial_prompt(glossary_terms), tmp_dir)
        if raw is None:
            return None
        engine_segments = _segments_from_engine(raw)
    finally:
        import shutil

        shutil.rmtree(tmp_dir, ignore_errors=True)

    # 跨段複讀迴圈（逐段指標抓不到的幻覺型態）— 先全局標記再逐段分類。
    repeat_doomed = _repeat_run_indexes(engine_segments)

    cc = _get_opencc()
    segments: list[dict[str, Any]] = []
    kept_texts: list[str] = []
    dropped = 0
    for i, seg in enumerate(engine_segments):
        keep, reason = _classify_segment(seg)
        if keep and i in repeat_doomed:
            keep, reason = False, "repetition_run"
        text = _to_traditional(seg["text"], cc)
        item: dict[str, Any] = {**seg, "text": text, "kept": keep}
        if not keep:
            item["drop_reason"] = reason
            dropped += 1
        elif item["avg_logprob"] < _LOGPROB_MIN:
            # 保留但標低信度 — 下游分級（skill_cards）別把這種段落當 confirmed 證據。
            item["low_confidence"] = True
        segments.append(item)
        if keep:
            kept_texts.append(text)

    lang_out = str(((raw.get("result") or {}).get("language")) or lang)
    return {
        "engine": "whisper.cpp",
        "model": os.path.basename(model_path),
        "language": lang_out,
        "text": "\n".join(kept_texts).strip(),
        "segments": segments,
        "dropped": dropped,
    }


def _fmt_ts(seconds) -> str:
    try:
        s = max(0, int(seconds))
    except (TypeError, ValueError):
        return "?"
    if s >= 3600:
        return f"{s // 3600}:{(s % 3600) // 60:02d}:{s % 60:02d}"
    return f"{s // 60:02d}:{s % 60:02d}"


def format_transcript(result: dict[str, Any], *, include_dropped: bool = False) -> str:
    """轉成「[mm:ss] 文字」逐行稿（餵 SOP 整合／RAG 用）。預設只含保留段；
    低信度段標（低信度）讓下游分級看得見。"""
    lines: list[str] = []
    for seg in (result or {}).get("segments") or []:
        if not seg.get("kept") and not include_dropped:
            continue
        flag = "（低信度）" if seg.get("low_confidence") else ""
        drop = f"（已濾:{seg['drop_reason']}）" if seg.get("drop_reason") else ""
        lines.append(f"[{_fmt_ts(seg.get('start'))}]{flag}{drop} {seg.get('text', '')}")
    return "\n".join(lines)
