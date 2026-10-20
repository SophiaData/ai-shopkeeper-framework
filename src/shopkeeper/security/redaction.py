"""统一脱敏守卫（SCHEMA §4.4：按**数据类型**判定，不按表点名）。

规矩：任何 JSONB/长文本列落库前必须过本管道。管道做三件事——
1. 白名单业务字段透传（entity/status/amount/timestamps/状态码等）；
2. PII 类字段按类型降级：`pii_ref`（指向权威表）/ HMAC 指纹（可对账不可还原）/ `[REDACTED]`；
3. 命中即记账，返回 `GuardReport`，让"这条被改了几个字段"可审计。

**为什么放在写入路径而不是读取路径**：一旦明文 PII 进了库，
事后清洗永远漏掉某一行；读取时过滤又挡不住备份/日志/Delta 拉取（R2 [P0]-3 的原话）。
所以这里默认 **strict**：不传守卫就拒绝落库。
"""

from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

# --------------------------------------------------------------------------
# 字段名维度：这些键的值一律视为 PII，不论内容长什么样。
# 只靠"内容像不像手机号"会漏（姓名、地址不含数字），所以键名优先。
# --------------------------------------------------------------------------
PII_FIELD_NAMES: frozenset[str] = frozenset({
    "phone", "mobile", "tel", "telephone", "msisdn",
    "address", "addr", "address_line1", "address_line2", "subdistrict", "district", "province",
    "recipient", "receiver", "receiver_name", "contact_name", "buyer_name", "consignee",
    "name", "fullname", "full_name", "nickname",
    "email", "mail",
    "id_card", "idcard", "passport", "tax_id", "national_id",
    "bank_account", "card_no", "cardno",
    "latitude", "longitude", "gps",
    "winport_url", "seller_login_id", "sellerloginid",
})

#: 明确允许透传的业务字段（白名单）。不在白名单也不在 PII 名单的，走内容嗅探。
PASSTHROUGH_FIELDS: frozenset[str] = frozenset({
    "id", "order_id", "sku_id", "product_id", "shop_id", "entity", "entity_type", "entity_id",
    "status", "state", "platform_raw_status", "event_type", "code", "error_code",
    "amount", "price", "price_cents", "total_amount", "shipping_fee", "discount", "currency",
    "qty", "quantity", "count", "moq", "stock",
    "created_at", "updated_at", "paid_at", "shipped_at", "delivered_at", "refund_completed_at",
    "platform_update_time", "timestamp", "time",
    "source", "source_platform", "source_item_id", "mode", "lang", "langs",
    "title", "title_raw", "headline", "sub", "bullets", "description",
    "output_path", "images", "main_images", "detail_images",
    "attempts", "duration_ms", "prompt_tokens", "completion_tokens", "total_tokens",
    "cost_amount", "cost_currency", "cost_note", "model", "finish_reason",
    "pii_ref", "address_ref", "blob_ref", "redaction",
})

