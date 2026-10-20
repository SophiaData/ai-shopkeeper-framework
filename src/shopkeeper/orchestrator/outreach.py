"""达人建联草稿步：确定性模板、提示词文件与合规校验。

口径对齐 ``prompts/influencer-outreach.md``（文件在时读取其中的系统提示）。
只写草稿，不声称已经发出，不编造佣金比例、粉丝数、保底播放或赠品。
默认不外呼；编排器在 mock 通道直接采用本模板，真实 LLM 通道用同一结构校验后再过
``shopkeeper.compliance`` 与泰语质检。批准只改草稿状态，发送仍由卖家本人完成。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from shopkeeper.compliance import hits_to_report
from shopkeeper.site_profile import SiteProfile, active_site_profile
from shopkeeper.workflow.thai_qa import review_copy

PROMPT_FILE = "prompts/influencer-outreach.md"
PROMPT_VERSION = "influencer-outreach-v1"
DRAFT_KIND = "influencer_outreach"
CLOSING_NOTE = (
    "请你本人在 TikTok、联盟或邮箱中发送。"
    "寄样地址和运费由你确认。"
    "佣金和付款由你完成，本草稿不是合同。"
)
CREATOR_TYPES: tuple[str, ...] = ("测评", "生活", "出租屋改造")
TERM_KEYS: tuple[str, ...] = ("sample", "commission", "code", "rights")
TERM_LABELS: dict[str, str] = {
    "sample": "寄样",
    "commission": "佣金",
    "code": "专属码",
    "rights": "内容授权",
}
REQUIRED_CLOSING: tuple[str, ...] = (
    "请你本人在 TikTok、联盟或邮箱中发送",
    "寄样地址和运费由你确认",
    "佣金和付款由你完成",
    "本草稿不是合同",
)
DEFAULT_AUDIENCE: dict[str, str] = {
    "测评": "关注尺寸、材质和前后对比的收纳测评观众",
    "生活": "关注厨房、衣柜或浴室日常整理的观众",
    "出租屋改造": "关注小户型出租屋整理的观众",
}
DEFAULT_OFFER = "寄样，预算面议"
DEFAULT_BUDGET = "预算面议"
DEFAULT_NAME = "达人"

_PROMPT_BLOCK = re.compile(r"##\s*系统提示\s+```text\s*(.*?)```", re.DOTALL)
_PERCENT = re.compile(r"\d+\s*[%％]")
_FOLLOWERS = re.compile(
    r"\d[\d,]*\s*(?:万)?\s*(?:粉丝|followers|ผู้ติดตาม)",
    re.IGNORECASE,
)
_LOAD_NUMBER = re.compile(r"\d+\s*(?:kg|กก\.?|公斤|千克)", re.IGNORECASE)

# 卖家原文没有这些说法时，草稿不得出现。分组只用于报错归类。
_FORBIDDEN_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("已发送", ("已发送", "已经发出", "已发出", "already sent", "has been sent", "ส่งแล้ว", "ส่งออกไปแล้ว")),
    ("立即付款", ("立即付款", "马上付款", "จ่ายตอนนี้", "จ่ายทันที", "pay now")),
    ("批量群发", ("批量群发", "群发私信", "mass dm", "mass message")),
    ("保底播放", ("保底", "播放量保证", "การันตี", "guaranteed view", "guaranteed play")),
    ("赠品", ("赠品", "ของแถม", "ของแจก", "free gift")),
    ("免打孔", ("免打孔", "ไม่ต้องเจาะ", "ไม่เจาะ", "no-drill", "no drill")),
    ("防水", ("防水", "waterproof", "กันน้ำ")),
    ("食品级", ("食品级", "food-grade", "food grade", "เกรดอาหาร")),
    ("承重", ("承重", "load-bearing", "load bearing", "รับน้ำหนัก")),
    ("全网最低", ("全网最低", "ถูกที่สุด")),
    ("一成佣金", ("一成",)),
)


def outreach_system(profile: SiteProfile | None = None) -> str:
    """达人建联系统提示。店铺名读画像；文件里的系统提示仍优先。默认画像与接线前逐字相同。"""
    stem = (profile or active_site_profile()).display_stem
    return (
        f"你是 TikTok Shop {stem}的达人合作文案助理。只写草稿，不声称消息已经发出。"
        "不要输出批量群发方案，不要要求对方立即付款，不要承诺未在输入里出现的佣金比例、保底播放量或赠品。"
        "昵称必须使用用户提供的名字。不知道对方数据时，不要编造粉丝数。"
        "测评侧重已核对的尺寸、材质和前后对比；生活侧重一个日常场景；"
        "出租屋改造只有输入里写了免打孔才能使用这个说法。"
        "佣金、专属码、内容授权未确定时写「待你填写」，不要填假百分比。"
        f"结尾必须写明：{CLOSING_NOTE}"
    )

_JSON_TAIL = (
    "请只返回 JSON：rationale，以及 messages 数组。"
    "每条含 creator_type、creator_name、audience、dm_th、dm_zh、dm_en、"
    "sample_th、sample_zh、terms（恰好 4 条，key 为 sample、commission、code、rights，"
    "未确定的 value 写「待你填写」）、followups（恰好两轮，day 为 3 和 7，各有 body_th 与 body_zh）、"
    f"closing_note。closing_note 必须包含：{CLOSING_NOTE}"
)


class OutreachTerm(BaseModel):
    key: str
    label: str
    value: str


class OutreachFollowup(BaseModel):
    day: int
    body_th: str
    body_zh: str


class OutreachMessage(BaseModel):
    creator_type: str
    creator_name: str
    audience: str
    dm_th: str
    dm_zh: str
    dm_en: str
    sample_th: str
    sample_zh: str
    terms: list[OutreachTerm] = Field(min_length=4, max_length=4)
    followups: list[OutreachFollowup] = Field(min_length=2, max_length=2)
    closing_note: str


class OutreachDecision(BaseModel):
    rationale: str = ""
    messages: list[OutreachMessage] = Field(min_length=1)


class OutreachInputError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def default_prompt_path() -> Path:
    return Path(__file__).resolve().parents[3] / PROMPT_FILE


def load_system_prompt(
    path: Path | None = None,
    profile: SiteProfile | None = None,
) -> tuple[str, str]:
    """返回 (system, source)。文件内容只提供措辞，内置安全约束与 JSON 契约始终附加。"""
    target = default_prompt_path() if path is None else path
    policy = outreach_system(profile)
    source = "embedded"
    if target.is_file():
        text = target.read_text(encoding="utf-8")
        match = _PROMPT_BLOCK.search(text)
        extracted = (match.group(1).strip() if match else "")
        if extracted:
            policy = f"{extracted}\n\n{policy}"
            source = str(target)
    return f"{policy}\n\n{_JSON_TAIL}", source


def _clip(text: str, limit: int = 80) -> str:
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1] + "…"


def parse_creators(raw: Any) -> list[dict[str, str]]:
    """缺省为三类达人各一条，昵称「达人」。非法类型不猜测。"""
    if raw is None or raw == "" or raw == []:
        return [
            {
                "creator_type": kind,
                "creator_name": DEFAULT_NAME,
                "audience": DEFAULT_AUDIENCE[kind],
                "offer": DEFAULT_OFFER,
                "budget_band": DEFAULT_BUDGET,
            }
            for kind in CREATOR_TYPES
        ]
    if not isinstance(raw, list):
        raise OutreachInputError("ORCH_OUTREACH_CREATOR", "creators 须为列表，或缺省以使用三类达人")
    parsed: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in raw:
        row = {"creator_type": item} if isinstance(item, str) else item
        if not isinstance(row, dict):
            raise OutreachInputError("ORCH_OUTREACH_CREATOR", "每条达人须为对象或类型字符串")
        kind = str(row.get("creator_type") or "").strip()
        if kind not in CREATOR_TYPES:
            allowed = "、".join(CREATOR_TYPES)
            raise OutreachInputError(
                "ORCH_OUTREACH_CREATOR",
                f"达人类型只能是{allowed}，收到 {kind or '空'}",
            )
        name = str(row.get("creator_name") or DEFAULT_NAME).strip() or DEFAULT_NAME
        audience = str(row.get("audience") or DEFAULT_AUDIENCE[kind]).strip() or DEFAULT_AUDIENCE[kind]
        offer = str(row.get("offer") or DEFAULT_OFFER).strip() or DEFAULT_OFFER
        budget = str(row.get("budget_band") or DEFAULT_BUDGET).strip() or DEFAULT_BUDGET
        key = f"{kind}\n{name}"
        if key in seen:
            raise OutreachInputError("ORCH_OUTREACH_CREATOR", f"重复的达人：{kind} / {name}")
        seen.add(key)
        parsed.append(
            {
                "creator_type": kind,
                "creator_name": name,
                "audience": audience,
                "offer": offer,
                "budget_band": budget,
            }
        )
    return parsed


def listing_outreach_source(
    *,
    listing_copy: dict[str, Any] | None,
    facts: dict[str, Any] | None,
    run_input: dict[str, Any] | None,
) -> dict[str, Any]:
    """从上一步 listing 与启动参数收集建联输入。卖点只取已有文案。"""
    copy = listing_copy or {}
    facts = facts or {}
    incoming = run_input or {}
    name = str(copy.get("title_th") or copy.get("title_en") or facts.get("title_raw") or "").strip()
    explicit = incoming.get("selling_points")
    if isinstance(explicit, list):
        points = "；".join(str(item).strip() for item in explicit if str(item).strip())
    else:
        points = str(explicit or "").strip()
    if not points:
        chunks = [str(copy.get("body_th") or "").strip(), str(copy.get("body_en") or "").strip()]
        points = "\n".join(chunk for chunk in chunks if chunk)
    if not name and not points:
        raise OutreachInputError("ORCH_OUTREACH_NO_COPY", "达人建联缺少上一步 listing 标题或卖点")
    if not name:
        name = "สินค้าจัดเก็บ"
    creators = parse_creators(incoming.get("creators"))
    parts = [
        name,
        points,
        str(copy.get("title_th") or ""),
        str(copy.get("title_en") or ""),
        str(copy.get("body_th") or ""),
        str(copy.get("body_en") or ""),
        str(facts.get("title_raw") or ""),
    ]
    for creator in creators:
        parts.extend(creator.values())
    return {
        "product": name,
        "selling_points": points or "卖家未给规格",
        "creators": creators,
        "source_text": "\n".join(part for part in parts if part),
    }


def _terms(creator: dict[str, str]) -> list[dict[str, str]]:
    sample_value = f"{creator['offer']}；地址与运费待你填写"
    if "待你填写" not in sample_value:
        sample_value += "；待你填写"
    rows = [
        ("sample", sample_value),
        ("commission", "待你填写"),
        ("code", "待你填写"),
        ("rights", "待你填写"),
    ]
    return [{"key": key, "label": TERM_LABELS[key], "value": value} for key, value in rows]


def _focus(kind: str, product: str, points: str, audience: str) -> tuple[str, str, str]:
    clipped = _clip(points, 72)
    if kind == "测评":
        th = (
            f"อยากชวนทำคลิปรีวิว {product} "
            f"จุดขายที่ตรวจแล้ว: {clipped} เน้นขนาด วัสดุ และก่อนกับหลัง"
        )
        zh = f"想请你做测评。已核对卖点：{clipped}。方便按尺寸、材质和前后对比来结构。"
        en = f"I would like a review of {product}. Checked points: {clipped}. Size, material, and a before/after fit the format."
        return th, zh, en
    if kind == "生活":
        th = (
            f"อยากชวนเล่าหนึ่งฉากในชีวิตประจำวันกับ {product} "
            f"จุดขายที่ตรวจแล้ว: {clipped} ฉากจากที่คุณเห็น: {_clip(audience, 48)}"
        )
        zh = f"想请你讲一个日常场景。已核对卖点：{clipped}。场景按你观察到的：{_clip(audience, 48)}。"
        en = f"I would like one daily scene with {product}. Checked points: {clipped}."
        return th, zh, en
    th = f"สำหรับห้องเช่าพื้นที่น้อย อยากชวนจัด {product} ให้หยิบง่าย จุดขายที่ตรวจแล้ว: {clipped}"
    zh = f"出租屋场景，只复述已核对卖点：{clipped}。"
    en = f"For a small rental, only the checked points: {clipped}."
    return th, zh, en


def build_deterministic_outreach(
    source: dict[str, Any],
    profile: SiteProfile | None = None,
) -> dict[str, Any]:
    """本地模板。私信只夹带卖家已给的卖点与达人输入，不另加规格或佣金数字。"""
    stem = (profile or active_site_profile()).display_stem
    product = str(source["product"])
    points = str(source["selling_points"])
    messages: list[dict[str, Any]] = []
    for creator in source["creators"]:
        kind = creator["creator_type"]
        nickname = creator["creator_name"]
        audience = creator["audience"]
        focus_th, focus_zh, focus_en = _focus(kind, product, points, audience)
        dm_th = (
            f"สวัสดีค่ะ {nickname} ฉันดูแลร้านของใช้จัดเก็บในบ้านบน TikTok Shop ไทย "
            f"{focus_th} ถ้าสนใจช่วยตอบข้อความนี้ได้ ถ้าไม่สะดวกก็บอกได้เลยค่ะ"
        )
        dm_zh = (
            f"你好 {nickname}。我是 TikTok Shop {stem}店的运营，这是草稿，还没有发送。"
            f"{focus_zh} 方便再回复；不合作也可以直接说。"
        )
        dm_en = (
            f"Hi {nickname}. I run a home-storage shop on TikTok Shop Thailand. "
            f"This is a draft and has not gone out. {focus_en} A reply is welcome, and declining is fine."
        )
        sample_th = f"ตัวอย่างคือ {product} ในคลิปช่วยถ่ายหนึ่งช็อตให้เห็นขนาดเทียบกับแก้วน้ำใบหนึ่ง"
        sample_zh = f"样品是 {product}。请在视频里拍一个和常见马克杯对照的规格镜头。地址与运费待你填写。"
        followups = [
            {
                "day": 3,
                "body_th": (
                    f"สวัสดีอีกครั้งค่ะ {nickname} ถ้ายังสนใจ {product} ตอบเมื่อสะดวกได้เลย "
                    "ถ้าไม่รับก็ไม่เป็นไรค่ะ"
                ),
                "body_zh": f"{nickname}，第 3 天跟进。如果还想看 {product}，方便时回复就好。不想合作也可以直接说。",
            },
            {
                "day": 7,
                "body_th": f"ขออนุญาตทักครั้งสุดท้ายค่ะ {nickname} ถ้าตอนนี้ไม่สะดวก ฉันจะไม่ทวงต่อ",
                "body_zh": f"{nickname}，第 7 天最后跟一次。如果现在不方便，我就不再追。",
            },
        ]
        messages.append(
            {
                "creator_type": kind,
                "creator_name": nickname,
                "audience": audience,
                "dm_th": dm_th,
                "dm_zh": dm_zh,
                "dm_en": dm_en,
                "sample_th": sample_th,
                "sample_zh": sample_zh,
                "terms": _terms(creator),
                "followups": followups,
                "closing_note": CLOSING_NOTE,
            }
        )
    return {
        "rationale": f"确定性模板 {PROMPT_FILE}；只写草稿，不发送，不编造佣金比例或粉丝数",
        "messages": messages,
    }


def outreach_plaintext(payload: dict[str, Any]) -> str:
    chunks: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, str):
            chunks.append(node)
        elif isinstance(node, dict):
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(payload)
    return "\n".join(chunks)


def shape_problems(payload: dict[str, Any], creators: list[dict[str, str]]) -> list[str]:
    """条数、昵称、条款键、跟进日和固定结尾。对不上就不入库。"""
    problems: list[str] = []
    messages = payload.get("messages") if isinstance(payload.get("messages"), list) else []
    expected = [(row["creator_type"], row["creator_name"]) for row in creators]
    got = [
        (str(msg.get("creator_type") or ""), str(msg.get("creator_name") or ""))
        for msg in messages
        if isinstance(msg, dict)
    ]
    if got != expected:
        problems.append("草稿条数或达人与输入不一致")
    for msg in messages:
        if not isinstance(msg, dict):
            problems.append("草稿不是对象")
            continue
        closing = str(msg.get("closing_note") or "")
        for phrase in REQUIRED_CLOSING:
            if phrase not in closing:
                problems.append(f"结尾缺少「{phrase}」")
                break
        terms = msg.get("terms") if isinstance(msg.get("terms"), list) else []
        keys = [str(term.get("key") or "") for term in terms if isinstance(term, dict)]
        if keys != list(TERM_KEYS):
            problems.append("合作条款须按寄样、佣金、专属码、内容授权")
        followups = msg.get("followups") if isinstance(msg.get("followups"), list) else []
        days = [item.get("day") for item in followups if isinstance(item, dict)]
        if days != [3, 7]:
            problems.append("跟进须为第 3 天和第 7 天")
    # 去重保序
    seen: set[str] = set()
    unique: list[str] = []
    for item in problems:
        if item not in seen:
            seen.add(item)
            unique.append(item)
    return unique


def ungrounded_claims(payload: dict[str, Any], source_text: str) -> list[str]:
    """卖家原文未出现的禁写说法。命中则建联步失败，不进入待审队列。"""
    body = outreach_plaintext(payload)
    source = source_text or ""
    # ⚠️ 同 `scripting.ungrounded_claims`：必须在**归一化后**的形态上比对。
    # 原先禁词组走裸子串（一个空格即绕过），而下面 `_PERCENT` 那一支却已经
    # 手动 `re.sub(r"\s+", "", ...)` —— **同一个函数里两套口径**，
    # 弱的那套就是绕过点。现统一复用 `compliance.match_forms`。
    from shopkeeper.compliance import match_forms

    body_cf = match_forms(body)[1].casefold()
    source_cf = match_forms(source)[1].casefold()
    hits: list[str] = []
    for label, terms in _FORBIDDEN_GROUPS:
        for term in terms:
            folded = term.casefold()
            if folded in body_cf and folded not in source_cf:
                hits.append(label)
                break
    for match in _PERCENT.finditer(body):
        token = match.group(0)
        if token.casefold() not in source_cf and re.sub(r"\s+", "", token) not in re.sub(r"\s+", "", source_cf):
            hits.append(f"佣金比例:{token}")
    for match in _FOLLOWERS.finditer(body):
        token = match.group(0)
        if token.casefold() not in source_cf:
            hits.append(f"粉丝数:{token}")
    for match in _LOAD_NUMBER.finditer(body):
        token = match.group(0)
        if token.casefold() not in source_cf:
            hits.append(f"承重数字:{token}")
    seen: set[str] = set()
    unique: list[str] = []
    for hit in hits:
        if hit not in seen:
            seen.add(hit)
            unique.append(hit)
    return unique


def review_message(message: dict[str, Any]) -> dict[str, Any]:
    """单条草稿走泰语质检 + 合规词表。block 命中则 ok 为假。"""
    thai_parts = [
        str(message.get("dm_th") or ""),
        str(message.get("sample_th") or ""),
    ]
    extra_parts = [
        str(message.get("dm_zh") or ""),
        str(message.get("dm_en") or ""),
        str(message.get("sample_zh") or ""),
        str(message.get("closing_note") or ""),
        str(message.get("audience") or ""),
        str(message.get("creator_name") or ""),
        str(message.get("creator_type") or ""),
    ]
    for followup in message.get("followups") or []:
        if isinstance(followup, dict):
            thai_parts.append(str(followup.get("body_th") or ""))
            extra_parts.append(str(followup.get("body_zh") or ""))
    for term in message.get("terms") or []:
        if isinstance(term, dict):
            extra_parts.append(str(term.get("label") or ""))
            extra_parts.append(str(term.get("value") or ""))
    qa = review_copy(
        "ข้อความเชิญ",
        "\n".join(part for part in thai_parts if part),
        require_thai=True,
        extra="\n".join(part for part in extra_parts if part),
    )
    compliance = qa.get("compliance") if isinstance(qa.get("compliance"), dict) else hits_to_report([])
    qa["compliance"] = compliance
    return qa
