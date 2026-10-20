"""错误码登记表（C3 完成条件 2：**代码与文档同源**）。

规矩：
- 任何 `AdapterError` 的 code 必须在这里登记，否则 `test_error_codes_are_registered` 红；
- 文档 `docs/error-codes.md` 由 `render_markdown()` 生成，不手抄——手抄必然过期；
- `disposition` 就是 DESIGN §3.3 的处置语义：重试 / 降级人工待办 / 立即停不重试 / 策略拒绝。

为什么值得单独建表：错误码是编排器唯一能读懂的输入。
码起得对但处置登记错（比如把执行类写成可重试），后果是重复下单。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from shopkeeper.errors import ErrorKind


class Disposition(StrEnum):
    RETRY = "retry"                        # 生成类：重试 ≤2 次后转人工待办
    DEGRADE_MANUAL_TODO = "degrade_manual" # 重试耗尽/不可自动：落人工待办
    STOP_NO_RETRY = "stop_no_retry"        # 执行类：立即停、保留现场，绝不自动重试
    POLICY_REJECT = "policy_reject"        # 合规/闸门拒绝：不提供跳过


#: 登记表里用短名，避免每行都写 Disposition.XXX（但必须显式从枚举取值，不写字符串）
RETRY = Disposition.RETRY
DEGRADE_MANUAL_TODO = Disposition.DEGRADE_MANUAL_TODO
STOP_NO_RETRY = Disposition.STOP_NO_RETRY
POLICY_REJECT = Disposition.POLICY_REJECT


@dataclass(frozen=True)
class CodeSpec:
    code: str
    kind: ErrorKind
    disposition: Disposition
    human_todo: bool
    meaning: str


def _c(code: str, kind: ErrorKind, disp: Disposition, *, human_todo: bool, meaning: str) -> CodeSpec:
    return CodeSpec(code=code, kind=kind, disposition=disp, human_todo=human_todo, meaning=meaning)


#: 全量登记表。新增码必须同时加行，否则 CI 红。
REGISTRY: dict[str, CodeSpec] = {
    spec.code: spec
    for spec in (
        # ---- 采集侧（ProductSource / A1 裁决：默认预期失败，不重轰） ----
        _c("P1688_BAD_URL", ErrorKind.VALIDATION, STOP_NO_RETRY, human_todo=False,
           meaning="入参不是合法 URL。重试无意义，直接拒。"),
        _c("P1688_ANTI_CRAWL", ErrorKind.GENERATION, DEGRADE_MANUAL_TODO, human_todo=True,
           meaning="1688 返回 punish 壳页（HTTP 200 但无数据）。**不在服务端循环重轰**（A1 实测：重轰会自伤本机 IP），直接转人工补录。"),
        _c("P1688_FETCH_FAILED", ErrorKind.GENERATION, DEGRADE_MANUAL_TODO, human_todo=True,
           meaning="网络层失败（超时/断连）。可单次重试，耗尽转人工。"),
        _c("P1688_MANUAL_INCOMPLETE", ErrorKind.VALIDATION, STOP_NO_RETRY, human_todo=True,
           meaning="人工补录缺必要字段（source_url/title_raw/price_cents）。回表单，不入库。"),
        _c("P1688_MANUAL_INVALID", ErrorKind.VALIDATION, STOP_NO_RETRY, human_todo=True,
           meaning="人工补录字段类型不合法。回表单，不入库。"),
        _c("P1688_MANUAL_NONPOSITIVE_COST", ErrorKind.VALIDATION, STOP_NO_RETRY, human_todo=True,
           meaning="补录成本 ≤0。0 会让选品报告宣称近 100% 毛利，属假数据，必须拒。"),
        _c("P1688_PARSE_EMPTY", ErrorKind.GENERATION, DEGRADE_MANUAL_TODO, human_todo=True,
           meaning="页面取到了但关键字段全空。转人工核对。"),
        # ---- 1688 Open API 货源（ProductSource 真实客户端骨架；默认关，mock 回退）----
        _c("A1688_MISSING_CREDENTIALS", ErrorKind.VALIDATION, STOP_NO_RETRY, human_todo=True,
           meaning="ALIBABA_1688_ENABLED=1 但缺少 APP_KEY/APP_SECRET/ACCESS_TOKEN。不签名、不外呼；"
                   "工厂层亦会回退 MockProductSource。"),
        _c("A1688_HTTP_ERROR", ErrorKind.GENERATION, DEGRADE_MANUAL_TODO, human_todo=True,
           meaning="1688 Open API HTTP/传输失败。生成类可降级人工补录；不重轰。"),
        _c("A1688_TIMEOUT", ErrorKind.GENERATION, RETRY, human_todo=False,
           meaning="1688 Open API 超时。生成类可有限重试，耗尽转人工。"),
        _c("A1688_API_ERROR", ErrorKind.GENERATION, DEGRADE_MANUAL_TODO, human_todo=True,
           meaning="1688 Open API 返回业务错误或非 JSON。转人工核对；不把错误当成功。"),
        _c("A1688_PARSE_EMPTY", ErrorKind.GENERATION, DEGRADE_MANUAL_TODO, human_todo=True,
           meaning="API 响应缺少标题/价格/主图等关键字段。转人工补录。"),
        _c("A1688_FETCH_FAILED", ErrorKind.GENERATION, DEGRADE_MANUAL_TODO, human_todo=True,
           meaning="1688 Open API 货源适配器未预期异常已收束为失败结果（兜底）。转人工。"),
        _c("MANUAL_UNKNOWN", ErrorKind.VALIDATION, STOP_NO_RETRY, human_todo=True,
           meaning="补录失败且适配器未给错误体（兜底码）。"),
        # ---- 文案侧（LlmClient） ----
        _c("LLM_NO_LANGS", ErrorKind.VALIDATION, STOP_NO_RETRY, human_todo=False,
           meaning="语种列表为空。不发外呼，省一次白烧。"),
        _c("LLM_UNKNOWN_TASK", ErrorKind.VALIDATION, STOP_NO_RETRY, human_todo=False,
           meaning="未配置该任务的调用预算（max_tokens 等）。配置错误，不是运行时故障。"),
        _c("LLM_TIMEOUT", ErrorKind.GENERATION, RETRY, human_todo=False,
           meaning="网关超时。生成类可重试（≤2 次）。"),
        _c("LLM_TRANSPORT", ErrorKind.GENERATION, RETRY, human_todo=False,
           meaning="传输层异常（连接被拒等）。生成类可重试。"),
        _c("LLM_HTTP_503", ErrorKind.GENERATION, RETRY, human_todo=False,
           meaning="网关 503。生成类可重试。"),
        _c("LLM_HTTP_504", ErrorKind.GENERATION, RETRY, human_todo=False,
           meaning="网关 504（A2 实测 pro 模型出现过 184.8s 才 504）。"),
        _c("LLM_HTTP_429", ErrorKind.GENERATION, DEGRADE_MANUAL_TODO, human_todo=True,
           meaning="限流。重试只会加剧，直接转人工并告警。"),
        _c("LLM_NO_CHOICES", ErrorKind.GENERATION, RETRY, human_todo=False,
           meaning="响应无 choices。可重试一次。"),
        _c("LLM_EMPTY_CONTENT", ErrorKind.GENERATION, RETRY, human_todo=False,
           meaning="content 为空（A2 实测：思考吃满 max_tokens 预算）。**先查预算配置再重试**，否则 3 次都白烧。"),
        _c("LLM_TRUNCATED", ErrorKind.GENERATION, RETRY, human_todo=False,
           meaning="finish_reason=length 输出被截断。需提高该任务 max_tokens 后重试。"),
        _c("LLM_NOT_JSON", ErrorKind.GENERATION, RETRY, human_todo=False,
           meaning="响应不是 JSON。可重试（模型偶发）。"),
        _c("LLM_JSON_DECODE", ErrorKind.GENERATION, RETRY, human_todo=False,
           meaning="JSON 解析失败。可重试。"),
        _c("LLM_SCHEMA_INVALID", ErrorKind.GENERATION, RETRY, human_todo=False,
           meaning="结构校验失败（缺字段/卖点不足）。重试 2 次耗尽 → 人工待办。"),
        _c("LLM_LANG_MISSING", ErrorKind.GENERATION, RETRY, human_todo=False,
           meaning="模型少给语种。**不得静默少交**，视为不合格产出。"),
        _c("LLM_LANG_UNSUPPORTED", ErrorKind.GENERATION, RETRY, human_todo=False,
           meaning="mock 未内置该语种（真实实现走通用模板）。"),
        _c("LLM_UPSTREAM_503", ErrorKind.GENERATION, RETRY, human_todo=False,
           meaning="上游不可用（测试/注入用同码语义）。"),
        _c("LLM_NO_RESULT", ErrorKind.GENERATION, DEGRADE_MANUAL_TODO, human_todo=True,
           meaning="文案失败且无错误体（兜底码）。"),
        _c("LLM_QUOTA_BANNED", ErrorKind.EXECUTION, STOP_NO_RETRY, human_todo=True,
           meaning="账号被封/配额耗尽。**执行类语义：立即停不重试**，重试会加重处罚。"),
        _c("LLM_HTTP_ERROR", ErrorKind.GENERATION, RETRY, human_todo=False,
           meaning="OpenAI 兼容对话接口传输失败或 HTTP 非成功。生成类可重试。"),
        _c("LLM_EMPTY", ErrorKind.GENERATION, RETRY, human_todo=False,
           meaning="OpenAI 兼容接口没有返回正文。生成类可重试。"),
        _c("DIFY_HTTP_ERROR", ErrorKind.GENERATION, RETRY, human_todo=False,
           meaning="Dify chat-messages 传输失败或 HTTP 非成功。生成类可重试。"),
        _c("DIFY_EMPTY", ErrorKind.GENERATION, RETRY, human_todo=False,
           meaning="Dify 响应里没有 answer。生成类可重试，不编造文案。"),
        _c("COZE_HTTP_ERROR", ErrorKind.GENERATION, RETRY, human_todo=False,
           meaning="Coze /v3/chat 传输失败或返回错误码。生成类可重试。"),
        _c("COZE_PENDING", ErrorKind.GENERATION, RETRY, human_todo=False,
           meaning="Coze 对话尚未完成。本次不编造文案，生成类可重试。"),
        _c("COZE_EMPTY", ErrorKind.GENERATION, RETRY, human_todo=False,
           meaning="Coze 没有返回答案。生成类可重试。"),
        # ---- 图像侧（ImageClient） ----
        _c("IMG_NO_REF", ErrorKind.VALIDATION, STOP_NO_RETRY, human_todo=False,
           meaning="缺少源图引用。"),
        _c("IMG_NO_LANG", ErrorKind.VALIDATION, STOP_NO_RETRY, human_todo=False,
           meaning="缺少目标语种。"),
        _c("IMG_NO_COPY", ErrorKind.VALIDATION, STOP_NO_RETRY, human_todo=True,
           meaning="没有可叠加的目标语言文案。不许画占位假字冒充交付。"),
        _c("IMG_FETCH_FAILED", ErrorKind.GENERATION, DEGRADE_MANUAL_TODO, human_todo=True,
           meaning="源图下载失败。降级：保留原图引用 + 人工。"),
        _c("IMG_FETCH_STATUS", ErrorKind.GENERATION, DEGRADE_MANUAL_TODO, human_todo=True,
           meaning="源图返回非 200 或过小。降级同上。"),
        _c("IMG_NOT_DECODABLE", ErrorKind.GENERATION, DEGRADE_MANUAL_TODO, human_todo=True,
           meaning="图像不可解码。降级同上。"),
        _c("IMG_FILE_MISSING", ErrorKind.GENERATION, DEGRADE_MANUAL_TODO, human_todo=True,
           meaning="本地源图文件不存在。降级同上。"),
        _c("IMG_FONT_MISSING", ErrorKind.GENERATION, DEGRADE_MANUAL_TODO, human_todo=True,
           meaning="该语种无可用字体（A3：泰语只有 Tahoma 有字形）。必须人工出图。"),
        _c("IMG_UPSTREAM_500", ErrorKind.GENERATION, DEGRADE_MANUAL_TODO, human_todo=True,
           meaning="图像处理服务 500。降级：original_kept + needs_human。"),
        _c("IMG_NO_RESULT", ErrorKind.GENERATION, DEGRADE_MANUAL_TODO, human_todo=True,
           meaning="图像处理失败且无错误体（兜底码）。"),
        _c("IMG_PILLOW_BOOM", ErrorKind.GENERATION, DEGRADE_MANUAL_TODO, human_todo=True,
           meaning="本地处理异常（注入式测试用同码语义）。降级 original_kept + needs_human。"),
        _c("IMG_REDRAW_DISABLED", ErrorKind.VALIDATION, STOP_NO_RETRY, human_todo=False,
           meaning="IMAGE_REDRAW_ENABLED=0：真重绘关闭，不外呼。上层走 Pillow 后处理。"),
        _c("IMG_REDRAW_STUB", ErrorKind.GENERATION, DEGRADE_MANUAL_TODO, human_todo=True,
           meaning="真重绘 stub：网关无 edits/图生图（spike/image.md），不外呼、不编造成功；降级 Pillow。"),
        # ---- 商品图托管（ImageStore：本地 artifacts / S3 兼容骨架）----
        _c("IMG_STORE_EMPTY", ErrorKind.VALIDATION, STOP_NO_RETRY, human_todo=False,
           meaning="ImageStore.put/open 收到空字节。不写盘、不外呼对象存储。"),
        _c("IMG_STORE_BAD_KEY", ErrorKind.VALIDATION, STOP_NO_RETRY, human_todo=False,
           meaning="ImageStore key 非法或越出根目录（含 ..）。拒绝解析。"),
        _c("IMG_STORE_NOT_FOUND", ErrorKind.GENERATION, DEGRADE_MANUAL_TODO, human_todo=True,
           meaning="托管对象不存在（本地缺文件或 S3 404）。转人工核对 key。"),
        _c("IMG_STORE_IO_ERROR", ErrorKind.GENERATION, DEGRADE_MANUAL_TODO, human_todo=True,
           meaning="本地 ImageStore 读写盘失败。转人工；不冒充已托管。"),
        _c("IMG_STORE_MISSING_CREDENTIALS", ErrorKind.VALIDATION, STOP_NO_RETRY, human_todo=True,
           meaning="IMAGE_STORE=s3 但缺少 ENDPOINT/BUCKET/ACCESS_KEY/SECRET_KEY。"
                   "工厂层回退 LocalFileImageStore；直接构造的 S3 客户端不外呼。"),
        _c("IMG_STORE_HTTP_ERROR", ErrorKind.GENERATION, DEGRADE_MANUAL_TODO, human_todo=True,
           meaning="S3 兼容 Put/Get/HEAD HTTP 或传输失败。转人工；不重轰。"),
        _c("IMG_STORE_TIMEOUT", ErrorKind.GENERATION, RETRY, human_todo=False,
           meaning="S3 兼容请求超时。生成类可有限重试，耗尽转人工。"),
        # ---- 平台侧（M2 落地，先登记语义，防止有人当生成类重试） ----
        _c("TT_RATE_LIMITED", ErrorKind.EXECUTION, STOP_NO_RETRY, human_todo=True,
           meaning="平台限流。**执行类不自动重试**（DESIGN §3.3 防重复上架/下单）。"),
        _c("TT_AUTH_INVALID", ErrorKind.POLICY, POLICY_REJECT, human_todo=True,
           meaning="店铺凭据已知失效。本地 access token 到期时间已过，或已观察到 HTTP 401 / "
                   "业务码 105002（TTS_AUTH_EXPIRED）。create_product、upload_image、update_stock "
                   "返回本码且不发这些请求。72h/24h 临期与未设到期时间不触发。"
                   "令牌刷新成功或到期时间更新到更晚的未来后解除；解除后写仍须 TTS_WRITE_*。"
                   "只读请求不因此跳过。DESIGN §7.8。"),
        _c("TT_RISK_CONTROL", ErrorKind.EXECUTION, STOP_NO_RETRY, human_todo=True,
           meaning="平台风控拦截。立即停、保留现场，交人工判断（RPA 通道尤其危险）。"),
        # ---- TikTok Shop 只读客户端（202309）。执行类不自动重试，写操作是策略拒绝。 ----
        _c("TTS_MISSING_CREDENTIALS", ErrorKind.VALIDATION, STOP_NO_RETRY, human_todo=False,
           meaning="直接构造客户端时 app_key、app_secret、access_token 缺一。不签名、不发请求。公共入口缺凭据时走离线空列表。"),
        _c("TTS_NO_REFRESH_TOKEN", ErrorKind.VALIDATION, STOP_NO_RETRY, human_todo=False,
           meaning="缺少 TIKTOK_REFRESH_TOKEN，无法刷新 access token。配置缺失，重试无意义。"),
        _c("GATEWAY_AUTH_REJECTED", ErrorKind.POLICY, POLICY_REJECT, human_todo=True,
           meaning="平台操作网关 token 校验失败（GATEWAY_TOKEN）。远程队列拒绝；检查主系统与海外侧口令。"),
        _c("GATEWAY_METHOD_FORBIDDEN", ErrorKind.POLICY, POLICY_REJECT, human_todo=True,
           meaning="网关拒绝发货/退款等方法（与工作台 HTTP 403 同口径）。须卖家中心人工处理。"),
        _c("GATEWAY_METHOD_UNKNOWN", ErrorKind.VALIDATION, STOP_NO_RETRY, human_todo=False,
           meaning="网关收到未登记的 method。不执行。"),
        _c("GATEWAY_TIMEOUT", ErrorKind.EXECUTION, STOP_NO_RETRY, human_todo=True,
           meaning="等待海外网关任务结果超时。执行类不自动重试。"),
        _c("GATEWAY_REMOTE_FAILED", ErrorKind.EXECUTION, STOP_NO_RETRY, human_todo=True,
           meaning="远程网关回传失败且无结构化错误体（兜底）。"),
        _c("SHOP_UNKNOWN", ErrorKind.VALIDATION, STOP_NO_RETRY, human_todo=False,
           meaning="请求的 shop_id 不在 TIKTOK_SHOPS（未设时仅 default）配置列表中。不读凭据、不外呼。"),
        _c("SHOP_ID_INVALID", ErrorKind.VALIDATION, STOP_NO_RETRY, human_todo=False,
           meaning="shop_id 含非法字符或超长（仅字母数字/_/-，最长 64）。"),
        _c("SHOP_CREDENTIALS_MISSING", ErrorKind.VALIDATION, STOP_NO_RETRY, human_todo=True,
           meaning="该 shop_id 未配置完整 TikTok 凭据（前缀 TIKTOK_<SHOP>_APP_KEY/SECRET/ACCESS_TOKEN；"
                   "default 可回落无前缀 TIKTOK_*）。不签名、不外呼；不解锁 TTS_WRITE_*。"),
        _c("TTS_WRITE_BLOCKED", ErrorKind.POLICY, POLICY_REJECT, human_todo=True,
           meaning="上架、发货、付款、退款与写路径方法默认不由本客户端执行；须 allow_writes=True，"
                   "或 tiktok_live 下 TTS_WRITE_ENABLED=1 且 shop_cipher 在白名单，否则卖家中心人工操作。"),
        _c("TTS_WRITE_QUOTA_EXCEEDED", ErrorKind.POLICY, POLICY_REJECT, human_todo=True,
           meaning="真写上架触及单次或每日数量上限（默认均为 1）。不外呼；调低频率或改 TTS_WRITE_DAILY_LIMIT。"),
        _c("TTS_RPA_STUB", ErrorKind.POLICY, POLICY_REJECT, human_todo=True,
           meaning="RPA 上架占位未实现。无跳过；改用 mock 或干跑，或卖家中心人工上架。"),
        _c("GATE_NOT_APPROVED", ErrorKind.POLICY, POLICY_REJECT, human_todo=True,
           meaning="上架未经人工审批或已拒绝/过期。不调用平台写适配器，无「跳过本次」。"),
        _c("DROPSHIP_ORDER_REQUIRED", ErrorKind.VALIDATION, STOP_NO_RETRY, human_todo=False,
           meaning="1688 代发草稿/下单缺少 order_id。不外呼。"),
        _c("DROPSHIP_DRAFT_FAILED", ErrorKind.EXECUTION, STOP_NO_RETRY, human_todo=True,
           meaning="代发草稿生成失败且无错误体（兜底）。不付款。"),
        _c("DROPSHIP_PLACE_FAILED", ErrorKind.EXECUTION, STOP_NO_RETRY, human_todo=True,
           meaning="代发下单失败且无错误体（兜底）。执行类不自动重试、不付款。"),
        _c("DROPSHIP_ITEMS_REQUIRED", ErrorKind.VALIDATION, STOP_NO_RETRY, human_todo=True,
           meaning="人工下单草稿缺少商品明细或 SKU。不猜一条、不外呼、不付款。"),
        _c("DROPSHIP_QTY_REQUIRED", ErrorKind.VALIDATION, STOP_NO_RETRY, human_todo=True,
           meaning="人工下单草稿的数量缺失、不是整数或小于 1。不按 1 猜测、不外呼、不付款。"),
        _c("DROPSHIP_MANUAL_NO_AUTOPAY", ErrorKind.POLICY, POLICY_REJECT, human_todo=True,
           meaning="人工下单通道拒绝 place_dropship_order。不调用 1688 下单、不付款；请复制下单信息后由人回填单号。"),
        _c("DROPSHIP_NOT_MANUAL", ErrorKind.VALIDATION, STOP_NO_RETRY, human_todo=False,
           meaning="该代发草稿不是人工下单通道（mode 不是 manual）。不能把它的 mock 单号当成 1688 单号，也不付款。"),
        _c("DROPSHIP_PO_INVALID", ErrorKind.VALIDATION, STOP_NO_RETRY, human_todo=True,
           meaning="回填的 1688 单号不是 16–20 位数字，或链接不是 1688 订单详情页。付款链接一律拒绝。不外呼。"),
        _c("DROPSHIP_PO_CONFLICT", ErrorKind.VALIDATION, STOP_NO_RETRY, human_todo=True,
           meaning="该订单已回填过不同的 1688 单号，不能覆盖。不外呼、不付款。"),
        _c("TRACKING_REQUIRED", ErrorKind.VALIDATION, STOP_NO_RETRY, human_todo=False,
           meaning="物流回填缺少 order_id / carrier / tracking_no。不回传。"),
        _c("TRACKING_INVALID", ErrorKind.VALIDATION, STOP_NO_RETRY, human_todo=False,
           meaning="承运商不在白名单，或单号不符合该承运商格式（Thailand Post 另核 UPU S10 校验位）。"
                   "格式不过时不查询承运方。不回传平台。"),
        _c("TRACKING_NOT_FOUND", ErrorKind.VALIDATION, STOP_NO_RETRY, human_todo=False,
           meaning="承运方只读查询返回 found=false。格式虽过，但查询结果说没有这票。不回传平台。"),
        _c("TRACKING_LOOKUP_MISMATCH", ErrorKind.VALIDATION, STOP_NO_RETRY, human_todo=False,
           meaning="承运方只读查询返回的承运商或单号与所问不一致。不把这票当成已确认，不回传。"),
        _c("TRACKING_LOOKUP_UNCONFIGURED", ErrorKind.VALIDATION, STOP_NO_RETRY, human_todo=True,
           meaning="TRACKING_LOOKUP_ENABLED 已开，但 URL 为空、不是 http(s)，或把令牌写进了 URL。"
                   "不外呼、不回传。"),
        _c("TRACKING_LOOKUP_FAILED", ErrorKind.EXECUTION, STOP_NO_RETRY, human_todo=True,
           meaning="承运方只读查询超时、非 200、非 JSON、found 不是布尔，或响应过大。"
                   "不把失败当成已确认，不回传。执行类不自动重试。"),
        _c("TRACKING_PUSH_FAILED", ErrorKind.EXECUTION, STOP_NO_RETRY, human_todo=True,
           meaning="物流回填失败且无错误体（兜底）。执行类不自动重试。"),
        _c("CS_CATEGORY_REQUIRED", ErrorKind.VALIDATION, STOP_NO_RETRY, human_todo=False,
           meaning="客服入站/草稿缺少 category。不外呼平台。"),
        _c("CS_REPLY_NOT_APPROVED", ErrorKind.POLICY, POLICY_REJECT, human_todo=True,
           meaning="非白名单客服回复未经审批或已拒绝/过期。不 stub 发送，无「跳过本次」。"),
        _c("CS_SEND_FAILED", ErrorKind.EXECUTION, STOP_NO_RETRY, human_todo=True,
           meaning="客服 stub 发送失败且无错误体（兜底）。执行类不自动重试；本切片不外呼平台。"),
        _c("CS_INBOX_NO_CONTRACT", ErrorKind.POLICY, POLICY_REJECT, human_todo=True,
           meaning="客服收件箱只读拉取未接通：本仓库 TikTok 适配器没有客服消息读端点契约，未臆造路径。"
                   "不外呼、不把结果当成空收件箱、不发送回复。接通需 owner 提供可核对的官方文档。"),
        # ---- 观测 / 告警（生产就绪 P0：结构化日志 + 可选外呼） ----
        _c("JOB_RUN_FAILED", ErrorKind.EXECUTION, STOP_NO_RETRY, human_todo=True,
           meaning="工作台定时任务（research/daily）执行抛错。已 release_job；可选告警，不自动重试轰炸。"),
        _c("STOCK_OUT_OF_STOCK", ErrorKind.EXECUTION, STOP_NO_RETRY, human_todo=True,
           meaning="供应商断货（可用量为 0），已生成下架审批单。需人工决定是否下架；不自动执行。"),
        _c("TIKTOK_CREDENTIAL_MISSING", ErrorKind.EXECUTION, STOP_NO_RETRY, human_todo=True,
           meaning="TikTok 凭据未配置（缺 APP_KEY/APP_SECRET/ACCESS_TOKEN），真写通道不可用。需人工补齐。"),
        _c("TIKTOK_CREDENTIAL_UNKNOWN_EXPIRY", ErrorKind.POLICY, POLICY_REJECT, human_todo=True,
           meaning="未设置 TIKTOK_ACCESS_TOKEN_EXPIRE_AT，无法判断 token 何时到期。这是 .env.example 的默认值，故为常态；token 可能早已过期而无人知晓。需人工补上到期时间以便提前告警。"),
        _c("TIKTOK_CREDENTIAL_EXPIRING", ErrorKind.EXECUTION, STOP_NO_RETRY, human_todo=True,
           meaning="access token 将在 72h / 24h 内过期（P0-12）。提前提醒以便在要上架前刷新；不自动刷新。"),
        _c("TIKTOK_CREDENTIAL_EXPIRED", ErrorKind.EXECUTION, STOP_NO_RETRY, human_todo=True,
           meaning="access token 已过期，真写会被平台拒绝。需人工刷新或重取后再试。"),
        _c("PLATFORM_AUDIT_FAILED", ErrorKind.EXECUTION, STOP_NO_RETRY, human_todo=True,
           meaning="平台商品审核未通过（P0-07 轮询回写）。需人工到卖家中心查看原因并改文案/图片后重新上架；不自动重提。"),
        _c("APPROVAL_EXECUTE_FAILED", ErrorKind.EXECUTION, STOP_NO_RETRY, human_todo=True,
           meaning="审批执行失败且无更具体错误码（如缺 platform_product_id）。执行类立即停，可选告警。"),
        _c("WEBHOOK_PUSH_FAILED", ErrorKind.EXECUTION, STOP_NO_RETRY, human_todo=True,
           meaning="WORKFLOW_WEBHOOK_URL 报表推送失败（拒绝/传输/HTTP）。可选告警；不含密钥。"),
        _c("FEISHU_NOTIFY_FAILED", ErrorKind.EXECUTION, STOP_NO_RETRY, human_todo=True,
           meaning="飞书审批卡片 webhook 外呼失败。已降级 stub 不阻断建单；可选告警。"),
        _c("FEISHU_TRANSPORT", ErrorKind.GENERATION, RETRY, human_todo=False,
           meaning="飞书 webhook 传输/5xx 瞬时失败。统一重试层可有限退避；耗尽后降级 stub 并记 FEISHU_NOTIFY_FAILED。"),
        _c("WEBHOOK_TRANSPORT", ErrorKind.GENERATION, RETRY, human_todo=False,
           meaning="WORKFLOW_WEBHOOK_URL 报表推送传输/5xx 瞬时失败。统一重试层可有限退避；耗尽后记 WEBHOOK_PUSH_FAILED。"),
        _c("EXT_RETRY_EXHAUSTED", ErrorKind.GENERATION, DEGRADE_MANUAL_TODO, human_todo=True,
           meaning="统一外部调用重试（指数退避+抖动）预算耗尽。生成类转人工待办；执行类写操作不得走此码自动重试。"),
        _c("EXT_IDEMPOTENCY_CONFLICT", ErrorKind.EXECUTION, STOP_NO_RETRY, human_todo=True,
           meaning="同幂等键的外部副作用写正在进行或并发占位失败。立即停，不二次外呼；待先前结果落库后回放。"),
        _c("BACKUP_FAILED", ErrorKind.EXECUTION, STOP_NO_RETRY, human_todo=True,
           meaning="artifacts 下 .db / 审计 jsonl 每日备份失败。已 release_job；可选告警，不自动重试轰炸。"),
        _c("PII_PURGE_FAILED", ErrorKind.EXECUTION, STOP_NO_RETRY, human_todo=True,
           meaning="买家 PII 保留期覆写任务失败（订单/客服会话）。已 release_job；可选告警，不自动重试轰炸。"
                   "配置见 PII_RETENTION_DAYS / PII_PURGE_DRY_RUN。"),
        _c("ORDER_SLA_DUE", ErrorKind.POLICY, DEGRADE_MANUAL_TODO, human_todo=True,
           meaning="订单发货 SLA 临期或已超期。只提醒，不自动发货（发货仍 403）。"),
        _c("PII_RETENTION_INVALID", ErrorKind.VALIDATION, STOP_NO_RETRY, human_todo=True,
           meaning="PII_RETENTION_DAYS 非法（非整数或 <1）。不执行清理；改配置后重跑。"),
        _c("LLM_BUDGET_EXCEEDED", ErrorKind.POLICY, POLICY_REJECT, human_todo=True,
           meaning="日 LLM 预算（LLM_DAILY_BUDGET_USD）已用尽。编排器暂停 run（budget_paused）并可选告警；不继续外呼。"),
        _c("LLM_FALLBACK_EXHAUSTED", ErrorKind.GENERATION, DEGRADE_MANUAL_TODO, human_todo=True,
           meaning="主模型与 LLM_FALLBACK_MODEL(/MODELS) 均因超时/5xx/限流失败。"
                   "编排器/文案生成回落本地模板并标 degraded；不冒充模型输出。备用调用计入 LLM_DAILY_BUDGET_USD。"),
        _c("ORDER_NOT_FULFILLABLE", ErrorKind.POLICY, POLICY_REJECT, human_todo=True,
           meaning="订单状态不是已支付且未发货（或未知），不能启动履约/代发；执行前会再次复核。"),
        _c("ORCH_STEP_RETRIES_EXCEEDED", ErrorKind.GENERATION, DEGRADE_MANUAL_TODO, human_todo=True,
           meaning="编排器 AI 步骤生成类重试耗尽。已告警；步骤失败，人工待办或 retry_step。"),
        _c("ORCH_QA_EXHAUSTED", ErrorKind.POLICY, POLICY_REJECT, human_todo=True,
           meaning="QA 打回 Stylist 重写已达上限（qa_max_rewrite_rounds）。流水线失败，无跳过。"),
        _c("ORCH_QA_BLOCK", ErrorKind.POLICY, POLICY_REJECT, human_todo=True,
           meaning="编排器质检未通过（非合规词原因，如缺泰文/空标题），草稿不可进审批通过。无跳过。"),
        _c("ORCH_QA_MISSING", ErrorKind.POLICY, POLICY_REJECT, human_todo=True,
           meaning="进入审批前缺少质检结果（context 无 qa）。缺失不等于通过，须重跑质检步。"),
        _c("ORCH_CANCELLED", ErrorKind.POLICY, POLICY_REJECT, human_todo=True,
           meaning="编排器 run 被取消。终态，不续跑；人工可开新 run。"),
        _c("ORCH_MISSING_URL", ErrorKind.VALIDATION, STOP_NO_RETRY, human_todo=False,
           meaning="启动编排 run 未提供 1688 url。"),
        _c("ORCH_SCRIPT_NO_COPY", ErrorKind.VALIDATION, STOP_NO_RETRY, human_todo=True,
           meaning="短视频脚本步缺少上一步 listing 标题或卖点。不编造文案，不进入审批。"),
        _c("ORCH_SCRIPT_DURATION", ErrorKind.VALIDATION, STOP_NO_RETRY, human_todo=False,
           meaning="短视频时长不是 15–60 的整数秒。缺省为 30，非法值不静默改时长。"),
        _c("ORCH_SCRIPT_CART", ErrorKind.VALIDATION, STOP_NO_RETRY, human_todo=False,
           meaning="是否挂车不是是/否（或缺省）。缺省视为挂车；非法值不猜测。"),
        _c("ORCH_SCRIPT_UNGROUNDED", ErrorKind.POLICY, POLICY_REJECT, human_todo=True,
           meaning="短视频脚本写出了卖家未提供的防水、食品级、承重数字或「全网最低」。"
                   "步骤失败，不进入审批；可 retry_step。不外呼平台。"),
        _c("ORCH_OUTREACH_NO_COPY", ErrorKind.VALIDATION, STOP_NO_RETRY, human_todo=True,
           meaning="达人建联步缺少上一步 listing 标题或卖点。不编造文案，不入库待审。"),
        _c("ORCH_OUTREACH_CREATOR", ErrorKind.VALIDATION, STOP_NO_RETRY, human_todo=False,
           meaning="达人类型不是测评、生活、出租屋改造，或 creators 结构非法、重复。缺省为三类各一条，昵称「达人」。"),
        _c("ORCH_OUTREACH_INCOMPLETE", ErrorKind.VALIDATION, STOP_NO_RETRY, human_todo=True,
           meaning="达人建联草稿条数、昵称、条款键、跟进日或固定结尾与输入不一致。不入库，不发送。"),
        _c("ORCH_OUTREACH_UNGROUNDED", ErrorKind.POLICY, POLICY_REJECT, human_todo=True,
           meaning="达人建联草稿写出了输入里没有的佣金比例、粉丝数、保底播放、赠品、免打孔、"
                   "已发送或未提供的规格。步骤失败，不进入待审；可 retry_step。不外呼平台。"),
        _c("ORCH_OUTREACH_COMPLIANCE", ErrorKind.POLICY, POLICY_REJECT, human_todo=True,
           meaning="达人建联草稿命中合规词 block、供给侧泄漏或泰语质检未过。"
                   "步骤失败，不进入待审；不发送。"),
        _c("AD_SPEND_INVALID", ErrorKind.VALIDATION, STOP_NO_RETRY, human_todo=False,
           meaning="广告费记账输入不合法（日期、金额、币种、渠道或来源）。不入库、不外呼、不改预算。"),
        _c("ADS_DATE_INVALID", ErrorKind.VALIDATION, STOP_NO_RETRY, human_todo=False,
           meaning="广告拉取的日期格式非法（须 YYYY-MM-DD）。走 AdapterResult 返回，不裸抛；只读，不外呼。"),
        _c("ADS_DATE_RANGE_INVERTED", ErrorKind.VALIDATION, STOP_NO_RETRY, human_todo=False,
           meaning="广告拉取的 date_from 晚于 date_to。刻意不返回空结果 —— 静默成功会让调用方"
                   "分不清「区间内没数据」（正常）与「参数写反了」（bug）。"),
        _c("ADS_LIVE_DISABLED", ErrorKind.POLICY, POLICY_REJECT, human_todo=True,
           meaning="TikTok Ads 只读客户端在开关关闭时被直接调用。不发起 HTTP、不改预算。"
                   "工厂在 TIKTOK_ADS_ENABLED 未开时返回 MockTikTokAds。"),
        _c("ADS_CREDENTIALS_MISSING", ErrorKind.VALIDATION, STOP_NO_RETRY, human_todo=True,
           meaning="TIKTOK_ADS_ENABLED 已开，但缺少 Access-Token 或广告主 ID。不外呼，不回退 mock，不改预算。"),
        _c("ADS_ENDPOINT_INVALID", ErrorKind.VALIDATION, STOP_NO_RETRY, human_todo=True,
           meaning="TIKTOK_ADS_API_BASE 不是官方 https://business-api.tiktok.com/open_api/v1.3。未发请求。"),
        _c("ADS_DATE_RANGE_TOO_LONG", ErrorKind.VALIDATION, STOP_NO_RETRY, human_todo=False,
           meaning="按天同步报表超过官方 30 天上限。未发请求，不改预算。"),
        _c("ADS_CURRENCY_UNKNOWN", ErrorKind.VALIDATION, STOP_NO_RETRY, human_todo=True,
           meaning="广告主信息没有可用的 currency。不假定 THB，不拉花费，不改预算。"),
        _c("ADS_CURRENCY_UNSUPPORTED", ErrorKind.POLICY, POLICY_REJECT, human_todo=True,
           meaning="广告主或报表行币种不是 THB。不换汇、不入库、不改预算。"),
        _c("ADS_HTTP_ERROR", ErrorKind.EXECUTION, STOP_NO_RETRY, human_todo=True,
           meaning="TikTok Ads 只读 GET 超时、传输失败、非 200、重定向或响应过大。不跟随重定向，不改预算。"),
        _c("ADS_AUTH_FAILED", ErrorKind.EXECUTION, STOP_NO_RETRY, human_todo=True,
           meaning="TikTok Ads 只读 GET 返回 401 或 403。不改预算。错误信息不含 Access-Token。"),
        _c("ADS_API_ERROR", ErrorKind.EXECUTION, STOP_NO_RETRY, human_todo=True,
           meaning="TikTok Ads 返回非 0 code、非 JSON，或报表结构不能安全记账。不把缺字段当成零花费，不改预算。"),
        _c("ADS_SPEND_UNPARSEABLE", ErrorKind.VALIDATION, STOP_NO_RETRY, human_todo=False,
           meaning="报表 spend 或展示/点击/转化无法解析，或缺少 stat_time_day。该次拉取不入库，不改预算。"),
        _c("ADS_REPORT_TRUNCATED", ErrorKind.EXECUTION, STOP_NO_RETRY, human_todo=True,
           meaning="同步报表页数超过只读客户端上限。拒绝只记一部分，不改预算。"),
        _c("ADS_WRITE_REFUSED", ErrorKind.POLICY, POLICY_REJECT, human_todo=True,
           meaning="改广告预算、建计划、调出价或调用允许表以外的路径被拒绝。只读花费，不产生花钱动作。"),
        _c("PRICING_COST_REQUIRED", ErrorKind.VALIDATION, STOP_NO_RETRY, human_todo=True,
           meaning="售价公式缺成本 cost_cny（或 ≤0）。不填假成本；须补录 1688 成本或人工录入后再定价。"),
        _c("PRICING_INVALID_INPUT", ErrorKind.VALIDATION, STOP_NO_RETRY, human_todo=False,
           meaning="售价公式输入/占位费率不合法（负费率、fx≤0、合计≥100% 等）。改配置或入参后重试。"),
        _c("PRICING_DEVIATION", ErrorKind.POLICY, POLICY_REJECT, human_todo=True,
           meaning="LLM/覆盖价相对公式建议价偏离超过 PRICING_DEVIATION_PCT。编排器挂起，须审批 pricing_override。"),
        _c("PROFIT_DATE_INVALID", ErrorKind.VALIDATION, STOP_NO_RETRY, human_todo=False,
           meaning="利润看板 from/to 日期非法（须 YYYY-MM-DD）或 from>to。只读查询不写库；改正参数后重试。"),
        _c("AGENT_STATS_DATE_INVALID", ErrorKind.VALIDATION, STOP_NO_RETRY, human_todo=False,
           meaning="各 agent 产出统计 from/to 日期非法（须 YYYY-MM-DD）或 from>to。只读查询不写库；改正参数后重试。"),
        _c("TTS_HTTP_ERROR", ErrorKind.EXECUTION, STOP_NO_RETRY, human_todo=True,
           meaning="TikTok Shop HTTP 传输失败。执行类立即停，不自动重试。"),
        _c("TTS_API_ERROR", ErrorKind.EXECUTION, STOP_NO_RETRY, human_todo=True,
           meaning="TikTok Shop 返回非成功 code。执行类立即停，不把错误当成空订单。"),
        _c("TTS_RATE_LIMITED", ErrorKind.EXECUTION, STOP_NO_RETRY, human_todo=True,
           meaning="TikTok Shop HTTP 429 或业务码 36009002。客户端已做有限次退避；耗尽后立即停，"
                   "编排器不得再自动重试（防重复上架/下单）。"),
        _c("TTS_TIMESTAMP_INVALID", ErrorKind.EXECUTION, STOP_NO_RETRY, human_todo=True,
           meaning="时间戳超出官方重放窗（不早于当前 5 分钟、不晚于当前 30 秒）或服务端 36009004+timestamp。"
                   "立即停；校正本机时钟后人工重试。"),
        _c("TTS_SIGN_INVALID", ErrorKind.EXECUTION, STOP_NO_RETRY, human_todo=True,
           meaning="签名无效（106001 或 invalid sign/signature）。立即停；核对 app_secret/path/body，不自动重试。"),
        _c("TTS_AUTH_EXPIRED", ErrorKind.EXECUTION, STOP_NO_RETRY, human_todo=True,
           meaning="access_token 已过期（105002）。客户端会尝试刷新一次；仍失败则立即停转人工。"),
        _c("TTS_NO_SHOP", ErrorKind.EXECUTION, STOP_NO_RETRY, human_todo=True,
           meaning="没有 shop_cipher，授权店铺接口也没有给出可用店铺。立即停，不自动重试。"),
        _c("TTS_REFRESH_FAILED", ErrorKind.EXECUTION, STOP_NO_RETRY, human_todo=True,
           meaning="刷新 access token 失败。执行类立即停，错误信息不得包含 app_secret。"),
        _c("TTS_IMAGE_EMPTY", ErrorKind.VALIDATION, STOP_NO_RETRY, human_todo=False,
           meaning="upload_image 收到空图片字节。不发上传请求。"),
        _c("TTS_CATEGORY_REQUIRED", ErrorKind.VALIDATION, STOP_NO_RETRY, human_todo=False,
           meaning="get_category_attributes 缺少 category_id。不发请求。"),
        _c("TTS_PRODUCT_REQUIRED", ErrorKind.VALIDATION, STOP_NO_RETRY, human_todo=False,
           meaning="get_product_audit_status 缺少 product_id。不发请求。"),
        _c("TTS_PAYLOAD_INVALID", ErrorKind.VALIDATION, STOP_NO_RETRY, human_todo=False,
           meaning="create_product 载荷为空或不是对象。不发创建请求。"),
        _c("TTS_TRACKING_REQUIRED", ErrorKind.VALIDATION, STOP_NO_RETRY, human_todo=False,
           meaning="update_tracking 缺少 order_id 或 tracking_no。不发物流写请求。"),
        _c("TTS_SHIPPING_PROVIDER_REQUIRED", ErrorKind.VALIDATION, STOP_NO_RETRY, human_todo=True,
           meaning="update_tracking 缺少 TIKTOK_SHIPPING_PROVIDER_ID 或显式承运商。不发物流写请求。"),
        _c("STOCK_LISTING_REQUIRED", ErrorKind.VALIDATION, STOP_NO_RETRY, human_todo=False,
           meaning="update_stock 缺少 listing_id。不发库存推送。"),
        _c("STOCK_PRODUCT_REQUIRED", ErrorKind.VALIDATION, STOP_NO_RETRY, human_todo=False,
           meaning="TikTok update_stock 缺少 platform_product_id。不发库存推送。"),
        _c("STOCK_QTY_INVALID", ErrorKind.VALIDATION, STOP_NO_RETRY, human_todo=False,
           meaning="update_stock 的 quantity 非整数或为负。不发库存推送。"),
        # ---- 闸门/合规 ----
        _c("GATE_STALE", ErrorKind.POLICY, POLICY_REJECT, human_todo=True,
           meaning="pre-flight 复校验超阈值 → 审批单转 stale 重审（SCHEMA §3.4）。"),
        _c("GATE_PAYLOAD_MISMATCH", ErrorKind.POLICY, POLICY_REJECT, human_todo=True,
           meaning="审批载荷哈希不符或 entity_id 与载荷内业务 id 不一致。直接拒。"),
        _c("COMPLIANCE_TERM_HIT", ErrorKind.POLICY, POLICY_REJECT, human_todo=True,
           meaning="合规词表 severity=block 命中（DESIGN §7.4/§7.5；词表 assets/compliance_lexicon.json）。"
                   "返回命中词与位置；文案不可进入审批通过；不提供「本次跳过」。"
                   "warn 级只写入 QA warnings / advisory_flags，不发本码、不阻断审批。"),
        _c("SUPPLIER_LEAK_HIT", ErrorKind.POLICY, POLICY_REJECT, human_todo=True,
           meaning="买家文案含供给侧事实（MOQ/批发/起订）。是否升格为 §7 硬规则待拍板，当前=转人工。"),
        _c("UNKNOWN", ErrorKind.VALIDATION, STOP_NO_RETRY, human_todo=True,
           meaning="适配器返回失败但未给错误体（兜底，应被消灭）。"),
        _c("VERSION_CAS_EXHAUSTED", ErrorKind.EXECUTION, STOP_NO_RETRY, human_todo=True,
           meaning="listing 版本号乐观锁 CAS 重读重试 3 次仍冲突（SCHEMA §4.5）。"
                   "执行类：重试上限内已按 §4.5 重读再试过，耗尽后**停下转人工**，"
                   "绝不无限重试（§3.3 执行类不自动重试，避免重复下单式双花），"
                   "也不把裸 IntegrityError 抛穿 API 层。"),
    )
}


def spec_for(code: str) -> CodeSpec | None:
    return REGISTRY.get(code)


#: 动态码模式。适配器里用 f-string 拼出的码（`LLM_HTTP_{status}`）无法逐个列举，
#: 但**必须匹配一条已登记的模式**——否则新增分支会静默绕过登记，
#: 编排器拿到没见过的码只能瞎猜处置（最坏：把执行类当生成类重试，变成重复下单）。
DYNAMIC_PATTERNS: tuple[tuple[re.Pattern[str], CodeSpec], ...] = (
    (re.compile(r"^LLM_HTTP_\d{3}$"),
     _c("LLM_HTTP_*", ErrorKind.GENERATION, RETRY, human_todo=False,
        meaning="网关返回任意 HTTP 错误（具体状态码在 message 里）。生成类可重试；已精确登记的 429/503/504 优先匹配。")),
    (re.compile(r"^TT_[A-Z][A-Z_]+$"),
     _c("TT_*", ErrorKind.EXECUTION, STOP_NO_RETRY, human_todo=True,
        meaning="平台侧错误（M2 落地时细化）。执行类一律不自动重试。")),
)


def resolve(code: str) -> CodeSpec | None:
    """精确登记优先，其次动态模式。返回 None = 未登记（CI 该红）。"""
    spec = REGISTRY.get(code)
    if spec is not None:
        return spec
    for pat, dyn in DYNAMIC_PATTERNS:
        if pat.match(code):
            return dyn
    return None


def disposition_of(code: str) -> Disposition | None:
    spec = resolve(code)
    return spec.disposition if spec else None


_DISPOSITION_TEXT = {
    Disposition.RETRY: "重试 ≤2 次，耗尽转人工待办",
    Disposition.DEGRADE_MANUAL_TODO: "降级 → 人工待办",
    Disposition.STOP_NO_RETRY: "立即停，**不自动重试**",
    Disposition.POLICY_REJECT: "策略拒绝（无跳过）",
}


def render_markdown() -> str:
    """由登记表生成文档正文。**唯一事实源是 REGISTRY**。"""
    lines = [
        "# 错误码登记表（由代码生成，勿手改）",
        "",
        "> 生成源：`src/shopkeeper/security/error_codes.py` 的 `REGISTRY`。",
        "> 重新生成：`python scripts/gen_error_codes.py`",
        "> 一致性由 `tests/security/test_error_codes.py` 把守：登记漏项或文档漂移都会让 CI 红。",
        "",
        "处置语义对应 DESIGN §3.3：生成类失败重试 2 次后降级人工待办；"
        "执行类失败立即停、保留现场、不自动重试（防重复下单/重复上架）。",
        "",
        "| 错误码 | kind | 处置 | 转人工 | 含义 |",
        "|--------|------|------|--------|------|",
    ]
    for code in sorted(REGISTRY):
        s = REGISTRY[code]
        lines.append(
            f"| `{s.code}` | {s.kind.value} | {_DISPOSITION_TEXT[s.disposition]} | "
            f"{'✅' if s.human_todo else '—'} | {s.meaning} |"
        )
    lines += [
        "",
        "## 不变式（登记表把守的四条）",
        "",
        "1. `kind=generation` 的码，处置必须是 `retry` 或 `degrade_manual`——不得写成执行类语义。",
        "2. `kind=execution` 的码，处置**必须**是 `stop_no_retry` 或 `policy_reject`：一旦允许重试就是重复下单风险。",
        "3. `kind=policy` 的码必须 `human_todo=True`：闸门拒绝必须留人工痕迹。",
        "4. 处置为 `retry` 的码，`human_todo` 由重试耗尽后的降级路径负责置位，登记处保持 False。",
        "",
    ]
    return "\n".join(lines)


def validate_registry() -> list[str]:
    """登记表自身的不变式检查，返回违规说明列表（空=通过）。"""
    problems: list[str] = []
    for code, s in REGISTRY.items():
        if s.code != code:
            problems.append(f"{code}: 键与 spec.code 不一致")
        if s.kind is ErrorKind.EXECUTION and s.disposition not in {
            Disposition.STOP_NO_RETRY, Disposition.POLICY_REJECT
        }:
            problems.append(f"{code}: 执行类码不得是可重试处置（会重复下单）")
        if s.kind is ErrorKind.POLICY and not s.human_todo:
            problems.append(f"{code}: 策略拒绝必须留人工痕迹")
        if s.kind is ErrorKind.GENERATION and s.disposition is STOP_NO_RETRY:
            problems.append(f"{code}: 生成类却登记为立即停，语义与 DESIGN §3.3 冲突")
    return problems
