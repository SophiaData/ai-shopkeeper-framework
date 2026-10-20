# SPIKE A4：1688 Open API「按外部单号反查订单」裁决报告

> **任务来源**：[../DESIGN.md](../DESIGN.md) §10 外部依赖清单最后一行（UNKNOWN 待验证项）、§9 M1-A 第 4 项、[../M1-PLAN.md](../M1-PLAN.md) §2 A4。
> **裁决问题**：1688 Open API 是否支持按「外部单号」（我们下单时传入的 request_id / 外部订单号）反查到 1688 订单？
> **本报告结论**：**不通（文档层面判定；端到端实测未做，坐实条件见 §3.3）**。
> **日期**：2026-10-10 ｜ **方式**：纯外部资料查证，无 1688 账号/凭据，无任何真实调用。
> **证据分级标注**：〔官方〕= 官方文档/官方站点明写；〔二手〕= ISV/开发者公开文章；〔推断〕= 本报告基于资料的推理，单独可识别。

---

## 0. 诚实声明（本项目现状）

当前环境**没有企业资质、没有 1688 Open API 凭据**：本会话核实了运行环境变量中不存在任何 1688/Alibaba 相关 key（PowerShell `Get-ChildItem Env:` 按 `ALIBABA|1688|APPKEY|APP_SECRET` 过滤为空），工作区亦无凭据文件。因此本裁决是**纯文档裁决**，下文所有"API 能力"论断均来自公开资料，未做任何真实 API 调用，不宣称任何实测结果。

## 1. 查证过程与通道约束（可复现）

### 1.1 检索通道现状（2026-10-10 实测）

