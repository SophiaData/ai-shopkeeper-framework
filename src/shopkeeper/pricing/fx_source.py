"""FX 来源的解析与可信度标注（HANDOFF_PLAN P2-09）。

## 缺口（2026-10-16 实测）

`PricingConfig.fx_source` 此前是一个**纯装饰字符串**：只在配置里存一下、
在利润页显示成 `FX 5.0（env:FX_CNY_THB）`，**从未被解析或校验**。

于是：

```
PRICING_FX_SOURCE="完全编造的源"  →  原样接受，页面上照样显示
```

用户看到的是一个**看起来有出处**的汇率，而那个出处是假的。比不显示来源更危险 ——
不显示时人会警惕，显示了假来源反而让人放心。

## 本模块做什么

1. **解析** `PRICING_FX_SOURCE` 为结构化来源（`env:` / `manual:` 前缀）
2. 给出**可信度等级**（`manual` 手动填写 / `unknown` 无法识别）
3. 对无法识别的写法**明确标注 `unknown` 并给出提示**，而不是假装它是有效来源

## 不做什么

**不接任何在线汇率源**（plan 明确「不接付费数据源」，且本仓库在无凭据时
不应外呼）。本模块只是让「这个汇率是哪来的、可不可信」变得**诚实可见**。
"""

from __future__ import annotations

import os

from dataclasses import dataclass
from typing import Any

#: 已知来源前缀。`env:` 表示从环境变量读取，`manual:` 表示人工填写。
KNOWN_PREFIXES: tuple[str, ...] = ("env:", "manual:")

#: 可信度等级。
TRUST_MANUAL = "manual"
TRUST_UNKNOWN = "unknown"

#: 无法识别来源时的提示。写死在这里，避免各处措辞不一。
UNKNOWN_HINT = (
    "PRICING_FX_SOURCE 不是已知写法，无法确认汇率出处。"
    "请改用 `env:<变量名>` 或 `manual:<说明>`；在此之前请按手动维护的汇率对待。"
)


@dataclass(frozen=True)
class FxSource:
    """结构化的汇率来源。"""

    raw: str
    kind: str  # env / manual / unknown
    detail: str  # env 的变量名 / manual 的说明 / 空
    trust: str
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "raw": self.raw,
            "kind": self.kind,
            "detail": self.detail,
            "trust": self.trust,
            "note": self.note,
        }


def _looks_like_number(text: str) -> bool:
    try:
        return float(str(text).strip()) > 0
    except (TypeError, ValueError):
        return False


def parse_fx_source(raw: str | None, env: dict[str, str] | None = None) -> FxSource:
    """把 `PRICING_FX_SOURCE` 解析成结构化来源。

    - `env:FX_CNY_THB`  → kind=env，detail=FX_CNY_THB
    - `manual:店长手填`  → kind=manual，detail=店长手填
    - 空                → 按默认 `env:FX_CNY_THB`（与 `load_pricing_config` 同口径）
    - 其余              → kind=unknown，**带 note 提示**，不假装有效
    """
    text = str(raw or "").strip()
    if not text:
        text = "env:FX_CNY_THB"

    for prefix in KNOWN_PREFIXES:
        if text.startswith(prefix):
            kind = prefix.rstrip(":")
            detail = text[len(prefix) :].strip()
            if not detail:
                # `env:` 后面空 —— 说了来源类型却没说是哪个，等于没说。
                return FxSource(
                    raw=text,
                    kind="unknown",
                    detail="",
                    trust=TRUST_UNKNOWN,
                    note=f"`{prefix}` 后缺少名称，无法确认汇率出处。",
                )
            if kind == "env":
                # ⚠️ **必须核对变量真的存在且是数字**。
                #
                # 原先只要写了 `env:` 就判可信 —— 于是 `env:TOTALLY_MADE_UP_VAR`
                # 会显示成「来自环境变量 TOTALLY_MADE_UP_VAR」，而那个变量根本不存在。
                # 这**正是本模块要消灭的那类假出处**，只是升了一层：
                # 用户看到「有出处的汇率」，出处却是编的。
                #
                # 更常见的是**默认路径**：`load_pricing_config({})` 在 FX_CNY_THB 未设时
                # 用硬编码占位 5.0，却照样显示「来自环境变量 FX_CNY_THB」。
                environ = env if env is not None else os.environ
                if detail not in environ:
                    return FxSource(
                        raw=text,
                        kind="unknown",
                        detail=detail,
                        trust=TRUST_UNKNOWN,
                        note=(
                            f"环境变量 {detail} 未设置，无法确认汇率出处；"
                            "当前数值可能只是占位默认值。"
                        ),
                    )
                if not _looks_like_number(environ.get(detail, "")):
                    return FxSource(
                        raw=text,
                        kind="unknown",
                        detail=detail,
                        trust=TRUST_UNKNOWN,
                        note=(
                            f"环境变量 {detail} 的值不是正数，无法作为汇率使用。"
                        ),
                    )
            return FxSource(
                raw=text,
                kind=kind,
                detail=detail,
                trust=TRUST_MANUAL,
                note="",
            )

    return FxSource(raw=text, kind="unknown", detail="", trust=TRUST_UNKNOWN, note=UNKNOWN_HINT)


def fx_source_is_trusted(raw: str | None, env: dict[str, str] | None = None) -> bool:
    """来源是否**可核实**（不代表汇率本身正确）。"""
    return parse_fx_source(raw, env).kind != "unknown"


def describe_fx_rate(
    fx_rate: float, raw_source: str | None, env: dict[str, str] | None = None
) -> str:
    """给页面用的一句人话说明。

    未知来源**必须显式写出「来源未知」** —— 不能只显示一个数字让人以为有据可依。
    """
    src = parse_fx_source(raw_source, env)
    if src.kind == "unknown":
        return f"1 CNY = {fx_rate} THB（**来源未知**：{src.raw}）"
    if src.kind == "manual":
        return f"1 CNY = {fx_rate} THB（人工维护：{src.detail}）"
    return f"1 CNY = {fx_rate} THB（来自环境变量 {src.detail}）"


def fx_rate_sanity_warning(fx_rate: float) -> str:
    """明显不合理的汇率给一句提醒（不阻断计算）。

    CNY→THB 长期在 4.5–5.5 区间。超出这个范围很可能是**填错了小数点或填反了方向**
    （例如把 THB→CNY 的 0.2 填进来）。只提醒，不擅自纠正。
    """
    try:
        rate = float(fx_rate)
    except (TypeError, ValueError):
        return f"汇率不是数字：{fx_rate!r}"
    if rate <= 0:
        return f"汇率必须为正数，当前 {rate}。"
    if rate < 4.0 or rate > 6.0:
        return (
            f"汇率 {rate} 超出常见区间 4.0–6.0（CNY→THB），"
            "请确认没有填错小数点或方向。"
        )
    return ""
