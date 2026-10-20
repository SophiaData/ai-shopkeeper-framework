# 管理后台前端（`/admin`）

Vite + 原生 JS，无外网 CDN。构建产物写入 `src/shopkeeper/admin/dist/`，由 FastAPI 静态托管。

```bash
cd apps/admin
npm ci
npm run build   # → ../../src/shopkeeper/admin/dist
npm test        # 产物冒烟（无 CDN、assets 非空）
```

开发时（需本机已起 `python -m shopkeeper.workflow.serve`）：

```bash
npm run dev
```

登录账号用 CLI 创建，见仓库根 README。

侧栏「治理 / 客服」对应 `/admin/cs`，复用 `/api/workflow/cs/*`（本地 inbox、草稿、白名单 stub 发送）。不外呼 TikTok 客服，不打开发货/退款。

侧栏「治理 / 达人建联」对应 `/admin/outreach`，复用 `/api/workflow/outreach/drafts`。通过或拒绝只改状态；没有发送按钮。`POST …/send` 由接口固定拒绝。

侧栏「运营 / 编排运行」对应 `/admin/runs`，只读投影已有编排 run 的逐步时间线。没有上架、付款、发货按钮，不改 `TTS_WRITE_*`。