| 通道 | 结果 | 含义 |
|---|---|---|
| [https://open.1688.com/](https://open.1688.com/) | 可达，但为 SPA 壳，服务端仅返回标题「阿里巴巴开放平台」，无正文 | 官方站点在线，文档内容不可公开抓取 |
| [https://open.1688.com/apiList.htm](https://open.1688.com/apiList.htm) | **302 重定向到 login.1688.com** | API 目录需登录（企业开发者账号）才可见 |
| [https://open.1688.com/doc/index.htm](https://open.1688.com/doc/index.htm) | 可达但空壳（SPA，未登录无正文） | **官方 API 参数表在无凭据条件下不可达** → 本报告无法以〔官方〕级引用任何字段表 |
| [CSDN 站内检索 API](https://so.csdn.net/api/v3/search?q=alibaba.trade.fastCreateOrder&t=blog&p=1) | 可用（多组关键词，见 §2.4 阴性检索） | 二手主力来源 |
| [cn.bing RSS 检索](https://cn.bing.com/search?q=1688+%E5%BC%80%E6%94%BE%E5%B9%B3%E5%8F%B0+%E4%B8%8B%E5%8D%95+API+%E5%A4%96%E9%83%A8%E8%AE%A2%E5%8D%95%E5%8F%B7&format=rss) | 可用但对开发者内容近乎空返回（页面自述"部分搜索结果未予显示"） | 弱检索源，仅作旁证 |
| 百度 / 360 / 搜狗 | `m.baidu.com` 返回 JS 壳、`so.com` 正文截断、`sogou` 空 | 中文检索基本只剩 CSDN 可深挖 |
| web_search 工具 | API 401（搜索端点凭据失效），整场不可用 | 工具层缺陷，如实记录 |
| archive.org / DuckDuckGo / Brave / Mojeek | 不可达（超时/403） | 本环境网络代理只通国内站，Wayback 快照这条路**走不通**（与 DESIGN §10 "本机 GitHub 超时"一致） |
| [GitHub API 检索](https://api.github.com/search/repositories?q=%22alibaba.trade%22+1688)、[Gitee API 检索](https://gitee.com/api/v5/search/repositories?q=1688%20sdk) | 均 0 结果 | 拿不到官方 SDK 域类源码来坐实字段表 |

### 1.2 查证逻辑

「按外部单号反查」成立需要**两个能力同时存在**：
(A) 下单 API 允许**写入**一个外部单号/幂等键字段；
(B) 查询 API 允许按该字段**过滤**。
只要 (A) 不成立，(B) 就无从谈起。因此检索重点放在：下单接口入参表、订单列表接口过滤参数、以及全生态是否有任何"外部订单号"字样与 1688 共现。

## 2. 能力边界发现（逐条论断 + 来源 + 分级）

### 2.1 交易类 API 清单（1688 开放平台，命名空间 com.alibaba.trade）

以下 API 名在多篇 2018–2026 年独立文章中互相印证（网关形态 `gw.open.1688.com/openapi/param2/1/{namespace}/{apiName}-{ver}/{appKey}`）：

| 能力 | API 名 | 与裁决的关系 | 来源与分级 |
|---|---|---|---|
| 快速创建订单 | `alibaba.trade.fastCreateOrder`（分销一件代发 = `flow: saleproxy`，普通批发 = `general`） | 我们的"下代发单"走它 | 〔二手〕[1688 买家端交易 API 全链路实战：订单创建](https://blog.csdn.net/speical/article/details/162271902)（2026-06）；分销侧另见 `alibaba.trade.fenxiaoorder.create`（同文标题提及，未展开） |
| 订单预校验 | `alibaba.createOrder.preview` | 下单前校验 | 〔二手〕同上 + [1688代采系统官方API接入指南](https://blog.csdn.net/Ob_API20230201/article/details/148005965)（2025-05，提到 `com.alibaba.trade.createOrder` 权限申请） |
| 单笔订单详情（买家视角） | `alibaba.trade.get.buyerView`（入参 = 1688 orderId） | **只认 orderId** | 〔二手〕162271902 |
| 买家订单列表 | `alibaba.trade.getBuyerOrderList`（示例参数：`page`、`pageSize`、`needBuyerAddressPhone`、`needMemoInfo`；过滤维度 = 时间范围 + 订单状态） | **过滤维度里没有外部单号项** | 〔二手〕[如何构建阿里API请求数据（C#，2018）](https://blog.csdn.net/spw55381155/article/details/80434607)、162271902 |
| 撤销订单 | `alibaba.trade.cancel`（**仅未付款订单可撤销**，已支付调用直接报错） | 影响三级降级设计 | 〔二手〕162271902 |
| 支付 | 收银台链接 `alibaba.alipay.url.get` / 免密代扣 `alibaba.trade.pay.protocolPay.isopen|preparePay` | UNKNOWN 重复扣款风险来源 | 〔二手〕162271902 |
| 消息服务 | `alibaba.message.*` / Webhook 推送订单事件（事件键 = orderId；最佳实践"消息仅为触发器，trade.order.get 才是真相源，状态推进依赖 order_id + status_version"） | 状态回流可作触发器，连接键仍是 orderId | 〔二手〕[1688消息服务落地方案](https://blog.csdn.net/yuweide19761218/article/details/167391663)（2026-10，注：该文带 CSDN GEO/AIGC 标，按弱二手处理） |

### 2.2 关键阴性发现（裁决的直接依据）

1. **创建订单入参不含外部单号字段**。162271902 给出的 `fastCreateOrder` 核心业务参数结构完整贴出为 `{flow, addressParam{fullName, mobile, phone, provinceText, cityText, areaText, address, postCode}, cargoParamList[{offerId, specId, quantity}]}`，其余全文（支付、地址、询盘、旺旺）均无 `outOrderId / extOrderId / requestKey` 类字段。〔二手〕。
2. **同一文章明确要求 ISV 自建映射**："多供应商商品必须拆单循环调用，系统需建立『**本地业务单号 → 多笔1688订单号**』的映射关系"。若平台支持按外部单号反查，这句就是多余的。〔二手〕，该文是唯一贴出参数 JSON 的完整实战文。
3. **订单列表过滤参数不含外部单号**：2018 年 C# 文与 2026 年全链路文分别独立列出 `getBuyerOrderList` 参数（page/pageSize/needBuyerAddressPhone/needMemoInfo + 时间/状态），跨度 8 年、多个作者，均无外部单号过滤项。〔二手〕。
4. **"外部订单号"与 1688 零共现**：CSDN 检索 `1688 外部订单号` 返回 [7 条全部无关](https://so.csdn.net/api/v3/search?q=1688%20%E5%A4%96%E9%83%A8%E8%AE%A2%E5%8D%95%E5%8F%B7&t=blog&p=1)（发票/代购架构类）；检索 `outOrderNo`（支付行业通用外部单号字段名，在[支付宝预授权](https://so.csdn.net/api/v3/search?q=outOrderNo&t=blog&p=1)、微信、抖音、话费充值文里高频出现）返回 10 条，**无一与 1688 订单 API 相关**。〔二手，阴性证据〕
5. **无备忘/备注写入 API**：检索 `alibaba.trade.memo` 为 [0 条](https://so.csdn.net/api/v3/search?q=alibaba.trade.memo&t=blog&p=1)——连"把 request_id 塞进订单备注再全文匹配"这种土办法都没有公开 API 支撑（`needMemoInfo` 只是读出字段）。〔二手，阴性证据〕
6. **下单类接口无平台幂等键**：162271902 通篇的防重策略全部是本地手段（"本地订单表建立支付流水唯一索引"、"免密支付未加锁，网络超时重试时容易出现重复扣款"、"同一订单必须加分布式锁"），无任何"传入幂等令牌由平台判重"的描述。〔二手〕。

### 2.3 资质/权限包/签名方式（接入前置，坐实 M1 的"企业资质"前提）

- **企业开发者账号 + 实名认证**：〔二手〕"[注册企业开发者账号并完成实名认证，创建应用获取 AppKey/AppSecret](https://blog.csdn.net/Ob_API20230201/article/details/148005965)"；[2018 年 C# 文同样明写"首先要用企业账号申请开发者，并创建应用，授权用户"](https://blog.csdn.net/spw55381155/article/details/80434607)。→ 与 DESIGN §10 前提一致：**无企业资质 = 无 Open API 通道**。
- **交易类接口 = 高权限写操作**，需在 1688 服务市场**订购权限包**（〔二手〕称 0 元、**按月生效、需每月续订、无自动续费**——运维上要当到期项管）：[162271902](https://blog.csdn.net/speical/article/details/162271902)。
- **OAuth2.0**：下单查询走买家账号授权的 `access_token`：〔二手〕148005965、164263642。
- **签名**：网关参数协议 `_aop_signature` = **HMAC-SHA1**（appSecret 参与，参数排序拼接），时间戳 `_aop_timestamp`：〔二手〕[80434607](https://blog.csdn.net/spw55381155/article/details/80434607)。
- **限流**：〔二手〕默认约 100 次/分钟/应用：162271902。〔推断〕足够支撑 30–60s 节奏的对账轮询，轮询方案在吞吐上不是瓶颈。

### 2.4 一条未坐实的旁系线索（如实记录，不采信）

CSDN 检索引擎的百度关联词里出现 **"1688 跨境开放平台 创建订单接口 alibaba.cross.trade 下单 apiname"**（见 [该检索结果](https://so.csdn.net/api/v3/search?q=alibaba.trade.fastCreateOrder&t=blog&p=1) 的元数据字段），提示可能存在"跨境开放平台"独立 API 族。其参数表在可达资料中**完全不可见**（登录墙 + 无镜像文），无法确认是否支持外部单号。**本报告不以此推翻裁决**，但把它列入 §3.3 实测核验清单第 4 步：拿到企业资质后，除主站 API 外应同时询问跨境线是否有 `out order no` 能力——若可用，一级反查可直接坐实。

## 3. 裁决

### 3.1 结论：不通

**1688 Open API（open.1688.com，com.alibaba.trade 交易族，含分销一件代发）不存在"按外部单号反查订单"的公开能力。** 具体拆开：

1. (A) 写入侧：没有任何下单接口入参可携带外部单号/幂等键 —— 不通；
2. (B) 查询侧：订单详情只认 orderId；订单列表只能按时间范围 + 状态过滤 —— 不能按外部单号查 —— 不通；
3. 因此 `external_writes.probe_strategy=query_by_request_id` 的语义（"1688 按 request_key 反查"）在平台侧**没有落点**——request_key 只能活在我们自己的账本里，1688 从未见过它。

**置信度：高（文档层面）。** 残余风险是"字段存在但 8 年无人写过"，概率低（该能力是代采/ERP 刚需，若存在必然与 `outOrderNo` 一样遍地共现——见 §2.2-4 的对照实验）。〔以上均为"官方文档明写"缺失下的二手+阴性证据链，不是官方文档明写。〕

### 3.2 实测未做的边界

以下三件事本裁决**没有**做、也无条件做，不得被引用为已验证：
- 未调用任何真实 API（无凭据）；
- 未阅读登录墙后的官方参数表（`fastCreateOrder`/`getBuyerOrderList` 是否藏有未见于公开文的可选字段，无法排除）；
- 未确认"1688 跨境开放平台"（`alibaba.cross.*`）独立 API 族的能力边界（§2.4）。

### 3.3 坐实测验的条件与最小路径

**条件**：企业主体（营业执照）注册 1688 开放平台开发者 → 创建应用 → 订购交易权限包 → 一个买家账号完成 OAuth2 授权。
**最小实测路径（不花一分钱商品费）**：
1. `alibaba.createOrder.preview` 对某个 0.1 元级 offer 预校验（只读）；
2. `alibaba.trade.fastCreateOrder` 下一笔**不付款**订单 → 立即在**官方登录后的参数文档页**逐项截图入参表（找 `outOrderNo/attribute/message` 类字段）；
3. 不付款 `alibaba.trade.cancel` 撤销，确认订单消失；
4. `alibaba.trade.getBuyerOrderList` 拉列表，确认过滤参数全集（找外部单号项）；
5. 开放平台工单问两句：a) 下单是否支持外部订单号字段及回查；b) 跨境线 `alibaba.cross.trade` 是否支持。
**触发放行的规则**：官方参数表截图证实无外部单号字段 → 本报告由"文档层面判定"升格为"实测坐实"；若发现隐藏字段 → 一级 API 反查复活，按 §4 反向修订（届时 `query_by_request_id` 保留，三级降级仅作权限包断供时的保险）。

## 4. 三级降级方案（触发条件 / 谁做什么 / 成本 / SCHEMA 影响）

> DESIGN §10 的原三级 = "API 反查(request_id) → 人工核对 → 作废重下"。裁决"不通"后，一级重定义为**API 模糊反查**（时间窗 + 商品指纹），语义降级为"证据器"而非"判决器"：能自动收敛大多数 UNKNOWN，歧义必须进二级人工。

### 4.1 一级：API 模糊反查（自动化，可用——修正版）

- **触发者**：对账任务（reconciler），条件 = `external_writes.state='UNKNOWN'` 且 `adapter='alibaba' AND operation='place_po'`（reaper 之后，见 [../SCHEMA.md](../SCHEMA.md) §4.2）。
- **做什么**：`alibaba.trade.getBuyerOrderList` 按 `submitted_at ±[0, +15min]` 时间窗 + 状态过滤拉增量 → 以 PO 指纹 `{offerId 集合, Σqty, 收货地址四要素(脱敏后 HMAC 比较), payable_amount}` 匹配 → 命中 1 条且金额精确相等且窗口内无同 offer 其他未决 place_po → 判"实际已创建"，**回填 orderId 到 `external_writes.probe_result`**；其余情况（0 命中 / 多命中 / 金额不符）→ 落二级。另：TMC 消息/Webhook（§2.1 消息行）可作触发器缩短检测延迟，但真相源仍是查询 API。〔匹配谓词为本报告设计，属推断〕
- **成本**：每 UNKNOWN 3–5 次 API 调用（限流内），零人工。**风险约束**：模糊判重存在理论误配（同 offer 同数量 15 分钟窗口内并发两单——SCHEMA §5 决策 3 的"批量自动批"会放大此场景），因此**自动判决仅允许写 `RECONCILED`（对账裁决），不允许直接冒充 `CONFIRMED`**；`purchase_orders.external_po_id` 的回填仍走 §2.2 映射的 `CONFIRMED/RECONCILED` 两态之一，符合现行规则。
- **SCHEMA 影响**：`external_writes.state UNKNOWN→RECONCILED`（probe_result 记录匹配证据链）；命中后 `po_status_history(source='poll_1688')` 落 SUBMITTED，触发 §2.3 accrual（`goods_cost` 分录，`basis='accrual'`）。**不引入新表**。

### 4.2 二级：人工核对（审批闸门 + 飞书卡片，DESIGN §6 既有通道）

- **触发者**：一级自动判决的前置条件不满足（0 命中/多命中/金额不符）→ 对账器生成 `recon_diffs(po_vs_1688, severity 视是否涉钱)` + 人工待办任务；卡片推送店铺 owner。
- **人工做什么**：登录 1688 买家工作台，按下单时间+商品肉眼确认订单是否存在/已付款，在卡片上三选一裁决：①"存在且已付"→ 系统代填 orderId 落 `RECONCILED`；②"确认不存在"→ 落 `FAILED`（该 request_key 永久 dead，SCHEMA §3.4 注释），PO 回 `PENDING_APPROVAL` 可重批 → 进入三级重下；③"存在未付款"→ 人工撤单后按②处理。**注意**：裁决动作本身即涉钱确认，走 approvals 表（approver=店铺 owner），不是控制台随手按钮。
- **成本**：每单 5–15 分钟人力（〔推断〕估计值）；UNKNOWN 频率取决于网络/风控，M3 初期（首 20 单逐单审）单量小，可承受。若权限包按月断供（§2.3），全部 place_po 写失败会瞬间把这里打爆——**权限包到期监控必须做成人命级告警**。
- **SCHEMA 影响**：`external_writes.state → RECONCILED/FAILED`；`approvals`(action_type=`reconcile_po`, channel=feishu)；`recon_diffs` 收敛；`po_status_history(source='manual')`。

### 4.3 三级：有损兜底"作废重下"（涉钱，强制审批）

- **触发者**：人工核对（二级）裁决"确认不存在"之后，运营发起重下申请；**没有二级前置，任何系统组件不得自动进入三级**。
- **人工做什么**：飞书审批卡（1:1 审批单，`gate_policies` 对此动作设 `require_approval=true` 且**排除批量自动批豁免**——批量豁免仅限常规 place_po，见 DESIGN §6 注），双人复核（同 SCHEMA §6 大额 ledger_correction 标准）→ 批准后对**同一 PO** 创建**新 `external_writes` 行（新 request_key + attempt_no=前值+1）**重新下单 → 付款前 pre-flight 复校验（现价/库存/汇率，同 DESIGN §3.2 Lister 的 stale 机制）。"作废"侧不产生平台动作（订单本就不存在），只是账本关闭。
- **重复下单风险（本方案的核心风险）**：如果二级误判（订单其实存在），三级会造成**双采购单 + 双扣款**。防线自上而下：
  1. `external_writes` UNIQUE `(adapter, operation, request_key, attempt_no)` —— 防同一写入单并发重放（SCHEMA §3.4），**但防不了"旧单 UNKNOWN + 新单重下"**，这是唯一执行账本的天然边界（[../SCHEMA.md](../SCHEMA.md) §4.1）；
  2. **应用层闸门（本报告建议补的硬规则）**：同一 `target_id`（PO）上若存在 `state ∈ {PENDING, SENT, UNKNOWN}` 的 place_po 行，禁止创建新行——必须先把旧行裁决到终态（FAILED/NON_RETRYABLE/RECONCILED），把"重下"变成对旧账本状态的显式消费而非旁路；旧行为 UNKNOWN 时唯一合法出口是二级的 `RECONCILED/FAILED` 裁决；
  3. 事后防线：若双单既成事实（幽灵单后来出现/被扣款），按 DESIGN §2.1 脚注⁷ 路径处理——`goods_cost` accrual 冲正（`reversal_kind='error'`）+ 平台售后退款 + `order_cancel_loss` 闭合 + `po_vs_1688` 对账线常驻差异单直到冲正落地（SCHEMA §4.7 R5 O3）——钱损失可见、台账可平。
- **成本**：每单一次双人审批（分钟级）+ 误判情形下的真金白银损失（货款占用 + 退款周期 +〔推断〕极少数供应商拒退尾风险）。
- **SCHEMA 影响**：`external_writes` 新 attempt 行；`approvals` 两跳（reconcile_po → place_po 重批）；误判场景额外触发 `ledger_entries` 冲正对 + `recon_diffs(critical)`。

## 5. 对 SCHEMA 的映射建议（只建议，不改冻结文档）

1. **`external_writes.probe_strategy` 枚举中的 `query_by_request_id`：标记 unavailable（不删除）**。它对应的能力已被裁决不存在；保留枚举值仅作历史语义，代码侧禁止对 `adapter='alibaba' AND operation='place_po'` 写入该值。
2. **过渡落地值：alibaba/place_po 的 `probe_strategy` 统一写 `'none'`**。依据 SCHEMA §3.4 行尾注释"`probe_strategy='none'` 的接口 UNKNOWN 只能转人工（写死）"+ §4.1 裁决优先级第 3 档——这与本裁决完全吻合，且**不需要修改 SCHEMA**。一级的时间窗模糊反查作为**人工裁决前的证据采集步骤**内置在对账任务里（probe_result 里留证据），自动判决仅限满足 §4.1 严格谓词的 `RECONCILED`——语义上是"none + 证据助手"，不违反"写死"条款。
3. **正式变更单（走流程后追加，非本次）**：枚举新增 `query_by_time_window_fuzzy`（含 §4.1 谓词与"只允许 RECONCILED"约束的成文定义），避免实现层靠注释心证。
4. **勘误项**：[../SCHEMA.md](../SCHEMA.md) §2.3 "回流（读）：对账任务轮询 1688 订单状态（按 `request_key`/`external_po_id`）"中的 **`request_key` 不是 1688 可查键**，应改为"按 `external_po_id`（订单详情 buyerView）+ 时间窗增量（getBuyerOrderList，用于发现漂移与 UNKNOWN 裁决）"。这是冻结文档的一处口径瑕疵，建议登记后随下一次正式修订收口。
5. **对 §4.1 裁决优先级链的影响**："平台幂等键（M2 逐接口核对）"这一档在 alibaba/place_po 上已可预先记 **无**（§2.2-6）；M2 核对只需确认其余接口（TikTok 侧）即可，省一格工作量。
6. **对 M1/M3 里程碑**：DESIGN §9 M1-A 第 4 项与 §9 "M1 Done 条件"第 4 条要求的"通/不通 + 降级选择"由本报告闭合：**不通；M3 采用本报告 §4 三级**。M1-B 商品生成 Demo 与该结论零耦合（DESIGN §10 本就约定不阻塞）。

## 6. 收尾三栏

### 已验证（含来源；均指"文档/检索层面"，非 API 实测）
- 官方 API 文档登录墙：open.1688.com/apiList.htm → login.1688.com（本会话直接观察，HTTP 重定向实证）。
- 交易 API 族存在与形态（fastCreateOrder / preview / get.buyerView / getBuyerOrderList / cancel / protocolPay）：≥4 篇独立二手互证（[162271902](https://blog.csdn.net/speical/article/details/162271902)、[148005965](https://blog.csdn.net/Ob_API20230201/article/details/148005965)、[80434607](https://blog.csdn.net/spw55381155/article/details/80434607)、[167391663](https://blog.csdn.net/yuweide19761218/article/details/167391663)）。
- 企业开发者资质 + 服务市场权限包（按月续订）+ OAuth2 + HMAC-SHA1 `_aop_signature`：〔二手〕148005965、80434607、162271902。
- 阴性检索："1688 × 外部订单号/outOrderNo/memo API"零相关命中（§2.2-4/5，检索 URL 已内嵌）。
- cancel 仅限未付款、免密代扣重复扣款风险、无平台幂等键：〔二手〕162271902。

### 刻意未验
- 端到端真实调用（无企业资质，本裁决的定义域之外）；
- 登录墙后的官方参数表逐字段核对（含 `alibaba.cross.*` 跨境族，§2.4/§3.3）；
- 消息服务/Webhook 订阅链实测；
- 权限包实际订购流程与月度费用条款（二手称 0 元，未见官方价页证实）。

### 不可宣称
- **不得宣称**"官方文档明写不支持外部单号反查"——官方明写的是**登录墙后的内容**，本环境不可达；我们的措辞只能是"全部公开资料（2018–2026，多网关多作者）零提及该能力 + 实战文反向要求本地映射"；
- **不得宣称**任何字段表完整准确（均来自二手转录，官方表为准）；
- **不得宣称**已实测 fastCreateOrder/cancel 行为（含"未付款可取消"）；
- **不得宣称**跨境线 `alibaba.cross.trade` 同样不通——它是"能力未知"，不是"能力不存在"。

---
*本报告为 A4 切片唯一产出；未触碰 SCHEMA.md / DESIGN.md / src/ / tests/ / 其他 docs/spike/ 文件。*
