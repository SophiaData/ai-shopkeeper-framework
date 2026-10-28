"""七个工作模型：提示词包、严格 JSON 契约、确定性护栏与调用记录。

提示词正文的唯一家在 ``docs/WORK-MODEL-PROMPTS.md``；本模块只读取它并按其章节
切分，不复制正文。契约与护栏是代码侧可执行的判据，模型结果只能进入候选、
素材草稿或本地计划，本模块不写任何平台状态。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
)

from shopkeeper.security.redaction import redact_spans, sniff_content

PROMPT_VERSION = 'work-models-v1.2'
PACK_FILE = 'docs/WORK-MODEL-PROMPTS.md'
SYSTEM_SECTION = '公共系统提示词'
MOCK_WARNING = 'mock 只证明流程可用，不代表真实模型质量。'

Module = Literal[
    'research',
    'verify',
    'write_copy',
    'quality_check',
    'prepare_campaign',
    'record_metrics',
    'orchestrator',
]

# 章节标题 → 模块 ID。模块 ID 与总控编排器 ``next_step`` 的取值同名。
MODULE_TITLES: dict[str, Module] = {
    '选品研究员': 'research',
    '事实核验员': 'verify',
    '文案生成器': 'write_copy',
    '素材质检员': 'quality_check',
    '投放计划助手': 'prepare_campaign',
    '数据复盘员': 'record_metrics',
    '总控编排器': 'orchestrator',
}
MODULES: tuple[Module, ...] = tuple(MODULE_TITLES.values())

# 失败码：契约与护栏各自可定位，便于按码断言。
CODE_PACK = 'WORK_MODEL_PACK'
CODE_INVALID_JSON = 'WORK_MODEL_INVALID_JSON'
CODE_MARKDOWN = 'WORK_MODEL_MARKDOWN'
CODE_CONTRACT = 'WORK_MODEL_CONTRACT'
CODE_METRIC_MISMATCH = 'WORK_MODEL_METRIC_MISMATCH'
CODE_METRIC_UNCOMPUTABLE = 'WORK_MODEL_METRIC_UNCOMPUTABLE'
CODE_CURRENCY_REQUIRED = 'WORK_MODEL_CURRENCY_REQUIRED'
CODE_ROUTE_UNSAFE = 'WORK_MODEL_ROUTE_UNSAFE'
CODE_SOURCE_MISSING = 'WORK_MODEL_QUALITY_SOURCE_MISSING'
CODE_PLATFORM_CLAIM = 'WORK_MODEL_PLATFORM_CLAIM'
CODE_UNGROUNDED_CLAIM = 'WORK_MODEL_UNGROUNDED_CLAIM'
CODE_EXECUTION_CLAIM = 'WORK_MODEL_EXECUTION_CLAIM'
CODE_TRACKING_FIELDS = 'WORK_MODEL_TRACKING_FIELDS'
CODE_CLAIM_LEAKED = 'WORK_MODEL_CLAIM_LEAKED'
CODE_VERIFY_REQUIRED = 'WORK_MODEL_VERIFY_REQUIRED'
CODE_VERIFY_UNSAFE_PASS = 'WORK_MODEL_VERIFY_UNSAFE_PASS'
CODE_SENSITIVE_LEAK = 'WORK_MODEL_SENSITIVE_LEAK'
CODE_INTERNAL = 'WORK_MODEL_INTERNAL'
CODE_EVIDENCE_REQUIRED = 'WORK_MODEL_EVIDENCE_REQUIRED'
CODE_QUALITY_CHECKS = 'WORK_MODEL_QUALITY_CHECKS'
# O-18：计划助手只能引用本次输入的素材；复盘员不得改写服务端读到的原始指标。
CODE_ASSET_OUT_OF_SCOPE = 'WORK_MODEL_ASSET_OUT_OF_SCOPE'
CODE_METRICS_TAMPERED = 'WORK_MODEL_METRICS_TAMPERED'
CODE_METRICS_SCOPE_MISMATCH = 'WORK_MODEL_METRICS_SCOPE_MISMATCH'

TRACKING_FIELDS = (
    'campaign_id',
    'day',
    'currency',
    'impressions',
    'clicks',
    'spend',
    'revenue',
    'orders',
)

# 绝对化/认证/功效类措辞：没有输入证据就不得出现在文案正文里。
# 中文按词表；英文/泰语按常见句式。英文/泰语仅豁免直接修饰命中声明的否定表达。
ABSOLUTE_CLAIMS = (
    '100%',
    '100％',
    '最好',
    '第一',
    '保证',
    '认证',
    '食品级',
    '医用',
    '全网最低',
    '绝对',
    '功效',
    '疗效',
)
_ABSOLUTE_CLAIM_RE = re.compile(
    '|'.join(re.escape(phrase) for phrase in ABSOLUTE_CLAIMS)
    + r'|\b(?:guaranteed\s+cure|guarantee(?:d|s)?|clinical(?:ly)?\s+proven|fda\s+approved|'
    r'certified|100\s*percent|best\s+seller|number\s*one|no\.?\s*1|#1)\b'
    + r'|(?:รับประกัน|การันตี|ผ่านการรับรอง|รักษาให้หาย|ดีที่สุด|อันดับ\s*1|100\s*เปอร์เซ็นต์)',
    re.IGNORECASE,
)
# 不得声称平台已审核或已执行。匹配前把「已经」归一成「已」；判据是**结构化正则**，
# 覆盖主语可省略、动宾两种语序与「审核|审批|批准|核准|过审」的同义表达。
_PLATFORM_CLAIM_RE = re.compile(
    # 主语 + 已 + 审核类动词（必须带「已」，否则「平台审核要求」这类名词会被误判）。
    r'(?:平台|广告|商品|计划|素材|listing)[^。；\n]{0,4}?已(?:审核|审批|批准|核准|过审)'
    # 审核类动词 + 通过/批准/核准/完成。
    r'|(?:审核|审批|批准|核准|过审)[^。；\n]{0,4}?(?:通过|批准|核准|完成)'
    # 通过 + 审核/审批（动宾倒序）。
    r'|(?:已)?通过[^。；\n]{0,4}?(?:审核|审批)'
    r'|已获(?:得)?[^。；\n]{0,6}?(?:批准|核准)'
    # 省主语/同义表达：已过审、已批准、已核准、已获批、审核过了、过了审、审核完毕、已审批。
    r'|已(?:过审|批准|核准|获批|审批)'
    r'|(?:审核|审批)已?过'
    r'|过了(?:审|审批)'
    r'|(?:审核|审批)完毕',
)
# 执行类：广告/投放/预算/扣费/推广的创建、启动、上线、调整与完成语序（必须带「已」）。
_EXECUTION_CLAIM_RE = re.compile(
    r'(?:广告|投放|预算|扣费|推广)[^。；\n]{0,4}?已(?:创建|投放|启动|开始|开启|上线|扣费|调整|修改|设置|完成)'
    r'|已(?:创建|投放|启动|开始|开启|上线|扣费|调整|修改|设置)[^。；\n]{0,4}?(?:广告|投放|预算|扣费|推广)'
    r'|已(?:投放|扣费|上线)',
)
# 直接修饰 claim 的否定词（出现在匹配段内部或紧邻之前）。
_CLAIM_NEGATIVE = re.compile(r'[未不待驳拒没]|尚未|暂停|正在|审核中|审批中|准备')
_CLAIM_NEGATIVE_HEAD = re.compile(
    r'(?:未被|没被|不被|未|没|不|待|遭|驳回|拒绝|正在|尚未|暂停|准备)$'
)
# claim 之后紧接的否定/拒绝/说明性延伸。
_CLAIM_NON_CLAIM_TAIL = re.compile(
    r'^(?:不通过|未通过|没通过|没有通过|被驳回|被拒绝|驳回|拒绝|未获通过|中|率|流程|规则|标准|要求|周期)'
)
# claim 之后紧接的条件/结果标记（「通过后才能…」「通过后再…」）。
_CLAIM_CONDITION_TAIL = re.compile(
    r'^(?:后才|后才能|后方可|后再|后，?\s*(?:才|方可|再|则)|则)'
)
# 英文/泰语的常见执行、审核、认证声明。只覆盖列出的常见句式，不穷尽自然语言；
# 命中后按直接修饰该声明的否定结构判断，不按整句或分句关键词豁免。
_EN_PLATFORM_CLAIM_RE = re.compile(
    r"(?i)\b(?:fda|ce|iso|organic|platform|tiktok|shopee|authorit(?:y|ies))\s+"
    r"(?:has\s+been\s+|have\s+been\s+|was\s+|were\s+|is\s+|are\s+)?(?:approved|certified)\b"
    r"|\bapproved\s+by\s+(?:the\s+)?(?:platform|fda|authorit(?:y|ies)|tiktok|shopee)\b"
)
_EN_EXECUTION_CLAIM_RE = re.compile(
    r"(?i)\b(?:has|have|had|was|were|is|are)\s+(?:been\s+)?"
    r"(?:launched|published|aired|created|approved|live)\b"
    r"|\b(?:campaign|ad|advertisement|budget)\s+(?:is\s+|was\s+|has\s+been\s+)?"
    r"(?:live|running|launched|created|adjusted)\b"
    r"|\bwe\s+(?:launched|created|adjusted|set)\b"
)
_TH_PLATFORM_CLAIM_RE = re.compile(r'ได้รับการอนุมัติ|ผ่านการอนุมัติ|อนุมัติแล้ว|ได้รับการรับรอง')
_TH_EXECUTION_CLAIM_RE = re.compile(r'เปิดตัวแล้ว|เผยแพร่แล้ว|เริ่มโฆษณาแล้ว|สร้างแคมเปญแล้ว|ปรับงบแล้ว')

# 凭据痕迹的**识别**口径：只认真正的认证 header、足够像 token 的取值和密钥字面量。
# 不复用 ``security.redaction`` 的清洗正则——那里为了脱敏宁滥勿缺，会把文案里
# 的 "Sign: gift wrap" 之类普通短语当成密钥；识别口径过宽会误判合法文案。
_CREDENTIAL_HEADER = re.compile(
    r'(?i)(?:^|[\s"\'{,&?])["\']?(?:authorization|proxy-authorization|'
    r'x-[\w-]*(?:token|secret|key)[\w-]*)["\']?\s*[:=]\s*\S',
)
# Cookie 只有在键值对形态下才算凭据：文案里讲 Cookie 政策不算泄密。
_CREDENTIAL_COOKIE = re.compile(
    r'(?i)(?:^|[\s"\'{,&?])["\']?(?:cookie|set-cookie)["\']?\s*[:=]\s*[^=\s]+=[^=\s]+',
)
# 裸认证方案（没有 header 名）：Bearer 后跟足够长的 token。不含 Basic——
# "Basic information/capacity" 是常见英文文案，误伤比命中多。
_CREDENTIAL_BEARER = re.compile(r'(?i)\b(?:bearer)\s+[A-Za-z0-9_.\-=+/]{8,}')
# 明确的凭据参数：键名出现在 JSON 引号里也认；取值 3 个字符起。
_CREDENTIAL_QUERY_STRONG = re.compile(
    r'(?i)(?:^|[\s"\'{,&?])["\']?(?:access_token|refresh_token|api[_-]?key|secret[_-]?key|app_secret|'
    r'shop_cipher)["\']?\s*[:=]\s*["\']?([A-Za-z0-9_.\-+=]{3,})',
)
# 无歧义上下文（URL query 的 ?/& 之后、JSON 引号键）里的 token/sign/secret 直接当凭据，
# 不要求含数字：`?sign=ABCDEFGH`、`{"secret":"abcdefgh"}` 都要拦。
_CREDENTIAL_QUERY_CONTEXTUAL = re.compile(
    r'(?i)(?:\?|&)["\']?(?:token|app_key|sign|signature|secret)["\']?\s*[:=]\s*["\']?'
    r'([A-Za-z0-9_.\-+=]{8,})'
    r'|(?:^|[\s{,])["\'](?:token|app_key|sign|signature|secret)["\']\s*[:=]\s*["\']?'
    r'([A-Za-z0-9_.\-+=]{8,})',
)
# 常见但易误判的词：token/app_key/sign/signature/secret/password/passwd。
# 取值需 8 位以上且含数字，这样 "Password: minimum length"、"Signature: hand-woven"
# 不会被当成密钥，而 "password=abc12345" 会。
_CREDENTIAL_QUERY_GUESSY = re.compile(
    r'(?i)(?:^|[\s"\'{,&?])["\']?(?:token|app_key|sign|signature|secret|password|passwd)["\']?'
    r'\s*[:=]\s*["\']?([A-Za-z0-9_.\-+=]{8,})',
)
_CREDENTIAL_LITERAL = re.compile(r'\b(?:sk-[A-Za-z0-9]{16,}|LTAI[0-9A-Za-z]{12,})\b')

_FENCE = re.compile(r'^\s*```', re.MULTILINE)
_BLOCK = re.compile(r'```(?:text|json)?\s*\n(.*?)```', re.DOTALL)
# 声明扫描的豁免字段：这里是模型写注意事项、核验台账或修改项的地方，引用不等于对外声明。
# 平台/执行声明不把其中的引用当成自身的声明；买家 PII 与密钥不在此豁免（写进台账一样是泄漏）。
META_KEYS = frozenset({
    'warnings',
    'missing',
    'missing_evidence',
    'data_gaps',
    'questions_for_operator',
    'claims_removed',
    'claims',
    'unsafe_claims',
    'corrections',
    'issues',
    'required_edits',
})

# 契约里由校验器给出的码（pydantic 会把 ValueError 文本并入 CONTRACT 详情），
# 命中时仍按原码报出，便于按码断言与定位。
_VALIDATOR_CODES = (CODE_TRACKING_FIELDS,)


class WorkModelError(ValueError):
    """契约或护栏不通过；带上已落库的调用记录 id（未落库时为 None）。"""

    def __init__(self, code: str, message: str, record_id: str | None = None) -> None:
        super().__init__(f'{code}: {message}')
        self.code = code
        self.message = message
        self.record_id = record_id


# ---- 提示词包 ----

@dataclass(frozen=True)
class PromptPack:
    system: str
    modules: dict[str, str]
    raw: str


def pack_path() -> Path:
    """仓库根下的提示词包；与 ``orchestrator.outreach`` 同一种取法。"""
    return Path(__file__).resolve().parents[3] / PACK_FILE


@lru_cache(maxsize=4)
def load_pack(text: str | None = None) -> PromptPack:
    """按二级标题切分提示词包。标题缺失或章节为空即报合同错误，不静默降级。"""
    raw = text if text is not None else pack_path().read_text(encoding='utf-8')
    sections: dict[str, str] = {}
    title: str | None = None
    body: list[str] = []
    for line in raw.splitlines():
        if line.startswith('## '):
            if title is not None:
                sections[title] = '\n'.join(body).strip()
            title = line[3:].strip()
            body = []
        elif title is not None:
            body.append(line)
    if title is not None:
        sections[title] = '\n'.join(body).strip()

    system_section = sections.get(SYSTEM_SECTION, '')
    match = _BLOCK.search(system_section)
    system = match.group(1).strip() if match else ''
    if not system:
        raise WorkModelError(CODE_PACK, f'{PACK_FILE} 缺少「{SYSTEM_SECTION}」代码块')

    modules: dict[str, str] = {}
    for heading, module in MODULE_TITLES.items():
        prompt = sections.get(heading, '')
        if not prompt:
            raise WorkModelError(CODE_PACK, f'{PACK_FILE} 缺少「{heading}」章节')
        modules[module] = prompt
    return PromptPack(system=system, modules=modules, raw=raw)


def system_prompt() -> str:
    return load_pack().system


def pack_version() -> str:
    """提示词包正文声明的版本（首行「版本 vX.Y」）；与 PROMPT_VERSION 一起校验。"""
    match = re.search(r'版本\s*v(\d+\.\d+)', load_pack().raw)
    if not match:
        raise WorkModelError(CODE_PACK, f'{PACK_FILE} 首部没有「版本 vX.Y」声明')
    return match.group(1)


def module_prompt(module: Module) -> str:
    return load_pack().modules[module]


# ---- 输出契约 ----

class Record(BaseModel):
    model_config = ConfigDict(extra='forbid', populate_by_name=True, str_strip_whitespace=True)


# 数组保留三态：缺字段或显式 null 表示「未知」，不归一成空数组——
# 把 data_gaps/missing 的 null 吞成 [] 等于宣称「没有缺口」。
StrList = list[str] | None
Money = Annotated[float, Field(ge=0, le=1e12, allow_inf_nan=False)]
Count = Annotated[int, Field(ge=0, le=10**12, strict=True)]
Ratio = Annotated[float, Field(ge=0, allow_inf_nan=False)]
Currency = Annotated[str, Field(pattern=r'^[A-Z]{3}$')]


class Cost(Record):
    amount: Money | None = None
    currency: Currency | None = None


class ResearchOutput(Record):
    name: str | None = None
    source_url: str | None = None
    facts: StrList = None
    cost: Cost = Field(default_factory=Cost)
    strengths: StrList = None
    risks: StrList = None
    missing: StrList = None
    recommendation: Literal['researching', 'shortlisted', 'rejected'] = 'researching'
    reason: str = ''
    warnings: StrList = None
    assumptions: StrList = None


class Claim(Record):
    claim: str = ''
    status: Literal['supported', 'unsupported', 'contradicted'] = 'unsupported'
    evidence: str | None = None


class VerifyOutput(Record):
    decision: Literal['pass', 'needs_review', 'reject'] = 'needs_review'
    claims: list[Claim] = Field(default_factory=list)
    missing_evidence: StrList = None
    unsafe_claims: StrList = None
    corrections: StrList = None
    warnings: StrList = None


class CopyOutput(Record):
    title_th: str = ''
    body_th: str = ''
    title_en: str = ''
    body_en: str = ''
    fact_used: StrList = None
    claims_removed: StrList = None
    missing: StrList = None
    warnings: StrList = None
    source_note: str = ''


class QualityChecks(Record):
    facts_supported: bool = False
    language_ok: bool = False
    sensitive_claims_checked: bool = False
    copyright_source_present: bool = False


class QualityOutput(Record):
    decision: Literal['pass', 'needs_review', 'reject'] = 'needs_review'
    checks: QualityChecks = Field(default_factory=QualityChecks)
    issues: StrList = None
    required_edits: StrList = None
    warnings: StrList = None


class CampaignOutput(Record):
    # platform 是事实字段，缺省不得替模型断言平台（未知按契约失败）。
    platform: Literal['tiktok', 'shopee', 'other']
    name: str = ''
    objective: str | None = None
    audience_hypotheses: StrList = None
    selected_asset_ids: StrList = None
    budget_note: str | None = None
    execution_checklist: StrList = None
    tracking_fields: list[str] = Field(default_factory=lambda: list(TRACKING_FIELDS))
    missing: StrList = None
    warnings: StrList = None
    status: Literal['draft'] = 'draft'

    @field_validator('tracking_fields')
    @classmethod
    def _tracking_fields_complete(cls, value: list[str]) -> list[str]:
        absent = [name for name in TRACKING_FIELDS if name not in value]
        if absent:
            raise ValueError(f'{CODE_TRACKING_FIELDS}: 缺少跟踪字段 {absent}')
        extra = [name for name in value if name not in TRACKING_FIELDS]
        if extra or len(set(value)) != len(value):
            raise ValueError(f'{CODE_TRACKING_FIELDS}: 跟踪字段只能是约定的八项，多出 {extra or "重复项"}')
        return value


class Scope(Record):
    campaign_id: str | None = None
    from_: str | None = Field(default=None, alias='from')
    to: str | None = None
    currency: Currency | None = None
    source: str | None = None


class KnownMetrics(Record):
    impressions: Count | None = None
    clicks: Count | None = None
    spend: Money | None = None
    revenue: Money | None = None
    orders: Count | None = None
    ctr: Ratio | None = None
    cpc: Ratio | None = None
    roas: Ratio | None = None


class MetricsOutput(Record):
    scope: Scope = Field(default_factory=Scope)
    known_metrics: KnownMetrics = Field(default_factory=KnownMetrics)
    observations: StrList = None
    data_gaps: StrList = None
    questions_for_operator: StrList = None
    next_actions: StrList = None
    warnings: StrList = None


NextStep = Literal[
    'research',
    'verify',
    'write_copy',
    'quality_check',
    'prepare_campaign',
    'record_metrics',
    'human_review',
    'blocked',
]


class OrchestratorOutput(Record):
    next_step: NextStep = 'human_review'
    reason: str = ''
    required_inputs: StrList = None
    artifact_ids: StrList = None
    risk_level: Literal['low', 'medium', 'high'] = 'medium'
    human_action: str = ''
    warnings: StrList = None


CONTRACTS: dict[str, type[Record]] = {
    'research': ResearchOutput,
    'verify': VerifyOutput,
    'write_copy': CopyOutput,
    'quality_check': QualityOutput,
    'prepare_campaign': CampaignOutput,
    'record_metrics': MetricsOutput,
    'orchestrator': OrchestratorOutput,
}


def model_class(module: Module) -> type[Record]:
    return CONTRACTS[module]


# ---- 解析 ----

def parse_output(module: Module, raw: str | bytes | dict[str, Any]) -> Record:
    """把模型原始输出解析成契约对象；Markdown、非 JSON、多字段都按失败处理。"""
    if isinstance(raw, dict):
        payload: Any = raw
    else:
        text = raw.decode('utf-8') if isinstance(raw, bytes) else raw
        if _FENCE.search(text):
            raise WorkModelError(CODE_MARKDOWN, '模型输出包含 Markdown 代码块，契约要求纯 JSON')
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise WorkModelError(CODE_INVALID_JSON, f'不是合法 JSON：{exc.msg}') from None
    try:
        return CONTRACTS[module].model_validate(payload)
    except ValidationError as exc:
        detail = '; '.join(
            f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
            for error in exc.errors(include_input=False, include_context=False)
        )
        # 校验器自定义码（如 tracking_fields 缺项）按原码报出，不淹没在 CONTRACT 里。
        for code in _VALIDATOR_CODES:
            if code in detail:
                raise WorkModelError(code, detail) from None
        raise WorkModelError(CODE_CONTRACT, detail) from None


def dump_output(output: Record) -> dict[str, Any]:
    return output.model_dump(mode='json', by_alias=True)


# ---- 确定性护栏 ----

def copy_asset_text(output: dict[str, Any] | None) -> str:
    """文案运行结果里的对外正文（泰文与英文标题/正文），用于版本绑定与素材落库。

    这是**服务端**从已持久化的结构化输出里取的，不接受客户端传入正文。
    """
    if not isinstance(output, dict):
        return ""
    parts = [
        str(output.get("title_th") or "").strip(),
        str(output.get("body_th") or "").strip(),
        str(output.get("title_en") or "").strip(),
        str(output.get("body_en") or "").strip(),
    ]
    return "\n".join(part for part in parts if part)


def copy_text_digest(text: str) -> str:
    """文案正文版本摘要；正文一改，摘要就变，旧质检不再背书。"""
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()


def expected_ratios(metrics: KnownMetrics) -> dict[str, float | None]:
    """CTR=clicks/impressions，CPC=spend/clicks，ROAS=revenue/spend；分母缺失或为零即 None。"""
    result: dict[str, float | None] = {}
    for key, numerator, denominator in (
        ('ctr', metrics.clicks, metrics.impressions),
        ('cpc', metrics.spend, metrics.clicks),
        ('roas', metrics.revenue, metrics.spend),
    ):
        value = None
        if numerator is not None and denominator and denominator > 0:
            value = numerator / denominator
        result[key] = value
    return result


def _strings(value: Any, skip: frozenset[str] = frozenset()):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            if key in skip:
                continue
            yield from _strings(item, skip)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _strings(item, skip)


# 服务端生成的**机器 ID**形态：uuid4 的 32 位十六进制。
#
# 为什么需要单列：``security.redaction.sniff_content`` 里的号码类规则
# （bank_card 13~19 位数字、th_mobile/cn_mobile 等）会把 hex 串里一段连续数字
# 当成银行卡号——32 位 hex 约 3% 概率出现 13 位以上连续数字。而工作模型输出里
# ``selected_asset_ids`` / ``artifact_ids`` 就是服务端自己的对象 ID，模型只是
# 原样回填。护栏拦它等于**随机拒绝合法计划**：同一候选换个素材就可能报
# WORK_MODEL_SENSITIVE_LEAK，用户看到「计划建议未通过」但查不出原因。
#
# 豁免口径严格限定在「整串就是一个 ID」：真正的手机号 11 位、身份证 18 位纯数字
# 都不满足这个形态，仍然照拦（见 test_real_pii_in_id_looking_fields_is_still_rejected）。
_MACHINE_ID_RE = re.compile(r'\A[0-9a-f]{32}\Z')


def _is_machine_id(text: str) -> bool:
    """该字符串整体就是服务端生成的机器 ID。"""
    return bool(_MACHINE_ID_RE.match(text.strip()))


def _claim_hit(output: Record, pattern: re.Pattern[str]) -> bool:
    """在非台账字段里找「已审核/已执行」类宣称。

    否定与条件只按**直接结构**判定：匹配段内部出现否定词、紧邻之前一个否定词、
    紧接之后是「不通过/没通过/被驳回/中/率/流程…」或「后才/后再/后方可…」条件标记。
    匹配之后的「待上架 / 如需调整 / 不需要再提交」属于后续从句，不豁免前半句的宣称。
    """
    text = '\n'.join(_strings(dump_output(output), META_KEYS))
    normalized = text.replace('已经', '已')
    for match in pattern.finditer(normalized):
        segment = match.group(0)
        if _CLAIM_NEGATIVE.search(segment):
            continue
        head = normalized[max(0, match.start() - 4):match.start()]
        if _CLAIM_NEGATIVE_HEAD.search(head) or re.search(r'没有|并非|未曾|尚未', head):
            continue
        tail = normalized[match.end():match.end() + 8]
        if _CLAIM_NON_CLAIM_TAIL.search(tail) or '通过率' in segment + tail:
            continue
        if _CLAIM_CONDITION_TAIL.search(tail):
            continue
        return True
    return False


# 分句边界用来限定「直接修饰」的查找范围：否定词必须紧邻该声明才豁免，
# 后半句或无关修饰语（without/before/时间）不构成否定。
_CLAUSE_SPLIT = re.compile(
    r'(?i)\b(?:and|but|while|although|whereas|however|yet|or)\b|[,;.!?。！？\n]|(?:และ|แต่|หรือ|โดย)'
)


# Only explicit, adjacent negation of the matched claim is exempted. Temporal
# adjuncts (before noon) and absence of side effects never negate execution/cure.
_EN_CLAIM_PREFIX = re.compile(
    r'(?i)\b(?:(?:not|never)(?:\s+(?:claim|promise|guarantee|assert))?|no|without)'
    r'(?:\s+(?:a|an|any))?\s*$'
)
_EN_CLAIM_SUFFIX = re.compile(
    r'(?i)^\s+(?:is|are|was|were)\s+(?:not|never)\s+'
    r'(?:claimed|promised|guaranteed|asserted|confirmed)\b'
)
_TH_CLAIM_PREFIX = re.compile(r'(?:ยังไม่ได้|ไม่ได้|ยังไม่|ไม่)\s*$')


def _claim_is_negated(text: str, match: re.Match[str]) -> bool:
    """Check negation attached to this claim, never an arbitrary word in its clause."""
    left, right = 0, len(text)
    for separator in _CLAUSE_SPLIT.finditer(text):
        if separator.end() <= match.start():
            left = separator.end()
        elif separator.start() >= match.end():
            right = separator.start()
            break
    head, tail = text[left:match.start()], text[match.end():right]
    if re.search(r'[ก-๙]', match.group(0)):
        return bool(_TH_CLAIM_PREFIX.search(head))
    if re.search(r'[A-Za-z]', match.group(0)):
        return bool(_EN_CLAIM_PREFIX.search(head) or _EN_CLAIM_SUFFIX.search(tail))
    return False


def _multilingual_claim_hit(output: Record, patterns: tuple[re.Pattern[str], ...]) -> bool:
    """Scan each claim separately; modifiers elsewhere cannot exempt it."""
    text = '\n'.join(_strings(dump_output(output), META_KEYS))
    return any(
        not _claim_is_negated(text, match)
        for pattern in patterns for match in pattern.finditer(text)
    )


def _guard_platform_claims(output: Record, context: dict[str, Any]) -> list[str]:
    hit = (
        _claim_hit(output, _PLATFORM_CLAIM_RE)
        or _multilingual_claim_hit(output, (_EN_PLATFORM_CLAIM_RE,))
        or _multilingual_claim_hit(output, (_TH_PLATFORM_CLAIM_RE,))
    )
    return [CODE_PLATFORM_CLAIM] if hit else []


def _absolute_claim_violations(body: str, allowed: str) -> bool:
    """正文里出现没有来源支撑的绝对化/认证/功效措辞。

    ``allowed`` 是被服务端认定为可用的来源文本（候选事实与有效核验声明）；命中片段
    逐字出现在其中才算有支撑，其余按未支撑处理。
    """
    allowed_fold = str(allowed or '').casefold()
    for match in _ABSOLUTE_CLAIM_RE.finditer(body):
        fragment = match.group(0)
        if fragment.casefold() in allowed_fold:
            continue
        if _claim_is_negated(body, match):
            continue
        return True
    return False


def _guard_copy_claims(output: Record, context: dict[str, Any]) -> list[str]:
    """只扫对外文案正文；claims_removed 是「被删掉的措辞」清单，不是文案本身。"""
    if not isinstance(output, CopyOutput):
        return []
    body = '\n'.join([output.title_th, output.body_th, output.title_en, output.body_en])
    if _absolute_claim_violations(body, str(context.get('allowed_text') or '')):
        return [CODE_UNGROUNDED_CLAIM]
    return []


def _as_verify(value: Any) -> VerifyOutput | None:
    """核验上下文可以是 VerifyOutput，也可以是上一步 ``run()`` 返回的 JSON 字典。

    空对象或没有显式 decision 的字典视为「没有核验信息」，不能被当成核验通过。
    """
    if isinstance(value, VerifyOutput):
        return value
    if isinstance(value, dict):
        if not value or 'decision' not in value:
            return None
        try:
            return VerifyOutput.model_validate(value)
        except ValidationError:
            return None
    return None


def _claim_has_evidence(claim: Claim) -> bool:
    """supported 必须有非空证据文本；null/空串/纯空白都按未证实处理。"""
    return claim.status == 'supported' and bool((claim.evidence or '').strip())


def _guard_copy_grounding(output: Record, context: dict[str, Any]) -> list[str]:
    """文案只能建立在**有证据的核验结论**上。

    - 正文全空且没有 ``fact_used``：空草稿，不做断言，放行。
    - 否则要求核验上下文存在、`decision == pass`、`missing_evidence` 为空；
    - 每一条 supported 声明都必须带非空 evidence，否则按未证实处理；
    - 未证实/相矛盾/unsafe_claims 的措辞不得出现在 ``fact_used`` 或四个正文里；
    - 非空正文至少要有**一条**可用（supported + 有证据）的声明背书：只有
      `decision=pass` 但没有任何支持的核验结论，不能给事实正文背书；
    - ``fact_used`` 非空时，同样要求存在可用声明可对应。
    """
    if not isinstance(output, CopyOutput):
        return []
    texts = [output.title_th, output.body_th, output.title_en, output.body_en]
    used = ' '.join(output.fact_used or []).casefold()
    if not any(text.strip() for text in texts) and not used:
        return []
    verify = _as_verify(context.get('verify'))
    if verify is None:
        return [CODE_VERIFY_REQUIRED]
    if verify.decision != 'pass' or verify.missing_evidence:
        return [CODE_VERIFY_REQUIRED]
    claims = verify.claims or []
    usable = [claim for claim in claims if _claim_has_evidence(claim)]
    risky = [claim.claim for claim in claims if not _claim_has_evidence(claim)]
    risky.extend(verify.unsafe_claims or [])
    body = '\n'.join(texts).casefold()
    for text in risky:
        phrase = (text or '').strip().casefold()
        if phrase and (phrase in used or phrase in body):
            return [CODE_CLAIM_LEAKED]
    # 核验结论里有 supported 却缺证据：该结论不能为任何文案背书。
    if len(usable) != len(claims):
        return [CODE_VERIFY_REQUIRED]
    # 非空正文（无论是否填 fact_used）都必须至少有一条可用声明背书。
    if not usable:
        return [CODE_EVIDENCE_REQUIRED]
    return []


def _guard_verify_decision(output: Record, context: dict[str, Any]) -> list[str]:
    """核验结论为 pass 时，每条 supported 都必须带证据；否则 pass 不成立。"""
    if not isinstance(output, VerifyOutput) or output.decision != 'pass':
        return []
    if output.unsafe_claims or output.missing_evidence:
        return [CODE_VERIFY_UNSAFE_PASS]
    if any(not _claim_has_evidence(claim) for claim in output.claims or []):
        return [CODE_VERIFY_UNSAFE_PASS]
    return []


def _guard_research_cost(output: Record, context: dict[str, Any]) -> list[str]:
    if isinstance(output, ResearchOutput) and output.cost.amount is not None and output.cost.currency is None:
        return [CODE_CURRENCY_REQUIRED]
    return []


def _looks_like_secret(text: str) -> bool:
    if (
        _CREDENTIAL_HEADER.search(text)
        or _CREDENTIAL_COOKIE.search(text)
        or _CREDENTIAL_BEARER.search(text)
        or _CREDENTIAL_QUERY_STRONG.search(text)
        or _CREDENTIAL_QUERY_CONTEXTUAL.search(text)
        or _CREDENTIAL_LITERAL.search(text)
    ):
        return True
    match = _CREDENTIAL_QUERY_GUESSY.search(text)
    if match and any(char.isdigit() for char in match.group(1)):
        return True
    return False


def _guard_sensitive(output: Record, context: dict[str, Any]) -> list[str]:
    """买家 PII、密钥与 Cookie 不得出现在模型产物里；元信息字段同样不得夹带。

    PII 走 ``security.redaction.sniff_content``；凭据按本地窄口径识别（认证 header、
    带足够长度取值的凭据参数、sk-/LTAI 字面量），避免把普通文案短语误判成密钥。

    整串就是服务端机器 ID（uuid4 hex）的值不按号码类 PII 判——那是服务端自己的
    对象标识（``selected_asset_ids`` / ``artifact_ids``），不是买家信息；按号码
    规则扫会随机误伤（32 位 hex 约 3% 概率含 13 位以上连续数字）。凭据检查对该
    值仍然照做：机器 ID 形态不构成凭据豁免。
    """
    for text in _strings(dump_output(output)):
        if _looks_like_secret(text):
            return [CODE_SENSITIVE_LEAK]
        if _is_machine_id(text):
            continue
        if sniff_content(text):
            return [CODE_SENSITIVE_LEAK]
    return []


def _guard_quality_source(output: Record, context: dict[str, Any]) -> list[str]:
    """质检 pass 必须四项检查全部为真；缺项/显式 false 都按未通过处理。

    不支持 ``not_applicable``：四项检查是 pass 的必要条件，缺检查信息时默认 False，
    不会被默认值洗成通过。

    模型布尔值不能覆盖服务端已知缺失：上下文里的 ``copyright_source`` 为空（版权
    来源确实缺失）或 ``facts_backed`` 为假（没有有效核验可背书）时，即使模型把
    对应布尔值写成 true，pass 也不成立。
    """
    if not isinstance(output, QualityOutput) or output.decision != 'pass':
        return []
    checks = output.checks
    if not checks.copyright_source_present:
        return [CODE_SOURCE_MISSING]
    if 'copyright_source' in context and not str(context.get('copyright_source') or '').strip():
        return [CODE_SOURCE_MISSING]
    if context.get('facts_backed') is False:
        return [CODE_VERIFY_REQUIRED]
    required = (checks.facts_supported, checks.language_ok, checks.sensitive_claims_checked)
    if not all(required):
        return [CODE_QUALITY_CHECKS]
    return []


def _guard_campaign_execution(output: Record, context: dict[str, Any]) -> list[str]:
    hit = (
        _claim_hit(output, _EXECUTION_CLAIM_RE)
        or _multilingual_claim_hit(output, (_EN_EXECUTION_CLAIM_RE,))
        or _multilingual_claim_hit(output, (_TH_EXECUTION_CLAIM_RE,))
    )
    return [CODE_EXECUTION_CLAIM] if hit else []


def _guard_campaign_assets(output: Record, context: dict[str, Any]) -> list[str]:
    """计划助手只能引用本次输入的素材。

    服务端把本次输入的素材 ID 集合放进上下文的 ``allowed_asset_ids``；模型返回的
    ``selected_asset_ids`` 必须是它的子集。擅自增加关联（哪怕同店存在）也算拒绝——
    模型没有资格扩充自己的输入范围。
    """
    if not isinstance(output, CampaignOutput):
        return []
    allowed = context.get('allowed_asset_ids')
    if allowed is None:
        return []
    allowed_set = {str(item) for item in allowed}
    for asset_id in output.selected_asset_ids or []:
        if str(asset_id) not in allowed_set:
            return [CODE_ASSET_OUT_OF_SCOPE]
    return []


def _guard_metrics(output: Record, context: dict[str, Any]) -> list[str]:
    if not isinstance(output, MetricsOutput):
        return []
    violations: list[str] = []
    expected = expected_ratios(output.known_metrics)
    for key, want in expected.items():
        got = getattr(output.known_metrics, key)
        if got is None:
            # 分母齐备却报 null：把可算的比率说成未知，同样是错报。
            if want is not None:
                violations.append(CODE_METRIC_MISMATCH)
            continue
        if want is None:
            violations.append(CODE_METRIC_UNCOMPUTABLE)
        elif abs(got - want) > max(1e-9, 1e-6 * abs(want)):
            violations.append(CODE_METRIC_MISMATCH)
    if output.scope.currency is None and (
        output.known_metrics.spend is not None or output.known_metrics.revenue is not None
    ):
        violations.append(CODE_CURRENCY_REQUIRED)
    return violations


# 服务端读到的原始指标与模型回传的差异容忍度。计数与金额必须**逐值相等**；
# 比率由服务端用原始指标重算，模型值只做等价比较。
_BASE_MEASURES = ('impressions', 'clicks', 'spend', 'revenue', 'orders')


def _measure_equal(got: Any, want: Any) -> bool:
    """原始指标的逐值比较。

    「缺失」与「零」是不同状态：服务端读到 None（未知）时，模型回传 0 属于改写
    数据；服务端读到 0 时，模型回传 None 属于把真实的零说成未知。两者都拒绝。
    """
    if got is None or want is None:
        return got is None and want is None
    if isinstance(got, bool) or isinstance(want, bool):
        return got == want
    try:
        return abs(float(got) - float(want)) <= max(1e-9, 1e-6 * abs(float(want)))
    except (TypeError, ValueError):
        return False


def _guard_metrics_not_tampered(output: Record, context: dict[str, Any]) -> list[str]:
    """模型不得改写服务端读到的原始指标。

    上下文里的 ``server_metrics`` 是**服务端从数据库读出**的基础指标，按币种分组。
    服务端只对单一币种发起复盘（不同币种不得相加），因此正常情况下只有一组。
    模型把 spend 改成别的数、或者把「缺失」补成 0，都按改写数据拒绝——即使模型
    自己报出的 CTR/CPC/ROAS 内部自洽。自洽的错数必须同样被挡住：内部自洽只
    证明模型算术没错，不证明它读对了原始数据。
    """
    if not isinstance(output, MetricsOutput):
        return []
    server_metrics = context.get('server_metrics')
    if not isinstance(server_metrics, dict) or not server_metrics:
        return []
    if len(server_metrics) > 1:
        # 多币种范围由服务端在上游拒绝，不让模型在混合口径上做复盘。
        return [CODE_METRICS_SCOPE_MISMATCH]
    currency, want_values = next(iter(server_metrics.items()))
    if not isinstance(want_values, dict):
        return []
    got_values = output.known_metrics.model_dump(mode='json')
    for measure in _BASE_MEASURES:
        if measure not in want_values:
            continue
        want = want_values.get(measure)
        got = got_values.get(measure)
        if not _measure_equal(got, want):
            return [CODE_METRICS_TAMPERED]
    # 模型报告的币种必须与服务端读到的币种一致。
    scope_currency = output.scope.currency
    if scope_currency is not None and str(scope_currency) != str(currency):
        return [CODE_METRICS_SCOPE_MISMATCH]
    return []


def _guard_metrics_scope(output: Record, context: dict[str, Any]) -> list[str]:
    """复盘范围必须落在服务端实际读取的范围内。

    服务端读到的范围是 ``(campaign_id, from, to, currency, source)`` 的组合；
    模型报告的 ``scope`` 若与之冲突（换了计划、换了日期、换了来源），说明复盘
    讲的不是这份数据，按范围不匹配拒绝。模型**未填**某项不算冲突——未知就留空。
    """
    if not isinstance(output, MetricsOutput):
        return []
    scope = context.get('query_scope')
    if not isinstance(scope, dict):
        return []
    for key in ('campaign_id', 'from', 'to', 'currency', 'source'):
        want = scope.get(key)
        if want in (None, ''):
            continue
        got = getattr(output.scope, key, None)
        if got in (None, ''):
            continue
        if key == 'campaign_id':
            # 服务端可能读了候选名下多个计划；模型报其中任一即为同一范围。
            allowed_ids = {str(x) for x in (scope.get('campaign_ids') or [])}
            if str(got) != str(want) and str(got) not in allowed_ids:
                return [CODE_METRICS_SCOPE_MISMATCH]
            continue
        if str(got) != str(want):
            return [CODE_METRICS_SCOPE_MISMATCH]
    return []


_GATE_KEYS = frozenset({
    'parseable',
    'cross_shop',
    'involves_money_or_pii',
    'facts_sufficient',
    'sensitive_claims',
    'copyright_missing',
    'currency_unknown',
})


def _guard_route(output: Record, context: dict[str, Any]) -> list[str]:
    """总控裁决必须与确定性前置裁决一致；调用方给出裁决输入时强制校验。"""
    if not isinstance(output, OrchestratorOutput):
        return []
    gate = context.get('gate')
    if isinstance(gate, dict):
        # 只认 route() 的裁决输入；多传的键是调用方笔误，不该把整次调用炸成 500。
        decision = route(**{key: value for key, value in gate.items() if key in _GATE_KEYS})
        if decision != 'ok' and output.next_step not in {'human_review', 'blocked'}:
            return [CODE_ROUTE_UNSAFE]
    if output.risk_level == 'high' and output.next_step not in {'human_review', 'blocked'}:
        return [CODE_ROUTE_UNSAFE]
    return []


_GLOBAL_GUARDS = (_guard_platform_claims, _guard_campaign_execution, _guard_sensitive)
GUARDS = {
    'research': (*_GLOBAL_GUARDS, _guard_research_cost),
    'verify': (*_GLOBAL_GUARDS, _guard_verify_decision),
    'write_copy': (*_GLOBAL_GUARDS, _guard_copy_claims, _guard_copy_grounding),
    'quality_check': (*_GLOBAL_GUARDS, _guard_quality_source),
    'prepare_campaign': (*_GLOBAL_GUARDS, _guard_campaign_assets),
    'record_metrics': (
        *_GLOBAL_GUARDS,
        _guard_metrics,
        _guard_metrics_not_tampered,
        _guard_metrics_scope,
    ),
    'orchestrator': (*_GLOBAL_GUARDS, _guard_route),
}


def validate_output(module: Module, output: Record, context: dict[str, Any] | None = None) -> list[str]:
    """返回违反码列表；空列表表示通过。同一码只报一次，按护栏顺序。"""
    scope = context or {}
    codes: list[str] = []
    for guard in GUARDS[module]:
        for code in guard(output, scope):
            if code not in codes:
                codes.append(code)
    return codes


def route(
    *,
    parseable: bool = True,
    cross_shop: bool = False,
    involves_money_or_pii: bool = False,
    facts_sufficient: bool = True,
    sensitive_claims: bool = False,
    copyright_missing: bool = False,
    currency_unknown: bool = False,
) -> Literal['ok', 'human_review', 'blocked']:
    """总控编排器的确定性前置裁决：无法解析或跨店对象一律 blocked。"""
    if not parseable or cross_shop:
        return 'blocked'
    if (
        involves_money_or_pii
        or not facts_sufficient
        or sensitive_claims
        or copyright_missing
        or currency_unknown
    ):
        return 'human_review'
    return 'ok'


# ---- 调用记录与运行 ----

class CallRecord(Record):
    module: Module
    prompt_version: str = PROMPT_VERSION
    model_channel: str = 'mock'
    input_object_ids: StrList = None
    output_object_ids: StrList = None
    source: str = 'manual'
    human_review: bool = False
    human_changes: StrList = None
    failure_reason: str | None = None


class WorkModelResult(Record):
    module: Module
    output: dict[str, Any]
    record: dict[str, Any]


def _redacted(text: str) -> str:
    """调用记录的统一脱敏边界：返回给调用者与入库的文本走同一口径。"""
    return redact_spans(str(text))[0]


def _clean_record_payload(record: CallRecord, shop_id: str, record_id: str | None) -> dict[str, Any]:
    """按脱敏边界清洗调用记录；有 store 时优先使用已落库视图（它已清洗）。"""
    payload = record.model_dump(mode='json')
    for key in ('input_object_ids', 'output_object_ids', 'human_changes'):
        values = payload.get(key)
        payload[key] = [_redacted(item) for item in values] if values else values
    if payload.get('failure_reason'):
        payload['failure_reason'] = _redacted(payload['failure_reason'])
    payload['id'] = record_id
    payload['shop_id'] = shop_id
    return payload


def run(
    module: Module,
    raw: str | bytes | dict[str, Any],
    *,
    store: Any | None = None,
    shop_id: str = 'default',
    model_channel: str = 'mock',
    source: str = 'manual',
    input_object_ids: list[str] | None = None,
    output_object_ids: list[str] | None = None,
    human_review: bool = False,
    human_changes: list[str] | None = None,
    context: dict[str, Any] | None = None,
) -> WorkModelResult:
    """解析 → 护栏 → 落调用记录。失败也会落一条带 failure_reason 的记录后再抛出。

    返回的 ``record`` 与持久化记录共用同一条脱敏边界：有 store 时直接用落库视图，
    无 store 时也按同一口径清洗，避免调用者拿到未清洗的原始文本。
    """
    record = CallRecord(
        module=module,
        model_channel=model_channel,
        source=source,
        input_object_ids=list(input_object_ids or []),
        output_object_ids=list(output_object_ids or []),
        human_review=human_review,
        human_changes=list(human_changes or []),
    )
    try:
        output = parse_output(module, raw)
        codes = validate_output(module, output, context)
        if codes:
            raise WorkModelError(codes[0], '模型输出未通过护栏：' + ', '.join(codes))
    except WorkModelError as exc:
        exc.message = _redacted(exc.message)
        exc.args = (f'{exc.code}: {exc.message}',)
        exc.record_id = _persist(store, shop_id, record, failure_reason=f'{exc.code}: {exc.message}')[1]
        # 不保留原始异常链：标准日志面打印 traceback 时不应出现未清洗的原始消息。
        raise exc from None
    except Exception as exc:  # noqa: BLE001 - 任何失败都要留痕，不能只留契约错误
        failure = WorkModelError(CODE_INTERNAL, _redacted(f'{type(exc).__name__}: {exc}'))
        failure.record_id = _persist(store, shop_id, record, failure_reason=f'{failure.code}: {failure.message}')[1]
        raise failure from None
    saved, record_id = _persist(store, shop_id, record)
    payload = saved if saved is not None else _clean_record_payload(record, shop_id, record_id)
    return WorkModelResult(module=module, output=dump_output(output), record=payload)


def _persist(store: Any | None, shop_id: str, record: CallRecord, failure_reason: str | None = None):
    if store is None:
        return None, None
    cleaned = _clean_record_payload(record, shop_id, None)
    cleaned.pop('id', None)
    cleaned.pop('shop_id', None)
    saved = store.record_call(
        shop_id, cleaned, failure_reason=failure_reason if failure_reason is None else _redacted(failure_reason))
    return saved, str(saved.get('id') or '')


# ---- mock 通道：流程可用性的确定性证据，不代表模型质量 ----

def mock_output(module: Module) -> str:
    # platform 是必填事实字段；mock 用最不具体的 other，不替模型断言具体平台。
    model = CampaignOutput(platform='other') if module == 'prepare_campaign' else CONTRACTS[module]()
    payload = model.model_dump(mode='json', by_alias=True)
    warnings = payload.get('warnings')
    if not isinstance(warnings, list):
        payload['warnings'] = [MOCK_WARNING]
    else:
        warnings.append(MOCK_WARNING)
    return json.dumps(payload, ensure_ascii=False)
