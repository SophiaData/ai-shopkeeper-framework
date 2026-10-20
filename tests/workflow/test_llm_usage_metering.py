"""P0-08：真实 LLM 接入的离线护栏。

2026-10-16 实测发现两处真实缺陷，本文件把它们钉住：

1. **模型 id 错误**：默认 `group/auto-deepseek-v4-flash` 在 magpie 网关上**不存在**，
   真实调用直接 404（`magpie knows no model ... did you mean
   group/auto-deepseek-v4-1-flash`）。正确 id 是 `group/auto-deepseek-v4-1-flash`。
   代码里 4 处、文档 1 处都是错的 —— 意味着**任何真 LLM 调用都会失败**。

2. **workflow 侧没有 token 计量**：`providers.py` 完全没读 `usage`，
   于是「这次生成花了多少」永远是缺数据（这是 P0-08 的明文验收项）。
"""

from __future__ import annotations

import json

import httpx
import pytest

from shopkeeper.workflow.providers import OpenAIChatProvider


def _provider(**env) -> OpenAIChatProvider:
    base = {
        "SHOPKEEPER_LLM_BASE_URL": "http://127.0.0.1:3425/v1",
        "SHOPKEEPER_LLM_MODEL": "group/auto-deepseek-v4-1-flash",
    }
    base.update(env)
    return OpenAIChatProvider(
        base_url=base["SHOPKEEPER_LLM_BASE_URL"],
        api_key="k",
        model=base["SHOPKEEPER_LLM_MODEL"],
        env=base,
    )


def _reply(payload: dict, *, status: int = 200) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=payload)

    return httpx.MockTransport(handler)


# --------------------------------------------------------------------------
# ① 模型 id
# --------------------------------------------------------------------------


def test_config_default_model_id_is_the_one_gateway_actually_has(monkeypatch):
    """默认模型名必须是网关上真实存在的那个。

    2026-10-16 实测：`group/auto-deepseek-v4-flash` → 404
    `magpie knows no model ... did you mean group/auto-deepseek-v4-1-flash`。
    """
    from shopkeeper.config import Settings

    monkeypatch.delenv("SHOPKEEPER_LLM_MODEL", raising=False)
    assert Settings.from_env().llm_model == "group/auto-deepseek-v4-1-flash"


def test_gateway_llm_default_model_id_is_correct():
    from shopkeeper.adapters.gateway_llm import GatewayLLM

    llm = GatewayLLM(env={})
    assert llm.model == "group/auto-deepseek-v4-1-flash"


def test_orchestrator_llm_default_model_id_is_correct(tmp_path):
    from shopkeeper.orchestrator.llm import OrchestratorLLM
    from shopkeeper.orchestrator.store import OrchestratorStore

    llm = OrchestratorLLM(OrchestratorStore(tmp_path / "o.db"), env={})
    assert llm.model == "group/auto-deepseek-v4-1-flash"


def test_no_stale_model_id_used_as_default():
    """那个 404 的 id **不得再被用作默认值**（防止有人从旧文档抄回来）。

    注意：它仍允许出现在 `PRICE_USD_PER_MTOK` 里作**查价别名** —— 用户 `.env`
    与历史审计记录里可能还写着它，删掉会让那些调用折算成 0 成本。
    所以这里只查「作为默认值」的用法，不查整体出现。
    """
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "src" / "shopkeeper"
    stale = "auto-deepseek-v4-flash"
    # 匹配 os.getenv("X", "<stale>") / env.get("X") or "<stale>" / 直接赋默认
    offenders: list[str] = []
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if stale not in text:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if stale not in line:
                continue
            # 查价表那一行是允许的
            if line.strip().startswith('"group/auto-deepseek-v4-flash"'):
                continue
            offenders.append(f"{path.relative_to(root)}:{lineno}: {line.strip()[:80]}")
    assert offenders == [], f"仍把不存在的模型 id 当默认值：{offenders}"


# --------------------------------------------------------------------------
# ② token 计量
# --------------------------------------------------------------------------


def test_usage_recorded_on_success():
    prov = _provider()
    prov._client = httpx.Client(
        transport=_reply(
            {
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
            }
        )
    )

    result = prov.complete("sys", "user")

    assert result.ok
    assert prov.usage == {
        "calls": 1,
        "prompt_tokens": 11,
        "completion_tokens": 7,
        "total_tokens": 18,
    }


def test_usage_accumulates_across_calls():
    prov = _provider()
    prov._client = httpx.Client(
        transport=_reply(
            {
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            }
        )
    )

    prov.complete("s", "u")
    prov.complete("s", "u")

    assert prov.usage["calls"] == 2
    assert prov.usage["prompt_tokens"] == 20
    assert prov.usage["total_tokens"] == 30


def test_usage_not_recorded_on_failure():
    """失败不该计入 —— 否则成本统计会虚高。"""
    prov = _provider()
    prov._client = httpx.Client(
        transport=_reply({"choices": [{"message": {"content": "   "}}]})
    )

    prov.complete("s", "u")

    assert prov.usage["calls"] == 0


def test_usage_tolerates_missing_usage_block():
    """网关没返回 usage 时只加 calls，**不编造 token 数**。"""
    prov = _provider()
    prov._client = httpx.Client(
        transport=_reply({"choices": [{"message": {"content": "ok"}}]})
    )

    prov.complete("s", "u")

    assert prov.usage == {"calls": 1, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    assert prov.cost_usd() == 0.0


def test_usage_ignores_reasoning_tokens():
    """**绝不读 reasoning_tokens** —— 网关该字段恒报 0，读它会严重低估成本。

    这里给一个矛盾的 payload：reasoning_tokens 很大，completion_tokens 很小。
    计量必须跟随 completion_tokens。
    """
    prov = _provider()
    prov._client = httpx.Client(
        transport=_reply(
            {
                "choices": [{"message": {"content": "ok"}}],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 4,
                    "total_tokens": 14,
                    "completion_tokens_details": {"reasoning_tokens": 99999},
                },
            }
        )
    )

    prov.complete("s", "u")

    assert prov.usage["completion_tokens"] == 4


def test_cost_usd_uses_vendor_price_table():
    prov = _provider()
    prov._client = httpx.Client(
        transport=_reply(
            {
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000},
            }
        )
    )

    prov.complete("s", "u")

    # flash 牌价 (0.14, 0.28) USD / 1M tokens
    assert prov.cost_usd() == pytest.approx(0.42)


def test_cost_usd_returns_none_for_unknown_model():
    """拿不到价目就返回 None，不猜一个数字出来。"""
    prov = _provider(SHOPKEEPER_LLM_MODEL="some/unpriced-model")
    prov._client = httpx.Client(
        transport=_reply(
            {
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 10},
            }
        )
    )

    prov.complete("s", "u")

    assert prov.cost_usd() is None


def test_last_model_tracks_failover():
    """发生主备切换时，`last_model` 应反映**实际**用的那条。"""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body["model"])
        if body["model"] == "primary-model":
            return httpx.Response(429, json={"error": "rate limited"})
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )

    prov = OpenAIChatProvider(
        base_url="http://127.0.0.1:3425/v1",
        api_key="k",
        model="primary-model",
        transport=httpx.MockTransport(handler),
        env={
            "SHOPKEEPER_LLM_MODEL": "primary-model",
            "LLM_FALLBACK_MODEL": "secondary-model",
        },
    )

    result = prov.complete("s", "u")

    assert result.ok
    assert seen == ["primary-model", "secondary-model"]
    assert prov.last_model == "secondary-model"
