"""教學影片自動學習 watcher — launchd 每日單發。

發現「飛越erp使用教學」資料夾新影片 → deep 學習（本機 ASR＋OCR gating）→
SOP＋知識卡入庫 → Telegram 通知。每輪支數/大小閘門見 agent_core/training_videos.py。

本 watcher 是 StartCalendarInterval「每日一次」：撞到瞬時 DNS/網路 blip（token
刷新連不上 oauth2.googleapis.com）時，不像 cron 型 daemon 幾分鐘後靠 StartInterval
自動重試，而會卡到隔天 19:07。故在此加「進程內小重試」：對傳輸層錯誤退避重試自癒；
程式/資料類錯誤則快速失敗 → exit 1 → Telegram 告警照舊。

2026-08-27 起根因已在上游修好：google_auth 不再把「網路故障」和「憑證失效」抹平成
同一道牆訊息，暫時性失敗改拋 GoogleAuthTransientError。因此這裡的分流跟著收斂——
  * GoogleAuthTransientError（型別判定，不靠字串）→ 瞬時，退避重試
  * 「daemon 模式下無法啟動互動式 OAuth 授權」→ 現在**只**在憑證真失效或根本沒
    token 時才出現，重試無益 → 快速失敗 + 告警，讓人真的去重新授權
"""
import sys
import time

sys.path.insert(0, __file__.rsplit("/launchd/", 1)[0])

from agent_core.daemon_helpers import rotate_log, run_with_deadline
from agent_core.env_utils import env_int

# 直接冒出的傳輸層 transient 特徵（比對 type 名 + 訊息，全轉小寫）。
_TRANSIENT_MARKS = (
    "nameresolution", "failed to resolve", "max retries", "transporterror",
    "temporarily", "timed out", "timeout", "connection reset",
    "connection aborted", "connectionerror", "gaierror", "ssl",
)


def _google_auth_transient_cls():
    """上游對「瞬時 token 刷新失敗」的專屬例外類別；匯入不到就回 None（退回字串比對）。"""
    try:
        from agent_core.google_auth import GoogleAuthTransientError
    except Exception:  # noqa: BLE001  匯入失敗不該擋住分流
        return None
    return GoogleAuthTransientError


def _is_transient(exc: BaseException) -> bool:
    """這次失敗是不是「退一步、幾分鐘後可能自癒」的瞬時網路/DNS blip。

    先看型別（google_auth 對暫時性 token 刷新失敗有專屬例外，不必猜訊息），再退回
    傳輸層字串特徵。程式 bug、資料錯誤、以及**真的**需要人重新授權的 OAuth 牆不在
    此列 —— 那些重試無益，該快速退出讓告警發出去（見模組 docstring）。
    """
    cls = _google_auth_transient_cls()
    if cls is not None and isinstance(exc, cls):
        return True
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(m in text for m in _TRANSIENT_MARKS)


def _run_with_transient_retry(fn, *, max_attempts, backoff_s, sleep=time.sleep):
    """跑 fn()，僅對瞬時網路/DNS blip 退避重試；終局錯誤或次數用盡即原樣拋出。

    fn 須具幂等性——check_and_learn_new 靠 skip-marker／new_found 去重／deferred
    追蹤，部分學完才 blip、重跑會跳過已學的只補剩下與通知，故整體重試安全。
    最後一次仍失敗時原樣 re-raise（保留 exit≠0 → daemon_fail Telegram 告警）。
    """
    for attempt in range(1, max_attempts + 1):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 — 分流 transient / 終局
            if attempt >= max_attempts or not _is_transient(exc):
                raise
            print(
                f"[training_watch] ⚠️ 第 {attempt}/{max_attempts} 次遇瞬時失敗，"
                f"{backoff_s}s 後重試（{type(exc).__name__}: {exc}）",
                flush=True,
            )
            sleep(backoff_s)


def main():
    rotate_log("training_video_watch")
    # 整輪 wall-clock 硬上限：每輪最多學 2 支、大檔 deep 一支 ~25 分鐘，加上
    # 下載與列舉，3 小時綽綽有餘；卡死（SSL 半關閉等）就看門狗殺掉明天重來。
    deadline_s = env_int("RED_TRAINING_WATCH_DEADLINE_S", 10_800,
                         min_value=600, max_value=21_600)
    # 瞬時 blip 自癒：總嘗試次數與每次退避秒數（皆有預設，不必進 plist）。
    # 3 次 × 180s ≈ 最多多花 6 分鐘退避，遠在 deadline 內。
    max_attempts = env_int("RED_TRAINING_WATCH_OAUTH_RETRIES", 3,
                           min_value=1, max_value=6)
    backoff_s = env_int("RED_TRAINING_WATCH_RETRY_BACKOFF_S", 180,
                        min_value=10, max_value=1_800)

    def _run():
        from agent_core.training_videos import check_and_learn_new

        summary = _run_with_transient_retry(
            check_and_learn_new, max_attempts=max_attempts, backoff_s=backoff_s)
        print(f"[training_watch] {summary}", flush=True)

    run_with_deadline(_run, deadline_s, label="training_video_watch")


if __name__ == "__main__":
    main()
