"""C3-1：统一脱敏守卫（SCHEMA §4.4）。

核心断言不是"函数返回了什么"，而是
**手机号/地址写进 tool_calls 之后，从库里查不出原文**——
这是 §4.4 存在的唯一理由，也是这条守卫是否真的在工作的判据。
"""

from __future__ import annotations

import re

import pytest
from sqlalchemy import select

from shopkeeper.db.engine import init_db, make_engine, make_session
from shopkeeper.db.models import M1_TABLES, Shop, Task, ToolCall
from shopkeeper.db.repo import (
    add_listing_version,
    create_listing,
    finish_task,
    guard_and_record,
    record_tool_call,
    submit_task,
    upsert_product,
)
from shopkeeper.security.redaction import (
    REDACTED,
    GuardReport,
    guard_payload,
    hmac_fingerprint,
    is_pii_field,
    make_guard,
    pii_ref,
    sniff_content,
)

PHONE = "13812345678"
ADDR = "广东省深圳市南山区科技园南路12号3栋501"
EMAIL = "buyer@example.com"


@pytest.fixture()
def session():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    assert set(init_db(engine)) == set(M1_TABLES)
    s = make_session(engine)
    s.add(Shop(id="shop_1", platform="tiktok", site="SEA", currency="THB"))
    s.commit()
    yield s
    s.close()


# ---------------------------------------------------------------- 字段名判定

@pytest.mark.parametrize(
    "key",
    ["phone", "mobile", "recipient", "receiver_name", "shipping_address", "buyerPhone",
     "address_line1", "email", "id_card", "bank_account", "sellerLoginId", "winport_url"],
)
def test_pii_field_names_recognised(key):
    assert is_pii_field(key), f"{key} 应判为 PII"


@pytest.mark.parametrize("key", ["order_id", "status", "amount", "created_at", "title", "sku_id", "lang"])
def test_business_fields_not_pii(key):
    assert not is_pii_field(key), f"{key} 是业务字段，不该被吞"


@pytest.mark.parametrize(
    "key",
    [
        "product_name", "shop_name", "category_name", "file_name", "filename",
        "table_name", "brand_name", "display_name_of_product", "naming",
    ],
)
def test_business_name_fields_are_not_pii(key):
    """守卫曾按子串匹配把 `name` 当 PII，于是 product_name / shop_name 被静默替换成
    [REDACTED]。误杀比漏杀更难发现：下游只看到"字段没了"，查不出是守卫干的。"""
    assert not is_pii_field(key), f"{key} 被误判为 PII"


@pytest.mark.parametrize(
    "key",
    ["name", "full_name", "fullname", "buyer_name", "receiver_name", "contact_name",
     "seller_name", "nick_name", "first_name", "last_name", "user_name", "recipient"],
)
def test_person_name_fields_are_pii(key):
    assert is_pii_field(key), f"{key} 是人名/收件人类字段，应判 PII"


@pytest.mark.parametrize("key", ["recipientPhone", "buyerPhone", "shipping_address", "receiver_name", "msisdn"])
def test_camel_and_snake_compounds_recognised(key):
    assert is_pii_field(key), key


def test_business_payload_passes_through_untouched():
    """典型商品/订单载荷：一个字段都不该被动。"""
    payload = {
        "product_name": "迷你风扇",
        "shop_name": "某某旗舰店",
        "file_name": "a.jpg",
        "order_id": "5745812345678901234",
        "amount": 3990,
        "paid_at": "2026-10-10T18:00:00+08:00",
        "items": [{"sku_name": "S1", "qty": 2}],
    }
    cleaned, rep = guard_payload(payload, row_key="tool_calls:t")
    assert rep.touched == 0, f"业务载荷被误伤: {rep.as_dict()}"
    assert cleaned == payload


@pytest.mark.parametrize(
    "value,label",
    [
        (PHONE, "cn_mobile"),
        ("+66812345678", "intl_phone"),
        (EMAIL, "email"),
        ("11010119900307123X", "national_id"),
        ("科技园南路12号3栋501", "street_address"),
    ],
)
def test_content_sniffing_catches_pii(value, label):
    assert sniff_content(value) == label


