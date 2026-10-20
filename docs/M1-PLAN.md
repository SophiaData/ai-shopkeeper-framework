# M1 执行切片计划（Plan 工件）

> 上游权威：[DESIGN.md](./DESIGN.md) §9（M1 Done 条件唯一权威）· [SCHEMA.md](./SCHEMA.md)（数据边界）
> 本文件只做**拆分与完成条件**，不复制 DESIGN/SCHEMA 正文（引用而非复制）。
> 版本：v1.0 ｜ 建立：2026-10-10 ｜ 状态：M1 软件侧已合，10 样本人工抽检待维护者（内部执行路线图不随本公开快照发布）

## 0. 本轮落定的取舍（Brainstorm 残留项处理）

| 项 | 决定 | 依据 / 后果 |
|----|------|------------|
| LLM 凭据 | **用本机 magpie 网关** `http://127.0.0.1:3425/v1`（OpenAI 兼容），主模型 `group/auto-deepseek-v4-flash`；公网备路 `https://apihub.agnes-ai.cn/v1` | 网关 chat 端点已实测返回 usage（2026-10-10 18:2x）；凭据只进 `.env`（gitignore），禁止入代码与 Git 历史 |
| 图像能力 | 网关配置项 `imageGen: workbuddy/hunyuan-image-v3.0-art`，**M1-A 实测裁决** | 不通则按 DESIGN §9 M1-B 降级：原图保留 + 标记人工处理 + Pillow 本地后处理 |
| 三语言语种 | **不硬编码**：语种列表进配置，缺省 `EN,TH,VI`。目标站点已定为 TikTok Shop Thailand，见 [DESIGN §12.1](./DESIGN.md) | 合规词表按站点配置（DESIGN §7.4），M1 只落"站点→语种/词表"的映射结构 |
| Demo 输入 | 由实现方自行寻找一个**公开 1688 商品详情页**做解析验证（用户 2026-10-10 拍板） | 若被反爬拦，如实记为 M1-A 阻塞项并走"手动补录必要字段"降级（DESIGN §9 M1-B 失败降级） |
| 远端仓库 | GitHub 本机超时 → **仅本地 commit**，远端推送留到海外 IP 环境就绪（DESIGN §10 硬依赖） | M1 Done 条件不含远端 CI，CI 以本地可跑命令为准 |
| 数据库 | 开发 SQLite；Docker `postgres:15` 做双库建库与 join 查询验证 | 沿用 DESIGN §8；DDL 证据见 [ddl-verify/](./ddl-verify/) |

## 0.1 M1-A 已定案结论（逐条有证据文件，随 Spike 完成增量更新）

| 依赖 | 裁决 | 证据 | 对 M1-B 的影响 |
|------|------|------|----------------|
| **图像重绘** | **网关只能纯文生图，图生图三条路全封死**（`/v1/images/edits` 与 `generations+image` 均 400、`/variations` 404；Lead 本人复验 400 与 200 各一次）；DashScope 通义万相无 key 未验证 | [spike/image.md](./spike/image.md) | **与 DESIGN §8「翻译重绘 API」+ §7.3「重绘流程内嵌水印检查」冲突**：唯一通的通道喂不进 1688 源图。M1-B 主链路改为**本地 Pillow 后处理**（实测 96–113ms/张、¥0、已实产出并目检）；网关文生图仅用于不承诺商品外观一致性的场景图，且强制 `ai_generated` 打标 + 人工抽检 |
| **泰语/越南语字形** | 越南语可本地自动叠加（Tahoma/SegoeUI 全覆盖）；**泰语仅 Tahoma 有字形且 Pillow 无 Raqm 排版引擎**，不承诺任意句子 | 同上 + `spike/image/out/05_font_render_compare.png` | TH 语种图片产物**必须人工抽检**，不得自动放行 |
| **1688 订单反查** | **不通**（文档层面判定；无企业资质，端到端实测未做，坐实路径见其 §3.3） | [spike/alibaba-order-lookup.md](./spike/alibaba-order-lookup.md) | 闭合 DESIGN §9 M1-A 第 4 项与 M1 Done 第 4 条；M3 采用其三级降级（一级已重定义为时间窗+商品指纹**模糊反查**，自动判决只写 `RECONCILED` 不冒充 `CONFIRMED`）；`probe_strategy=query_by_request_id` 标 unavailable，过渡写 `'none'`；**与 M1-B 零耦合** |
| **LLM 文案** | 网关 `group/auto-deepseek-v4-flash` 可用：连续 3 次 schema 首过（Lead 抽查原始 JSON 证实），22.6–45.0s/商品，约 $0.003–0.006/商品（**厂商牌价折算，非网关实扣**） | [spike/llm.md](./spike/llm.md) | 文案主链路用 flash；分类/评分小杯用 `auto-glm-5-3-flash`（2.7s，但**泰语拼写有硬伤**→TH 批量需人工抽检）；**大杯 pro 不进主链路**（504 一次、240.6s、900-token 小任务被思考吃满预算 0 产出） |

