"""Orange agent — Sales / 業務.

對應 dept_rules 的 "業務"。Phase 3c 把：
  - quote.py（quote history 抽取 + 查詢）
  - quote_batch.py（parquet batch 抽取）
  - quote_gen.py（Excel 報價生成）
  - customer_intel.py（客戶 360 / 活躍清單 / 警示）
全部搬入本子套件，原 agent_core/<file>.py 改為 re-export shim。

公開：
  - OrangeSalesAgent
  - quote.* / quote_batch.* / quote_gen.* / customer_intel.* — 真實 impl
"""
from agent_core.agents.orange_sales.agent import OrangeSalesAgent

__all__ = ["OrangeSalesAgent"]
