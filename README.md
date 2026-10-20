# AI 店长（ai-shopkeeper）

AI 店长（ai-shopkeeper）是一个面向跨境电商的 AI 工作流框架公开快照：本地 mock 可走选品、listing、审批与编排演示。当前实现仍以 TikTok Shop Thailand、家居收纳和 THB satang 为默认示例；Shopee 只读 CSV 导入属于局部能力，未实现任意站点或多币种运行。真实上架、首单履约和利润闭环仍须独立验收，涉钱动作在应用内走审批闸门（[DESIGN §2.1](docs/DESIGN.md)）。

本仓是另行导出的公开快照，不包含原私有开发仓的 Git 历史，也不包含原真店运营手册、第一周清单或调优 prompt 正文；[示例工作流](docs/thailand-home-workflow.md) 与 [通用 prompt 模板](prompts/README.md) 均为合成的 example 资料。根目录 [MIT LICENSE](LICENSE) 适用；内置 Noto 字体仍遵循 SIL OFL，见 [fonts.md](docs/fonts.md)。

## 当前进度

权威勾选与 Done 条件在 [DESIGN §9](docs/DESIGN.md)。设计基线：`DESIGN` **v1.120**（v1.0 于 2026-10-10 冻结，之后按实现增量修订）+ `SCHEMA` **v1.0**（冻结，43 表，PG15 空库实测通过）；工作流库 schema **v11**（`CURRENT_SCHEMA_VERSION = 11`）。生产运行见 [docs/PRODUCTION.md](docs/PRODUCTION.md)，首单前跑 python -m shopkeeper.workflow.preflight，步骤见 [docs/FIRST_ORDER_RUNBOOK.md](docs/FIRST_ORDER_RUNBOOK.md)。

| 里程碑 | 状态 | 说明 |
|---|---|---|
| **M1** 骨架 + 酷 demo | 软件侧已合；待维护者 | 离线一键 `make demo`、状态机矩阵、1688 反查结论（文档层面判定不通+降级，端到端未实测）已闭合；双库 DDL 有历史 PG15 证据，本轮无 Docker 未复跑。**人工抽检通过率 ≥ 80%** 仍待维护者填写（[phase2](docs/phase2-m1-demo-acceptance.md)） |
| **M2** 上架通道 + 审批闸门 | 软件侧已合；待维护者 | 八态审批、飞书卡片 stub、商品库、只读订单/SLA、台账 bootstrap、库存缓冲、可选重绘、Tauri 壳、平台网关等已合。**真店 TikTok 凭据 + 按 [phase5](docs/phase5-real-listing-acceptance.md) 用 1 个 SKU 验收真写**（或卖家中心 ≥3 SKU）仍待维护者；`TTS_WRITE_ENABLED` **默认锁定为 0** |
| **M3** 履约 + 客服闭环 | 软件侧已合；真履约待续 | 代发 mock、人工下单包（复制链接、回填单号、不付款）、物流回填（承运商格式校验，可选只读查询默认关）、`/admin/orders` 订单履约工作台（mock/manual 浏览器链、物流审批快照修订与恢复）、已同步订单后台编排 `order_fulfillment_v1`（默认关）、客服白名单自动回复、编排器 v1、售价公式、日志告警、迁移备份、PDPA 配套等已合。**Open API 自动下单 / 承运商官方查询账号 / 首单完整交付**待续 |
| **M4** 利润看板 + 公开边界 | 利润表 + 广告费记账 + `/admin` 利润控制台 + agent 产出统计 + 公开评估文档已合 | 订单级利润只读 API / CSV + `/admin/profit` 与 `/workflow` 利润区块；`/admin/agents` 只读产出统计；MIT 许可、合成示例与发布边界见 [公开框架快照评估](docs/M4-open-source-assessment.md) |

> 本仓是公开框架快照，不含原私有开发仓的历史。本地演示与既有测试不能替代真实店铺、外部 API 或生产验收。

本地验收链：[Phase 1](docs/phase1-local-acceptance.md) → [阶段 2 Demo](docs/phase2-m1-demo-acceptance.md) → [阶段 3 上架](docs/phase3-m2-listing-acceptance.md) → [阶段 4 商品库](docs/phase4-catalog-acceptance.md) → [阶段 5 真写](docs/phase5-real-listing-acceptance.md)。

## 快速开始

