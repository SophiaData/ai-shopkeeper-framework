# 首单真实运营 Runbook

## 0. 先跑只读体检

    python -m shopkeeper.workflow.preflight

输出每项为 PASS / WARN / FAIL。WARN 允许继续 mock/manual；真实首单要求全部关键项 PASS。

严格模式：

    python -m shopkeeper.workflow.preflight --require-real

当前真实首单预期会 FAIL 的项：TikTok 凭据、真实写闸门、1688 真实下单适配器、当日备份。这是有意的 fail-closed。

## 1. 环境

- `.env` 只放在本机，确认未被 git 跟踪。
- `TTS_WRITE_ENABLED=0` 时只能 mock/manual；开启真实写必须由 owner 明确同意并记录时间。
- `WORKFLOW_DB_PATH` 指向正式库；先确认最新备份存在。
- `WORKFLOW_WORKER_INLINE=1` 单机，或分离 Web/Worker 并让 `/readyz` 为 200。

## 2. 首单前检查

1. TikTok 凭据可用，平台模式为 live；先用只读接口验证店铺授权。
2. 只同步一条真实订单，人工核对 SKU、数量、收件人、地址、币种。
3. 代发步骤：manual 模式由人去 1688 下单并付款；系统只回填采购单号，不自动付款。
4. 物流步骤：人工填写真实承运商与运单号，核对后 owner 审批。
5. 审批通过后才允许平台写；失败原因从服务器读取，不重复点击。
6. 写后在 TikTok 卖家中心人工复核，不把本地 shipment 当成平台已发货。

## 3. 观察与回滚

- GET /readyz 必须 200；GET /metrics 观察 shopkeeper_jobs_dead。
- 任何 dead 任务先停 worker，导出 job_queue 与日志，再人工处理。
- 回滚：停 Web/Worker，用 BACKUP_DIR 最新备份替换 WORKFLOW_DB_PATH，启动后跑 /readyz 与 preflight。
- 不回滚数据库版本号；需要降级时用迁移前自动副本 workflow.db.premigrate.vN。

## 4. 当前硬限制

- 真实 1688 createOrder 适配器尚未实现；manual 模式需要人付款。
- PostgreSQL 运行时尚未替换 SQLite。
- 付款、发货、退款仍人工；系统不自动执行。
- 没有真实首单验收前，不得对外宣称真实运营已打通。

## 5. 单机边界

- 只启动一个 Web 实例；第二个实例会被 `workflow.db.lock` 拒绝。
- 停机走正常 shutdown，确保 worker 停止并 checkpoint WAL。
- `/readyz` 建议设置 `WORKFLOW_REQUIRE_BACKUP=1`，避免无备份直接接真实单。

## 6. 真实物流写前置

- `TIKTOK_SHIPPING_PROVIDER_ID` 必须是 TikTok 店铺可接受的承运商 ID；缺省时 `update_tracking` 返回 `TTS_SHIPPING_PROVIDER_REQUIRED`，不发请求。
- `TikTokLivePlatform.update_tracking` 已调用 `/order/202309/orders/{order_id}/shipping_info/update`；`dry_run=False` 才表示平台接口返回接受。
- 客服真实收件/发送、1688 自动 createOrder 仍未实现，preflight `--require-real` 会显式失败。
