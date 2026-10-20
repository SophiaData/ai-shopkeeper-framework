# RPA 兜底适配器设计稿（P3-03）

> 上游权威：[DESIGN.md](./DESIGN.md) §1.2 框架腿、§5 平台操作、§12 未决问题 ｜
> 接口：[adapters/base.py](../src/shopkeeper/adapters/base.py) `PlatformAdapter` ｜
> 现状：[adapters/tiktok_shop.py](../src/shopkeeper/adapters/tiktok_shop.py) `TikTokRpaStub`
> 状态：**设计稿，未实现**（`rpa_stub` 仍是拒绝态）｜ 不启用任何浏览器自动化

---

## 0. 先说结论

| 问题 | 结论 |
|---|---|
| 现在要实现 RPA 吗？ | **不。** 优先走 TikTok Shop Open API；RPA 是**兜底**，当前没有触发条件 |
| 本文是什么？ | **设计稿**：把「万一 API 走不通」的路径写清楚，避免真需要时从零讨论 |
| 当前行为 | `WORKFLOW_PLATFORM=rpa_stub` → 所有写操作返回 `TTS_RPA_STUB` 拒绝，**不做任何浏览器自动化** |
| 新增依赖？ | **不新增** —— Playwright 已是 dev 依赖（浏览器冒烟测试在用） |

---

## 1. 为什么是兜底而不是主路径

DESIGN §5 已经把平台操作定为「**Open API 优先，Playwright RPA 兜底**」，理由如实写下：

| 维度 | Open API | RPA |
|---|---|---|
| 稳定性 | 契约稳定、有版本 | **页面改版即失效**，且无通知 |
| 合规 | 官方授权通道 | 取决于平台条款，**可能违反 ToS** |
| 凭据 | App Key / Secret | 需要**卖家账号的登录态**（含 2FA） |
| 速率 | 有明确配额 | 无配额但**更易触发风控** |
| 可审计 | 请求/响应可留痕 | 只能录屏/截图 |

**结论**：RPA 的每一项都不如 API。它只在 **API 拿不到权限**（例如类目/站点未开放、
入驻审核未通过）时才值得考虑，且必须接受上面全部代价。

---

## 2. 触发条件（须同时满足）

**不满足则不做**（与 [ORCHESTRATOR-LANGGRAPH.md](./ORCHESTRATOR-LANGGRAPH.md) §5 同样的门槛写法）：

1. **API 路径确证走不通** —— 具体到「哪个接口、平台为何拒绝」，有工单/邮件等凭据
2. **owner 书面接受 ToS 风险** —— 这是法律与账号安全判断，**AI 不代判**
3. **有可核对的页面结构证据** —— 至少一次人工操作录屏或 DOM 快照（**不能靠猜选择器**）
4. **接受维护成本** —— 页面改版时需人工修复，且要有「失效即停」的检测

第 3 条尤其重要：本仓库**无法访问 TikTok 卖家中心**（需要真实登录态），
所以**任何选择器都无法在此验证** —— 本文因此**不写具体选择器**（AGENTS 规则 4）。

---

## 3. 接口契约（与 API 版共用）

`PlatformAdapter` 是抽象接口，RPA 版**必须实现同一套方法**，对编排器透明：

```python
class PlatformAdapter(ABC):
    name: str
    def search_products(self, query: str) -> AdapterResult[dict]: ...
    def create_listing(self, payload: dict) -> AdapterResult[dict]: ...
    def get_orders(self, since: str) -> AdapterResult[list[dict]]: ...
    def update_tracking(self, order_id: str, tracking_no: str) -> AdapterResult[dict]: ...
```

（以上为 `adapters/base.py` 的**实际**抽象方法，写本文时逐条核对过。）

### 3.1 硬约束（与 API 版一致，不得放宽）

- **发货、退款、付款永远不做** —— `review_routes.py` 的 `ship` / `refund` 路由**固定 403**（源码位置已核对），RPA 也不例外
- **写操作过同一个审批闸门**与写开关（`TTS_WRITE_ENABLED` + 白名单 + 日上限）
- **每次写操作写审计日志**（不含密钥）
- **默认 `rpa_stub` 是拒绝态**，实现后也**默认不启用**

> 理由：RPA 改变的是「怎么点」，不是「能不能点」。闸门与审计是**业务约束**，
> 与通道无关。若 RPA 绕开闸门，等于把 DESIGN §2.1 的整个安全模型作废。

---

## 4. 若真要实现：分阶段落地

### 阶段 A：只读探测（最低风险）

用 Playwright 打开卖家中心，**只截图与导出 DOM**，不做任何点击。

- 目的：拿到**真实的**页面结构证据，回答「阶段 B 是否可行」
- 产出：DOM 快照 + 截图（**不入库**，见 §5）
- 出口：若结构无法稳定定位，**到此为止**，回到 API 路线

### 阶段 B：单步人机协同

人类完成登录（含 2FA），程序只做**一步**操作，且**每步要人确认**。

- 不保存登录态、不自动过验证码
- 每步截图留证
- 出口：验证「定位 + 提交」这条链路是否可靠

### 阶段 C：受控自动填表（**仍需人点最终提交**）

程序填表到「提交」前一步，**最后一下由人点**。

- 这与本仓库「涉钱动作过人工闸门」一致
- 失败即停，不做自动重试（重试可能造成重复上架）

### 阶段 D：全自动（**本文不建议**）

除非前面三阶段长期稳定且有强理由，否则不进入。理由见 §1。

---

## 5. 安全与隐私（RPA 引入的新攻击面）

| 风险 | 处置 |
|---|---|
| 登录态泄漏 | **不持久化**任何 cookie/storage；每次由人登录 |
| 页面含买家 PII | 截图/DOM 一律**不入库**；确需留证则先过 `security/` 脱敏，且视为 PII |
| 选择器含密钥 | 不把 token 写进脚本；从 `.env` 读 |
| 误点涉钱按钮 | 只允许白名单内的动作；发货/退款/付款按钮**显式排除** |
| 静默失效 | 每次运行前做「关键元素存在性」断言，缺失即**停并告警**，不盲点 |

第 5 条是 RPA 最危险的地方：**页面改版后选择器可能命中错误的元素** ——
按错一个按钮的代价不可逆。所以「找不到就停」必须优先于「尽力完成」。

---

## 6. 与现有代码的关系

- `TikTokRpaStub` **保持拒绝态**，本文不改变它
- 真要实现时，新增 `TikTokRpaPlatform`，**与 `TikTokLivePlatform` 并列**，
  共用 `WritePolicy` / `WriteAuditLog` / 审批闸门
- 浏览器冒烟测试已有 Playwright 用法可参考（`tests/workflow/test_workflow_browser_smoke.py`）

---

## 7. 验收（本文档自身）

```bash
python scripts/check_links.py          # 链接门
python -m pytest -m "not real" tests/adapters/test_tiktok_platform.py
```

期望：全绿。本文**不引入运行时依赖、不改默认开关、不解锁任何写通道**。

---

## 8. 何时重新评估

出现以下任一情况时回来更新本文：

- API 权限申请被明确拒绝（附凭据）
- owner 书面要求评估 RPA 路径
- Playwright 已在其他场景稳定运行（降低维护成本判断）

**在此之前 `rpa_stub` 保持拒绝态。**
