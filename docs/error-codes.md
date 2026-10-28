# 错误码登记表（由代码生成，勿手改）

> 生成源：`src/shopkeeper/security/error_codes.py` 的 `REGISTRY`。
> 重新生成：`python scripts/gen_error_codes.py`
> 一致性由 `tests/security/test_error_codes.py` 把守：登记漏项或文档漂移都会让 CI 红。

处置语义对应 DESIGN §3.3：生成类失败重试 2 次后降级人工待办；执行类失败立即停、保留现场、不自动重试（防重复下单/重复上架）。

| 错误码 | kind | 处置 | 转人工 | 含义 |
|--------|------|------|--------|------|
| `A1688_ANTIBLOCK` | generation | 降级 → 人工待办 | ✅ | 1688 触发反爬滑块拦截（bxpunish/punish）。转人工补录或使用浏览器小书签。 |
| `A1688_API_ERROR` | generation | 降级 → 人工待办 | ✅ | 1688 Open API 返回业务错误或非 JSON。转人工核对；不把错误当成功。 |
| `A1688_FETCH_FAILED` | generation | 降级 → 人工待办 | ✅ | 1688 Open API 货源适配器未预期异常已收束为失败结果（兜底）。转人工。 |
| `A1688_HTTP_ERROR` | generation | 降级 → 人工待办 | ✅ | 1688 Open API HTTP/传输失败。生成类可降级人工补录；不重轰。 |
| `A1688_MISSING_CREDENTIALS` | validation | 立即停，**不自动重试** | ✅ | ALIBABA_1688_ENABLED=1 但缺少 APP_KEY/APP_SECRET/ACCESS_TOKEN。不签名、不外呼；工厂层亦会回退 MockProductSource。 |
| `A1688_PARSE_EMPTY` | generation | 降级 → 人工待办 | ✅ | API 响应缺少标题/价格/主图等关键字段。转人工补录。 |
| `A1688_PARSE_ERROR` | generation | 降级 → 人工待办 | ✅ | 1688 页面内联 JSON 或结构体解析失败。转人工核对。 |
| `A1688_TIMEOUT` | generation | 重试 ≤2 次，耗尽转人工待办 | — | 1688 Open API 超时。生成类可有限重试，耗尽转人工。 |
| `ADS_API_ERROR` | execution | 立即停，**不自动重试** | ✅ | TikTok Ads 返回非 0 code、非 JSON，或报表结构不能安全记账。不把缺字段当成零花费，不改预算。 |
| `ADS_AUTH_FAILED` | execution | 立即停，**不自动重试** | ✅ | TikTok Ads 只读 GET 返回 401 或 403。不改预算。错误信息不含 Access-Token。 |
| `ADS_CREDENTIALS_MISSING` | validation | 立即停，**不自动重试** | ✅ | TIKTOK_ADS_ENABLED 已开，但缺少 Access-Token 或广告主 ID。不外呼，不回退 mock，不改预算。 |
| `ADS_CURRENCY_UNKNOWN` | validation | 立即停，**不自动重试** | ✅ | 广告主信息没有可用的 currency。不假定 THB，不拉花费，不改预算。 |
| `ADS_CURRENCY_UNSUPPORTED` | policy | 策略拒绝（无跳过） | ✅ | 广告主或报表行币种不是 THB。不换汇、不入库、不改预算。 |
| `ADS_DATE_INVALID` | validation | 立即停，**不自动重试** | — | 广告拉取的日期格式非法（须 YYYY-MM-DD）。走 AdapterResult 返回，不裸抛；只读，不外呼。 |
| `ADS_DATE_RANGE_INVERTED` | validation | 立即停，**不自动重试** | — | 广告拉取的 date_from 晚于 date_to。刻意不返回空结果 —— 静默成功会让调用方分不清「区间内没数据」（正常）与「参数写反了」（bug）。 |
| `ADS_DATE_RANGE_TOO_LONG` | validation | 立即停，**不自动重试** | — | 按天同步报表超过官方 30 天上限。未发请求，不改预算。 |
| `ADS_ENDPOINT_INVALID` | validation | 立即停，**不自动重试** | ✅ | TIKTOK_ADS_API_BASE 不是官方 https://business-api.tiktok.com/open_api/v1.3。未发请求。 |
| `ADS_HTTP_ERROR` | execution | 立即停，**不自动重试** | ✅ | TikTok Ads 只读 GET 超时、传输失败、非 200、重定向或响应过大。不跟随重定向，不改预算。 |
| `ADS_LIVE_DISABLED` | policy | 策略拒绝（无跳过） | ✅ | TikTok Ads 只读客户端在开关关闭时被直接调用。不发起 HTTP、不改预算。工厂在 TIKTOK_ADS_ENABLED 未开时返回 MockTikTokAds。 |
| `ADS_REPORT_TRUNCATED` | execution | 立即停，**不自动重试** | ✅ | 同步报表页数超过只读客户端上限。拒绝只记一部分，不改预算。 |
| `ADS_SPEND_UNPARSEABLE` | validation | 立即停，**不自动重试** | — | 报表 spend 或展示/点击/转化无法解析，或缺少 stat_time_day。该次拉取不入库，不改预算。 |
| `ADS_WRITE_REFUSED` | policy | 策略拒绝（无跳过） | ✅ | 改广告预算、建计划、调出价或调用允许表以外的路径被拒绝。只读花费，不产生花钱动作。 |
| `AD_SPEND_INVALID` | validation | 立即停，**不自动重试** | — | 广告费记账输入不合法（日期、金额、币种、渠道或来源）。不入库、不外呼、不改预算。 |
| `AGENT_STATS_DATE_INVALID` | validation | 立即停，**不自动重试** | — | 各 agent 产出统计 from/to 日期非法（须 YYYY-MM-DD）或 from>to。只读查询不写库；改正参数后重试。 |
| `APPROVAL_EXECUTE_FAILED` | execution | 立即停，**不自动重试** | ✅ | 审批执行失败且无更具体错误码（如缺 platform_product_id）。执行类立即停，可选告警。 |
| `BACKUP_FAILED` | execution | 立即停，**不自动重试** | ✅ | artifacts 下 .db / 审计 jsonl 每日备份失败。已 release_job；可选告警，不自动重试轰炸。 |
| `COMPLIANCE_TERM_HIT` | policy | 策略拒绝（无跳过） | ✅ | 合规词表 severity=block 命中（DESIGN §7.4/§7.5；词表 assets/compliance_lexicon.json）。返回命中词与位置；文案不可进入审批通过；不提供「本次跳过」。warn 级只写入 QA warnings / advisory_flags，不发本码、不阻断审批。 |
| `COZE_EMPTY` | generation | 重试 ≤2 次，耗尽转人工待办 | — | Coze 没有返回答案。生成类可重试。 |
| `COZE_HTTP_ERROR` | generation | 重试 ≤2 次，耗尽转人工待办 | — | Coze /v3/chat 传输失败或返回错误码。生成类可重试。 |
| `COZE_PENDING` | generation | 重试 ≤2 次，耗尽转人工待办 | — | Coze 对话尚未完成。本次不编造文案，生成类可重试。 |
| `CS_CATEGORY_REQUIRED` | validation | 立即停，**不自动重试** | — | 客服入站/草稿缺少 category。不外呼平台。 |
| `CS_INBOX_NO_CONTRACT` | policy | 策略拒绝（无跳过） | ✅ | 客服收件箱只读拉取未接通：本仓库 TikTok 适配器没有客服消息读端点契约，未臆造路径。不外呼、不把结果当成空收件箱、不发送回复。接通需 owner 提供可核对的官方文档。 |
| `CS_REPLY_NOT_APPROVED` | policy | 策略拒绝（无跳过） | ✅ | 非白名单客服回复未经审批或已拒绝/过期。不 stub 发送，无「跳过本次」。 |
| `CS_SEND_FAILED` | execution | 立即停，**不自动重试** | ✅ | 客服 stub 发送失败且无错误体（兜底）。执行类不自动重试；本切片不外呼平台。 |
| `DIFY_EMPTY` | generation | 重试 ≤2 次，耗尽转人工待办 | — | Dify 响应里没有 answer。生成类可重试，不编造文案。 |
| `DIFY_HTTP_ERROR` | generation | 重试 ≤2 次，耗尽转人工待办 | — | Dify chat-messages 传输失败或 HTTP 非成功。生成类可重试。 |
| `DROPSHIP_DRAFT_FAILED` | execution | 立即停，**不自动重试** | ✅ | 代发草稿生成失败且无错误体（兜底）。不付款。 |
| `DROPSHIP_ITEMS_REQUIRED` | validation | 立即停，**不自动重试** | ✅ | 人工下单草稿缺少商品明细或 SKU。不猜一条、不外呼、不付款。 |
| `DROPSHIP_MANUAL_NO_AUTOPAY` | policy | 策略拒绝（无跳过） | ✅ | 人工下单通道拒绝 place_dropship_order。不调用 1688 下单、不付款；请复制下单信息后由人回填单号。 |
| `DROPSHIP_NOT_MANUAL` | validation | 立即停，**不自动重试** | — | 该代发草稿不是人工下单通道（mode 不是 manual）。不能把它的 mock 单号当成 1688 单号，也不付款。 |
| `DROPSHIP_ORDER_REQUIRED` | validation | 立即停，**不自动重试** | — | 1688 代发草稿/下单缺少 order_id。不外呼。 |
| `DROPSHIP_PLACE_FAILED` | execution | 立即停，**不自动重试** | ✅ | 代发下单失败且无错误体（兜底）。执行类不自动重试、不付款。 |
| `DROPSHIP_PO_CONFLICT` | validation | 立即停，**不自动重试** | ✅ | 该订单已回填过不同的 1688 单号，不能覆盖。不外呼、不付款。 |
| `DROPSHIP_PO_INVALID` | validation | 立即停，**不自动重试** | ✅ | 回填的 1688 单号不是 16–20 位数字，或链接不是 1688 订单详情页。付款链接一律拒绝。不外呼。 |
| `DROPSHIP_QTY_REQUIRED` | validation | 立即停，**不自动重试** | ✅ | 人工下单草稿的数量缺失、不是整数或小于 1。不按 1 猜测、不外呼、不付款。 |
| `EXT_IDEMPOTENCY_CONFLICT` | execution | 立即停，**不自动重试** | ✅ | 同幂等键的外部副作用写正在进行或并发占位失败。立即停，不二次外呼；待先前结果落库后回放。 |
| `EXT_RETRY_EXHAUSTED` | generation | 降级 → 人工待办 | ✅ | 统一外部调用重试（指数退避+抖动）预算耗尽。生成类转人工待办；执行类写操作不得走此码自动重试。 |
| `FEISHU_NOTIFY_FAILED` | execution | 立即停，**不自动重试** | ✅ | 飞书审批卡片 webhook 外呼失败。已降级 stub 不阻断建单；可选告警。 |
| `FEISHU_TRANSPORT` | generation | 重试 ≤2 次，耗尽转人工待办 | — | 飞书 webhook 传输/5xx 瞬时失败。统一重试层可有限退避；耗尽后降级 stub 并记 FEISHU_NOTIFY_FAILED。 |
| `GATEWAY_AUTH_REJECTED` | policy | 策略拒绝（无跳过） | ✅ | 平台操作网关 token 校验失败（GATEWAY_TOKEN）。远程队列拒绝；检查主系统与海外侧口令。 |
| `GATEWAY_METHOD_FORBIDDEN` | policy | 策略拒绝（无跳过） | ✅ | 网关拒绝发货/退款等方法（与工作台 HTTP 403 同口径）。须卖家中心人工处理。 |
| `GATEWAY_METHOD_UNKNOWN` | validation | 立即停，**不自动重试** | — | 网关收到未登记的 method。不执行。 |
| `GATEWAY_REMOTE_FAILED` | execution | 立即停，**不自动重试** | ✅ | 远程网关回传失败且无结构化错误体（兜底）。 |
| `GATEWAY_TIMEOUT` | execution | 立即停，**不自动重试** | ✅ | 等待海外网关任务结果超时。执行类不自动重试。 |
| `GATE_NOT_APPROVED` | policy | 策略拒绝（无跳过） | ✅ | 上架未经人工审批或已拒绝/过期。不调用平台写适配器，无「跳过本次」。 |
| `GATE_PAYLOAD_MISMATCH` | policy | 策略拒绝（无跳过） | ✅ | 审批载荷哈希不符或 entity_id 与载荷内业务 id 不一致。直接拒。 |
| `GATE_STALE` | policy | 策略拒绝（无跳过） | ✅ | pre-flight 复校验超阈值 → 审批单转 stale 重审（SCHEMA §3.4）。 |
| `IMG_FETCH_FAILED` | generation | 降级 → 人工待办 | ✅ | 源图下载失败。降级：保留原图引用 + 人工。 |
| `IMG_FETCH_STATUS` | generation | 降级 → 人工待办 | ✅ | 源图返回非 200 或过小。降级同上。 |
| `IMG_FILE_MISSING` | generation | 降级 → 人工待办 | ✅ | 本地源图文件不存在。降级同上。 |
| `IMG_FONT_MISSING` | generation | 降级 → 人工待办 | ✅ | 该语种无可用字体（A3：泰语只有 Tahoma 有字形）。必须人工出图。 |
| `IMG_NOT_DECODABLE` | generation | 降级 → 人工待办 | ✅ | 图像不可解码。降级同上。 |
| `IMG_NO_COPY` | validation | 立即停，**不自动重试** | ✅ | 没有可叠加的目标语言文案。不许画占位假字冒充交付。 |
| `IMG_NO_LANG` | validation | 立即停，**不自动重试** | — | 缺少目标语种。 |
| `IMG_NO_REF` | validation | 立即停，**不自动重试** | — | 缺少源图引用。 |
| `IMG_NO_RESULT` | generation | 降级 → 人工待办 | ✅ | 图像处理失败且无错误体（兜底码）。 |
| `IMG_PILLOW_BOOM` | generation | 降级 → 人工待办 | ✅ | 本地处理异常（注入式测试用同码语义）。降级 original_kept + needs_human。 |
| `IMG_REDRAW_DISABLED` | validation | 立即停，**不自动重试** | — | IMAGE_REDRAW_ENABLED=0：真重绘关闭，不外呼。上层走 Pillow 后处理。 |
| `IMG_REDRAW_STUB` | generation | 降级 → 人工待办 | ✅ | 真重绘 stub：网关无 edits/图生图（spike/image.md），不外呼、不编造成功；降级 Pillow。 |
| `IMG_STORE_BAD_KEY` | validation | 立即停，**不自动重试** | — | ImageStore key 非法或越出根目录（含 ..）。拒绝解析。 |
| `IMG_STORE_EMPTY` | validation | 立即停，**不自动重试** | — | ImageStore.put/open 收到空字节。不写盘、不外呼对象存储。 |
| `IMG_STORE_HTTP_ERROR` | generation | 降级 → 人工待办 | ✅ | S3 兼容 Put/Get/HEAD HTTP 或传输失败。转人工；不重轰。 |
| `IMG_STORE_IO_ERROR` | generation | 降级 → 人工待办 | ✅ | 本地 ImageStore 读写盘失败。转人工；不冒充已托管。 |
| `IMG_STORE_MISSING_CREDENTIALS` | validation | 立即停，**不自动重试** | ✅ | IMAGE_STORE=s3 但缺少 ENDPOINT/BUCKET/ACCESS_KEY/SECRET_KEY。工厂层回退 LocalFileImageStore；直接构造的 S3 客户端不外呼。 |
| `IMG_STORE_NOT_FOUND` | generation | 降级 → 人工待办 | ✅ | 托管对象不存在（本地缺文件或 S3 404）。转人工核对 key。 |
| `IMG_STORE_TIMEOUT` | generation | 重试 ≤2 次，耗尽转人工待办 | — | S3 兼容请求超时。生成类可有限重试，耗尽转人工。 |
| `IMG_UPSTREAM_500` | generation | 降级 → 人工待办 | ✅ | 图像处理服务 500。降级：original_kept + needs_human。 |
| `JOB_RUN_FAILED` | execution | 立即停，**不自动重试** | ✅ | 工作台定时任务（research/daily）执行抛错。已 release_job；可选告警，不自动重试轰炸。 |
| `LLM_BUDGET_EXCEEDED` | policy | 策略拒绝（无跳过） | ✅ | 日 LLM 预算（LLM_DAILY_BUDGET_USD）已用尽。编排器暂停 run（budget_paused）并可选告警；不继续外呼。 |
| `LLM_EMPTY` | generation | 重试 ≤2 次，耗尽转人工待办 | — | OpenAI 兼容接口没有返回正文。生成类可重试。 |
| `LLM_EMPTY_CONTENT` | generation | 重试 ≤2 次，耗尽转人工待办 | — | content 为空（A2 实测：思考吃满 max_tokens 预算）。**先查预算配置再重试**，否则 3 次都白烧。 |
| `LLM_FALLBACK_EXHAUSTED` | generation | 降级 → 人工待办 | ✅ | 主模型与 LLM_FALLBACK_MODEL(/MODELS) 均因超时/5xx/限流失败。编排器/文案生成回落本地模板并标 degraded；不冒充模型输出。备用调用计入 LLM_DAILY_BUDGET_USD。 |
| `LLM_HTTP_429` | generation | 降级 → 人工待办 | ✅ | 限流。重试只会加剧，直接转人工并告警。 |
| `LLM_HTTP_503` | generation | 重试 ≤2 次，耗尽转人工待办 | — | 网关 503。生成类可重试。 |
| `LLM_HTTP_504` | generation | 重试 ≤2 次，耗尽转人工待办 | — | 网关 504（A2 实测 pro 模型出现过 184.8s 才 504）。 |
| `LLM_HTTP_ERROR` | generation | 重试 ≤2 次，耗尽转人工待办 | — | OpenAI 兼容对话接口传输失败或 HTTP 非成功。生成类可重试。 |
| `LLM_JSON_DECODE` | generation | 重试 ≤2 次，耗尽转人工待办 | — | JSON 解析失败。可重试。 |
| `LLM_LANG_MISSING` | generation | 重试 ≤2 次，耗尽转人工待办 | — | 模型少给语种。**不得静默少交**，视为不合格产出。 |
| `LLM_LANG_UNSUPPORTED` | generation | 重试 ≤2 次，耗尽转人工待办 | — | mock 未内置该语种（真实实现走通用模板）。 |
| `LLM_NOT_JSON` | generation | 重试 ≤2 次，耗尽转人工待办 | — | 响应不是 JSON。可重试（模型偶发）。 |
| `LLM_NO_CHOICES` | generation | 重试 ≤2 次，耗尽转人工待办 | — | 响应无 choices。可重试一次。 |
| `LLM_NO_LANGS` | validation | 立即停，**不自动重试** | — | 语种列表为空。不发外呼，省一次白烧。 |
| `LLM_NO_RESULT` | generation | 降级 → 人工待办 | ✅ | 文案失败且无错误体（兜底码）。 |
| `LLM_QUOTA_BANNED` | execution | 立即停，**不自动重试** | ✅ | 账号被封/配额耗尽。**执行类语义：立即停不重试**，重试会加重处罚。 |
| `LLM_SCHEMA_INVALID` | generation | 重试 ≤2 次，耗尽转人工待办 | — | 结构校验失败（缺字段/卖点不足）。重试 2 次耗尽 → 人工待办。 |
| `LLM_TIMEOUT` | generation | 重试 ≤2 次，耗尽转人工待办 | — | 网关超时。生成类可重试（≤2 次）。 |
| `LLM_TRANSPORT` | generation | 重试 ≤2 次，耗尽转人工待办 | — | 传输层异常（连接被拒等）。生成类可重试。 |
| `LLM_TRUNCATED` | generation | 重试 ≤2 次，耗尽转人工待办 | — | finish_reason=length 输出被截断。需提高该任务 max_tokens 后重试。 |
| `LLM_UNKNOWN_TASK` | validation | 立即停，**不自动重试** | — | 未配置该任务的调用预算（max_tokens 等）。配置错误，不是运行时故障。 |
| `LLM_UPSTREAM_503` | generation | 重试 ≤2 次，耗尽转人工待办 | — | 上游不可用（测试/注入用同码语义）。 |
| `MANUAL_UNKNOWN` | validation | 立即停，**不自动重试** | ✅ | 补录失败且适配器未给错误体（兜底码）。 |
| `ORCH_CANCELLED` | policy | 策略拒绝（无跳过） | ✅ | 编排器 run 被取消。终态，不续跑；人工可开新 run。 |
| `ORCH_MISSING_URL` | validation | 立即停，**不自动重试** | — | 启动编排 run 未提供 1688 url。 |
| `ORCH_OUTREACH_COMPLIANCE` | policy | 策略拒绝（无跳过） | ✅ | 达人建联草稿命中合规词 block、供给侧泄漏或泰语质检未过。步骤失败，不进入待审；不发送。 |
| `ORCH_OUTREACH_CREATOR` | validation | 立即停，**不自动重试** | — | 达人类型不是测评、生活、出租屋改造，或 creators 结构非法、重复。缺省为三类各一条，昵称「达人」。 |
| `ORCH_OUTREACH_INCOMPLETE` | validation | 立即停，**不自动重试** | ✅ | 达人建联草稿条数、昵称、条款键、跟进日或固定结尾与输入不一致。不入库，不发送。 |
| `ORCH_OUTREACH_NO_COPY` | validation | 立即停，**不自动重试** | ✅ | 达人建联步缺少上一步 listing 标题或卖点。不编造文案，不入库待审。 |
| `ORCH_OUTREACH_UNGROUNDED` | policy | 策略拒绝（无跳过） | ✅ | 达人建联草稿写出了输入里没有的佣金比例、粉丝数、保底播放、赠品、免打孔、已发送或未提供的规格。步骤失败，不进入待审；可 retry_step。不外呼平台。 |
| `ORCH_QA_BLOCK` | policy | 策略拒绝（无跳过） | ✅ | 编排器质检未通过（非合规词原因，如缺泰文/空标题），草稿不可进审批通过。无跳过。 |
| `ORCH_QA_EXHAUSTED` | policy | 策略拒绝（无跳过） | ✅ | QA 打回 Stylist 重写已达上限（qa_max_rewrite_rounds）。流水线失败，无跳过。 |
| `ORCH_QA_MISSING` | policy | 策略拒绝（无跳过） | ✅ | 进入审批前缺少质检结果（context 无 qa）。缺失不等于通过，须重跑质检步。 |
| `ORCH_SCRIPT_CART` | validation | 立即停，**不自动重试** | — | 是否挂车不是是/否（或缺省）。缺省视为挂车；非法值不猜测。 |
| `ORCH_SCRIPT_DURATION` | validation | 立即停，**不自动重试** | — | 短视频时长不是 15–60 的整数秒。缺省为 30，非法值不静默改时长。 |
| `ORCH_SCRIPT_NO_COPY` | validation | 立即停，**不自动重试** | ✅ | 短视频脚本步缺少上一步 listing 标题或卖点。不编造文案，不进入审批。 |
| `ORCH_SCRIPT_UNGROUNDED` | policy | 策略拒绝（无跳过） | ✅ | 短视频脚本写出了卖家未提供的防水、食品级、承重数字或「全网最低」。步骤失败，不进入审批；可 retry_step。不外呼平台。 |
| `ORCH_STEP_RETRIES_EXCEEDED` | generation | 降级 → 人工待办 | ✅ | 编排器 AI 步骤生成类重试耗尽。已告警；步骤失败，人工待办或 retry_step。 |
| `ORDER_NOT_FULFILLABLE` | policy | 策略拒绝（无跳过） | ✅ | 订单状态不是已支付且未发货（或未知），不能启动履约/代发；执行前会再次复核。 |
| `ORDER_SLA_DUE` | policy | 降级 → 人工待办 | ✅ | 订单发货 SLA 临期或已超期。只提醒，不自动发货（发货仍 403）。 |
| `P1688_ANTI_CRAWL` | generation | 降级 → 人工待办 | ✅ | 1688 返回 punish 壳页（HTTP 200 但无数据）。**不在服务端循环重轰**（A1 实测：重轰会自伤本机 IP），直接转人工补录。 |
| `P1688_BAD_URL` | validation | 立即停，**不自动重试** | — | 入参不是合法 URL。重试无意义，直接拒。 |
| `P1688_FETCH_FAILED` | generation | 降级 → 人工待办 | ✅ | 网络层失败（超时/断连）。可单次重试，耗尽转人工。 |
| `P1688_MANUAL_INCOMPLETE` | validation | 立即停，**不自动重试** | ✅ | 人工补录缺必要字段（source_url/title_raw/price_cents）。回表单，不入库。 |
| `P1688_MANUAL_INVALID` | validation | 立即停，**不自动重试** | ✅ | 人工补录字段类型不合法。回表单，不入库。 |
| `P1688_MANUAL_NONPOSITIVE_COST` | validation | 立即停，**不自动重试** | ✅ | 补录成本 ≤0。0 会让选品报告宣称近 100% 毛利，属假数据，必须拒。 |
| `P1688_PARSE_EMPTY` | generation | 降级 → 人工待办 | ✅ | 页面取到了但关键字段全空。转人工核对。 |
| `PII_PURGE_FAILED` | execution | 立即停，**不自动重试** | ✅ | 买家 PII 保留期覆写任务失败（订单/客服会话）。已 release_job；可选告警，不自动重试轰炸。配置见 PII_RETENTION_DAYS / PII_PURGE_DRY_RUN。 |
| `PII_RETENTION_INVALID` | validation | 立即停，**不自动重试** | ✅ | PII_RETENTION_DAYS 非法（非整数或 <1）。不执行清理；改配置后重跑。 |
| `PLATFORM_AUDIT_FAILED` | execution | 立即停，**不自动重试** | ✅ | 平台商品审核未通过（P0-07 轮询回写）。需人工到卖家中心查看原因并改文案/图片后重新上架；不自动重提。 |
| `PRICING_COST_REQUIRED` | validation | 立即停，**不自动重试** | ✅ | 售价公式缺成本 cost_cny（或 ≤0）。不填假成本；须补录 1688 成本或人工录入后再定价。 |
| `PRICING_DEVIATION` | policy | 策略拒绝（无跳过） | ✅ | LLM/覆盖价相对公式建议价偏离超过 PRICING_DEVIATION_PCT。编排器挂起，须审批 pricing_override。 |
| `PRICING_INVALID_INPUT` | validation | 立即停，**不自动重试** | — | 售价公式输入/占位费率不合法（负费率、fx≤0、合计≥100% 等）。改配置或入参后重试。 |
| `PROFIT_DATE_INVALID` | validation | 立即停，**不自动重试** | — | 利润看板 from/to 日期非法（须 YYYY-MM-DD）或 from>to。只读查询不写库；改正参数后重试。 |
| `SHOP_CREDENTIALS_MISSING` | validation | 立即停，**不自动重试** | ✅ | 该 shop_id 未配置完整 TikTok 凭据（前缀 TIKTOK_<SHOP>_APP_KEY/SECRET/ACCESS_TOKEN；default 可回落无前缀 TIKTOK_*）。不签名、不外呼；不解锁 TTS_WRITE_*。 |
| `SHOP_ID_INVALID` | validation | 立即停，**不自动重试** | — | shop_id 含非法字符或超长（仅字母数字/_/-，最长 64）。 |
| `SHOP_UNKNOWN` | validation | 立即停，**不自动重试** | — | 请求的 shop_id 不在 TIKTOK_SHOPS（未设时仅 default）配置列表中。不读凭据、不外呼。 |
| `STOCK_LISTING_REQUIRED` | validation | 立即停，**不自动重试** | — | update_stock 缺少 listing_id。不发库存推送。 |
| `STOCK_OUT_OF_STOCK` | execution | 立即停，**不自动重试** | ✅ | 供应商断货（可用量为 0），已生成下架审批单。需人工决定是否下架；不自动执行。 |
| `STOCK_PRODUCT_REQUIRED` | validation | 立即停，**不自动重试** | — | TikTok update_stock 缺少 platform_product_id。不发库存推送。 |
| `STOCK_QTY_INVALID` | validation | 立即停，**不自动重试** | — | update_stock 的 quantity 非整数或为负。不发库存推送。 |
| `SUPPLIER_LEAK_HIT` | policy | 策略拒绝（无跳过） | ✅ | 买家文案含供给侧事实（MOQ/批发/起订）。是否升格为 §7 硬规则待拍板，当前=转人工。 |
| `TIKTOK_CREDENTIAL_EXPIRED` | execution | 立即停，**不自动重试** | ✅ | access token 已过期，真写会被平台拒绝。需人工刷新或重取后再试。 |
| `TIKTOK_CREDENTIAL_EXPIRING` | execution | 立即停，**不自动重试** | ✅ | access token 将在 72h / 24h 内过期（P0-12）。提前提醒以便在要上架前刷新；不自动刷新。 |
| `TIKTOK_CREDENTIAL_MISSING` | execution | 立即停，**不自动重试** | ✅ | TikTok 凭据未配置（缺 APP_KEY/APP_SECRET/ACCESS_TOKEN），真写通道不可用。需人工补齐。 |
| `TIKTOK_CREDENTIAL_UNKNOWN_EXPIRY` | policy | 策略拒绝（无跳过） | ✅ | 未设置 TIKTOK_ACCESS_TOKEN_EXPIRE_AT，无法判断 token 何时到期。这是 .env.example 的默认值，故为常态；token 可能早已过期而无人知晓。需人工补上到期时间以便提前告警。 |
| `TRACKING_INVALID` | validation | 立即停，**不自动重试** | — | 承运商不在白名单，或单号不符合该承运商格式（Thailand Post 另核 UPU S10 校验位）。格式不过时不查询承运方。不回传平台。 |
| `TRACKING_LOOKUP_FAILED` | execution | 立即停，**不自动重试** | ✅ | 承运方只读查询超时、非 200、非 JSON、found 不是布尔，或响应过大。不把失败当成已确认，不回传。执行类不自动重试。 |
| `TRACKING_LOOKUP_MISMATCH` | validation | 立即停，**不自动重试** | — | 承运方只读查询返回的承运商或单号与所问不一致。不把这票当成已确认，不回传。 |
| `TRACKING_LOOKUP_UNCONFIGURED` | validation | 立即停，**不自动重试** | ✅ | TRACKING_LOOKUP_ENABLED 已开，但 URL 为空、不是 http(s)，或把令牌写进了 URL。不外呼、不回传。 |
| `TRACKING_NOT_FOUND` | validation | 立即停，**不自动重试** | — | 承运方只读查询返回 found=false。格式虽过，但查询结果说没有这票。不回传平台。 |
| `TRACKING_PUSH_FAILED` | execution | 立即停，**不自动重试** | ✅ | 物流回填失败且无错误体（兜底）。执行类不自动重试。 |
| `TRACKING_REQUIRED` | validation | 立即停，**不自动重试** | — | 物流回填缺少 order_id / carrier / tracking_no。不回传。 |
| `TTS_API_ERROR` | execution | 立即停，**不自动重试** | ✅ | TikTok Shop 返回非成功 code。执行类立即停，不把错误当成空订单。 |
| `TTS_AUTH_EXPIRED` | execution | 立即停，**不自动重试** | ✅ | access_token 已过期（105002）。客户端会尝试刷新一次；仍失败则立即停转人工。 |
| `TTS_CATEGORY_REQUIRED` | validation | 立即停，**不自动重试** | — | get_category_attributes 缺少 category_id。不发请求。 |
| `TTS_HTTP_ERROR` | execution | 立即停，**不自动重试** | ✅ | TikTok Shop HTTP 传输失败。执行类立即停，不自动重试。 |
| `TTS_IMAGE_EMPTY` | validation | 立即停，**不自动重试** | — | upload_image 收到空图片字节。不发上传请求。 |
| `TTS_MISSING_CREDENTIALS` | validation | 立即停，**不自动重试** | — | 直接构造客户端时 app_key、app_secret、access_token 缺一。不签名、不发请求。公共入口缺凭据时走离线空列表。 |
| `TTS_NO_REFRESH_TOKEN` | validation | 立即停，**不自动重试** | — | 缺少 TIKTOK_REFRESH_TOKEN，无法刷新 access token。配置缺失，重试无意义。 |
| `TTS_NO_SHOP` | execution | 立即停，**不自动重试** | ✅ | 没有 shop_cipher，授权店铺接口也没有给出可用店铺。立即停，不自动重试。 |
| `TTS_PAYLOAD_INVALID` | validation | 立即停，**不自动重试** | — | create_product 载荷为空或不是对象。不发创建请求。 |
| `TTS_PRODUCT_REQUIRED` | validation | 立即停，**不自动重试** | — | get_product_audit_status 缺少 product_id。不发请求。 |
| `TTS_RATE_LIMITED` | execution | 立即停，**不自动重试** | ✅ | TikTok Shop HTTP 429 或业务码 36009002。客户端已做有限次退避；耗尽后立即停，编排器不得再自动重试（防重复上架/下单）。 |
| `TTS_REFRESH_FAILED` | execution | 立即停，**不自动重试** | ✅ | 刷新 access token 失败。执行类立即停，错误信息不得包含 app_secret。 |
| `TTS_RPA_STUB` | policy | 策略拒绝（无跳过） | ✅ | RPA 上架占位未实现。无跳过；改用 mock 或干跑，或卖家中心人工上架。 |
| `TTS_SHIPPING_PROVIDER_REQUIRED` | validation | 立即停，**不自动重试** | ✅ | update_tracking 缺少 TIKTOK_SHIPPING_PROVIDER_ID 或显式承运商。不发物流写请求。 |
| `TTS_SIGN_INVALID` | execution | 立即停，**不自动重试** | ✅ | 签名无效（106001 或 invalid sign/signature）。立即停；核对 app_secret/path/body，不自动重试。 |
| `TTS_TIMESTAMP_INVALID` | execution | 立即停，**不自动重试** | ✅ | 时间戳超出官方重放窗（不早于当前 5 分钟、不晚于当前 30 秒）或服务端 36009004+timestamp。立即停；校正本机时钟后人工重试。 |
| `TTS_TRACKING_REQUIRED` | validation | 立即停，**不自动重试** | — | update_tracking 缺少 order_id 或 tracking_no。不发物流写请求。 |
| `TTS_WRITE_BLOCKED` | policy | 策略拒绝（无跳过） | ✅ | 上架、发货、付款、退款与写路径方法默认不由本客户端执行；须 allow_writes=True，或 tiktok_live 下 TTS_WRITE_ENABLED=1 且 shop_cipher 在白名单，否则卖家中心人工操作。 |
| `TTS_WRITE_QUOTA_EXCEEDED` | policy | 策略拒绝（无跳过） | ✅ | 真写上架触及单次或每日数量上限（默认均为 1）。不外呼；调低频率或改 TTS_WRITE_DAILY_LIMIT。 |
| `TT_AUTH_INVALID` | policy | 策略拒绝（无跳过） | ✅ | 店铺凭据已知失效。本地 access token 到期时间已过，或已观察到 HTTP 401 / 业务码 105002（TTS_AUTH_EXPIRED）。create_product、upload_image、update_stock 返回本码且不发这些请求。72h/24h 临期与未设到期时间不触发。令牌刷新成功或到期时间更新到更晚的未来后解除；解除后写仍须 TTS_WRITE_*。只读请求不因此跳过。DESIGN §7.8。 |
| `TT_RATE_LIMITED` | execution | 立即停，**不自动重试** | ✅ | 平台限流。**执行类不自动重试**（DESIGN §3.3 防重复上架/下单）。 |
| `TT_RISK_CONTROL` | execution | 立即停，**不自动重试** | ✅ | 平台风控拦截。立即停、保留现场，交人工判断（RPA 通道尤其危险）。 |
| `UNKNOWN` | validation | 立即停，**不自动重试** | ✅ | 适配器返回失败但未给错误体（兜底，应被消灭）。 |
| `VERSION_CAS_EXHAUSTED` | execution | 立即停，**不自动重试** | ✅ | listing 版本号乐观锁 CAS 重读重试 3 次仍冲突（SCHEMA §4.5）。执行类：重试上限内已按 §4.5 重读再试过，耗尽后**停下转人工**，绝不无限重试（§3.3 执行类不自动重试，避免重复下单式双花），也不把裸 IntegrityError 抛穿 API 层。 |
| `WEBHOOK_PUSH_FAILED` | execution | 立即停，**不自动重试** | ✅ | WORKFLOW_WEBHOOK_URL 报表推送失败（拒绝/传输/HTTP）。可选告警；不含密钥。 |
| `WEBHOOK_TRANSPORT` | generation | 重试 ≤2 次，耗尽转人工待办 | — | WORKFLOW_WEBHOOK_URL 报表推送传输/5xx 瞬时失败。统一重试层可有限退避；耗尽后记 WEBHOOK_PUSH_FAILED。 |

## 不变式（登记表把守的四条）

1. `kind=generation` 的码，处置必须是 `retry` 或 `degrade_manual`——不得写成执行类语义。
2. `kind=execution` 的码，处置**必须**是 `stop_no_retry` 或 `policy_reject`：一旦允许重试就是重复下单风险。
3. `kind=policy` 的码必须 `human_todo=True`：闸门拒绝必须留人工痕迹。
4. 处置为 `retry` 的码，`human_todo` 由重试耗尽后的降级路径负责置位，登记处保持 False。
