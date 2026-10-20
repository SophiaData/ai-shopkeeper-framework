"""声明式流程定义：步骤、依赖、重试、是否必经人工审批。"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class RetryPolicy:
    """生成类步骤：最多 max_retries 次重试（总尝试 = max_retries+1），指数退避。"""

    max_retries: int = 2
    backoff_base_s: float = 0.05
    backoff_max_s: float = 2.0


@dataclass(frozen=True)
class StepDef:
    id: str
    depends_on: tuple[str, ...] = ()
    requires_approval: bool = False
    #: ai = 可走 LLM；execution = 平台写/外呼类（失败不自动重试，handler 须幂等可重入）；
    #: deterministic = 纯规则（副作用须稳定键 + upsert）
    kind: str = "deterministic"
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    description: str = ""


@dataclass(frozen=True)
class FlowDefinition:
    id: str
    steps: tuple[StepDef, ...]
    qa_max_rewrite_rounds: int = 2
    version: str = "1"

    def step(self, step_id: str) -> StepDef:
        for s in self.steps:
            if s.id == step_id:
                return s
        raise KeyError(step_id)

    def ordered_ids(self) -> list[str]:
        return [s.id for s in self.steps]


#: 后台时间线与 agent 目录共用的中文名。新增步必须在这里登记。
STEP_LABELS: dict[str, str] = {
    "scout": "选品官",
    "pricing": "定价",
    "stylist": "文案图片",
    "qa": "合规质检",
    "script": "短视频脚本",
    "outreach": "达人建联",
    "human_approval": "人工审批",
    "publish": "上架",
    "order_sync": "订单同步",
    "order_load": "订单载入",
    "dropship": "代发",
    "tracking": "物流",
    "cs": "客服",
}


#: 默认流水线：Scout → 定价 → Stylist → QA → 短视频脚本 → 达人建联草稿 → 八态审批 → 上架 → 订单 → 代发 → 物流 → 客服
PIPELINE_V1 = FlowDefinition(
    id="listing_to_care_v1",
    qa_max_rewrite_rounds=2,
    steps=(
        StepDef(
            "scout",
            kind="ai",
            description="选品官：1688 链接 → 商品事实 + 选品报告",
            retry=RetryPolicy(max_retries=0),  # 采集失败不重轰（A1）
        ),
        StepDef(
            "pricing",
            depends_on=("scout",),
            kind="ai",
            description="售价公式（占位费率）→ 建议 THB；LLM 只写理由，偏离阈值挂起审批",
            retry=RetryPolicy(max_retries=1),
        ),
        StepDef(
            "stylist",
            depends_on=("pricing",),
            kind="ai",
            description="listing 师：多语言文案 + 图片草稿",
            retry=RetryPolicy(max_retries=2),
        ),
        StepDef(
            "qa",
            depends_on=("stylist",),
            kind="deterministic",
            description="合规与质检；不通过则回 Stylist 重写",
        ),
        StepDef(
            "script",
            depends_on=("qa",),
            kind="ai",
            description="短视频编剧：分镜、Hook、封面；默认确定性模板，可选 LLM",
            retry=RetryPolicy(max_retries=2),
        ),
        StepDef(
            "outreach",
            depends_on=("script",),
            kind="ai",
            description="达人建联：泰语邀约草稿；默认确定性模板，可选 LLM；只入库待审，不发送",
            retry=RetryPolicy(max_retries=2),
        ),
        StepDef(
            "human_approval",
            depends_on=("outreach",),
            requires_approval=True,
            kind="deterministic",
            description="八态审批闸门（create_listing）；挂起待批",
        ),
        StepDef(
            "publish",
            depends_on=("human_approval",),
            kind="execution",
            description="上架（受 TTS_WRITE_*；默认可挂起 TTS_WRITE_BLOCKED）",
            retry=RetryPolicy(max_retries=0),
        ),
        StepDef(
            "order_sync",
            depends_on=("publish",),
            kind="deterministic",
            description="只读订单同步 stub（不履约）",
        ),
        StepDef(
            "dropship",
            depends_on=("order_sync",),
            requires_approval=True,
            kind="execution",
            description="代发草稿闸门（不自动付钱）",
            retry=RetryPolicy(max_retries=0),
        ),
        StepDef(
            "tracking",
            depends_on=("dropship",),
            requires_approval=True,
            kind="execution",
            description="物流回填（格式校验，查询默认关，须审批）",
            retry=RetryPolicy(max_retries=0),
        ),
        StepDef(
            "cs",
            depends_on=("tracking",),
            kind="deterministic",
            description="客服白名单草稿 stub（涉钱类必审）",
        ),
    ),
)

#: 后台订单履约独立 flow：只接受工作台已同步缓存里的真实订单，按 order_id + shop_id 幂等启动。
#:
#: 与 PIPELINE_V1 解耦的原因：演示流水线里的 ``order_sync`` 会生成 ``source=orchestrator_stub``
#: 的合成订单，代发/物流/客服步骤也带确定性演示兜底（例如缺少运单时用 run id 派生 JNT 单号）。
#: 真实履约流程不得把那些演示内容当成平台事实，因此单独声明步骤：
#: ``order_load``（只读缓存校验）→ ``dropship``（八态审批）→ ``tracking``（八态审批）。
#: 客服步在真实流程中暂不接入：现有 ``run_cs`` 会生成合成泰语入站消息，不能冒充真实平台消息。
ORDER_FULFILLMENT_V1 = FlowDefinition(
    id="order_fulfillment_v1",
    version="1",
    steps=(
        StepDef(
            "order_load",
            kind="deterministic",
            description="只读载入已同步订单缓存；拒绝演示/合成订单并校验店铺归属",
        ),
        StepDef(
            "dropship",
            depends_on=("order_load",),
            requires_approval=True,
            kind="execution",
            description="代发草稿 + 八态审批；当前只接 mock/人工通道，不真实下单、不付款",
            retry=RetryPolicy(max_retries=0),
        ),
        StepDef(
            "tracking",
            depends_on=("dropship",),
            requires_approval=True,
            kind="execution",
            description="物流回填 + 八态审批；缺少真实承运商/单号时挂起人工待办",
            retry=RetryPolicy(max_retries=0),
        ),
    ),
)


FLOWS: dict[str, FlowDefinition] = {
    PIPELINE_V1.id: PIPELINE_V1,
    ORDER_FULFILLMENT_V1.id: ORDER_FULFILLMENT_V1,
}
