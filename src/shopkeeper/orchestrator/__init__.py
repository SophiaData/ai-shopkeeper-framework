"""AI 工作流编排器（DESIGN §3.1 Orchestrator）。

声明式流水线：选品 → 定价 → 文案/图 → QA → 短视频脚本 → 达人建联草稿 → 人工审批（八态）→ 上架
→ 订单 → 代发 → 物流 → 客服。状态落工作台 SQLite，进程重启可断点续跑。
涉钱写操作仍经审批闸门；默认不打开真实 TikTok 写入。
"""

from shopkeeper.orchestrator.definition import PIPELINE_V1, FlowDefinition
from shopkeeper.orchestrator.engine import Orchestrator

__all__ = ["PIPELINE_V1", "FlowDefinition", "Orchestrator"]
