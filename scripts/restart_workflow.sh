#!/usr/bin/env bash
# 安全重启包装：优先 docker compose；否则提示 systemd。
# 不读写密钥；不打开发货/退款/付款。探活：GET /healthz
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PORT="${WORKFLOW_PORT:-8780}"
HEALTH_URL="http://127.0.0.1:${PORT}/healthz"

if command -v docker >/dev/null 2>&1 && [[ -f docker-compose.yml ]]; then
  if docker compose ps --status running 2>/dev/null | grep -q shopkeeper; then
    echo "restarting via docker compose…"
    docker compose restart shopkeeper
  else
    echo "starting via docker compose…"
    test -f .env || cp .env.example .env
    docker compose up -d --build
  fi
elif command -v systemctl >/dev/null 2>&1 && systemctl cat shopkeeper.service >/dev/null 2>&1; then
  echo "restarting via systemctl…"
  sudo systemctl restart shopkeeper
else
  echo "未找到运行中的 docker compose 服务或 systemd unit shopkeeper。" >&2
  echo "Docker:  cp .env.example .env && docker compose up -d --build" >&2
  echo "systemd: 见 deploy/shopkeeper.service" >&2
  exit 1
fi

# 给进程一点启动时间再探活
for i in 1 2 3 4 5 6 7 8 9 10; do
  if command -v curl >/dev/null 2>&1; then
    if curl -sf "$HEALTH_URL" >/dev/null; then
      echo "healthz ok: $HEALTH_URL"
      exit 0
    fi
  else
    if python3 -c "import urllib.request; urllib.request.urlopen('$HEALTH_URL', timeout=3)" 2>/dev/null; then
      echo "healthz ok: $HEALTH_URL"
      exit 0
    fi
  fi
  sleep 2
done

echo "restart 已发出，但 ${HEALTH_URL} 尚未就绪（可稍后重试 curl）。" >&2
exit 0