# --------------------------------------------------------------------------
# 内容维度：值本身像 PII 就降级（防止字段名起得不规范时漏网）
# --------------------------------------------------------------------------
CONTENT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("cn_mobile", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")),
    # 泰国本地手机号：`0X-XXX-XXXX`（10 位、以 0 开头），可选 `-`/空格分隔。
    #
    # ⚠️ **本项目是泰国站，这是主格式**。此前只配了 `cn_mobile`（中国 1[3-9]）
    # 与 `intl_phone`（必须带 `+`），于是泰国买家最常见的 `0812345678`
    # **一条规则都不命中**，而模块 docstring 承诺「避免手机号/地址/邮箱进 stderr」。
    # `(?<![\d])` / `(?!\d)` 防止咬到长数字串的一部分。
    (
        "th_mobile",
        re.compile(r"(?<![\d])0[689]\d(?:[\s-]?\d{3}){2}(?![\d])"),
    ),
    # 泰国固定电话（曼谷 02 + 7 位；外府 0XX + 6 位）
    (
        "th_landline",
        re.compile(r"(?<![\d])0[2-7]\d(?:[\s-]?\d{3,4}){2}(?![\d])"),
    ),
    ("intl_phone", re.compile(r"(?<!\d)\+\d{7,15}(?!\d)")),
    ("email", re.compile(r"[\w.+-]+@[\w-]+\.[\w.+-]+")),
    ("national_id", re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")),
    ("bank_card", re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)")),
    # 门牌/街道类地址：必须有**地址语境**（CJK 门牌字或街道关键词）。
    # 注意：绝不能把裸连字符算进来——`\d+\s*[-号…]` 会命中 ISO 时间戳
    # "2026-10-10T18:00:00+08:00"，那等于把每条业务时间都脱敏掉。
    ("street_address", re.compile(
        r"(\d+\s*[号楼室栋幢单元弄巷]\d*\d"
        r"|(?:路|街|道|巷)\s*\d+\s*号"
        r"|\bJl\.|\bSoi\s+\d|\bRuang\s+\d|\bNo\.\s*\d+\s*,)"
        # 泰国地址：`123/45 Soi ...`、`ถนน...`、`ซอย...`、`หมู่ ...`、`แขวง/เขต`
        r"|\d+\s*/\s*\d+\s+\S"
        r"|(?:ถนน|ซอย|หมู่|ตำบล|อำเภอ|แขวง|เขต|จังหวัด)\s*\S"
    )),
)

def _pattern_by_name(name: str) -> re.Pattern[str]:
    """按名字从 `CONTENT_PATTERNS` 取规则。

    存在的意义：**避免下标耦合**。按下标取时，中间插入一条新规则就会静默取错，
    而错取的后果是脱敏失效（不报错、只是悄悄不脱了）。
    """
    for key, pat in CONTENT_PATTERNS:
        if key == name:
            return pat
    raise KeyError(f"CONTENT_PATTERNS 里没有 {name!r}")


# 营销正文字段：命中 PII 时只做**片段替换**，保住可交付文案。
# 与 PASSTHROUGH_FIELDS 不同——那份是"别按字段名打掉"的业务字段清单，
# 里面大量是 order_id/price/时间戳等非文本字段，不该享受片段待遇。
_COPY_TEXT_FIELDS: frozenset[str] = frozenset(
    {"title", "description", "bullets", "headline", "sub", "body", "text", "content", "copy"}
)

REDACTED = "[REDACTED]"


def redact_spans(value: str) -> tuple[str, str | None]:
    """把**嵌在自由文本里**的 PII 片段替换掉，其余文字原样保留。

    为什么需要它：营销正文（title/description/bullets）既不能整值打掉
    （那是损坏交付物），也不能因为"是文案"就完全跳过
    （§4.4 没给文案字段开豁免，实测 "Contact us at 13812345678" 会明文入库）。

    顺序有讲究：先跑 national_id / bank_card 这类长数字，再跑 cn_mobile，
    否则 18 位身份证的前 11 位会被手机号规则抢先咬掉，留下半截数字。
    """
    out = value
    first_hit: str | None = None
    for name, pat in _SPAN_ORDERED:
        new, n = pat.subn(REDACTED, out)
        if n:
            out = new
            first_hit = first_hit or name
    # 卡号单独一轨：要"分组书写 + 过 Luhn"双条件才算（见 _CARD_GROUPED 说明）。
    card_scrubbed, card_hits = _scrub_card_numbers(out)
    if card_hits:
        out = card_scrubbed
        first_hit = first_hit or "bank_card"
    # 凭据单独一轨（P1-05）：密钥本身没有值特征，只能按 URL query / header 形态认。
    secret_scrubbed, secret_hits = _scrub_secrets(out)
    if secret_hits:
        out = secret_scrubbed
        first_hit = first_hit or "secret"
    return out, first_hit


def _luhn_ok(text: str) -> bool:
    """Luhn 校验。真实卡号按定义必过，随机长数字约 1/10 概率过。"""
    digits = [int(c) for c in text if c.isdigit()]
    if not 13 <= len(digits) <= 19:
        return False
    total = 0
    parity = len(digits) % 2
    for i, d in enumerate(digits):
        if i % 2 == parity:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


# 正文里的卡号判定：**必须 4-4-4-… 分组书写，且过 Luhn**。
# 为什么不直接用"13~19 位数字"那条通用正则：
#   平台订单号 / 物流单号 / 商品型号都是 13~19 位**连续**数字，与卡号同形，
#   无脑打掉会损坏业务数据（test_contiguous_model_number_in_copy_survives 钉着）。
#   而人写卡号时几乎总是分组带空格或连字符的。
# 两个条件同时满足才判卡号：分组形状 + Luhn 校验。
# 字段名本身就是 phone/card_no 的情况由 is_pii_field 整值处理，不走这里。
_CARD_GROUPED = re.compile(r"(?<![\dA-Za-z])(?:\d{4}[ -]){3}\d{3,4}(?![\dA-Za-z])")


def _scrub_card_numbers(value: str) -> tuple[str, int]:
    hits = 0

    def _rep(m: "re.Match[str]") -> str:
        nonlocal hits
        if _luhn_ok(m.group(0)):
            hits += 1
            return REDACTED
        return m.group(0)

    return _CARD_GROUPED.sub(_rep, value), hits


# 自由文本专用：比 CONTENT_PATTERNS 更保守，避免咬坏型号/规格。
# 手机号/身份证的前后 lookbehind 排除紧邻字母数字，避免咬进型号内部
# （"AB1234567890123" 这种型号曾被整段打掉）。
# 卡号不在这里——它需要 Luhn 这层语义判定，走 _scrub_card_numbers 单独一轨。
_SPAN_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("email", re.compile(r"[\w.+-]+@[\w-]+\.[\w.+-]+")),
    ("national_id", re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")),
    ("intl_phone", re.compile(r"(?<!\d)\+\d{7,15}(?!\d)")),
    ("cn_mobile", re.compile(r"(?<![\dA-Za-z])1[3-9]\d{9}(?![\dA-Za-z])")),
    # 泰国手机号 `0X-XXX-XXXX`（10 位）。**本项目是泰国站，这是主格式**，
    # 而此前只配了中国 `cn_mobile` 与必须带 `+` 的 `intl_phone` ——
    # 泰国买家最常见的 `0812345678` 一条都不命中，与模块 docstring 的承诺相悖。
    # 排在 national_id 之后：长数字优先，避免被短规则咬掉一截。
    # 泰国手机号共 **10 位**：`0` + `[689]` + 3 + 4 = 9 位？不对 ——
    # 10 位 = `0X` (2) + `XXX` (3) + `XXXXX` (5)。写 `\d{4}` 会少一位，
    # 末尾的负向断言随即失败，整条规则形同虚设（我第一版就是这么错的）。
    # 两种书写各一条：无分隔 `0XXXXXXXXX`(10 位) 与分组 `0XX-XXX-XXXX`(3+3+4)。
    # 只写一条会在另一种书写上失配 —— 实测 `081-234-5678` 就漏了。
    ("th_mobile_plain", re.compile(r"(?<![\dA-Za-z])0[689]\d{8}(?![\dA-Za-z])")),
    ("th_mobile_grouped", re.compile(r"(?<![\dA-Za-z])0[689]\d[\s-]\d{3}[\s-]\d{4}(?![\dA-Za-z])")),
    # 泰国固定电话：曼谷 `02-XXX-XXXX`，外府 `0XX-XXX-XXX`。
    ("th_landline", re.compile(r"(?<![\dA-Za-z])0[2-7][\s-]?\d{3}[\s-]?\d{3,4}(?![\dA-Za-z])")),
    # ⚠️ **按名字取，不按下标取**。原先写的是 `CONTENT_PATTERNS[5][1]`，
    # 任何人往 CONTENT_PATTERNS 中间插一条规则都会让下标错位 ——
    # 我加泰国手机号规则时就踩到了：`[5]` 从 street_address 变成了 national_id，
    # 于是地址脱敏静默失效（测试 test_task_input_and_output_are_guarded 抓到）。
    ("street_address", _pattern_by_name("street_address")),
)
_SPAN_ORDERED = _SPAN_PATTERNS

# --------------------------------------------------------------------------
# 凭据维度：URL / header 里的密钥形态（P1-05）
# --------------------------------------------------------------------------
# 为什么单独一轨：PII 规则按「值像手机号/邮箱」识别，密钥是任意字符串，值本身
# 没有任何特征。TikTok 的签名请求把 access_token 与 sign 放在 **query string** 里
# （见 adapters/tiktok_shop.py 的 _dispatch_signed），一旦有代码把 URL 写进日志、
# 异常文本或审计，密钥就跟着出去了。字段名维度（guard_payload 的 is_pii_field）
# 也管不到——那里看的是 dict 的 key，而 URL 是一个普通字符串值。
#
# 做法：定位 query 串里的敏感参数名，把它的值替换掉，保留参数名便于排查
# （看得出「这里原本有 token」，但看不到是什么）。
_SECRET_QUERY_KEYS = (
    "access_token",
    "refresh_token",
    "app_secret",
    "app_key",
    "sign",
    "signature",
    "shop_cipher",
    "api_key",
    "apikey",
    "token",
    "secret",
    "password",
    "authorization",
)
_SECRET_QUERY_ALT = "|".join(re.escape(k) for k in _SECRET_QUERY_KEYS)
# 匹配 `key=value`（value 到下一个 & 或空白/引号为止），保留 key 与前缀符号。
#
# ⚠️ **前缀必须是可选的**。原先写死 `[?&\s]`，于是**整段就是一个 `key=value`** 时
# 匹配不上：`redact_spans('access_token=SECRET')` 原样返回。
# 这不是边角情况 —— 异常文本、平台回显的错误体、`%s` 拼接的日志
# 都长成「裸 key=value」的样子，而那条路径**没有字段名可依赖**
# （dict 的 key 由 `is_pii_field` 管，一旦拍平成字符串就只剩这一层）。
# 现在允许行首/引号/花括号/逗号等分隔，且 `=` 与 `:` 都认（JSON 形态）。
_SECRET_QUERY_PATTERN = re.compile(
    rf"(?P<prefix>(?:^|[?&\s\"'{{,]|\\)\s*\"?)"
    rf"(?P<key>{_SECRET_QUERY_ALT})"
    rf"(?P<closekey>\"?)(?P<sep>\s*[:=]\s*)"
    # value 允许被引号包起来（JSON / Python dict 形态）。
    # 引号分两组（开/闭）分别捕获：用反向引用 `(?P=quote)` 会把闭引号也吃掉，
    # 从 `{"access_token":"X"}` 变成 `{"access_token:"[REDACTED]"}` —— 结构被改坏。
    rf"(?P<quote>[\"']?)(?P<value>[^&\s\"'<>]+)(?P<endquote>[\"']?)",
    re.IGNORECASE | re.MULTILINE,
)
# header 形态：`x-tts-access-token: tok_xxx` 或 `Authorization: Bearer xxx`
# ⚠️ **必须吃掉认证方案前缀**。原先 value 类是 `[^\s\"'<>]+`（到第一个空白为止），
# 于是 `Authorization: Bearer <token>` 只打掉 `Bearer`，**token 原样留下**。
# 而 `Bearer` 恰恰是最常见的形态（providers.py / orchestrator/llm.py 都发这个头）。
# 现有测试用的是 `sk-...` 样本，被 `_SECRET_LITERAL_PATTERN` 兜住了 ——
# 于是这条规则看起来是好的，实际对真实 JWT / ROW_ / t- 开头的 token 全都漏。
_SECRET_HEADER_PATTERN = re.compile(
    r"(?P<prefix>(?:^|\s)(?:authorization|x-[\w-]*(?:token|secret|key)[\w-]*)\s*:\s*)"
    r"(?:(?:Bearer|Basic|Digest|Token|OAuth)\s+)?"
    r"(?P<value>[^\s\"'<>]+)",
    re.IGNORECASE | re.MULTILINE,
)
# 常见密钥字面量：sk-xxx / LTAIxxx（与 check_desktop_scaffold 的形态一致，但这里是通用防线）
_SECRET_LITERAL_PATTERN = re.compile(r"\b(?:sk-[A-Za-z0-9]{16,}|LTAI[0-9A-Za-z]{12,})\b")


def _scrub_secrets(text: str) -> tuple[str, int]:
    """把文本里的密钥形态替换为 ``[REDACTED]``。返回 (新文本, 命中数)。"""
    if not text:
        return text, 0
    hits = 0

    def _q(match: re.Match[str]) -> str:
        """保留原有结构（分隔符与引号），只把 value 换掉。

        写死成 `key=[REDACTED]` 会把 JSON 的引号与冒号吃掉，
        从 `{"access_token": "X"}` 变成 `{"access_token=[REDACTED]}` ——
        **改坏了原文结构**，让下游解析失败或日志难以阅读。
        """
        nonlocal hits
        hits += 1
        return (
            f"{match.group('prefix')}{match.group('key')}{match.group('closekey')}"
            f"{match.group('sep')}{match.group('quote')}"
            f"{REDACTED}{match.group('endquote')}"
        )

    def _h(match: re.Match[str]) -> str:
        nonlocal hits
        hits += 1
        # prefix 已含 `key: ` 与方案名（如 `Authorization: `），直接接 [REDACTED]。
        return f"{match.group('prefix')}{REDACTED}"

    def _l(match: re.Match[str]) -> str:
        nonlocal hits
        hits += 1
        return REDACTED

    # ⚠️ **顺序有讲究：header 先于 query**。
    #
    # `authorization` 同时在两张表的 key 里，而 query 规则的 value 类
    # `[^&\s"'<>]+` 到**第一个空白**为止 —— 于是它会把
    # `Authorization: Bearer <token>` 只吃到 `Bearer`，把真正的 token 留下，
    # 且吃掉之后 header 规则再也匹配不上（前缀已被替换）。
    # 先跑 header 规则，它认识 `Bearer`/`Basic` 等方案名，能连 token 一起打掉。
    out = _SECRET_HEADER_PATTERN.sub(_h, text)
    out = _SECRET_QUERY_PATTERN.sub(_q, out)
    out = _SECRET_LITERAL_PATTERN.sub(_l, out)
    return out, hits


@dataclass
class GuardReport:
    """一次守卫处理的账目。落库时随数据留痕，便于事后清点。"""

    redacted: list[str] = field(default_factory=list)        # 被整体替换的字段路径
    hashed: list[str] = field(default_factory=list)          # 被换成 HMAC 指纹的字段路径
    ref_replaced: list[str] = field(default_factory=list)    # 被换成 pii_ref 的字段路径
    # 只替掉片段、正文其余保留的路径。单独记账：它不算"整字段被吞"，
    # 但必须看得见，否则文案里出现过手机号这件事会在报告里彻底隐身。
    span_redacted: list[str] = field(default_factory=list)
    sniffed: dict[str, str] = field(default_factory=dict)    # 路径 -> 命中的内容类型

    @property
    def touched(self) -> int:
        return (
            len(self.redacted)
            + len(self.hashed)
            + len(self.ref_replaced)
            + len(self.span_redacted)
        )

    @property
    def clean(self) -> bool:
        return self.touched == 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "redacted": self.redacted,
            "hashed": self.hashed,
            "ref_replaced": self.ref_replaced,
            "span_redacted": self.span_redacted,
            "sniffed": self.sniffed,
            "touched": self.touched,
        }