def test_sniffing_does_not_eat_ordinary_numbers():
    """过度脱敏 = 损坏业务数据，比漏脱敏更难发现。这几个都是真实会出现的形态。"""
    for v in [
        "1990", "3990", "500ml", "EN",
        "2026-10-10T18:00:00+08:00",   # ISO 时间戳（裸连字符曾被当门牌号）
        "2026-10-10",
        "5745812345678901234",         # TikTok 订单号：15~19 位纯数字
        "6222021234567890123",         # 纯数字串不当卡号看（平台单号同形）
    ]:
        assert sniff_content(v) is None, f"{v} 被误判"


def test_card_in_prose_is_still_caught():
    """纯数字放过，但卡号出现在散文里必须抓到。"""
    assert sniff_content("我的卡号 6222021234567890123 请核对") == "bank_card"


def test_business_fields_survive_guard_end_to_end(session):
    """白名单业务字段（order_id / 长数字单号）不得被内容嗅探打掉。"""
    record_tool_call(
        session,
        adapter="platform",
        method="get_order",
        request={"order_id": "5745812345678901234", "shop_id": "s1"},
        response={"order_id": "5745812345678901234", "paid_at": "2026-10-10T18:00:00+08:00", "amount": 3990},
        status="ok",
    )
    session.commit()
    row = session.scalars(select(ToolCall)).one()
    assert row.request["order_id"] == "5745812345678901234"
    assert row.response["order_id"] == "5745812345678901234"
    assert row.response["paid_at"] == "2026-10-10T18:00:00+08:00"
    assert row.response["amount"] == 3990


def test_real_address_still_caught_after_regex_tightening():
    """收紧门牌正则后，真实中文地址必须仍然命中（别把防护改成摆设）。"""
    assert sniff_content(ADDR) == "street_address"
    assert sniff_content("科技园南路12号3栋501") == "street_address"
    assert sniff_content("No.12, Jl. Sudirman, Jakarta") is not None


# ---------------------------------------------------------------- 守卫行为

def test_guard_replaces_pii_by_field_name():
    payload = {"order_id": "o1", "recipient": "张三", "phone": PHONE, "amount": 1990}
    cleaned, rep = guard_payload(payload, row_key="tool_calls:t1")
    assert cleaned["phone"] == REDACTED
    assert cleaned["recipient"] == REDACTED
    assert cleaned["order_id"] == "o1" and cleaned["amount"] == 1990
    assert rep.touched == 2


def test_guard_does_not_mutate_input():
    payload = {"phone": PHONE}
    original = dict(payload)
    guard_payload(payload, row_key="t")
    assert payload == original, "守卫必须返回新对象，不能就地改调用方的 dict"


def test_guard_recurses_into_nested_and_arrays():
    payload = {
        "order": {"buyer": {"phone": PHONE, "id": "b1"}},
        "shipments": [{"tracking": "SF1", "address": ADDR}],
    }
    cleaned, rep = guard_payload(payload, row_key="tool_calls:t2")
    assert cleaned["order"]["buyer"]["phone"] == REDACTED
    assert cleaned["order"]["buyer"]["id"] == "b1"
    assert cleaned["shipments"][0]["address"] == REDACTED
    assert cleaned["shipments"][0]["tracking"] == "SF1"
    assert rep.touched >= 2


def test_sniffed_content_in_business_field_is_still_caught():
    """字段名不起眼（note），但值本身是手机号 → 仍要拦。"""
    cleaned, rep = guard_payload({"note": f"联系 {PHONE}"}, row_key="t")
    assert cleaned["note"] == REDACTED
    assert rep.sniffed["$.note"].startswith("content:")


def test_sniff_off_preserves_marketing_copy():
    """商品文案关内容嗅探：长数字串不该被当卡号打掉。"""
    payload = {"description": "Model 1234567890123456 in stock", "phone": PHONE}
    cleaned, _ = guard_payload(payload, row_key="lv:1", sniff=False)
    assert "1234567890123456" in cleaned["description"], "文案被损坏"
    assert cleaned["phone"] == REDACTED, "字段名规则仍须生效"


def test_hmac_mode_is_deterministic_and_one_way():
    key = b"k" * 32
    a = hmac_fingerprint(PHONE, field_name="phone", row_key="tool_calls:t1", secret=key)
    b = hmac_fingerprint(PHONE, field_name="phone", row_key="tool_calls:t1", secret=key)
    assert a == b, "同一行同字段必须可核对"
    assert PHONE not in a and a.startswith("hmac-sha256:")