## 0.1.1 进实现就必须遵守的硬约束（M1-A/B 实测发现，非推测）

1. **`reasoning_content` 与 `content` 共享 `max_tokens` 预算。**
   实测：`max_tokens=4000` → **0/3 全败**（重试 2 次耗尽→人工待办）；提到 `12000` + `reasoning_effort=low` → 3/3 首过。
   → `LlmAdapter` 的 `max_tokens` / `reasoning_effort` **必须是按任务路由的配置项，禁止全局常量**。
   缺省底线：文案 `max_tokens ≥ 12000`、`timeout ≥ 240s`、`reasoning_effort=low`、重试 2 次→人工待办。
2. **成本与预算告警不得使用 `reasoning_tokens`。**
   实测：该字段在 DeepSeek 路由上**恒为 0**，而同批 run 的 `reasoning_content` 实测 7293 / 16717 / 21787 字符
   （Lead 抽查原始 JSON 证实）→ 用它核算会长期显示"思考没花钱"。只用 `completion_tokens` 总量。
3. **合规信号必须分三轨，不许合并**（B4 真机跑出来的，单测抓不到）：
   - `compliance_flags` = **词表硬命中**，唯一可阻断（DESIGN §7.4/§7.5）；
   - `advisory_flags` = 模型自查提示（"疑似产地声明""主观用词"这类），**只给人看，不参与阻断**；
   - `leak_flags` = 供给侧信息泄漏，需人工处置。
   混轨的实测后果：模型对同一份正常童装文案吐出"洋气潮—主观用词""韩国—疑似产地"，
   导致 3 语种**全被判违规**（3 次运行里 2 次如此）——demo 直接失去意义。
4. **供给侧信息必须确定性拦截，prompt 禁令不可靠。**
   实测：prompt 明写"不得出现 MOQ/批发/成本价"后，模型**仍在 3 次里 2 次**写出 `"MOQ 1"` /
   `"order from just 1 piece"`。且根因是**我们自己的 prompt 把 MOQ/成本喂给了模型**。
   → 两处都改：prompt 不再输入供给侧事实；输出侧加确定性 `scan_supplier_leakage`
   （moq / minimum order / wholesale / dropship / 起订 / 批发 / 一件代发 / 成本价）。
   无货源模式下这类词出现在买家可见文案 = 自曝代发，比合规词命中更伤。
5. **词表匹配必须词边界 + 词干屈折。**
   实测：朴素子串匹配把 `"Secure packaging"` 命中成 `cure`（误杀正常商品）；
   而 `cure(?:s|es|ed|ing)?` 又漏掉 `"fully cured"`（silent-e）→ 用词干 `cur(?:e|es|ed|ing)?`。
   CJK 无词边界概念，仍走子串（`治愈率90%` 必须命中 `治愈`）。
6. **1688 的"图片 CDN"与"商品详情页"是两套封锁策略**（B4 实测，对 M2 排期有影响）：
   `cbu01.alicdn.com/img/...` 匿名 GET **200 / 215KB 真图**，而 `detail.1688.com` / `m.1688.com` 页面被 punish 壳拦。
   → **图像腿可以是真的，商品事实腿不行**。M2 若要补商品事实，方向是"图片先取到、文字字段人工补"，
   而不是继续攻页面。
