# M1-A 外部依赖裁决表（Gate 工件）

> 上游：[DESIGN.md](../DESIGN.md) §9 M1-A ｜ 切片定义：[M1-PLAN.md](../M1-PLAN.md) §2
> 生成：2026-10-10 ｜ 状态：**M1-A Gate 通过**（A1–A4 全部闭合，见下）
> 纪律：本表只给**结论 + 指路**，正文在证据文件里（引用而非复制）。所有"实测"均指本轮真实运行，
> 未实测之处显式标注边界，不把内部一致说成"证明真跑"。

---

## 1. 裁决表

| # | 依赖 | 实测结论 | 接入方式（M1-B 采用） | 降级方案 | 成本与耗时 | 证据 |
|---|------|----------|----------------------|----------|-----------|------|
| A1 | **1688 商品链接解析** | **不稳定，默认预期失败**。移动版页面能匿名出全量数据，但单发命中率个位数百分比（统计 4/45≈9%），两组"连续 3 次"为 0/3 与 1/3。~~判活特征是 UA~~ → **Lead 复验反驳**：桌面 UA 单发曾取到 179309B 完整页，随后两种 UA 交替 0/6 全拦；真实机制是**概率性 + 频率/会话相关的风控** | **mock + 人工补录为主路**；真实解析仅作 best-effort 增强（单发、全新无 cookie 会话、**不重轰**） | ① 一次成功即落 `products.raw_snapshot` 持久复用，后续只重跑生成；② `ANTIBLOCK` → 转人工待办，补录 `title/price/currency/moq/main_images/detail_images/seller` | 抓取 15–20s/次（含退避），命中率低；本机 IP 当前处易拦状态，≥1h 内不铺量 | [1688-parse.md](./1688-parse.md) §1、§5、§6、**§11（Lead 复核修正）** |
| A2 | **LLM 三语言文案** | **可用**。本机网关 `group/auto-deepseek-v4-flash` 连续 3 次 schema 首过（Lead 抽查原始 JSON 证实 `attempt=1/schema_ok=True/finish_reason=stop`）；累计 15 次真实运行 13 次首过 | 文案主链路 = `group/auto-deepseek-v4-flash`；分类/评分小杯 = `group/auto-glm-5-3-flash` | 重试 2 次（共 3 次尝试）耗尽 → 结构化 `failed` + `degraded_action=人工待办`（已在 `retry.py` + Stylist 落地并测） | 22.6–36.2s/商品（3 语种）；5848–10357 tokens；**$0.0031–0.0058/商品**（厂商牌价折算，**非网关实扣**；GLM/agnes 无价目未折算） | [llm.md](./llm.md) §0、§3、§4、§9、§11 |
| A2b | **推理预算（工程硬约束）** | `reasoning_content` 与 `content` **共享 `max_tokens` 预算**：`max_tokens=4000` → **0/3 全败**；`12000 + reasoning_effort=low` → 3/3 首过。且 `reasoning_tokens` **恒报 0**（同批 run 思考流实测 7293/16717/21787 字符，Lead 抽查证实） | `max_tokens`/`reasoning_effort` **必须按任务路由配置，禁止全局常量**；成本核算只用 `completion_tokens` | — | 4000 预算那次白烧 12000 token 才失败 | [llm.md](./llm.md) §4、§9.3 |
| A3 | **图像重绘/翻译** | **图生图不可得**。网关唯一通的通道是**纯文生图**：`/v1/images/edits` 与 `generations+image` 均 **400**、`/variations` 404（Lead 本人复验 400 与 200 各一次）→ **喂不进 1688 源图**。DashScope 通义万相**无 key，未验证** | **本地 Pillow 后处理为主链路**（缩放白底补齐→水印遮盖→中文带抹除→目标语言叠加→残留三项检查→PNG 打标）；网关文生图仅用于**不承诺商品外观一致性**的场景图 | DESIGN §9 M1-B 授权的降级形态转正：图像失败 → `original_kept` + `needs_human=True`（已在 Stylist 落地并测） | 本地 96–113ms/张、**¥0**；网关文生图 8.7–13.4s/张、¥/张拿不到（无费用接口） | [image.md](./image.md) 裁决表、原始报错段；产出目检 `spike/image/out/10_processed_TH.png` |
| A3b | **泰/越字形渲染** | 越南语可本地自动叠加（Tahoma/SegoeUI 全覆盖）；**泰语仅 Tahoma 有字形，Pillow 无 Raqm 排版引擎**，不承诺任意句子；GLM 泰语另有同文档拼写不一致硬伤（`น้ำหนักเบว`/`เบา`） | TH 图片产物与 GLM 泰语文案 **必须人工抽检**，不得自动放行 | 抽检不通过 → 回退英文图 + 人工处理 | — | [image.md](./image.md) 字形段 + `spike/image/out/05_font_render_compare.png`；[llm.md](./llm.md) §6 |
| A4 | **1688 订单反查（外部单号）** | **不通**（文档层面判定；无企业资质，端到端实测未做，坐实条件见其 §3.3）。下单入参无外部单号字段；`getBuyerOrderList` 仅按时间+状态过滤 | M1 不涉及；M3 履约按其三级降级落地 | 一级=时间窗+商品指纹**模糊反查**（自动判决只写 `RECONCILED`，**不冒充 `CONFIRMED`**）→ 二级=人工核对（飞书卡+`recon_diffs`）→ 三级=作废重下（须二级前置+审批） | — | [alibaba-order-lookup.md](./alibaba-order-lookup.md) §3.1、§4、§5 |
| A0 | **DDL 可建性**（M1-A 首项，R8 已前置闭合） | 冻结 43 表 DDL 在 PG15 空库建库通过；本轮 C1 再次实测复现（表数=43，`ON_ERROR_STOP=1`） | 全量 DDL 走 [ddl.sql](../ddl-verify/ddl.sql)；M1 业务表走 SQLAlchemy 双库 | — | 本轮双库校验见 `m1-ddl-verify.log` | [DESIGN.md](../DESIGN.md) §9 M1-A 第 1 项；`docs/ddl-verify/pg15-ddl-verify.log`、`m1-ddl-verify.log` |

