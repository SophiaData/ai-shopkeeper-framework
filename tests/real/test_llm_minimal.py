"""LLM 一次最小 chat/completions 调用（不跑完整三语 listing）。

需要可访问的 `SHOPKEEPER_LLM_BASE_URL`（默认本机 magpie）与
`SHOPKEEPER_LLM_API_KEY`（本机网关可用占位 `local-gateway-no-auth`）。
网关不可达或缺钥时 skip，并说明所需 env。
"""

from __future__ import annotations

import os

import httpx
import pytest

pytestmark = pytest.mark.real


def _llm_skip_reason() -> str | None:
    base = str(os.environ.get("SHOPKEEPER_LLM_BASE_URL") or "http://127.0.0.1:3425/v1").rstrip("/")
    key = str(os.environ.get("SHOPKEEPER_LLM_API_KEY") or "").strip()
    model = str(os.environ.get("SHOPKEEPER_LLM_MODEL") or "group/auto-deepseek-v4-1-flash").strip()
    auth = key or "local-gateway-no-auth"
    try:
        with httpx.Client(timeout=5.0) as client:
            response = client.get(f"{base}/models", headers={"Authorization": f"Bearer {auth}"})
        if response.status_code >= 500:
            return (
                f"LLM 网关不可用（HTTP {response.status_code}）。"
                "需要可访问的 SHOPKEEPER_LLM_BASE_URL 与 SHOPKEEPER_LLM_API_KEY"
                f"（当前 base={base}, model={model}）"
            )
        return None
    except Exception as exc:  # noqa: BLE001
        return (
            f"LLM 网关不可达（{type(exc).__name__}）。"
            "需要可访问的 SHOPKEEPER_LLM_BASE_URL 与 SHOPKEEPER_LLM_API_KEY"
            f"（当前 base={base}, model={model}）"
        )


def test_llm_minimal_chat_completion():
    reason = _llm_skip_reason()
    if reason:
        pytest.skip(reason)
    base = str(os.environ.get("SHOPKEEPER_LLM_BASE_URL") or "http://127.0.0.1:3425/v1").rstrip("/")
    key = str(os.environ.get("SHOPKEEPER_LLM_API_KEY") or "local-gateway-no-auth").strip()
    model = str(os.environ.get("SHOPKEEPER_LLM_MODEL") or "group/auto-deepseek-v4-1-flash").strip()
    # max_tokens 必须留够 reasoning 预算：实测（2026-10-16，magpie
    # `group/auto-deepseek-v4-1-flash`）`reasoning_effort="low"` + `max_tokens=32`
    # 会把 **全部 32 token 花在思考上**（usage.reasoning_tokens=32），
    # 返回 `content=None` / `finish_reason="length"` —— 看起来像「模型不会回答」，
    # 实际是预算被思考吃光。生产 profile 用 12000，故只在测试里踩到。
    body = {
        "model": model,
        "messages": [{"role": "user", "content": "Reply with exactly one word: pong"}],
        "temperature": 0,
        "max_tokens": 512,
        "reasoning_effort": "low",
    }
    with httpx.Client(timeout=90.0) as client:
        response = client.post(
            f"{base}/chat/completions",
            json=body,
            headers={"Authorization": f"Bearer {key}"},
        )
    assert response.status_code < 500, f"LLM HTTP {response.status_code}: {response.text[:200]}"
    payload = response.json()
    assert response.status_code < 400, f"LLM 拒绝: {str(payload)[:200]}"
    choices = payload.get("choices") or []
    assert choices, f"无 choices: {str(payload)[:200]}"
    content = str((choices[0].get("message") or {}).get("content") or "").strip()
    assert content, "LLM 返回空 content"