本地试用保持 `.env` 里 `WORKFLOW_PLATFORM=mock` 与 `WORKFLOW_LLM_PROVIDER=mock`：可走审批闸门上架到本地 mock ID，不访问 TikTok。不填密钥也能用——订单/商品为空并标明离线，文案用本地模板。先用 `python -m shopkeeper.cli demo-orch` 走完整 mock 演示；[合成三对象样例](examples/synthetic-workflow.json) 只展示订单/listing/审批字段形状，不是可导入平台的载荷。适配器契约入口见 [DESIGN §2.1](docs/DESIGN.md) 与 [tests/adapters/](tests/adapters/)：实现新平台前先写 `MockXxx`，同契约测试通过后才接真实读写，默认真写仍关。

> **端口**：Windows 上 `8000` 常被系统占用。下面示例默认用 **`WORKFLOW_PORT=8010`**（也可写入 `.env`）。Docker 映射为宿主机 **8780**。

正式管理后台：http://127.0.0.1:8010/admin （须先创建管理员，见下）。开发自测面板仍在 http://127.0.0.1:8010/workflow 。

创建管理员（本地账号，密码 pbkdf2 哈希，勿把真实密码写进 Git）：

```bash
PYTHONPATH=src python3 -m shopkeeper.workflow.admin_cli create-user \
  --username owner --password '换成你的强密码' --role owner
```

角色：`owner`（含审批通过/打回，含 AI 工作流审批节点）、`operator`（可写不可审批）、`viewer`（只读）。有管理员后，`/api/workflow` 与 `/api/orchestrator` 写类 API 须登录会话或 `WORKFLOW_API_TOKEN`；机器调用继续带 `X-Workflow-Token`。账号表在工作台库 migrate **v4**（已升到 v5/v6/v7 的旧库会幂等补建）。前端源码在 `apps/admin`（Vite，无外网 CDN）；产物由 FastAPI 托管于 `/admin`（含「AI 工作流」、「编排运行」与「达人建联」页）。若改了前端：

```bash
cd apps/admin && npm ci && npm run build
```

### Windows（推荐一键脚本）

面向中文 Windows 新手卖家（控制台常为 GBK；`8000` 常被系统占用）。在仓库根目录用 PowerShell：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\windows\install.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\windows\start.ps1
```

或安装后双击 `scripts\windows\start.bat`。脚本会检查 Python 3.12、创建 `.venv`、安装依赖；若无 `.env` 则从 `.env.example` 复制占位（**不写入任何密钥**）；默认 `WORKFLOW_PORT=8010`，占用则自动顺延；**不修改** `TTS_WRITE_ENABLED`（保持 `0`）。可选桌面快捷方式：给 `install.ps1` 或 `start.ps1` 加 `-CreateShortcut`。

浏览器打开 http://127.0.0.1:8010/admin （须先 `admin_cli create-user`；若端口被顺延，以脚本提示或 `.env` 中的 `WORKFLOW_PORT` 为准）。开发自测页为 `/workflow`。不要把真实密钥提交进 Git。

手动步骤（脚本不可用时）：

```bat
copy .env.example .env
python -m venv .venv
.venv\Scripts\activate
python -m pip install -e .
set PYTHONPATH=src
set WORKFLOW_PORT=8010
python -m shopkeeper.workflow.serve
```

### Linux / macOS

```bash
cp .env.example .env
python -m pip install -e '.[dev]'
export WORKFLOW_PORT=8010
make dev
# 等价：PYTHONPATH=src python -m shopkeeper.workflow.serve
```

浏览器打开 http://127.0.0.1:8010/admin （须先 `admin_cli create-user`）；开发自测页为 `/workflow`。

把候选表、指标表路径写进 `.env` 的 `RESEARCH_INPUT_PATH` / `DAILY_METRICS_PATH` 后，曼谷时间 08:30 与 21:00 各写一份报表；文件不存在时只说明缺文件，不编造数字。

自检：`make check`（默认 `pytest -m "not real"`）。离线 Demo：`make demo`。编排器 mock 全链路演示：`make demo-orch`（或 `python -m shopkeeper.cli demo-orch`；产物在 `artifacts/demo-orch/`）。有凭据时可选真 API 只读：`make test-real`（[REAL-TESTS.md](docs/REAL-TESTS.md)，不进默认 CI、不解锁 TTS 真写）。真写预检（只读，不外呼写接口）：`python -m shopkeeper.cli tts-preflight`——输出凭据/店铺/region/cipher/token 到期的脱敏报告，`exit 2` 表示有阻塞项；**它本身不写任何东西**。完整六步 CI：`powershell -File scripts/ci.ps1`（无 Docker 时加 `-SkipPg`）。

### Docker

```bash
cp .env.example .env   # 若尚无 .env
docker compose up -d --build
# 或：make up（无 .env 时会先复制 .env.example）
```

浏览器打开 http://127.0.0.1:8780/admin （须先创建管理员）；开发自测页为 `/workflow`。探活：`curl -sf http://127.0.0.1:8780/healthz`（无鉴权，只返回 `ok` / `version` / `publish_enabled`）。

