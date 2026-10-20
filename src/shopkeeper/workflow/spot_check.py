"""M1 人工抽检的打分计算（HANDOFF_PLAN P0-09）。

## 为什么单独一个纯函数模块

抽检的**判定标准是「通过率 ≥ 80%」**（DESIGN §9 Done 条件 2）。这个数字会决定
M1 能不能算完成，所以它的算法必须：

- 是纯函数（给定输入必得同一结果），不掺 IO
- **能被单测直接钉住**，而不是藏在路由或前端里

这里刻意**不替人打分**：四个判据由人逐条给出 `pass`/`fail`/`skip`。
「没打分」与「打分为不通过」是**两件不同的事** —— 前者是「还没抽检」，
后者是「抽检发现问题」。混在一起会让通过率虚高或虚低。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: 四个判据的稳定 id（前后端与报告共用，不要改）。
CRITERIA: tuple[str, ...] = ("language", "accuracy", "listable", "image")

#: 判据的人话名称。报告与页面都用这套，避免两处口径不一。
CRITERIA_LABELS: dict[str, str] = {
    "language": "语种地道",
    "accuracy": "事实准确",
    "listable": "可上架",
    "image": "图可用",
}

#: DESIGN §9 Done 条件 2 的通过率门槛。
PASS_THRESHOLD = 0.80

#: 单元格取值。空/未打分为 `pending` —— 它与 `fail` 语义不同，计分时分开处理。
VERDICT_PASS = "pass"
VERDICT_FAIL = "fail"
VERDICT_PENDING = "pending"

_VALID = {VERDICT_PASS, VERDICT_FAIL, VERDICT_PENDING}


@dataclass
class SpotCheckScores:
    """一张抽检表的打分结果。"""

    passed: int = 0
    failed: int = 0
    pending: int = 0
    #: 逐判据的明细，便于报告里指出「哪一列最差」。
    by_criteria: dict[str, dict[str, int]] = field(default_factory=dict)
    #: 打了分但判不通过的样本，报告里要列出来给人看。
    failed_items: list[dict[str, Any]] = field(default_factory=list)

    @property
    def scored(self) -> int:
        return self.passed + self.failed

    @property
    def pass_rate(self) -> float | None:
        """已打分单元格的通过率。**一个都没打分时返回 None**，不返回 0 或 1。

        返回 0 会让人以为「抽检了但全挂」，返回 1 会让人以为「全过」——
        两种都是编造。没数据就说没数据。
        """
        if self.scored == 0:
            return None
        return self.passed / self.scored

    @property
    def complete(self) -> bool:
        return self.pending == 0 and self.scored > 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "failed": self.failed,
            "pending": self.pending,
            "scored": self.scored,
            "pass_rate": None if self.pass_rate is None else round(self.pass_rate, 4),
            "threshold": PASS_THRESHOLD,
            # 只有真正打完分才给结论；否则一律 None（「还不知道」）。
            "meets_threshold": None if self.pass_rate is None else self.pass_rate >= PASS_THRESHOLD,
            "complete": self.complete,
            "by_criteria": self.by_criteria,
            "failed_items": self.failed_items,
        }


def normalize_verdict(raw: Any) -> str:
    """把任意输入规整成三种取值之一。

    认得的「通过」写法：`pass` / `ok` / `y` / `yes` / `true` / `1` / 中文「通过」。
    认得的「不通过」：`fail` / `ng` / `n` / `no` / `false` / `0` / 中文「不通过」「打回」。
    其余（含空）一律 `pending` —— **不认识就当没打分**，不猜。
    """
    text = str(raw or "").strip().lower()
    if text in {"pass", "ok", "y", "yes", "true", "1", "通过", "合格"}:
        return VERDICT_PASS
    if text in {"fail", "ng", "n", "no", "false", "0", "不通过", "不合格", "打回"}:
        return VERDICT_FAIL
    return VERDICT_PENDING


def score_spot_check(rows: list[dict[str, Any]]) -> SpotCheckScores:
    """按「样本 × 四判据」统计通过率。

    ``rows`` 每项形如 ``{"offer_id": "...", "scores": {"language": "pass", ...}}``。
    缺判据按 `pending` 处理（没打分，不是不通过）。
    """
    result = SpotCheckScores(
        by_criteria={
            key: {"pass": 0, "fail": 0, "pending": 0} for key in CRITERIA
        }
    )
    for row in rows:
        scores = row.get("scores") if isinstance(row.get("scores"), dict) else {}
        row_failed: list[str] = []
        for key in CRITERIA:
            verdict = normalize_verdict(scores.get(key))
            if verdict not in _VALID:  # pragma: no cover - normalize 已保证
                verdict = VERDICT_PENDING
            result.by_criteria[key][verdict] += 1
            if verdict == VERDICT_PASS:
                result.passed += 1
            elif verdict == VERDICT_FAIL:
                result.failed += 1
                row_failed.append(key)
            else:
                result.pending += 1
        if row_failed:
            result.failed_items.append(
                {
                    "offer_id": str(row.get("offer_id") or ""),
                    "title": str(row.get("title") or ""),
                    "criteria": row_failed,
                }
            )
    return result


def render_report_markdown(
    rows: list[dict[str, Any]], scores: SpotCheckScores, *, generated_at: str = ""
) -> str:
    """把打分结果导成 md（P0-09 要求「导出 md」）。"""
    lines = [
        "# M1 人工抽检结果",
        "",
        f"- 生成时间：{generated_at or '（未记录）'}",
        f"- 门槛：通过率 ≥ {PASS_THRESHOLD:.0%}（DESIGN §9 Done 条件 2）",
        f"- 已打分单元格：{scores.scored}（通过 {scores.passed} / 不通过 {scores.failed}）",
        f"- 未打分：{scores.pending}",
        "",
    ]
    if scores.pass_rate is None:
        lines.append("**通过率：无数据** —— 尚未打分，不能据此判定 M1 是否达标。")
    else:
        verdict = "达标" if scores.pass_rate >= PASS_THRESHOLD else "**未达标**"
        lines.append(f"**通过率：{scores.pass_rate:.1%}（{verdict}）**")
    lines += ["", "## 分判据明细", "", "| 判据 | 通过 | 不通过 | 未打分 |", "|---|---|---|---|"]
    for key in CRITERIA:
        c = scores.by_criteria.get(key, {})
        lines.append(
            f"| {CRITERIA_LABELS[key]} | {c.get('pass', 0)} | {c.get('fail', 0)} | {c.get('pending', 0)} |"
        )
    if scores.failed_items:
        lines += ["", "## 判不通过的样本", ""]
        for item in scores.failed_items:
            labels = "、".join(CRITERIA_LABELS.get(k, k) for k in item["criteria"])
            lines.append(f"- `{item['offer_id']}` {item['title']} —— {labels}")
    lines += ["", "## 逐样本明细", ""]
    header = "| # | offer_id | 输入标题 | " + " | ".join(
        CRITERIA_LABELS[k] for k in CRITERIA
    ) + " |"
    lines += [header, "|---|" + "---|" * (len(CRITERIA) + 2)]
    for idx, row in enumerate(rows, 1):
        scores_map = row.get("scores") if isinstance(row.get("scores"), dict) else {}
        cells = " | ".join(normalize_verdict(scores_map.get(k)) for k in CRITERIA)
        lines.append(
            f"| {idx} | {row.get('offer_id') or ''} | {row.get('title') or ''} | {cells} |"
        )
    lines.append("")
    return "\n".join(lines)