7. **生成物必须人眼看，单测绿不等于交付对。**
   B4 三个缺陷（擦掉商品本体、水印区被当文字带、泰语标题溢出截断）**全部**在 299 条单测通过的情况下发生。
   → M1-B 起，图像/文案类产出要有"抽检看图"这一步（也正是 DESIGN §9 "人工抽检 ≥80%" 存在的理由）。
8. **跨平台是硬约束，不是收尾工作**（2026-10-12 在 macOS arm64 实测）。
   - 启动崩溃：`NO_PROXY` 含 `[::1]`（macOS 常见写法）时，httpx 抛 `InvalidURL: Invalid port ':1]'`，链路直接崩。
     实测：`NO_PROXY=localhost,127.0.0.1,::1,[::1]` → 崩；去掉 `[::1]` → 通过。
     → 出站客户端构造必须容错（或启动自检并在解析失败时给出可读提示），不得依赖"环境变量恰好干净"。
   - 图像链路仅 Windows 可用：`pillow_image.py` 取 `SYSTEMROOT` 默认 `C:\Windows` 找字体，
     字体表全为 `segoeui.ttf` / `tahoma.ttf` 等 Windows 字体；macOS 无此目录，字体缺失直接失败。
     macOS 实测 `tests/adapters/test_pillow_image.py` **15 failed**（根因 `IMG_FONT_MISSING`）。
     → 跨平台 MVP 要求图像腿在两端可用且产出一致，字体探测与后备方案属前置工作，不是收尾项。
   - 本仓库既有产物均出自 Windows 环境（`ci.ps1`、`spike/*.md` 的运行环境戳记）；
     迁移到 macOS 时不得假设既有验证结果在新平台自动成立。

## 0.2 待你拍板（M1-A/B 跑出来的新冲突，我不擅自改冻结文档）

1. ~~**图像链路降级转正**~~ **✅ 已拍板（2026-10-12）**：接受「M1 主链路 = 本地 Pillow 后处理 + 原图保留 + 人工抽检」，真重绘推 M2。已落 [DESIGN §8](./DESIGN.md)（图像行改写）与 §7.3（水印检测边界）；DESIGN 升至 v1.1。
2. ~~**SCHEMA §2.3 口径瑕疵**~~ **✅ 已勘误（2026-10-20）**：`request_key` 只存在于 `external_writes` 账本，1688 侧无从反查，正文已改为"按 `external_po_id` 轮询"并附勘误说明；`probe_strategy=query_by_request_id` 按 A4 标 unavailable，过渡写 `'none'`。
3. **R8 遗留 L6/L7/L8 落点**：币种一致性（C9？）、`kpi_daily` 罚分列、幂等键冲突用例——本轮 B2 已把幂等键冲突用例落地（`tests/db/test_m1_schema.py`），L6/L7 仍待你定 M1 补还是留 M4。
4. **供给侧泄漏是否升格为 §7 硬规则**（B4 新发现）：目前实现为"确定性检出 + 转人工"，
   **没有**当成 §7 的"闸门直接拒绝、不提供跳过"。理由：§7 现有 8 条不含此项，是否升格属业务裁量。
   若你认可，建议新增 §7.9：*买家可见文案不得出现供给侧事实（MOQ/批发/起订/一件代发/成本价），
   检出即阻断上架，须人工删除*。
5. **币种为推断值**（A1 §11.4）：1688 页面无显式币种字段，`CNY` 由 `¥` 符号推断；
   与 L6 币种一致性合并裁决——落 `products` 时是否要带"币种=推断"标记位？

## 1. 切片原则

- **垂直切片**：每片切穿"入口→任务→适配器→落库→产出"，能独立演示或独立验证；禁止按技术层横切。
- 每片必须有**可执行的完成条件**（命令 + 期望输出），没有新鲜证据不得标 completed。
- 依赖用 `blocked_by` 表达；M1-A 的 A4（1688 反查）**不阻塞** M1-B（DESIGN §9/§10 明示）。