容器非 root、`restart: unless-stopped`、挂载 `./artifacts`；默认 `TTS_WRITE_ENABLED=0`、`FEISHU_NOTIFY=0`、`BACKUP_DAILY=1`。宿主机只绑 `127.0.0.1:8780`，不要改成 `0.0.0.0:8780:8780`。重启：`bash scripts/restart_workflow.sh` 或 `docker compose restart shopkeeper`。非 Docker 部署见 [deploy/shopkeeper.service](deploy/shopkeeper.service)。

桌面壳（可选）：[apps/desktop](apps/desktop/README.md)——先起本机工作台，再 `npm run dev`；打开同一 `/workflow`，不另写 UI。三处入口分工见 [DESIGN §5](docs/DESIGN.md)。

## 功能模块一览

细节与 API 在链接文档；此处只标默认开关。

| 模块 | 作用 | 默认 | 详见 |
|---|---|---|---|
| 正式管理后台 `/admin` | 总览 / AI 工作流 / 编排运行（只读时间线）/ AI 代理（只读产出统计）/ 审批 / 客服 / 达人建联 / 商品 / 订单（含履约工作台）/ 报表（含广告费）等；本地登录 + CSRF | 须先 `admin_cli create-user` | [DESIGN §5](docs/DESIGN.md)、[apps/admin](apps/admin/README.md) |
| 泰国站工作台 `/workflow` | 草稿队列、审批、商品库、只读订单/SLA、报表调度、利润看板（只读；开发自测） | 开启（`make dev`） | [DESIGN §5](docs/DESIGN.md) |
| 多店铺最小切片 | 逻辑 `shop_id`（默认 `default`）隔离草稿/审批/订单/库存/客服/编排器；`TIKTOK_SHOPS` + 每店前缀凭据 | 单店兼容；列表可滤 `shop_id` | [DESIGN §5](docs/DESIGN.md) |
| 多平台订单只读 | `synced_orders` 以 `(platform, shop_id, order_id)` 隔离；Shopee CSV 只读导入；工作台可筛选 | TikTok 履约保持；Shopee 代发/物流写未接 | [DESIGN v1.119](docs/DESIGN.md) |
| 审批闸门（八态） | 涉钱/写店铺动作先建 `approvals`，批准后才执行 | 开启 | [DESIGN §2.1 / §6](docs/DESIGN.md)、[SCHEMA](docs/SCHEMA.md) |
| 平台适配器 | `mock` / `tiktok` 只读 / `tiktok_dry_run` / `tiktok_live` / `rpa_stub` | `WORKFLOW_PLATFORM=mock` | [phase3](docs/phase3-m2-listing-acceptance.md)、[phase5](docs/phase5-real-listing-acceptance.md) |
| TTS 真写 | `tiktok_live` 外呼创建商品；须白名单与日/次上限 | **关**（`TTS_WRITE_ENABLED=0`） | [phase5](docs/phase5-real-listing-acceptance.md) |
| 选品 / listing / 图像 | Scout→三语言文案；Pillow 后处理为主，可选重绘 | mock；`IMAGE_REDRAW_ENABLED=0` | [spike/image.md](docs/spike/image.md)、[fonts.md](docs/fonts.md) |
| 图片托管 `ImageStore` | 本地内容哈希去重；可选 S3 兼容骨架 | `IMAGE_STORE=local` | [DESIGN §8](docs/DESIGN.md) |
| 售价公式 | 成本+运费×占位费率反解建议 THB；偏离挂起审批 | 开启（占位费率） | [DESIGN §3.1](docs/DESIGN.md) |
| M4 利润看板 | 订单级利润（收入/佣金/支付/采购/运费/广告占位/毛利）；按店·日·周汇总；`GET /api/workflow/profit` + `profit-export` CSV；正式控制台 `/admin/profit`（`GET /dashboard/profit` 同 SPA）；工作台自测入口 `/workflow`「利润」区块；缺数据标「缺数据」；页脚注明占位费率；低于保本只提醒；无付款/退款/提现 | 只读开启；不解锁真写 | [DESIGN §9 M4](docs/DESIGN.md) |
| 广告费记账 | 工作台 `ad_spend`（migrate v8）；API 与 `ad-spend-import` CSV；利润与 CSV 扣记账广告费并给出 ROAS / ACOS / 扣广告后利润；保本 ROAS 与最高 CPA 只提醒；`/admin` 报表页与 `/workflow` 利润区展示。TikTok Ads 只读拉取默认 mock；`TIKTOK_ADS_ENABLED=1` 且凭据齐全时只 GET 同步报表，不改预算 | 记账开启；真拉取默认关；改预算关 | [DESIGN §5](docs/DESIGN.md) |
| 各 agent 产出统计 | `/admin/agents` 只读卡片：按流水线汇总运行/成功/QA 打回/待审/失败；`GET /api/admin/agents/stats` 与 run 时间线。缺 LLM 计量标「缺数据」。不加表、不写库 | 只读开启 | [DESIGN §5](docs/DESIGN.md) |
| AI 编排器 | 选品→定价→文案/图→QA→短视频脚本→达人建联草稿→审批→上架→订单→代发→物流→客服可恢复流水线。建联只入库待审，`/admin/outreach` 标记通过或拒绝，不发送。`/admin/runs` 只读看每次 run 的时间线 | API 可用；`ORCHESTRATOR_AUTORUN=0` | [DESIGN §3.1](docs/DESIGN.md)、[ORCHESTRATOR-LANGGRAPH.md](docs/ORCHESTRATOR-LANGGRAPH.md) |
| 编排端到端 mock 演示 | 一键串通全链路并写出 `artifacts/demo-orch/` 摘要与各步产物 | `make demo-orch`（强制 mock，不解锁真写） | [DESIGN §3.1 / §5](docs/DESIGN.md) |
| 平台操作网关 | 主系统与海外侧可分离；`local` 直连 / `remote` 长轮询 | `GATEWAY_MODE=local` | [DESIGN §2.1](docs/DESIGN.md) |
| 1688 货源（只读） | Open API 骨架；与代发分离 | **关**（`ALIBABA_1688_ENABLED=0`） | [DESIGN §5](docs/DESIGN.md) |
| M3 代发 mock | 代发草稿 + 八态 `dropship_order`；不外呼、不付款 | mock 开启 | [DESIGN §9 M3](docs/DESIGN.md) |
| M3 物流回填 | 承运商格式校验；可选只读查询默认关；物流审批快照修订走 CAS（新值换新审批，旧批准失效），REAPER 同步投影草稿，迟到结果不覆盖新修订；真写仍受 `TTS_WRITE_*` | 查询默认关 | [DESIGN §9 M3](docs/DESIGN.md) |
| 订单履约工作台 | `/admin/orders` 从已同步订单进入「查看 / 处理」：代发 → 人工采购单 → 物流草稿 → 审批 → 本地 shipment；服务器草稿/审批/shipment 为唯一进度来源；owner 审批、operator 执行获批步骤、viewer 只读；不自动下单、不付款、不发货、不退款 | 须登录；`ALIBABA_DROPSHIP_MODE=mock|manual` | [DESIGN §9 M3](docs/DESIGN.md) |
| 已同步订单后台编排 | 独立 `order_fulfillment_v1`（`order_load` → `dropship` → `tracking`）；按 `(order_id, shop_id)` 幂等启动（确定性 run 主键，跨进程不重复）；只接真实同步缓存订单，拒绝演示订单；缺真实运单挂起人工待办，工作台补录并批准后由 tick 或显式 resume 恢复 | **关**（`ORCHESTRATOR_ORDER_AUTORUN=0`）；开启后仍需审批 | [DESIGN §9 M3](docs/DESIGN.md) |
| M3 客服白名单回复 | 物流类免审 stub；退款等必审；收件箱只读拉取无契约则为 stub（未臆造端点）；不外呼 TikTok 客服。正式后台入口 `/admin/cs` | mock 开启 | [DESIGN §9 M3](docs/DESIGN.md) |
| 飞书审批卡片 | 建单发卡片；按钮推进八态 | **不外呼**（`FEISHU_NOTIFY=0`，落盘 stub） | [DESIGN §5](docs/DESIGN.md) |
| 日志 / 告警 | 统一 logging；可选失败 webhook | 日志开；`WEBHOOK_ALERT_URL` 空=告警关 | [error-codes.md](docs/error-codes.md) |
| 迁移 / 备份 | 工作台 `schema_version` 迁移 + WAL；`backup` CLI | 迁移开；`BACKUP_DAILY` 本机默认 0（Docker 默认 1） | [DESIGN §5](docs/DESIGN.md) |
| 泰国 PDPA 配套 | 日志脱敏；按保留期覆写买家 PII（工程默认，**非法律意见**） | 脱敏开；`PII_PURGE_DRY_RUN` 默认 1（演练，不写库） | [DESIGN §7.6](docs/DESIGN.md) |
| 合规词表 | 泰/英/中 block\|warn；命中阻断审批 | 开启 | [DESIGN §7.4](docs/DESIGN.md) |
| 真 API 只读测试 | TikTok / 1688 / LLM 最小调用 | **不进默认 CI** | [REAL-TESTS.md](docs/REAL-TESTS.md) |