def test_hmac_domain_separation_blocks_cross_row_linking():
    """同一手机号进不同行/不同字段 → 指纹必须不同，否则指纹变成可串联的身份证。"""
    key = b"k" * 32
    a = hmac_fingerprint(PHONE, field_name="phone", row_key="tool_calls:t1", secret=key)
    b = hmac_fingerprint(PHONE, field_name="phone", row_key="tool_calls:t2", secret=key)
    c = hmac_fingerprint(PHONE, field_name="recipient_phone", row_key="tool_calls:t1", secret=key)
    assert len({a, b, c}) == 3


def test_ref_fields_become_pii_ref_pointers():
    payload = {"address": ADDR}
    cleaned, rep = guard_payload(payload, row_key="orders:o9", ref_fields=["address"])
    assert cleaned["address"] == pii_ref("orders:o9", "address")
    assert rep.ref_replaced == ["$.address"]


def test_max_depth_stops_runaway_nesting():
    deep: dict = {"phone": PHONE}
    for _ in range(12):
        deep = {"child": deep}
    cleaned, _rep = guard_payload(deep, row_key="t", max_depth=3)
    assert cleaned is not None


# ---------------------------------------------------------------- 落库后查不出原文（C3 完成条件 1 的硬判据）

def test_phone_never_retrievable_from_tool_calls(session):
    task, _ = submit_task(session, agent="scout", type="parse", input={"url": "u"}, idempotency_key="c3-1")
    session.commit()
    record_tool_call(
        session,
        adapter="platform",
        method="get_order",
        task_id=task.id,
        request={"order_id": "o1"},
        response={
            "order_id": "o1",
            "status": "paid",
            "recipient": "王大锤",
            "phone": PHONE,
            "address": ADDR,
            "note": f"回拨 {PHONE}",
            "amount": 3990,
        },
    )
    session.commit()

    row = session.scalars(select(ToolCall)).one()
    stored = session.execute(
        select(ToolCall.response).where(ToolCall.id == row.id)
    ).scalar_one()
    raw_text = str(stored)

    assert PHONE not in raw_text, "手机号原文进了库"
    assert ADDR not in raw_text, "地址原文进了库"
    assert "王大锤" not in raw_text and "回拨" not in raw_text
    # 业务字段必须还在：脱敏不能把有用的数据一起打掉
    assert stored["order_id"] == "o1" and stored["status"] == "paid" and stored["amount"] == 3990
    assert stored["redaction"]["touched"] >= 4, "留痕必须记下动了哪些字段"


def test_default_guard_applies_without_caller_hook(session):
    """不传 redact 也必须脱敏——把守卫做成可选钩子等于没有守卫。"""
    call = record_tool_call(
        session, adapter="a", method="m", request={"phone": PHONE, "id": "x1"}
    )
    session.commit()
    stored = session.scalars(select(ToolCall)).one()
    assert stored.request["phone"] == REDACTED
    assert stored.request["id"] == "x1"
    assert call.id is not None


def test_explicit_hook_still_wins(session):
    """调用方自带守卫时（例如接了 HMAC 密钥），默认守卫不得覆盖它。"""
    def my_guard(payload):
        return {k: "MASKED" for k in payload}

    record_tool_call(session, adapter="a", method="m", request={"phone": PHONE}, redact=my_guard)
    session.commit()
    stored = session.scalars(select(ToolCall)).one()
    assert stored.request["phone"] == "MASKED"


def test_task_input_and_output_are_guarded(session):
    task, _ = submit_task(
        session, agent="care", type="read_msg",
        input={"message": f"我的地址是{ADDR}", "order_id": "o7"}, idempotency_key="c3-io",
    )
    finish_task(session, task, status="succeeded", output={"reply": "好的", "buyer_phone": PHONE})
    session.commit()

    row = session.get(Task, task.id)
    assert ADDR not in str(row.input) and PHONE not in str(row.output)
    assert row.input["order_id"] == "o7" and row.output["reply"] == "好的"