def hmac_fingerprint(value: Any, *, field_name: str, row_key: str, secret: bytes) -> str:
    """HMAC 指纹：可对账、不可还原（SCHEMA §4.4 第 1/4 类）。

    域分离 = `{row_key}||'|'||{field_name}`，与 §4.4 的 AAD 绑定同构：
    同一手机号写在两个字段/两行上会得到不同指纹，防止跨行串联比对。
    """
    mac = hmac.new(secret, f"{row_key}|{field_name}".encode("utf-8"), hashlib.sha256)
    mac.update(str(value).encode("utf-8"))
    return f"hmac-sha256:{mac.hexdigest()[:32]}"


def pii_ref(row_key: str, field_name: str) -> str:
    """指向权威表的引用占位（真实 PII 只存在 order_addresses / 加密 blob 里）。"""
    return f"pii_ref:{row_key}:{field_name}"


def _norm_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", key.lower())


#: 任一**词元**命中即判 PII（词元级，不是子串级）
#: `key` 单独太宽（foreign_key / sort_key 都是业务字段），必须与限定词同现才算凭据。
_KEY_QUALIFIERS: frozenset[str] = frozenset({"api", "secret", "access", "private", "app", "auth"})

_PII_TOKENS: frozenset[str] = frozenset({
    "phone", "mobile", "tel", "telephone", "msisdn", "fax",
    "address", "addr", "subdistrict", "district", "province",
    "recipient", "receiver", "consignee", "contact",
    "email", "mail",
    "passport", "taxid", "nationalid", "idcard",
    "bank", "cardno", "card",
    "latitude", "longitude", "gps",
    "winport", "login", "loginid", "nickname", "fullname",
    "evidence",
    # ---- 凭据（P1-05）。这些不是"个人数据"，但同样是绝不能进日志/审计的东西。
    # 走同一套字段名机制，而不是另起分支：`is_pii_field` 已经按**词元**判定，
    # 不会把 product_token 之类误伤成 PII（同 sign 的教训见函数 docstring）。
    "token", "secret", "sign", "signature", "credential", "apikey", "cipher",
    "password", "passwd",
})