`/workflow` 及其子页面在创建管理员后须先到 `/admin` 登录，浏览器使用会话与 CSRF；未建管理员且未配置机器口令时保留本机引导访问。`WORKFLOW_API_TOKEN` 仅用于机器请求的 `X-Workflow-Token` 头，不写入页面。`/api/workflow/*` 与 `/api/orchestrator/*` 保留会话与机器认证；有管理员后写操作按角色校验。`/healthz` 例外、无鉴权。

备份示例（恢复必须 `--confirm YES`）：

```bash
PYTHONPATH=src python3 -m shopkeeper.workflow.backup backup
PYTHONPATH=src python3 -m shopkeeper.workflow.backup restore --stamp 20261013T060000Z --confirm YES
```

## 配置变量

完整占位与注释见 [.env.example](.env.example)。真实密钥只放 `.env`（已 gitignore）。

| 变量 | 作用 |
|---|---|
| `WORKFLOW_PORT` / `WORKFLOW_HOST` | 工作台端口与绑定；本机示例建议 `8010`，Docker 用 `8780` |
| `WORKFLOW_PLATFORM` / `WORKFLOW_LLM_PROVIDER` | 平台与文案通道；本地试用保持 `mock` |
| `WORKFLOW_API_TOKEN` | 留空=仅本机回环；设置后须 `X-Workflow-Token` |
| `TIKTOK_APP_KEY` / `APP_SECRET` / `ACCESS_TOKEN` | 三者齐全才请求 TikTok Open API；可选 refresh |
| `TTS_WRITE_ENABLED` 等 `TTS_WRITE_*` | 真写总闸与白名单/日限；**默认全关** |
| `RESEARCH_INPUT_PATH` / `DAILY_METRICS_PATH` | 本地候选表与指标表；供曼谷定时报表 |
| `WORKFLOW_WEBHOOK_URL` | 报表额外 POST Markdown；留空则只落盘 |
| `LOG_LEVEL` / `LOG_FORMAT` / `WEBHOOK_ALERT_URL` | 日志级别与格式；可选失败告警（体不含密钥） |
| `BACKUP_DIR` / `BACKUP_KEEP` / `BACKUP_DAILY` | 备份目录、保留份数、是否每日备份 |
| `PII_RETENTION_DAYS` / `PII_PURGE_DAILY` / `PII_PURGE_DRY_RUN` | 买家 PII 保留与每日覆写（工程默认，非法律意见） |
| `ORCHESTRATOR_AUTORUN` / `LLM_DAILY_BUDGET_USD` | 编排器日自动开跑（默认关）；日 LLM 预算 |
| `FX_CNY_THB` / `PRICING_*` | 售价公式固定汇率与占位费率 |
| `GATEWAY_MODE` / `GATEWAY_TOKEN` | 平台网关 local/remote；token 只写 `.env` |
| `ALIBABA_1688_*` / `IMAGE_STORE*` / `FEISHU_*` / `LLM_FALLBACK_*` | 货源、图片托管、飞书、LLM 备用；默认安全关闭或 mock |