## 1.1 MVP 切片（2026-10-12 起生效）

依据 [DESIGN §12.1](./DESIGN.md) 已定的 MVP 口径：**本地全流程跑通 + GUI/Web 双端 + macOS 与 Windows**，不含真实上架。

M1-A/B/C 的既有切片（§2–§4）是已完成的历史切片，保留不动。MVP 的新增工作按本表推进；编号独立，不续用 A/B/C。

| ID | 切片 | 完成条件（可验证） | blocked_by | write scope |
|----|------|--------------------|------------|-------------|
| P1 | 跨平台环境可用 | macOS 与 Windows 上 `pytest -m "not real"` 全绿（含 `test_pillow_image.py`；字体见 [fonts.md](./fonts.md)）；`NO_PROXY` 含 `[::1]` 时链路不崩 | — | `src/shopkeeper/adapters/pillow_image.py`, `src/shopkeeper/assets/fonts/`, `src/shopkeeper/config.py` |
| P2 | 图像腿跨平台一致 | 同一输入在两端产出可打开、尺寸一致、文字不溢出的图；EN/VI 自动、TH 抽检的既有边界不变 | P1 | `src/shopkeeper/adapters/pillow_image.py`, `tests/adapters/` |
| P3 | 本地全流程闭环 | 一条命令在两端跑完：链接 → 选品报告 → 三语言 listing → 商品图 → 落库；产物可打开 | P2 | `src/shopkeeper/cli.py`, `src/shopkeeper/stylist/` |
| P4 | 前端工程初始化 | Next.js + TypeScript 工程就位，能消费现有 FastAPI 端点（补 CORS）；`pnpm build` 通过 | P3 | 新建前端目录（待 Tauri 切片时定名） |
| P5 | 本地 API 服务化 | 后端可一条命令起服务，前端可连；端点对齐 [DESIGN §5](./DESIGN.md) | P4 | `src/shopkeeper/api/` |
| P6 | Tauri 壳 | macOS 与 Windows 均能出可运行 GUI，加载同一份前端构建产物 | P5 | Tauri 工程目录 |
| P7 | MVP 验收 | 两端各跑一遍完整本地流程，人工确认产物；验收记录落 `docs/` | P6 | `docs/m1-verify.md` 或新增验收记录 |

**硬约束**：P1 是其余全部切片的阻塞项。原先写「当前图像腿在 macOS 完全不可用」，该描述已被实测推翻：本机 macOS（Pillow 12.3.0、FreeType 可用、**Raqm 不可用**）上 `test_pillow_image.py` 与 `test_image_redraw.py` 共 28 例全绿，图像腿可用。Raqm 缺失的影响是有界的：TH 声调位置转人工（`HUMAN_REVIEW_LANGS`，见 [fonts.md](./fonts.md)），不作为 P1 的阻塞理由。P1 剩下的唯一未竟项是 Windows 侧全绿，详见 §1.1.1。

### 1.1.1 P1 验收进度（2026-10-20）

三条完成条件逐条对账，**部分达成，尚未可标 completed**：

| 条件 | 状态 | 证据 |
|---|---|---|
| macOS 上 `pytest -m "not real"` 全绿（含 `test_pillow_image.py`） | ✅ 达成 | 见下代码块，1843 passed / 0 failed；`test_pillow_image.py` 21 passed |
| `NO_PROXY` 含 `[::1]` 时链路不崩 | ✅ 达成 | 见下代码块，本机环境变量实为 `localhost,127.0.0.1,::1,[::1]`，`TikTokShopClient` 构造成功 |
| Windows 上 `pytest -m "not real"` 全绿 | ⛔ **未验证** | 本机无 Windows；GitHub Actions 因账户计费问题无法启动（job 未运行即被拒绝）。`check_deploy.py` 的 Windows 环境变量缺失已修（`37dbae2`），但这只是静态可达性，不能替代真机全绿 |

