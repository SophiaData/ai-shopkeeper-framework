"""选品官 Scout（DESIGN §3.2）。只读采集 + 出报告，不创建上架物。"""

from shopkeeper.scout.pipeline import ScoutPipeline, idempotency_key_for

__all__ = ["ScoutPipeline", "idempotency_key_for"]
