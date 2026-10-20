"""海外网关 worker 进程入口：`python -m shopkeeper.gateway.serve`。

默认接内存队列不可用（需 remote hub）。本入口用于：
- GATEWAY_ROLE=hub：在主系统侧起队列枢纽（开发/单机）
- GATEWAY_ROLE=worker：海外侧长轮询 mock/真实 PlatformAdapter

真海外 IP 与真 TikTok 凭据不在本切片范围；默认 mock 平台。
"""

from __future__ import annotations

import argparse
import os

import uvicorn

from shopkeeper.adapters.mock import MockPlatform
from shopkeeper.gateway.config import gateway_settings_from_env
from shopkeeper.gateway.hub import create_gateway_hub
from shopkeeper.gateway.queue import MemoryJobQueue
from shopkeeper.gateway.server import GatewayServer


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Platform ops gateway (DESIGN §2.1)")
    parser.add_argument(
        "--role",
        choices=("hub", "worker"),
        default=os.getenv("GATEWAY_ROLE", "hub"),
        help="hub=队列枢纽；worker=海外长轮询执行器",
    )
    parser.add_argument("--host", default=os.getenv("GATEWAY_HUB_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("GATEWAY_HUB_PORT", "8090")))
    args = parser.parse_args(argv)

    settings = gateway_settings_from_env()
    token = settings.token or os.getenv("GATEWAY_TOKEN", "").strip()
    if not token:
        print("GATEWAY_TOKEN is required for hub/worker (do not commit secrets).", flush=True)
        return 2

    if args.role == "hub":
        queue = MemoryJobQueue(token=token)
        # 进程内单例：开发期 hub 与同机 worker 需共享队列时用 remote+HTTP，或同测 Memory。
        app = create_gateway_hub(queue, token=token, claim_wait_seconds=settings.claim_wait_seconds)
        print(f"[gateway-hub] http://{args.host}:{args.port} (token required)", flush=True)
        uvicorn.run(app, host=args.host, port=args.port, log_level="info")
        return 0

    # worker：当前最小可用仍以同进程 Memory 为主；HTTP claim 客户端可后续接上。
    # 独立 worker 进程若无共享内存，请与 hub 同测或后续加 HttpJobQueue。
    print(
        "[gateway-worker] standalone HTTP claim client not wired in this slice; "
        "use MemoryJobQueue loopback in tests, or run hub+client in-process.",
        flush=True,
    )
    platform = MockPlatform()
    queue = MemoryJobQueue(token=token)
    server = GatewayServer(
        platform=platform,
        queue=queue,
        token=token,
        claim_wait_seconds=settings.claim_wait_seconds,
    )
    print("[gateway-worker] idle memory queue — for mock demo only", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