def test_listing_copy_keeps_marketing_text(session):
    """listing 文案走 sniff=False：数字型号不能被损坏。"""
    product, _ = upsert_product(session, {
        "source_platform": "1688", "source_item_id": "p1", "source_url": "u", "title_raw": "t",
    })
    listing = create_listing(session, shop_id="shop_1", product_id=product.id, price=100, currency="THB")
    add_listing_version(session, listing.id, {
        "versions": [{"lang": "EN", "title": "Model 1234567890123456 fan", "bullets": ["a", "b", "c"]}],
    })
    session.commit()
    lv = session.scalars(select(__import__("shopkeeper.db.models", fromlist=["ListingVersion"]).ListingVersion)).one()
    assert "1234567890123456" in str(lv.payload), "商品型号被脱敏损坏"


def test_guard_report_has_no_plaintext_leak():
    """脱敏报告本身不能成为泄漏点：只记路径与类型，不记原值。"""
    cleaned, rep = guard_payload({"phone": PHONE, "address": ADDR}, row_key="t")
    blob = str(rep.as_dict())
    assert PHONE not in blob and ADDR not in blob
    assert isinstance(rep, GuardReport)


def test_make_guard_attaches_report():
    guard = make_guard("tool_calls:t9")
    out = guard({"recipient": "李四", "order_id": "o"})
    assert out["recipient"] == REDACTED and out["order_id"] == "o"
    assert out["redaction"]["touched"] == 1


def test_guard_and_record_handles_none():
    assert guard_and_record(None, row_key="t") is None


# --------------------------------------------------------------------------
# 泰国 PII（本项目是**泰国站**，而规则只配了中国格式）
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "0812345678",
        "081-234-5678",
        "081 234 5678",
        "0661234567",
        "0861234567",
        "0912345678",
    ],
)
def test_thai_mobile_is_redacted(text):
    """泰国手机号必须脱敏。

    原先只配了 `cn_mobile`（中国 `1[3-9]` 开头）与 `intl_phone`（**必须带 `+`**），
    于是泰国买家最常见的 `0812345678` **一条规则都不命中** ——
    而模块 docstring 明写「避免手机号/地址/邮箱进 stderr、webhook 或飞书文本」。
    """
    from shopkeeper.security.redaction import REDACTED, redact_spans

    out, hit = redact_spans(text)
    assert text not in out, f"{text!r} 未脱敏"
    assert REDACTED in out
    assert hit, "应记录命中的规则名"


@pytest.mark.parametrize(
    "text",
    ["021234567", "02-123-4567", "053123456"],
)
def test_thai_landline_is_redacted(text):
    from shopkeeper.security.redaction import REDACTED, redact_spans

    out, _ = redact_spans(text)
    assert text not in out
    assert REDACTED in out


def test_thai_phone_inside_sentence_is_redacted():
    """嵌在句子里（含泰文）也要脱敏，而不是只处理整段是号码的情况。"""
    from shopkeeper.security.redaction import REDACTED, redact_spans

    for text in ("buyer 0812345678 please call", "โทร 081 234 5678", "ติดต่อ 0812345678"):
        out, _ = redact_spans(text)
        assert "081" not in out, f"{text!r} 里的号码漏了"
        assert REDACTED in out


@pytest.mark.parametrize(
    "text",
    [
        "AB1234567890123",       # 型号（紧邻字母）
        "2026-10-16",            # 日期
        "1234567890",            # 10 位但不是 0 开头（非泰国号）
        "SKU-08123456789012",    # 长数字串
    ],
)
def test_non_pii_numbers_survive(text):
    """不能因为加规则就咬坏型号/日期 —— 过度脱敏同样损坏业务数据。"""
    from shopkeeper.security.redaction import redact_spans

    out, _ = redact_spans(text)
    assert out == text, f"{text!r} 被误脱敏成 {out!r}"


def test_street_address_pattern_is_resolved_by_name():
    """`_SPAN_PATTERNS` 必须**按名字**取地址规则，不能按下标。

    原先是 `CONTENT_PATTERNS[5][1]` —— 往 `CONTENT_PATTERNS` 中间插一条规则
    就会静默取到别的规则，地址脱敏随之失效且**不报错**。
    我加泰国手机号规则时正是这样踩到的。
    """
    from shopkeeper.security.redaction import CONTENT_PATTERNS, _pattern_by_name

    by_index_would_be = CONTENT_PATTERNS[5][0]
    assert by_index_would_be != "street_address", (
        "若这条断言失败说明下标恰好又对上了 —— 但仍不该依赖下标"
    )
    assert _pattern_by_name("street_address").search("广东省深圳市南山区科技园南路12号3栋501")


