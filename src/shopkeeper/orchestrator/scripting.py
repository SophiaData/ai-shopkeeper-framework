"""短视频脚本步的确定性模板与落地校验。

口径对齐 ``prompts/short-video-script.md``：只复述上架文案里已有的卖点，
不补防水、食品级、承重数字或「全网最低」。默认不外呼；编排器在 mock 通道
直接采用本模板，真实 LLM 通道用同一结构校验后再过本模块的禁写扫描。
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field

from shopkeeper.site_profile import SiteProfile, active_site_profile

PROMPT_FILE = "prompts/short-video-script.md"
PROMPT_VERSION = "short-video-script-v1"
DEFAULT_DURATION_S = 30
DEFAULT_SCENE = "雨季防潮"
CLOSING_NOTE = "发布、挂车和活动价由拍摄者本人在 TikTok 上确认后点击。"
DURATION_MIN = 15
DURATION_MAX = 60

# 卖家原文里没有这些说法时，脚本不得出现。分组只用于报错归类。
_FORBIDDEN_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("防水", ("防水", "waterproof", "กันน้ำ")),
    ("食品级", ("食品级", "food-grade", "food grade", "เกรดอาหาร")),
    ("承重", ("承重", "load-bearing", "load bearing", "รับน้ำหนัก")),
    ("全网最低", ("全网最低", "ถูกที่สุด")),
)
_LOAD_NUMBER = re.compile(r"\d+\s*(?:kg|กก\.?|公斤|千克)", re.IGNORECASE)

def script_system(profile: SiteProfile | None = None) -> str:
    """短视频系统提示。店铺名读画像；默认画像与接线前逐字相同。"""
    stem = (profile or active_site_profile()).display_stem
    return (
        f"你是 TikTok Shop {stem}的短视频编剧。输出给中文运营拍摄，口播给泰国观众。"
        "只使用用户给出的卖点。没有实测依据时，不写防水、食品级、承重数字和「全网最低」。"
        "时长遵守用户指定的秒数（15–60），结构落在 Hook、痛点、演示、对比、行动号召。"
        "开头 3 秒内是乱的抽屉或衣柜特写；中段展示开箱、安装或塞满；对比段用用户给的本地场景；"
        "规格只念用户给过的文字；若挂车，用合规说法引导点击购物车，不编造库存和折扣。"
        "请输出 JSON：分镜表（秒数、画面、泰语口播、中文对照、泰语字幕、备注）、"
        "3 个 Hook 备选（泰语+中文）、BGM 情绪（不点名有版权歌曲）、"
        "按时间顺序的泰语提词、封面文案 2 版（泰语+中文，能看懂品类）、"
        "拍摄检查（是否有尺寸参照物、泰语是否避开绝对化承诺）、"
        f"以及结尾声明：{CLOSING_NOTE}"
    )


class ScriptShot(BaseModel):
    seconds: str
    beat: str
    visual: str
    voice_th: str
    voice_zh: str
    subtitle_th: str
    note: str = ""


class ScriptHook(BaseModel):
    voice_th: str
    voice_zh: str


class ScriptCover(BaseModel):
    text_th: str
    text_zh: str


class ScriptShootChecks(BaseModel):
    has_size_reference: bool
    avoids_absolute_claims: bool


class ScriptDecision(BaseModel):
    rationale: str = ""
    duration_s: int
    cart: bool
    scene: str
    shots: list[ScriptShot] = Field(min_length=5, max_length=5)
    hooks: list[ScriptHook] = Field(min_length=3, max_length=3)
    bgm_mood: str
    teleprompter_th: list[str] = Field(min_length=1)
    covers: list[ScriptCover] = Field(min_length=2, max_length=2)
    shoot_checks: ScriptShootChecks
    closing_note: str


class ScriptInputError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def parse_duration(raw: Any) -> int:
    if raw is None or raw == "":
        return DEFAULT_DURATION_S
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ScriptInputError(
            "ORCH_SCRIPT_DURATION",
            "短视频时长须为 15–60 的整数秒，缺省 30",
        ) from exc
    if value < DURATION_MIN or value > DURATION_MAX:
        raise ScriptInputError(
            "ORCH_SCRIPT_DURATION",
            f"短视频时长须在 {DURATION_MIN}–{DURATION_MAX} 秒，收到 {value}",
        )
    return value


def parse_cart(raw: Any) -> bool:
    """缺省视为挂车（本流水线的上架目标是带购物车的商品片）。"""
    if raw is None or raw == "":
        return True
    if isinstance(raw, bool):
        return raw
    text = str(raw).strip().lower()
    if text in {"1", "true", "yes", "y", "是", "挂车"}:
        return True
    if text in {"0", "false", "no", "n", "否", "不挂"}:
        return False
    raise ScriptInputError("ORCH_SCRIPT_CART", f"是否挂车须为是/否，收到 {raw!r}")


def shot_windows(duration_s: int) -> list[tuple[int, int, str]]:
    """五段压缩进指定时长。Hook 固定落在开头 3 秒内。"""
    hook_end = 3 if duration_s > 3 else duration_s
    weights = (2, 4, 3, 2)
    rest = duration_s - hook_end
    weight_sum = sum(weights)
    cuts = [hook_end]
    acc = 0
    for weight in weights[:-1]:
        acc += weight
        cuts.append(hook_end + int(round(rest * acc / weight_sum)))
    cuts.append(duration_s)
    for index in range(1, len(cuts)):
        if cuts[index] <= cuts[index - 1]:
            cuts[index] = min(duration_s, cuts[index - 1] + 1)
    cuts[-1] = duration_s
    for index in range(len(cuts) - 2, 0, -1):
        if cuts[index] >= cuts[index + 1]:
            cuts[index] = max(cuts[index - 1], cuts[index + 1] - 1)
    labels = ("痛点", "演示", "对比", "行动号召")
    windows = [(0, hook_end, "Hook")]
    for index, label in enumerate(labels):
        windows.append((cuts[index], cuts[index + 1], label))
    return windows


def listing_script_source(
    *,
    listing_copy: dict[str, Any] | None,
    facts: dict[str, Any] | None,
    run_input: dict[str, Any] | None,
) -> dict[str, Any]:
    """从上一步 listing 与启动参数收集脚本输入。卖点只取已有文案。"""
    copy = listing_copy or {}
    facts = facts or {}
    incoming = run_input or {}
    name = str(
        copy.get("title_th")
        or copy.get("title_en")
        or facts.get("title_raw")
        or ""
    ).strip()
    explicit = incoming.get("selling_points")
    if isinstance(explicit, list):
        points = "；".join(str(item).strip() for item in explicit if str(item).strip())
    else:
        points = str(explicit or "").strip()
    if not points:
        chunks = [str(copy.get("body_th") or "").strip(), str(copy.get("body_en") or "").strip()]
        points = "\n".join(chunk for chunk in chunks if chunk)
    scene = str(incoming.get("scene") or DEFAULT_SCENE).strip() or DEFAULT_SCENE
    if not name and not points:
        raise ScriptInputError("ORCH_SCRIPT_NO_COPY", "短视频脚本缺少上一步 listing 标题或卖点")
    if not name:
        name = "สินค้าจัดเก็บ"
    duration_s = parse_duration(incoming.get("duration", incoming.get("video_duration")))
    cart = parse_cart(incoming.get("cart"))
    source_text = "\n".join(
        part
        for part in (
            name,
            points,
            scene,
            str(copy.get("title_th") or ""),
            str(copy.get("title_en") or ""),
            str(copy.get("body_th") or ""),
            str(copy.get("body_en") or ""),
            str(facts.get("title_raw") or ""),
        )
        if part
    )
    return {
        "name": name,
        "selling_points": points or "卖家未给规格",
        "duration_s": duration_s,
        "cart": cart,
        "scene": scene,
        "source_text": source_text,
    }


def _clip(text: str, limit: int = 80) -> str:
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1] + "…"


def build_deterministic_script(source: dict[str, Any]) -> dict[str, Any]:
    """本地模板。口播只夹带卖家已给的卖点原文，不另加规格。"""
    name = str(source["name"])
    points = _clip(str(source["selling_points"]))
    scene = str(source["scene"])
    cart = bool(source["cart"])
    duration_s = int(source["duration_s"])
    windows = shot_windows(duration_s)
    if cart:
        cta_visual = "手指指向商品页购物车按钮的示意；不出现库存件数或折扣数字"
        cta_th = "แตะตะกร้าในหน้าสินค้าได้หลังตรวจคลิปนี้ ไม่ระบุสต็อกหรือส่วนลด"
        cta_zh = "核对这条片子后，可在商品页点击购物车。不报库存，也不报折扣。"
        cta_sub = "แตะตะกร้าในหน้าสินค้า"
    else:
        cta_visual = "停在商品外观；不出现购物车按钮"
        cta_th = "ดูรายละเอียดสินค้าในคลิปนี้ก่อน"
        cta_zh = "先看商品细节。本条不引导购物车。"
        cta_sub = "ดูรายละเอียดในคลิป"
    visuals = {
        "Hook": "乱的抽屉或衣柜特写，3 秒内让人看懂乱",
        "痛点": "手在格子里翻找小物，仍然找不到",
        "演示": f"开箱、安装或塞满「{name}」，旁边放一个常见马克杯作尺寸参照",
        "对比": f"整理前后对比，场景是{scene}",
        "行动号召": cta_visual,
    }
    voices = {
        "Hook": (
            "ลิ้นชักรกแบบนี้ ยังหาของไม่เจอใช่ไหม",
            "抽屉乱成这样，还是找不到东西吗？",
            "ลิ้นชักรก",
            "只交代乱，不介绍规格",
        ),
        "痛点": (
            "ของเล็กหายทุกที ใช้เวลาก่อนออกจากบ้าน",
            "小东西老找不到，出门前要多花时间翻。",
            "ของเล็กหาย",
            "痛点不写功效",
        ),
        "演示": (
            f"ใส่ {name} ตามจุดขายที่ตรวจแล้ว: {points}",
            f"按已核对卖点使用 {name}：{points}",
            _clip(name, 40),
            "口播只念卖家已给的卖点原文",
        ),
        "对比": (
            f"ก่อนกับหลัง ในฉาก{scene}",
            f"前后对比，场景是{scene}。不把场景说成产品认证。",
            _clip(scene, 24),
            "本地场景来自输入，不是新品规格",
        ),
        "行动号召": (cta_th, cta_zh, cta_sub, "不编造库存和折扣"),
    }
    shots: list[dict[str, str]] = []
    for start, end, beat in windows:
        voice_th, voice_zh, subtitle, note = voices[beat]
        shots.append(
            {
                "seconds": f"{start}-{end}",
                "beat": beat,
                "visual": visuals[beat],
                "voice_th": voice_th,
                "voice_zh": voice_zh,
                "subtitle_th": subtitle,
                "note": note,
            }
        )
    hooks = [
        {
            "voice_th": "ลิ้นชักเต็มแบบนี้ ยังหาของไม่เจอใช่ไหม",
            "voice_zh": "抽屉满成这样，还是找不到东西吗？",
        },
        {
            "voice_th": "ของเล็กหายทุกที ดูจังหวะจัดให้เห็นหมด",
            "voice_zh": "小东西老是找不到。看这一下怎么收成一眼能看见。",
        },
        {
            "voice_th": f"{name} ใส่ของยังไง ดูในคลิปนี้",
            "voice_zh": f"{name} 怎么把东西放进去，看这条片子。",
        },
    ]
    covers = [
        {
            "text_th": f"กล่องจัดเก็บ · {_clip(name, 36)}",
            "text_zh": f"家居收纳 · {_clip(name, 36)}",
        },
        {
            "text_th": f"จัดบ้าน · {_clip(scene, 24)}",
            "text_zh": f"整理前后 · {_clip(scene, 24)}",
        },
    ]
    return {
        "rationale": (
            f"确定性模板 {PROMPT_FILE} duration={duration_s}s cart={cart}；"
            "只复述已给卖点，不外呼"
        ),
        "duration_s": duration_s,
        "cart": cart,
        "scene": scene,
        "shots": shots,
        "hooks": hooks,
        "bgm_mood": "轻快、清爽",
        "teleprompter_th": [shot["voice_th"] for shot in shots],
        "covers": covers,
        "shoot_checks": {
            "has_size_reference": True,
            "avoids_absolute_claims": True,
        },
        "closing_note": CLOSING_NOTE,
    }


def script_plaintext(payload: dict[str, Any]) -> str:
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


def ungrounded_claims(payload: dict[str, Any], source_text: str) -> list[str]:
    """卖家原文未出现的禁写说法。命中则脚本步失败，不进入审批。"""
    body = script_plaintext(payload)
    source = source_text or ""
    # ⚠️ **必须在归一化后的形态上比对**，否则一个空格就能绕过。
    #
    # 原先是裸 `in` 子串匹配（`folded in body_cf`），于是：
    #   "防水等级 IPX7"   → 命中
    #   "防 水等级 IPX7"  → **漏放**   ← 只多了一个空格
    #   "防\u200b水等级"  → **漏放**   ← 零宽空格
    #   "防　水等级"       → **漏放**   ← 全角空格
    #
    # 模型只要把词拆开写，脚本就**通过**并写进 context、进而交给人工拍摄。
    #
    # 这类规避不是假想：`compliance` 模块**早就**做了同样的归一化
    # （`match_forms` → NFKC + 去空白/零宽），编排器自己这一份却没有 ——
    # 同一个仓库里两套口径，弱的那套会成为绕过点。
    # 现在直接复用 `match_forms`，不再自建第二套。
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
    for match in _LOAD_NUMBER.finditer(body):
        token = match.group(0)
        if token.casefold() not in source_cf:
            hits.append(f"承重数字:{token}")
    # 去重保序
    seen: set[str] = set()
    unique: list[str] = []
    for hit in hits:
        if hit not in seen:
            seen.add(hit)
            unique.append(hit)
    return unique
