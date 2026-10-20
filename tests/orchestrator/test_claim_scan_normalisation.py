"""禁写说法扫描必须抗空白规避（独立复核发现的 P1-1）。

## 缺陷

`scripting.ungrounded_claims` / `outreach.ungrounded_claims` 的禁词组用**裸子串匹配**：

```python
if folded in body_cf and folded not in source_cf:
```

于是**插入一个空格就能绕过**：

| 输入 | 修复前 |
|---|---|
| `防水等级 IPX7` | ✅ 命中 |
| `防 水等级 IPX7` | ❌ **漏放** |
| `防\u200b水等级` | ❌ **漏放**（零宽空格） |
| `防　水等级` | ❌ **漏放**（全角空格） |

模型只要把词拆开写，脚本就**通过**并写进 context、进而交给人工拍摄。

## 同一仓库里两套口径

`compliance` 模块**早就**做了归一化（`match_forms` → NFKC + 去空白/零宽），
而编排器自己这一份没有；`outreach` 里甚至**同一个函数内**两种写法
（禁词组裸子串，`_PERCENT` 那一支却手动 `re.sub`）。**弱的那套就是绕过点。**

现两处统一复用 `compliance.match_forms`，不再自建第二套。
"""

from __future__ import annotations

import pytest

from shopkeeper.orchestrator.outreach import ungrounded_claims as outreach_claims
from shopkeeper.orchestrator.scripting import ungrounded_claims as script_claims

SOURCE = "卖家原文没有提到这些说法"

EVASIONS = [
    "防水等级 IPX7",
    "防 水等级 IPX7",
    "防\u200b水等级",
    "防　水等级",  # 全角空格
    "防\u00a0水等级",  # 不换行空格
]


@pytest.mark.parametrize("text", EVASIONS)
def test_script_scan_resists_whitespace_evasion(text):
    """插空格 / 零宽 / 全角都不能绕过脚本禁写扫描。"""
    hits = script_claims({"shots": [{"voice_zh": text}]}, SOURCE)
    assert "防水" in hits, f"{text!r} 漏放，实际 {hits}"


@pytest.mark.parametrize("text", ["食品级材质", "食品 级材质", "食品　级材质"])
def test_script_scan_resists_evasion_food_grade(text):
    hits = script_claims({"shots": [{"voice_zh": text}]}, SOURCE)
    assert "食品级" in hits, f"{text!r} 漏放，实际 {hits}"


@pytest.mark.parametrize("text", ["免打孔", "免 打孔", "免\u200b打孔"])
def test_outreach_scan_resists_whitespace_evasion(text):
    hits = outreach_claims({"messages": [{"dm_zh": text}]}, SOURCE)
    assert "免打孔" in hits, f"{text!r} 漏放，实际 {hits}"


def test_normal_text_still_passes():
    """**反向**：正常文案不得被误判 —— 过度拦截会挡住可交付内容。"""
    hits = script_claims(
        {"shots": [{"voice_zh": "把抽屉整理干净，找东西更快"}]}, SOURCE
    )
    assert hits == [], f"正常文案被误判：{hits}"


def test_source_stated_claim_is_allowed():
    """**反向**：卖家原文里**已经说过**的卖点不该被拦。"""
    text = "防水等级 IPX7"
    hits = script_claims({"shots": [{"voice_zh": text}]}, text)
    assert hits == [], "卖家自己写过的说法应放行"


def test_uses_shared_normaliser_not_a_second_copy():
    """归一化必须复用 `compliance.match_forms`，不能再造一套。

    这条断言存在的意义：本项目的失败模式不是「没做归一化」，
    而是**同一仓库里两套口径**（弱的那套成为绕过点）。
    """
    from pathlib import Path as _P

    root = _P(__file__).resolve().parents[2] / "src/shopkeeper/orchestrator"
    for name in ("scripting.py", "outreach.py"):
        src = (root / name).read_text(encoding="utf-8")
        assert "from shopkeeper.compliance import match_forms" in src, (
            f"{name} 应复用 compliance.match_forms"
        )