```text
$ PYTHONPATH=src python -m pytest tests -m "not real" -q -o addopts="" --tb=no
1843 passed, 2 skipped, 11 deselected, 1 warning in 144.40s (0:02:24)

$ PYTHONPATH=src python -m pytest tests/adapters/test_pillow_image.py -q --tb=no
.....................                                                    [100%]   # 21 passed

$ PYTHONPATH=src python -m pytest tests/adapters/test_pillow_image.py tests/adapters/test_image_redraw.py -q
............................                                             [100%]   # 28 passed

$ python -c "from PIL import Image, features; print(Image.__version__, features.check('raqm'))"
12.3.0 False        # Raqm 不可用 → TH 声调位置转人工，见 fonts.md

$ echo $NO_PROXY
localhost,127.0.0.1,::1,[::1]

$ PYTHONPATH=src python -c "from shopkeeper.adapters.tiktok_shop import TikTokShopClient, TikTokCredentials; TikTokShopClient(TikTokCredentials(app_key='k',app_secret='s',access_token='t',shop_cipher='ROW_x'),allow_writes=False)"
✅ TikTokShopClient 构造成功，链路不崩
```

**卡点**：第三条要等 CI 恢复（GitHub Billing & plans 处理支付 / spending limit）或拿到 Windows 真机后才能贴出输出。在此之前 P1 不标 completed，P2 不开工。

## 2. M1-A 外部依赖 Spike（0.5–1 天，有时限）

| ID | 切片 | 完成条件（可验证） | write scope |
|----|------|--------------------|-------------|
| A1 | 1688 商品链接解析 | 对真实公开 offer 页连续 3 次抓取，脚本稳定产出 `{title, price, stock, main_images[], detail_images[]}`（缺字段显式 `null`）；裁决写进 `docs/spike/1688-parse.md`（可用 / 需 Cookie / 不可用→手动导入） | `spike/alibaba_parse/`, `docs/spike/1688-parse.md` |
| A2 | LLM 三语言文案 | 真实调用网关 ≥1 模型，从固定 fixture 商品产出结构化 JSON（`title/bullets/description/compliance_flags` × 3 语种）；记录单次**耗时与 token 成本**；写 `docs/spike/llm.md` | `spike/llm/`, `docs/spike/llm.md` |
| A3 | 图像重绘/翻译 | 真实调用图像能力产出 ≥1 张图到本地（或如实记录不可用 + 报错原文）；给出降级方案；写 `docs/spike/image.md` | `spike/image/`, `docs/spike/image.md` |
| A4 | 1688 订单反查裁决 | 查证 Open API 是否支持按外部单号反查；产出**通/不通 + 三级降级**（API 反查→人工核对→作废重下须审批）结论；不阻塞 M1-B；写 `docs/spike/alibaba-order-lookup.md` | `docs/spike/alibaba-order-lookup.md` |
| A5 | 外部依赖裁决表 | 汇总 A1–A4 为一张表：`依赖 / 结论 / 接入方式 / 降级方案 / 成本耗时`，逐条可定位到上面的证据文件 | `docs/spike/dependency-verdict.md` |

**M1-A Gate**：A5 存在且每行有出处；A1/A2/A3 各有当次运行输出。DDL 实测一项已由 R8 前置闭合（[pg15-ddl-verify.log](./ddl-verify/pg15-ddl-verify.log)）。

## 3. M1-B 最小可用 Demo（3–5 天）