def test_pattern_by_name_raises_on_unknown():
    from shopkeeper.security.redaction import _pattern_by_name

    with pytest.raises(KeyError, match="不存在"):
        _pattern_by_name("不存在")


# --------------------------------------------------------------------------
# 裸 key=value 与认证方案（独立复核发现的 F2 / F3）
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "access_token=ROW_live_SECRET_ABC",
        "app_secret=LEAKED_XYZ",
        "api_key=AK123456789",
        "password=hunter2hunter2",
        'access_token="SECRETVALUE123"',
        "access_token='SECRET'",
        '{"access_token": "SECRETVALUE123"}',
        '{"access_token":"SECRETVALUE123"}',
    ],
)
def test_bare_key_value_secret_is_redacted(text):
    """**整段就是一个 `key=value`** 时也必须脱敏。

    原先的前缀类写死 `[?&\s]`（必须前面有 `?`/`&`/空白），于是
    `redact_spans('access_token=SECRET')` **原样返回**。

    这不是边角情况：异常文本、平台回显的错误体、`%s` 拼接的日志都长这样，
    而那条路径**没有字段名可依赖** —— dict 的 key 归 `is_pii_field` 管，
    一旦拍平成字符串就只剩这一层防线。
    """
    from shopkeeper.security.redaction import redact_spans

    # 断言「冒号/等号右侧的原值不在了」，而不是硬编码某个词 ——
    # 各样本的值不同（SECRET / LEAKED_XYZ / AK123456789 / hunter2hunter2）。
    out, _ = redact_spans(text)
    assert "[REDACTED]" in out, f"{text!r} 未脱敏 → {out!r}"
    value = re.split(r"[:=]", text, maxsplit=1)[1].strip("\"'{} ")
    assert value not in out, f"{text!r} 的原值 {value!r} 仍在 → {out!r}"


@pytest.mark.parametrize(
    "text",
    [
        "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.SECRETTOKEN",
        "Authorization: Bearer t-g1044ghJRSX4PO4VP7LQYVQ5NKGQ7W2Z",
        "Authorization: Basic dXNlcjpwYXNzd29yZA==",
        "authorization: token abcdef123456",
    ],
)
def test_authorization_scheme_token_is_redacted(text):
    """`Authorization: Bearer <token>` 必须连 token 一起打掉，不能只打 `Bearer`。

    value 类原为「到第一个空白为止」的字符集，于是只吃掉方案名、留下凭据。
    而 `Bearer` 正是最常见的形态（`providers.py` / `orchestrator/llm.py` 都发这个头）。

    ⚠️ 现有用例用的是 `sk-...` 样本，**被另一条规则兜住了**，所以看起来是好的。
    本用例故意用**没有 `sk-` 前缀**的真实 token 形态。
    """
    from shopkeeper.security.redaction import redact_spans

    out, _ = redact_spans(text)
    assert "[REDACTED]" in out
    for leaked in ("eyJhbGciOiJIUzI1NiJ9", "t-g1044", "dXNlcjpwYXNz", "abcdef123456"):
        assert leaked not in out, f"{text!r} 里 {leaked!r} 漏了"


def test_redaction_preserves_structure():
    """脱敏要保留原文结构（分隔符与引号），不能改坏 JSON。"""
    from shopkeeper.security.redaction import redact_spans

    out, _ = redact_spans('{"access_token": "SECRET"}')
    assert out.startswith('{"access_token"')
    assert out.endswith('"}'), f"结构被改坏：{out!r}"

    out2, _ = redact_spans("?access_token=SECRET&keep=1")
    assert "&keep=1" in out2, "不该把后续参数吃掉"


@pytest.mark.parametrize("text", ['{"sku": "ABC-123"}', "normal text", "?limit=50&page=2"])
def test_no_false_positive_on_non_secrets(text):
    """不能因为加规则就把普通字段也打掉。"""
    from shopkeeper.security.redaction import redact_spans

    out, _ = redact_spans(text)
    assert out == text, f"{text!r} 被误脱敏成 {out!r}"
