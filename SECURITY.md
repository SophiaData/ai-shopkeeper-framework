# 安全披露

本文件说明**如何报告安全问题**，以及本仓库处理凭据与买家数据的既有纪律。

> 上游权威：[DESIGN.md](./docs/DESIGN.md)（§7.6 脱敏）、[AGENTS.md](./AGENTS.md)（常驻规则）、
> [M4-open-source-assessment.md](./docs/M4-open-source-assessment.md)（开源缺口清单）。

---

## 报告安全问题

**请不要在公开 issue 里贴敏感信息。** 包括但不限于：

- API 密钥、App Secret、access token、店铺 cipher、Webhook 地址与验签密钥
- 买家姓名、电话、地址、聊天内容
- 订单号与任何可定位到具体买家或订单的标识
- 数据库文件、`.env` 内容、审计日志原文

`web_search` / issue / PR 评论 / 讨论区都是公开的，一旦贴出**无法撤回**。

### 披露渠道

| 事项 | 渠道 |
|---|---|
| 安全问题（含凭据泄漏、越权、注入） | 仓库 Security 标签页的私密通报（若该仓启用了 private vulnerability reporting）；未启用时先联系仓库维护者取得非公开渠道，切勿在公开 issue 放复现敏感数据 |
| 非敏感的普通 bug | 正常开 issue |

**响应预期**：本仓库为单人维护的早期项目，**不承诺响应时限**。
真实的安全问题会优先于普通功能需求处理。

### 报告时请提供

1. 影响范围（哪个接口 / 哪条路径）
2. 复现步骤（**用假数据**，不要贴真实凭据或 PII）
3. 你预期的行为 vs 实际行为
4. 若已知，附上修复建议

---

## 本仓库的既有安全纪律

这些**不是承诺**，而是**代码里已经在执行**的规则。报告问题时可以据此判断某个行为是不是缺陷：

### 凭据

- 密钥**只**从 `.env` 读取，`.env` 已在 `.gitignore` 内
- 日志、告警、审计输出前**经过脱敏**（`src/shopkeeper/security/`）
- 审计日志里的请求/响应会递归抹掉密钥字段，并对字符串值做片段脱敏
  （例如 URL 里的 `?access_token=...`）
- 凭据健康检查（`workflow/credential_health.py`）只报「是否已配置」与「还剩多久过期」，
  **不报值**

### 买家 PII

- 落库前脱敏；日志与告警输出前再脱敏
- 超期 PII 按 `PII_RETENTION_DAYS`（默认 90 天）覆写姓名/电话/地址/聊天内容，
  **保留订单号与金额**
- 管不到你手动复制到网页或 SaaS 的内容

> **非法律意见**：以上是本仓库的工程默认，不构成对泰国 PDPA 或其它隐私法律的合规保证。
> 正式义务请咨询合格法律顾问。

### 涉钱动作

- 上架、改价、下单、投放**一律先过审批闸门**，纯读与纯生成不过闸门
- 发货、退款、付款**在本仓库始终禁用**（去卖家中心人工操作）
- 真写需要显式开关（`TTS_WRITE_ENABLED=1`）+ 店铺白名单 + 每日上限，
  默认全关

### 公开快照的边界

[LICENSE](./LICENSE) 采用 MIT，内置 Noto 字体仍按 SIL OFL 单独许可（见 [fonts.md](./docs/fonts.md)）。两份同路径手册与 `prompts/` 均为合成 example；[synthetic-workflow.json](./examples/synthetic-workflow.json) 只示意订单/listing/审批形状，不是实际平台记录。

本仓是另行导出的公开快照，不包含原私有开发仓的 Git 历史、真店运营手册、第一周清单或调优 prompt 正文。残余站点假设和未验证能力见 [M4-open-source-assessment.md](./docs/M4-open-source-assessment.md) 与 [P3-02-dehardcode.md](./docs/P3-02-dehardcode.md)。

---

## 明确不在范围内

- **平台侧的安全问题**：TikTok Shop 的漏洞请报给 TikTok
- **你自行部署实例的配置问题**：例如你把 `.env` 提交到了自己的 fork
- **依赖链上游漏洞**：请报给对应上游项目

---

## 加固建议（自部署者）

如果你自己部署本工作台，建议至少做到：

1. `.env` 权限收紧，不要进任何 git 仓库
2. 真写开关用完**立刻关**（`scripts/windows/tts-disarm.ps1`，或手动改回 0）
3. 工作台只监听 `127.0.0.1`（默认即如此），不要暴露到公网
4. 管理员口令用强口令；`ADMIN_COOKIE_SECURE` 在非本机访问时保持开启