| ID | 切片 | 完成条件 | blocked_by | write scope |
|----|------|----------|------------|-------------|
| B1 | 仓库骨架 + mock 适配器 | `pytest` 一条命令全绿；`MockProductSource/MockLLM/MockImage` 确定性输出（固定 seed/延时）；适配器接口 `ProductSource/LlmClient/ImageClient` 就位；`pyproject.toml` + 本地 CI 脚本 | — | `pyproject.toml`, `src/shopkeeper/adapters/`, `tests/adapters/`, `scripts/ci.ps1` |
| B2 | 最小四表 + 任务幂等 | `products/tasks/listing_versions/tool_calls` SQLAlchemy 模型；SQLite 空库建库通过；**同幂等键重复提交不产生重复产出**（单测证据） | B1 | `src/shopkeeper/db/`, `tests/db/` |
| B3 | 切片：链接 → 入库 + 选品报告 | 一条命令 `POST /products/from-1688`（或 CLI 等价）从真实链接产出入库记录 + 报告；任务状态可查；解析失败路径有明确错误码与人工待办；成功+失败两路测试 | A5,B2 | `src/shopkeeper/scout/`, `src/shopkeeper/api/`, `tests/scout/` |
| B4 | 切片：→ 三语言 listing + 商品图 | 在 B3 产出上生成 3 语种 `listing_versions` + 图片资产目录；**连续 3 次运行 ≥2 次 < 3 分钟**；每次成本/耗时有记录 | A5,B3 | `src/shopkeeper/stylist/`, `tests/stylist/` |
| B5 | Demo 入口 + Done 判据实测 | 一条命令跑完整 demo 并打印产物路径；DESIGN §9「M1 Done 条件」逐条实测（含人工抽检 ≥80% 的抽检表） | B4 | `src/shopkeeper/cli.py`, `docs/m1-demo-run.md` |

## 4. M1-C 工程化收口（2–3 天）

| ID | 切片 | 完成条件 | blocked_by | write scope |
|----|------|----------|------------|-------------|
| C1 | 双库建库 CI + join 查询 | `scripts/ddl-verify.*` 在 SQLite 与 Docker PG15 上均建库通过，日志落盘；关键查询以 **join 形态**写死（SCHEMA:75 登记项） | B2 | `scripts/`, `docs/ddl-verify/` |
| C2 | 订单状态机矩阵单测 | 14 事件 × 11 态**格子行为 100% 覆盖** + 三条可达性断言 + 两条路径级断言到 CLOSED（SCHEMA §2.1） | B1 | `src/shopkeeper/domain/order_state.py`, `tests/domain/` |
| C3 | 脱敏 / 错误码 / 重试边界收口 | JSONB 落库统一过脱敏守卫（SCHEMA §4.4 四类）；错误码表可定位；生成类重试 2 次后降级人工待办、执行类不自动重试（DESIGN §3.3）；迟到事件只回填 + 幂等键冲突 + 售后了结行级规则三项遗留单测补齐 | B3 | `src/shopkeeper/security/`, `tests/security/` |

## 5. Verify（独立上下文，双轴不许合并）

- V1：Standards 轴（是否符合 DESIGN §2.1 原则 + SCHEMA 口径）与 Spec 轴（是否忠实 M1 Done 条件）**两个独立 subagent 分别审**，报告并排呈现；Blocker 未清不放行 M1。
- Lead 收尾必须自己跑一遍验收命令并核对最终 diff，不转述审查结论。

## 6. 明确不做（防跑偏）

- 不做完整 43 张表的业务流程（订单域/结算域仅 C2 的纯函数状态机落地）
- 不发真实店铺、不做审批闸门与飞书卡片（M2）
- 不做多店铺矩阵、自建商城、数字人直播（DESIGN §1.3）
- 不做完整 Next.js 控制台。任务流日志的最小可用是 FastAPI `GET /tasks/{id}/logs` + `/logs/stream`（SSE）与 `/workflow` 内嵌视图；阶段 2 Demo 入口仍是 `make demo`，见 [phase2-m1-demo-acceptance.md](./phase2-m1-demo-acceptance.md)。
- ⚠️ **待 owner 确认的申报（Review P-M1，2026-10-11）**：DESIGN §9 M1-B 有一条
  "控制台提供商品输入、任务状态、生成结果和失败原因"，M1 目前**没有做控制台**——
  现状是 API（含 `/listings/generate`）+ `shopkeeper.cli`（demo/batch/idempotency）
  + 人工抽检表（`docs/m1-spot-check.md`）。这条之前既没实现、也没出现在本清单里，
  属于**静默缩水**，是双轴 Review 抓出来的。两个选项：
  (a) 把"控制台 UI"改判为 CLI/API 即算满足输入·状态·结果·失败原因四要素 → 认可即结；
  (b) M1 补一个最小控制台（预计要一个只读任务面板 + 贴链接表单）→ 需追加工作量。
  **默认按 (a) 处理但不算数，直到 owner 明确点头。**