LLM / 图像本机网关与公网备路占位亦在 `.env.example`；实测裁决见 [docs/spike/](docs/spike/)。

## 安全边界

- **人工审批**：工作台「通过」只改草稿/审批态；未批准不得 publish / 代发 execute / 物流 push / 非白名单客服发送。
- **涉钱过闸门**：上架、改价、代发下单、投放等先生成 `approvals`（[DESIGN §2.1](docs/DESIGN.md)）；纯读与纯生成不过闸门。
- **发货 / 退款 / 付款**：应用接口固定拒绝（403）；只在 TikTok 卖家中心由你本人操作。
- **`TTS_WRITE_ENABLED=0`**：默认锁定，不外呼创建商品；开启须同时满足 platform、白名单与日/次上限（[phase5](docs/phase5-real-listing-acceptance.md)）。
- **密钥只放 `.env`**：禁止进入代码、文档、Git 历史与告警正文；Docker 宿主机绑定保持 `127.0.0.1`。
- **PII**：落库脱敏守卫 + 日志/告警输出脱敏 + 保留期覆写默认演练（`PII_PURGE_DRY_RUN` 默认 1，真覆写须显式改为 0）；备份可能含清理前明文，须轮转（[DESIGN §7.6](docs/DESIGN.md)）。

## 文档索引

