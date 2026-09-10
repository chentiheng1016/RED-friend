"""Project Rainbow 權限矩陣 — single source of truth.

10 色對應現實部門：
  RED     — 老闆 / GM Office（SUPER_ADMIN，可查所有人）
  ORANGE  — Sales（業務）
  YELLOW  — 採購 Procurement
  GREEN   — 樣品室 Sample Room
  BLUE    — 船務 Shipping
  INDIGO  — 倉庫 Warehouse
  PURPLE  — 會計 Accounting
  GRAY    — 生產管理 Production
  BLACK   — 出納 Cashier
  WHITE   — 法務 Legal（SoT，所有合約 / 測試報告 / 規格）

QUERY_MATRIX 定義「caller 可以詢問哪些 target」。White 為純 SoT，不主動發
查詢；Red 為 SUPER_ADMIN，可查任何人。
"""
from enum import Enum
from typing import FrozenSet, Mapping


class Agent(str, Enum):
    RED = "red"
    ORANGE = "orange"
    YELLOW = "yellow"
    GREEN = "green"
    BLUE = "blue"
    INDIGO = "indigo"
    PURPLE = "purple"
    GRAY = "gray"
    BLACK = "black"
    WHITE = "white"


# dept_rules.py 用中文 key；這裡建立中→color 的對應，方便郵件分類結果直接接到
# permission middleware。dept_rules 的 "老闆" → RED；其餘對應如下。
# 未來若 dept_rules 新增分類（例如 "出納" / "法務"），更新本表即可。
DEPT_TO_COLOR: Mapping[str, "Agent"] = {
    "老闆": Agent.RED,
    "業務": Agent.ORANGE,
    "採購": Agent.YELLOW,
    "樣品室": Agent.GREEN,
    "船務": Agent.BLUE,
    "倉庫": Agent.INDIGO,
    "會計": Agent.PURPLE,
    "生產管理": Agent.GRAY,
    "出納": Agent.BLACK,
    "法務": Agent.WHITE,
}


QUERY_MATRIX: Mapping[Agent, FrozenSet[Agent]] = {
    Agent.RED: frozenset(Agent),  # SUPER_ADMIN
    Agent.ORANGE: frozenset({
        Agent.YELLOW, Agent.GREEN, Agent.BLUE, Agent.INDIGO, Agent.WHITE,
    }),
    Agent.YELLOW: frozenset({
        Agent.ORANGE, Agent.BLUE, Agent.INDIGO, Agent.GRAY, Agent.WHITE,
    }),
    Agent.GREEN: frozenset({
        Agent.ORANGE, Agent.INDIGO, Agent.WHITE,
    }),
    Agent.BLUE: frozenset({
        Agent.ORANGE, Agent.YELLOW, Agent.INDIGO, Agent.PURPLE, Agent.GRAY, Agent.WHITE,
    }),
    Agent.INDIGO: frozenset({
        Agent.ORANGE, Agent.GREEN, Agent.BLUE, Agent.GRAY, Agent.WHITE,
    }),
    Agent.PURPLE: frozenset({
        Agent.ORANGE, Agent.YELLOW, Agent.GREEN, Agent.BLUE, Agent.INDIGO,
        Agent.BLACK, Agent.WHITE,
    }),
    Agent.GRAY: frozenset({
        Agent.ORANGE, Agent.YELLOW, Agent.GREEN, Agent.BLUE, Agent.INDIGO, Agent.WHITE,
    }),
    Agent.BLACK: frozenset({
        Agent.ORANGE, Agent.YELLOW, Agent.GREEN, Agent.BLUE, Agent.INDIGO,
        Agent.PURPLE, Agent.WHITE,
    }),
    Agent.WHITE: frozenset(),  # SoT，不主動查
}


def can_query(caller: Agent, target: Agent) -> bool:
    """caller 是否被允許查 target。RED 永遠 True；部門可查自己。"""
    if caller is Agent.RED:
        return True
    if caller is target:
        return True
    return target in QUERY_MATRIX.get(caller, frozenset())
