"""M1 人工抽检的打分计算（HANDOFF_PLAN P0-09）。

## 为什么单独一个纯函数模块

抽检的**判定标准是「十包中至少八包四项全部通过」**（DESIGN §9 Done 条件 2）。
这个数字会决定 M1 能不能算完成，所以它的算法必须：

- 是纯函数（给定输入必得同一结果），不掺 IO
- **能被单测直接钉住**，而不是藏在路由或前端里

这里刻意**不替人打分**：四个判据由人逐条给出 `pass`/`fail`/`pending`。

## 两种比率必须分清（这是本模块最容易写错的地方）

1. **正式口径 = 样本级**：一个包的四项**全部** pass，才算这个包通过；
   `pass_rate = 整包通过数 / 十`。门槛是 `>= 8/10`。
2. **逐格诊断 = 单元格级**：`passed / scored`，只用来指出「哪一列最差」，
   **不得**当作 M1 通过率，也不得用来判达标。

两者会给出相反结论，这正是旧实现的缺陷：7 包全过、另 3 包各失败一项时，
逐格是 37/40 = 92.5%（看着"达标"），而样本级是 7/10 = 70%（**未达标**）。

同时「没打分」与「判不通过」是**两件不同的事**：前者是「还没抽检」，
后者是「抽检发现问题」。所以 `pending` 不进分母，但它一旦存在就说明
**样本没评完**，正式比率必须是 `None` 而不是某个数。
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

#: DESIGN §9 Done 条件 2 的通过率门槛（样本级）。
PASS_THRESHOLD = 0.80

#: M1 人工抽检的十样本 cohort。**权威来源是 docs/m1-spot-check.md 的表格顺序**，
#: 这里只是代码侧的一份副本；`tests/workflow/test_spot_check.py` 有一条测试
#: 直接解析该文档并断言两边逐项一致，防止漂移。
M1_COHORT: tuple[str, ...] = (
    "896192389922",
    "616585743654",
    "545270531656",
    "890830589498",
    "944417108008",
    "606944529753",
    "674469789480",
    "994309419918",
    "992823585175",
    "935010924275",
)

#: cohort 规模（十）。
M1_SIZE: int = len(M1_COHORT)

#: 达标所需的整包全过数（8/10）。
M1_THRESHOLD_SAMPLES: int = 8

#: 单元格取值。空/未打分为 `pending` —— 它与 `fail` 语义不同，计分时分开处理。
VERDICT_PASS = "pass"
VERDICT_FAIL = "fail"
VERDICT_PENDING = "pending"

_VALID = {VERDICT_PASS, VERDICT_FAIL, VERDICT_PENDING}

#: 认得的写法。**只有**在这三组里的值才算合法输入；
#: 显式写了别的东西（如 `maybe`）是非法值，由校验层拒绝，不静默当 pending。
_PASS_WORDS = {"pass", "ok", "y", "yes", "true", "1", "通过", "合格"}
_FAIL_WORDS = {"fail", "ng", "n", "no", "false", "0", "不通过", "不合格", "打回"}
_PENDING_WORDS = {"", "pending", "未打分", "未评", "待评"}


@dataclass
class SpotCheckScores:
    """一张抽检表的打分结果。"""

    # ---- 逐格诊断（保留：用来看哪一列最差，**不是** M1 通过率） ----
    passed: int = 0
    failed: int = 0
    pending: int = 0
    #: 逐判据的明细，便于报告里指出「哪一列最差」。
    by_criteria: dict[str, dict[str, int]] = field(default_factory=dict)
    #: 打了分但判不通过的样本，报告里要列出来给人看。
    failed_items: list[dict[str, Any]] = field(default_factory=list)

    # ---- 样本级（正式口径） ----
    #: 提交的行数。
    samples: int = 0
    #: 命中 cohort 的行数（去重后）。
    cohort_matched: int = 0
    #: 四项**全部** pass 的包数。
    whole_sample_passed: int = 0
    #: 未做到四项全过的包（含未评完的），报告里逐个列出。
    whole_sample_short: list[dict[str, Any]] = field(default_factory=list)

    @property
    def scored(self) -> int:
        """已打分的单元格数（诊断用）。"""
        return self.passed + self.failed

    @property
    def cell_pass_rate(self) -> float | None:
        """**逐格**通过率（诊断用）。一个都没打分时返回 None。

        不要把这个数当 M1 通过率：它对「某包只挂一项」过于宽容。
        """
        if self.scored == 0:
            return None
        return self.passed / self.scored

    @property
    def complete(self) -> bool:
        """是否已按 M1 口径评完：恰好十个 cohort 样本、且四十格都已判定。"""
        return (
            self.samples == M1_SIZE
            and self.cohort_matched == M1_SIZE
            and self.pending == 0
        )

    @property
    def status(self) -> str:
        """`no_data` / `incomplete` / `complete` —— 报告与页面显示用的三态。"""
        if self.samples == 0 or self.scored == 0:
            return "no_data"
        if not self.complete:
            return "incomplete"
        return "complete"

    @property
    def pass_rate(self) -> float | None:
        """**正式**通过率 = 整包全过数 / 10。未评完时返回 None。

        返回 0 会让人以为「抽检了但全挂」，返回 1 会让人以为「全过」——
        两种都是编造。没评完就说没评完。
        """
        if not self.complete:
            return None
        return self.whole_sample_passed / M1_SIZE

    @property
    def meets_threshold(self) -> bool | None:
        """`pass_rate` 为 None 时同样是 None —— 「还不知道」，不是「未达标」。"""
        if not self.complete:
            return None
        return self.whole_sample_passed >= M1_THRESHOLD_SAMPLES

    def to_dict(self) -> dict[str, Any]:
        return {
            # 正式口径
            "status": self.status,
            "complete": self.complete,
            "samples": self.samples,
            "cohort_size": M1_SIZE,
            "cohort_matched": self.cohort_matched,
            "whole_sample_passed": self.whole_sample_passed,
            "whole_sample_short": self.whole_sample_short,
            "pass_rate": None if self.pass_rate is None else round(self.pass_rate, 4),
            "threshold": PASS_THRESHOLD,
            "threshold_samples": M1_THRESHOLD_SAMPLES,
            # 只有真正评完才给结论；否则一律 None（「还不知道」）。
            "meets_threshold": self.meets_threshold,
            # 逐格诊断（不是 M1 通过率）
            "passed": self.passed,
            "failed": self.failed,
            "pending": self.pending,
            "scored": self.scored,
            "cell_pass_rate": None if self.cell_pass_rate is None else round(self.cell_pass_rate, 4),
            "by_criteria": self.by_criteria,
            "failed_items": self.failed_items,
        }


def verdict_is_legal(raw: Any) -> bool:
    """这个单元格值是否**合法**（合法 ≠ 通过）。

    空值、`pending` 及其中文写法都合法（表示「还没打分」）；
    认得的 pass/fail 写法合法；**其余是非法值**，调用方应拒绝而不是当 pending。
    """
    text = str(raw or "").strip().lower()
    return text in _PASS_WORDS or text in _FAIL_WORDS or text in _PENDING_WORDS


def normalize_verdict(raw: Any) -> str:
    """把任意输入规整成三种取值之一。

    认得的「通过」写法：`pass` / `ok` / `y` / `yes` / `true` / `1` / 中文「通过」。
    认得的「不通过」：`fail` / `ng` / `n` / `no` / `false` / `0` / 中文「不通过」「打回」。
    其余（含空）一律 `pending` —— **不认识就当没打分**，不猜。

    注意：这里只是「规整」，**合法性判断**用 `verdict_is_legal`；
    非法值应由校验层在进入计分前拒绝。
    """
    text = str(raw or "").strip().lower()
    if text in _PASS_WORDS:
        return VERDICT_PASS
    if text in _FAIL_WORDS:
        return VERDICT_FAIL
    return VERDICT_PENDING


def validate_spot_check_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """校验提交的打分行；返回错误列表（空表示通过）。

    拒绝的情形（对应 NEXT-WORK §3 的表）：
    - 超过十个样本
    - `offer_id` 重复
    - `offer_id` 不在 M1 cohort 里
    - 任一单元格是非法判定值（显式写了不认识的字符串）
    """
    errors: list[dict[str, Any]] = []
    if len(rows) > M1_SIZE:
        errors.append({"kind": "too_many_rows", "message": f"最多 {M1_SIZE} 个样本，收到 {len(rows)} 个。"})

    seen: set[str] = set()
    for idx, row in enumerate(rows):
        offer_id = str(row.get("offer_id") or "").strip()
        if not offer_id:
            errors.append({"kind": "missing_offer_id", "index": idx, "message": "缺少 offer_id。"})
            continue
        if offer_id in seen:
            errors.append({"kind": "duplicate_offer_id", "offer_id": offer_id, "message": f"offer_id 重复：{offer_id}。"})
        seen.add(offer_id)
        if offer_id not in M1_COHORT:
            errors.append(
                {"kind": "not_in_cohort", "offer_id": offer_id, "message": f"offer_id 不在 M1 cohort 里：{offer_id}。"}
            )
        scores = row.get("scores") if isinstance(row.get("scores"), dict) else {}
        for key in CRITERIA:
            if key in scores and not verdict_is_legal(scores.get(key)):
                errors.append(
                    {
                        "kind": "illegal_verdict",
                        "offer_id": offer_id,
                        "criterion": key,
                        "value": str(scores.get(key)),
                        "message": f"{offer_id} 的 {key} 判定值非法：{scores.get(key)!r}。",
                    }
                )
    return errors


def score_spot_check(rows: list[dict[str, Any]]) -> SpotCheckScores:
    """按「样本 × 四判据」统计通过率。

    ``rows`` 每项形如 ``{"offer_id": "...", "scores": {"language": "pass", ...}}``。
    缺判据按 `pending` 处理（没打分，不是不通过）。

    **正式 `pass_rate` 是样本级的**：四项全过的包数 / 10；未评完时为 None。
    逐格统计放在 `passed`/`failed`/`pending` 与 `cell_pass_rate` 里，只作诊断。
    """
    result = SpotCheckScores(
        by_criteria={
            key: {"pass": 0, "fail": 0, "pending": 0} for key in CRITERIA
        }
    )
    seen: set[str] = set()
    for row in rows:
        scores = row.get("scores") if isinstance(row.get("scores"), dict) else {}
        result.samples += 1
        offer_id = str(row.get("offer_id") or "").strip()
        if offer_id and offer_id in M1_COHORT and offer_id not in seen:
            seen.add(offer_id)
            result.cohort_matched += 1

        row_failed: list[str] = []
        row_pending: list[str] = []
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
                row_pending.append(key)

        if row_failed:
            result.failed_items.append(
                {
                    "offer_id": offer_id,
                    "title": str(row.get("title") or ""),
                    "criteria": row_failed,
                }
            )
        # 样本级：四项全 pass 才算这个包通过。
        if not row_failed and not row_pending:
            result.whole_sample_passed += 1
        else:
            result.whole_sample_short.append(
                {
                    "offer_id": offer_id,
                    "title": str(row.get("title") or ""),
                    "failed_criteria": row_failed,
                    "pending_criteria": row_pending,
                }
            )
    return result


_STATUS_HEADLINE = {
    "no_data": "**通过率：无数据** —— 尚未打分，不能据此判定 M1 是否达标。",
    "incomplete": "**通过率：未完成（样本未齐）** —— 正式比率与达标结论均为「还不知道」。",
}


def render_report_markdown(
    rows: list[dict[str, Any]], scores: SpotCheckScores, *, generated_at: str = ""
) -> str:
    """把打分结果导成 md（P0-09 要求「导出 md」）。

    正式结论只认**样本级**比率；逐格比率单列为诊断。
    """
    lines = [
        "# M1 人工抽检结果",
        "",
        f"- 生成时间：{generated_at or '（未记录）'}",
        f"- 门槛：**十包中至少 {M1_THRESHOLD_SAMPLES} 包四项全部通过**"
        f"（即样本级通过率 ≥ {PASS_THRESHOLD:.0%}，DESIGN §9 Done 条件 2）",
        f"- 样本：{scores.samples}/{M1_SIZE}（命中 cohort {scores.cohort_matched}）",
        f"- 整包四项全过：**{scores.whole_sample_passed}/{M1_SIZE}**",
        "",
    ]
    if scores.status in _STATUS_HEADLINE:
        lines.append(_STATUS_HEADLINE[scores.status])
    else:
        verdict = "达标" if scores.meets_threshold else "**未达标**"
        lines.append(
            f"**通过率（样本级）：{scores.pass_rate:.1%}**"
            f"（{scores.whole_sample_passed}/{M1_SIZE}，{verdict}）"
        )
    lines += [
        "",
        "## 逐格诊断（**不是** M1 通过率）",
        "",
        f"- 已打分单元格：{scores.scored}（通过 {scores.passed} / 不通过 {scores.failed}）",
        f"- 未打分：{scores.pending}",
        f"- 逐格比率：{'无数据' if scores.cell_pass_rate is None else f'{scores.cell_pass_rate:.1%}'}"
        "（只用于看哪一列最差，不足以判达标）",
        "",
        "| 判据 | 通过 | 不通过 | 未打分 |",
        "|---|---|---|---|",
    ]
    for key in CRITERIA:
        c = scores.by_criteria.get(key, {})
        lines.append(
            f"| {CRITERIA_LABELS[key]} | {c.get('pass', 0)} | {c.get('fail', 0)} | {c.get('pending', 0)} |"
        )
    if scores.whole_sample_short:
        lines += ["", "## 未做到四项全过的样本", ""]
        for item in scores.whole_sample_short:
            parts = []
            if item["failed_criteria"]:
                parts.append("不通过：" + "、".join(CRITERIA_LABELS.get(k, k) for k in item["failed_criteria"]))
            if item["pending_criteria"]:
                parts.append("未打分：" + "、".join(CRITERIA_LABELS.get(k, k) for k in item["pending_criteria"]))
            lines.append(f"- `{item['offer_id']}` {item['title']} —— {'；'.join(parts)}")
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
