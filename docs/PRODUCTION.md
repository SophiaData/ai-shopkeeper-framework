# 生产运行与后台任务

本文说明如何把系统从本地预览部署成可运营服务，以及后台订单编排的持久任务模型。真实平台写入仍需 .env 凭据和审批；未配置时保持 mock/manual。

## 1. 进程模型

| 进程 | 命令 | 职责 |
|---|---|---|
| Web | python -m shopkeeper.workflow.serve | 登录、审批、工作台、只读 API、入队 |
| Worker | python -m shopkeeper.workflow.worker | claim job_queue、执行后台任务、重试/死信、心跳 |
| Scheduler | Web 内线程 | 每 30 秒把 scheduler_tick / orchestrator_tick 写入队列，不直接执行 |

默认 WORKFLOW_WORKER_INLINE=1：Web 进程内启动一个 worker，适合单机/本地预览。多进程部署可设 WORKFLOW_WORKER_INLINE=0，另起 worker 进程。

## 2. 关键环境变量

WORKFLOW_SCHEDULER=1
WORKFLOW_WORKER_INLINE=1
WORKFLOW_REQUIRE_WORKER=0
WORKFLOW_WORKER_POLL_S=1.0
WORKFLOW_WORKER_LEASE_S=90
WORKFLOW_DB_PATH=artifacts/workflow.db

- WORKFLOW_REQUIRE_WORKER=1 时，/readyz 在 90 秒内没有 worker 心跳会返回 503。
- job_queue 使用 dedupe_key 部分唯一索引；同一任务同时最多一个 pending/running。
- worker 崩溃后租约到期会自动回收；超过 max_attempts 落 dead，不静默丢失。

## 3. 健康与指标

- GET /healthz：进程存活与版本。
- GET /readyz：数据库可用、队列可读、可选 worker 心跳；失败返回 503。
- GET /metrics：Prometheus 文本，含队列 pending/running/dead 与存活 worker 数。

## 4. 启动

单机：

    python -m shopkeeper.workflow.serve

分离 worker（需先确认共享 SQLite 文件在目标文件系统上的锁语义；生产建议尽快切 PostgreSQL）：

    WORKFLOW_WORKER_INLINE=0 python -m shopkeeper.workflow.serve
    python -m shopkeeper.workflow.worker

Docker Compose 默认单容器内联 worker；worker 服务使用 split-worker profile 显式开启：

    docker compose --profile split-worker up -d

开启前在 .env 设 WORKFLOW_WORKER_INLINE=0、WORKFLOW_REQUIRE_WORKER=1。

## 5. 备份与恢复

- 迁移前自动落 workflow.db.premigrate.vN，保留最近 3 份。
- 每日备份由 BACKUP_DAILY=1 控制，目录 BACKUP_DIR，保留 BACKUP_KEEP 天。
- 恢复前先停 Web/Worker，用备份替换 WORKFLOW_DB_PATH，再启动并跑 /readyz。
- 最近一次完整演练（备份 → 破坏 → 恢复 → preflight/readyz/实例锁）记录保留在私有开发仓，不随本公开快照发布。

## 6. 真实首单前检查

1. TikTok 凭据已配置，TTS_WRITE_ENABLED 的开启经过 owner 明确同意。
2. 1688 走 manual 或已配置真实代发适配器；不自动付款。
3. 真实订单只同步一条，先人工核对收件信息与 SKU。
4. 代发、物流各自过审批；失败原因从服务器记录读取，不重复点击。
5. 观察 /metrics 的队列与 worker，确认没有 dead 任务。
6. 物流写入后到 TikTok 卖家中心人工复核，不把本地 shipment 当成平台已发货。

## 7. 当前明确未完成

- PostgreSQL 运行时尚未替换 SQLite；多容器共享 SQLite 只适合受控试用。
- 真实 1688 下单/付款、真实发货/退款、真实首单尚未验证。
- 多租户、限流、分布式锁、外部对象存储、正式告警接入仍属后续切片。

## 8. 单机实例锁与停机

- Web 启动时获取 `WORKFLOW_DB_PATH + .lock`；第二个 Web 实例会 fail-fast，避免两个进程同时写 SQLite。
- 停机时先停 scheduler/worker，再执行 `PRAGMA wal_checkpoint(TRUNCATE)`，减少 WAL 尾丢失。
- 生产建议设 `WORKFLOW_REQUIRE_BACKUP=1`；`/readyz` 会检查最近备份年龄（默认 36 小时）。
- 单机 SQLite 是当前受支持生产形态；PostgreSQL 是扩容路径，尚未替换运行时。

## 9. 本轮可靠性收口

- 调度入队 `run_after` 统一 UTC；claim 按时区解析比较，不再因 +07:00 字符串被延迟。
- worker 长任务每 lease/3 续租；失去租约后不写完成，交给 reaper。
- `WorkflowService.stop(timeout_s=...)` 返回线程是否全部退出；超时不做 WAL checkpoint，Web 保留实例锁。
- `/readyz` 与 preflight 只接受带 manifest 和 db 的有效备份；空目录不算备份。
- preflight 使用 SQLite `mode=ro`，不创建库、不迁移旧库、不改主库内容；WAL 库读取可能生成 `-wal`/`-shm` 辅助文件。
- 真实 TikTok 物流写已接 `shipping_info/update`，需 `TIKTOK_SHIPPING_PROVIDER_ID`；客服收件/发送与 1688 自动下单仍未实现。

## 10. 多平台订单（当前只读切片）

- TikTok：原有同步/履约/物流链保持不变。
- Shopee：仅 POST /api/workflow/orders/import/shopee 导入 CSV，落本地 platform=shopee 订单缓存；工作台可筛选查看，但行只读，不能代发或回传物流。
- 迁移 v11 后 synced_orders 主键为 (platform, shop_id, order_id)；同平台同订单号跨店仍拒绝，不同平台可同号。
- 编排与财务只处理 TikTok；Shopee 订单不会进入 mock 履约或 TikTok 财务口径。
- Shopee 真实 API、代发、物流写、库存/上架仍未接入。
