"""Factory Progress Manager — 工廠進度管理模塊。

負責追蹤和管理所有工廠生產進度，包括：
- 樣品開發進度
- 生產排程進度
- 供應鏈進度
- 品質檢查進度
- 整體項目進度

功能：
- 從ERP系統獲取進度數據
- 計算預計完成時間
- 識別延遲風險
- 生成進度報告
- 發送進度警報

併發（2026-07 深檢）：舊版在 import 時把 progress.json 快照進記憶體、
每次更新整檔覆寫（非 atomic、無跨行程鎖）— 多個 daemon / worker 行程
各持各的快照互相蓋寫，壞檔還會被靜默當空歸零。現在寫入走
state_io.locked_json（fcntl 跨行程鎖、進鎖重讀、atomic 寫回），讀取
每次直讀磁碟。**路徑保持不變**（改路徑會孤兒化既有資料）。
"""

import json
import os
from datetime import datetime, timedelta
from typing import Dict, Any

from agent_core.logging_and_paths import logger, RUNTIME_ROOT
from agent_core.erp import _ERP_WORKFLOW_DIR  # noqa: F401 - 保留既有依賴面
from agent_core.gemini_client import GEMINI_MODEL, _gemini_generate
from agent_core.state_io import locked_json

# 走 RUNTIME_ROOT（吃 RED_RUNTIME_DIR）而不是自己拼 repo 根 + var，理由同
# cost_tracker._get_cost_log_path。這裡還是 import time 就 makedirs，所以繞過
# runtime 根等於「光 import 就在 repo 裡生目錄」。沒設 env 時解析結果不變。
PROGRESS_DATA_DIR = os.path.join(RUNTIME_ROOT, "progress_data")
os.makedirs(PROGRESS_DATA_DIR, exist_ok=True)


def _now_str() -> str:
    return datetime.now().isoformat(timespec="seconds")


class ProgressManager:
    def __init__(self):
        # 只記路徑，不做 import 時快照 — 每次讀寫都以磁碟為準。
        self._data_file = os.path.join(PROGRESS_DATA_DIR, "progress.json")

    # ── 讀 ──────────────────────────────────────────────────────────
    def _read_progress_data(self) -> Dict[str, Any]:
        """讀當下磁碟狀態。讀取端不需要鎖（寫入 atomic，不會讀到半截）。"""
        try:
            with open(self._data_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return {}
        except Exception as e:
            logger.error(f"載入進度數據失敗: {e}")
            return {}

    @property
    def progress_data(self) -> Dict[str, Any]:
        """向後相容：舊呼叫端直接讀 progress_data 屬性。每次回新鮮磁碟狀態。"""
        return self._read_progress_data()

    # ── 寫 ──────────────────────────────────────────────────────────
    def update_sample_progress(self, sample_id: str, stage: str, status: str, notes: str = ""):
        """更新樣品開發進度（跨行程安全的 read-modify-write）。"""
        now = _now_str()
        try:
            with locked_json(self._data_file, default={}) as data:
                samples = data.setdefault("samples", {})
                sample = samples.setdefault(sample_id, {
                    "stages": {},
                    "created_at": now,
                    "last_updated": now,
                })
                sample.setdefault("stages", {})[stage] = {
                    "status": status,
                    "notes": notes,
                    "updated_at": now,
                }
                sample["last_updated"] = now
        except Exception as e:
            logger.error(f"保存進度數據失敗: {e}")

    # ── 查詢 / 統計 ─────────────────────────────────────────────────
    def get_sample_progress(self, sample_id: str) -> Dict[str, Any]:
        """獲取樣品進度"""
        return self._read_progress_data().get("samples", {}).get(sample_id, {})

    def get_all_samples_progress(self) -> Dict[str, Any]:
        """獲取所有樣品進度"""
        return self._read_progress_data().get("samples", {})

    def calculate_overall_progress(self) -> Dict[str, Any]:
        """計算整體工廠進度"""
        samples = self._read_progress_data().get("samples", {})
        total_samples = len(samples)
        completed_samples = sum(1 for s in samples.values()
                              if any(stage.get("status") == "completed"
                                   for stage in s.get("stages", {}).values()))

        # 計算延遲風險
        delayed_samples = []
        for sample_id, data in samples.items():
            if self._is_sample_delayed(data):
                delayed_samples.append(sample_id)

        return {
            "total_samples": total_samples,
            "completed_samples": completed_samples,
            "completion_rate": completed_samples / total_samples if total_samples > 0 else 0,
            "delayed_samples": delayed_samples,
            "delayed_count": len(delayed_samples),
            "last_updated": _now_str(),
        }

    def _is_sample_delayed(self, sample_data: Dict[str, Any]) -> bool:
        """檢查樣品是否延遲"""
        # 簡單邏輯：如果最後更新超過7天且未完成
        last_updated = sample_data.get("last_updated")
        if isinstance(last_updated, str):
            try:
                last_updated = datetime.fromisoformat(last_updated)
            except ValueError:
                last_updated = None

        if last_updated and (datetime.now() - last_updated) > timedelta(days=7):
            stages = sample_data.get("stages", {})
            if not any(stage.get("status") == "completed" for stage in stages.values()):
                return True
        return False

    def generate_progress_report(self) -> str:
        """生成進度報告"""
        overall = self.calculate_overall_progress()

        report = f"""工廠進度報告 - {datetime.now().strftime('%Y-%m-%d %H:%M')}

整體進度：
- 總樣品數：{overall['total_samples']}
- 已完成：{overall['completed_samples']}
- 完成率：{overall['completion_rate']:.1%}
- 延遲樣品：{overall['delayed_count']} 個

延遲樣品清單：
"""

        for sample_id in overall['delayed_samples'][:5]:  # 只顯示前5個
            report += f"- {sample_id}\n"

        if overall['delayed_count'] > 5:
            report += f"... 還有 {overall['delayed_count'] - 5} 個\n"

        # 使用Gemini生成摘要
        try:
            prompt = f"基於以下數據生成簡潔的進度摘要：{json.dumps(overall, default=str, ensure_ascii=False)}"
            resp = _gemini_generate(model=GEMINI_MODEL, contents=[prompt])
            summary = (resp.text or "").strip()
            report += f"\nAI摘要：{summary}\n"
        except Exception as e:
            logger.error(f"生成摘要失敗: {e}")

        return report

# 全域實例
progress_manager = ProgressManager()

# 工具函數
def update_sample_progress(sample_id: str, stage: str, status: str, notes: str = ""):
    """更新樣品開發進度"""
    return progress_manager.update_sample_progress(sample_id, stage, status, notes)

def get_sample_progress(sample_id: str):
    """獲取樣品進度"""
    return progress_manager.get_sample_progress(sample_id)

def get_all_samples_progress():
    """獲取所有樣品進度"""
    return progress_manager.get_all_samples_progress()

def get_overall_progress():
    """獲取整體工廠進度"""
    return progress_manager.calculate_overall_progress()

def generate_progress_report():
    """生成進度報告"""
    return progress_manager.generate_progress_report()
