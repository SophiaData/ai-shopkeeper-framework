"""平台操作网关（DESIGN §2.1 原则 5）：主系统 ↔ 海外侧可分离部署。

- `GATEWAY_MODE=local`（默认）：同进程直连 PlatformAdapter，不经队列。
- `GATEWAY_MODE=remote`：主系统 GatewayClient 入队；海外 GatewayServer 长轮询领任务并回传结果。
mock 回环用内存队列，不需要真海外 IP。写操作仍受 TTS_WRITE_* / 审批闸门；发货/退款在网关层硬拒。
"""

from shopkeeper.gateway.client import GatewayClient
from shopkeeper.gateway.config import GatewaySettings, gateway_settings_from_env
from shopkeeper.gateway.queue import MemoryJobQueue
from shopkeeper.gateway.server import GatewayServer

__all__ = [
    "GatewayClient",
    "GatewayServer",
    "GatewaySettings",
    "MemoryJobQueue",
    "gateway_settings_from_env",
]