#: `name` 单独太宽（product_name / shop_name / file_name 都是业务数据），
#: 必须与人名限定词同现才算 PII。
_NAME_QUALIFIERS: frozenset[str] = frozenset({
    "buyer", "receiver", "contact", "recipient", "consignee", "seller",
    "nick", "full", "first", "last", "real", "shopper", "customer", "user", "staff",
})

#: 整键即人名
_BARE_NAME_KEYS: frozenset[str] = frozenset({"name", "fullname", "nickname", "realname"})

_TOKEN_SPLIT = re.compile(r"[^A-Za-z0-9]+|(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def _tokens(key: str) -> list[str]:
    """把 buyerPhone / shipping_address / receiver_name 统一切成词元。"""
    return [t.lower() for t in _TOKEN_SPLIT.split(key or "") if t]


def is_pii_field(key: str) -> bool:
    """按**词元**判定字段名，绝不用裸子串。

    这里踩过一个真实且隐蔽的坑：旧实现用 `_norm_key(p) in k` 子串匹配，
    而词表里有 `name`（4 字符，过了长度下限），于是
    `product_name` / `shop_name` / `file_name` 全被判成 PII 并被替换成 `[REDACTED]`。
    误杀比漏杀更危险：数据被静默改掉，下游只看到"字段没了"，没人知道是守卫干的。
    """
    toks = _tokens(key)
    if not toks:
        return False
    joined = "".join(toks)
    if joined in _BARE_NAME_KEYS:
        return True
    if any(t in _PII_TOKENS for t in toks):
        return True
    if "name" in toks and any(t in _NAME_QUALIFIERS for t in toks):
        return True
    # `api_key` 切成 ['api','key']。这里刻意 **不加** 裸 `key` 词元——
    # `foreign_key` / `sort_key` / `hotkey` 都是正常业务字段，加了会误杀。
    if "key" in toks and any(t in _KEY_QUALIFIERS for t in toks):
        return True
    return False


def sniff_content(value: str) -> str | None:
    for label, pat in CONTENT_PATTERNS:
        if not pat.search(value):
            continue
        # 纯数字串（可含连字符/空格）不当银行卡看：平台订单号就是 15~19 位纯数字。
        # 真卡号出现在**散文里**（"卡号 6222… 请核对"）仍然会命中，因为那时 value 不是纯数字。
        if label == "bank_card" and re.fullmatch(r"[\d\- ]+", value or ""):
            continue
        return label
    return None


def guard_payload(
    payload: Any,
    *,
    row_key: str = "",
    secret: bytes | None = None,
    ref_fields: Iterable[str] = (),
    max_depth: int = 6,
    sniff: bool = True,
    report: GuardReport | None = None,
    path: str = "$",
) -> tuple[Any, GuardReport]:
    """递归守卫。**返回新对象，不就地改入参**（调用方常拿同一份 dict 做别的事）。

    secret 缺省时不做 HMAC，退化为 `[REDACTED]`——宁可少给信息，不可造一个无法核对的假指纹。

    `sniff=False` 关掉**内容嗅探**，只按字段名判 PII。
    给商品文案用：营销文本里的长数字串会被银行卡号正则误伤，那是损坏交付物。
    """
    rep = report or GuardReport()
    if max_depth < 0:
        return REDACTED, rep
    ref_set = {_norm_key(x) for x in ref_fields}

    if isinstance(payload, dict):
        out: dict[str, Any] = {}
        for key, value in payload.items():
            child_path = f"{path}.{key}"
            nk = _norm_key(str(key))
            if isinstance(value, (dict, list, tuple)):
                out[key], rep = guard_payload(
                    value, row_key=row_key, secret=secret, ref_fields=ref_set,
                    max_depth=max_depth - 1, sniff=sniff, report=rep, path=child_path,
                )
                continue
            if is_pii_field(str(key)):
                if value is None or value == "":
                    out[key] = value
                    continue
                if nk in ref_set:
                    out[key] = pii_ref(row_key or child_path, str(key))
                    rep.ref_replaced.append(child_path)
                elif secret is not None:
                    out[key] = hmac_fingerprint(value, field_name=str(key), row_key=row_key, secret=secret)
                    rep.hashed.append(child_path)
                else:
                    out[key] = REDACTED
                    rep.redacted.append(child_path)
                rep.sniffed.setdefault(child_path, "field_name")
                continue
            # 字符串分两类处理，这个区分是 C3 就定下的、被测试钉着的，别合并掉：
            # ① 营销正文（title/description/bullets…）→ **片段替换**：
            #    整值打掉等于把交付物吞了；完全跳过又会漏掉嵌在正文里的手机号
            #    （实测 "Contact us at 13812345678" 明文入库）。
            # ② 其它字段（note/remark/自由备注…）→ **整值打掉**，保守优先：
            #    这类字段没有"必须原样交付"的要求，留半句反而可能带出上下文
            #    （test_phone_never_retrievable_from_tool_calls 钉的就是这个）。
            if isinstance(value, str) and sniff:
                scrubbed, hit = redact_spans(value)
                if hit:
                    is_copy = nk in _COPY_TEXT_FIELDS
                    if is_copy and scrubbed != REDACTED:
                        out[key] = scrubbed
                        rep.span_redacted.append(child_path)
                    elif secret is not None:
                        out[key] = hmac_fingerprint(value, field_name=str(key), row_key=row_key, secret=secret)
                        rep.hashed.append(child_path)
                    else:
                        out[key] = REDACTED
                        rep.redacted.append(child_path)
                    rep.sniffed[child_path] = f"content:{hit}"
                    continue
            out[key] = value
        return out, rep

    if isinstance(payload, (list, tuple)):
        arr: list[Any] = []
        for idx, item in enumerate(payload):
            if isinstance(item, (dict, list, tuple)):
                # 必须把 sniff 传下去：旧写法漏了它，嵌套数组里的 sniff=False 请求
                # 会被静默忽略（反之亦然），守卫语义随嵌套深度漂移。
                conv, rep = guard_payload(
                    item, row_key=row_key, secret=secret, ref_fields=ref_set,
                    max_depth=max_depth - 1, sniff=sniff, report=rep, path=f"{path}[{idx}]",
                )
                arr.append(conv)
            elif isinstance(item, str) and sniff:
                # 列表元素没有字段名可判，一律片段替换：
                # 旧写法整条替换，于是 bullets 里 "call 13800138000"
                # 变成光秃秃一个 [REDACTED]，整条卖点没了（损坏交付物）。
                scrubbed, hit = redact_spans(item)
                if hit:
                    arr.append(scrubbed)
                    if scrubbed == REDACTED:
                        rep.redacted.append(f"{path}[{idx}]")
                    else:
                        rep.span_redacted.append(f"{path}[{idx}]")
                    rep.sniffed[f"{path}[{idx}]"] = f"content:{hit}"
                else:
                    arr.append(item)
            else:
                arr.append(item)
        return arr, rep

    if isinstance(payload, str):
        hit = sniff_content(payload)
        if hit:
            rep.redacted.append(path)
            rep.sniffed[path] = f"content:{hit}"
            return REDACTED, rep
    return payload, rep


def make_guard(
    row_key: str,
    *,
    secret: bytes | None = None,
    ref_fields: Iterable[str] = (),
) -> Callable[[dict[str, Any] | None], dict[str, Any] | None]:
    """给 `repo.record_tool_call(redact=...)` 用的守卫工厂（B2 留的钩子位在此落地）。"""
    refs = list(ref_fields)

    def _guard(payload: dict[str, Any] | None) -> dict[str, Any] | None:
        if payload is None:
            return None
        cleaned, rep = guard_payload(payload, row_key=row_key, secret=secret, ref_fields=refs)
        if not rep.clean:
            # 留痕本身也要可审计：记"动了哪些字段"，不记原值
            cleaned = dict(cleaned) if isinstance(cleaned, dict) else {"value": cleaned}
            cleaned["redaction"] = rep.as_dict()
        return cleaned

    return _guard