---

## 2. M1-B 用什么（一句话可执行）

| 适配器 | M1-B 实现 | 理由出处 |
|--------|-----------|----------|
| `ProductSource` | **`MockProductSource` + 人工补录**（真实移动页解析作为可选 best-effort，**不进 Done 判据的关键路径**） | A1：默认预期失败 + 本机 IP 易拦 |
| `LlmClient` | **真实网关** `group/auto-deepseek-v4-flash`，`max_tokens≥12000`、`reasoning_effort=low`、`timeout≥240s`、重试 2 次 | A2 / A2b |
| `ImageClient` | **本地 Pillow 后处理**（`mode=post_processed`），失败降级 `original_kept + needs_human` | A3 |
| `PlatformAdapter` | **仅 `MockPlatform`**（真平台通道 M2 落地，依赖 TikTok 入驻 + 海外 IP） | DESIGN §10 |
| 语种 | 配置项 `LISTING_LANGS`，缺省 `EN,TH,VI`；站点未定前不改代码 | [M1-PLAN §0](../M1-PLAN.md) |

## 3. Gate 判定

- A1–A4 四项**全部闭合**（A1 含 Lead 复核修正、A2 含两条工程硬约束、A3 含与冻结设计的冲突上报、A4 给出"不通"而非 UNKNOWN）→ **M1-A Gate 通过，M1-B 放行**。
- 上表所有"未验证"项均已标注边界与坐实条件，无一项以推测冒充实测。

## 4. 上报待拍板（不改冻结文档）

1. **图像链路降级转正**：DESIGN §8/§7.3 假设"可重绘源图"，实测证明本机不可得 → 是否接受 M1-B 用 Pillow 后处理，真重绘推 M2（配通义万相/即梦 key）？
2. **SCHEMA §2.3 口径瑕疵**（A4 发现）：正文"按 `request_key` 轮询 1688"中 `request_key` 非外部可查键 → 是否登记勘误？
3. **币种为推断值**（A1 §11.4）：1688 页面无显式币种字段，`CNY` 由 `¥` 符号推断 → 与 R8 L6（币种一致性校验）合并裁决，落 `products` 时是否要带"推断"标记？
4. **R8 遗留 L6/L7**：币种一致性式、`kpi_daily` 罚分列 → M1 补还是留 M4？