| 文档 | 内容 |
|---|---|
| [docs/DESIGN.md](docs/DESIGN.md) | 产品目标、架构、模块、里程碑 Done（**v1.120**） |
| [docs/SCHEMA.md](docs/SCHEMA.md) | 数据模型、状态机、守恒式、DDL（**v1.0 冻结**，43 表） |
| [docs/M1-PLAN.md](docs/M1-PLAN.md) | 切片拆分、完成条件、write scope |
| [docs/P3-05-pg-assessment.md](docs/P3-05-pg-assessment.md) | 工作台 SQLite→PG 切换评估（**未实施**，附实测成本） |
| [docs/CONVENTIONS.md](docs/CONVENTIONS.md) | 写作规则、文档归属、评审结构、验证纪律 |
| [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) | 领任务 → 实现 → 验证 → PR → 合并 |
| [docs/M4-open-source-assessment.md](docs/M4-open-source-assessment.md) | 公开框架快照边界、能力限制与文件排除说明 |
| [docs/P3-02-dehardcode.md](docs/P3-02-dehardcode.md) | 站点画像与公开示例：展示文案、业务时区与运行时币种比较/文案已读画像；库内 satang 仍是 THB |
| [LICENSE](LICENSE) | MIT；Noto 字体的 SIL OFL 通知仍在 `src/shopkeeper/assets/fonts/` |
| [docs/error-codes.md](docs/error-codes.md) | 错误码登记 |
| [docs/fonts.md](docs/fonts.md) | 图像内置字体（Noto / SIL OFL） |
| [docs/ORCHESTRATOR-LANGGRAPH.md](docs/ORCHESTRATOR-LANGGRAPH.md) | 编排器 vs LangGraph 评估 |
| [docs/REAL-TESTS.md](docs/REAL-TESTS.md) | 真 API 只读集成测试 |
| [docs/spike/](docs/spike/) | 外部依赖裁决（LLM / 图像 / 1688 等） |
| [AGENTS.md](AGENTS.md) | 给 AI 开发者的常驻指令 |
| [apps/desktop/README.md](apps/desktop/README.md) | Tauri 2 桌面壳 |
| [prompts/README.md](prompts/README.md) | 通用 prompt 示例模板 |
| [n8n/README.md](n8n/README.md) | 提醒工作流存根（默认不激活） |

技术栈：Python 3.12、FastAPI、SQLAlchemy；开发 SQLite、上量 PostgreSQL。

## 合成示例

[本地 mock 工作流](docs/thailand-home-workflow.md)、[example 起步清单](docs/week-1-starter.md)、[通用 prompt 模板](prompts/README.md) 和 [synthetic-workflow.json](examples/synthetic-workflow.json) 都明确标为 example/synthetic。模板由使用者填入自己的合法商品事实与政策；草稿必须人工复核。`prompts/influencer-outreach.md` 若在仓库内存在，会被编排器读取，代码内安全约束与 JSON 输出要求始终追加。商品价带规则仍是泰国站示例，不能把模板名称理解为已支持多站点。

示例命令的结果不是生产验证。公开快照的文件来源、排除范围与能力限制见 [公开框架快照评估](docs/M4-open-source-assessment.md)；贡献路径见 [CONTRIBUTING.md](CONTRIBUTING.md)，安全披露见 [SECURITY.md](SECURITY.md)。
