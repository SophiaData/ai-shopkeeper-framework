"""相对公式建议价的偏离校验。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DeviationResult:
    within_threshold: bool
    deviation_pct: float
    threshold_pct: float
    formula_price_thb: int
    proposed_price_thb: int
    reason: str

    def as_dict(self) -> dict[str, float | int | bool | str]:
        return {
            "within_threshold": self.within_threshold,
            "deviation_pct": self.deviation_pct,
            "threshold_pct": self.threshold_pct,
            "formula_price_thb": self.formula_price_thb,
            "proposed_price_thb": self.proposed_price_thb,
            "reason": self.reason,
        }


def check_deviation(
    *,
    formula_price_thb: int,
    proposed_price_thb: int,
    threshold_pct: float,
) -> DeviationResult:
    """|proposed - formula| / formula × 100 与阈值比较。formula≤0 视为不可比。"""
    if formula_price_thb <= 0:
        return DeviationResult(
            within_threshold=False,
            deviation_pct=float("inf"),
            threshold_pct=threshold_pct,
            formula_price_thb=formula_price_thb,
            proposed_price_thb=proposed_price_thb,
            reason="公式价无效，无法校验偏离。",
        )
    delta = abs(int(proposed_price_thb) - int(formula_price_thb))
    pct = (delta / formula_price_thb) * 100.0
    within = pct <= float(threshold_pct) + 1e-9
    if within:
        reason = f"偏离 {pct:.2f}% ≤ 阈值 {threshold_pct}%，可沿用。"
    else:
        reason = f"偏离 {pct:.2f}% 超过阈值 {threshold_pct}%，须人工审批。"
    return DeviationResult(
        within_threshold=within,
        deviation_pct=round(pct, 4),
        threshold_pct=float(threshold_pct),
        formula_price_thb=int(formula_price_thb),
        proposed_price_thb=int(proposed_price_thb),
        reason=reason,
    )
