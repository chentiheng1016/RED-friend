"""Green / sample room department profile.

This is the contract an Edge Agent uses before it is allowed to touch ERP:
which modules it may open, which recipes are approved, which fields are
required, and which steps require human confirmation.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any


GREEN_PROFILE: dict[str, Any] = {
    "color": "green",
    "department": "樣品室",
    "version": "2026-05-20",
    "purpose": "管理樣品建檔、樣品狀態、客戶回饋與樣品到期跟催。",
    "default_edge_mode": "assistive",
    "erp_modules": [
        {
            "id": "sample_master",
            "label": "樣品主檔",
            "risk": "medium",
            "allowed_actions": [
                "create_sample_record",
                "update_sample_status",
                "attach_sample_note",
            ],
        },
        {
            "id": "customer_sample_followup",
            "label": "客戶樣品追蹤",
            "risk": "medium",
            "allowed_actions": [
                "create_followup_task",
                "update_expected_feedback_date",
            ],
        },
    ],
    "rpa_recipes": [
        {
            "id": "green_create_sample_record",
            "label": "建立樣品資料",
            "erp_module": "sample_master",
            "risk": "medium",
            "mode": "human_confirm_before_submit",
            "required_fields": [
                "sample_id",
                "customer",
                "description",
                "sent_date",
                "expected_feedback_date",
            ],
            "optional_fields": [
                "contact_email",
                "notes",
            ],
            "preconditions": [
                "員工必須屬於 green 或 red。",
                "ERP 已登入且停在樣品主檔或可從主選單進入。",
                "sample_id 不可與現有開啟樣品重複。",
            ],
            "steps": [
                "開啟樣品主檔新增畫面。",
                "輸入樣品編號、客戶、描述、寄出日與預期回饋日。",
                "若有窗口 email 或備註，一併填入。",
                "停在送出前畫面並截圖。",
                "等待員工確認後才按儲存。",
            ],
            "confirmation": {
                "before_start": False,
                "before_submit": True,
                "dangerous_confirmation": False,
            },
            "audit": [
                "task_id",
                "employee_email",
                "device_id",
                "sample_id",
                "customer",
                "before_submit_screenshot",
                "completion_screenshot",
            ],
            "rollback": "若尚未儲存，直接取消；若已儲存，只允許標記作廢，不自動刪除。",
        },
        {
            "id": "green_update_sample_status",
            "label": "更新樣品狀態",
            "erp_module": "sample_master",
            "risk": "low",
            "mode": "human_confirm_before_submit",
            "required_fields": [
                "sample_id",
                "status",
            ],
            "optional_fields": [
                "note",
                "expected_feedback_date",
            ],
            "allowed_status": [
                "open",
                "delayed",
                "feedback_received",
                "closed",
            ],
            "preconditions": [
                "員工必須屬於 green 或 red。",
                "ERP 已登入。",
                "sample_id 必須能在樣品主檔查到。",
            ],
            "steps": [
                "搜尋 sample_id。",
                "核對客戶與描述，避免改到錯誤樣品。",
                "更新狀態與備註。",
                "停在送出前畫面並截圖。",
                "等待員工確認後才按儲存。",
            ],
            "confirmation": {
                "before_start": False,
                "before_submit": True,
                "dangerous_confirmation": False,
            },
            "audit": [
                "task_id",
                "employee_email",
                "device_id",
                "sample_id",
                "old_status",
                "new_status",
                "before_submit_screenshot",
                "completion_screenshot",
            ],
            "rollback": "新增一筆更正備註；不自動回寫狀態，避免覆蓋人工修正。",
        },
    ],
    "forbidden_actions": [
        "刪除樣品主檔",
        "修改價格、付款條件或正式訂單資料",
        "寄出對外郵件",
        "跳過員工送出前確認",
        "操作非 green 部門授權的 ERP 模組",
    ],
    "notifications": {
        "on_failure": ["red", "green"],
        "on_submit": ["green"],
        "on_dangerous_attempt": ["red"],
    },
}


def get_profile() -> dict[str, Any]:
    return deepcopy(GREEN_PROFILE)


def list_rpa_recipes() -> list[dict[str, Any]]:
    return deepcopy(GREEN_PROFILE["rpa_recipes"])


def get_rpa_recipe(recipe_id: str) -> dict[str, Any] | None:
    needle = str(recipe_id or "").strip()
    for recipe in GREEN_PROFILE["rpa_recipes"]:
        if recipe.get("id") == needle:
            return deepcopy(recipe)
    return None
