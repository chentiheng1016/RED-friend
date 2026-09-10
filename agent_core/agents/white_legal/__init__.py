"""White agent — 法務 / Legal（SoT）.

對應 Project Rainbow 規格中的 "White: Single Source of Truth for all
test reports and contracts"，目前承載 specs/ 目錄的解析後客戶規格書。

權限矩陣（permission_matrix.QUERY_MATRIX）:
  - 所有 color 都可以查 White（SoT 公開可讀）
  - White 自己不主動查任何 color（QUERY_MATRIX[WHITE] = empty）

公開：
  - WhiteLegalAgent
  - specs.* — spec sheet 解析 / 列表 / 比對（從 agent_core.specs 搬來）
"""
from agent_core.agents.white_legal.agent import WhiteLegalAgent

__all__ = ["WhiteLegalAgent"]
