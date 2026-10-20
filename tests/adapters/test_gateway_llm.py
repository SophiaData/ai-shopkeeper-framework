"""GatewayLLM 契约与失败语义测试。

离线部分用注入 transport 跑（CI 不依赖网关），真实调用一条单独标记并在网关不可达时跳过。
断言重点在**失败语义**：A2 实测的三种真实失败（思考吃满预算、截断、非法 JSON）
必须都变成结构化错误，而不是异常或"看起来成功"的空产出。
"""

from __future__ import annotations

import json

import httpx
import pytest

from shopkeeper.adapters.base import LlmClient
from shopkeeper.adapters.contract_suite import assert_llm_contract
from shopkeeper.adapters.gateway_llm import TASK_PROFILES, GatewayLLM, build_prompt
from shopkeeper.adapters.mock import MockProductSource
from shopkeeper.errors import ErrorKind
from shopkeeper.models import ProductFacts

URL = "https://detail.1688.com/offer/600123456789.html"


@pytest.fixture()
def product() -> ProductFacts:
    return MockProductSource().fetch(URL).data


def ok_payload(langs=("EN", "TH", "VI")):
    """正常三语言响应（构造见下方 _versions_payload / _wrap）。"""
    return _wrap(json.dumps(_versions_payload(list(langs))))


def make(payload=None, exc=None):
    def transport(body):
        if exc is not None:
            raise exc
        return payload
    return GatewayLLM(transport=transport)


# ------------------------------------------------------------------ 契约（真实实现放宽确定性）

def test_gateway_passes_structural_contract(product):
    assert_llm_contract(lambda: make(ok_payload()), product, deterministic=False)


def test_real_llm_is_not_held_to_byte_determinism(product):
    """确认 deterministic=False 这条通道存在，而不是把断言整体删掉。"""
    assert GatewayLLM.deterministic is False
    assert isinstance(GatewayLLM(), LlmClient)


# ------------------------------------------------------------------ A2 实测的三种真实失败

def test_empty_content_from_thinking_budget_is_generation_error(product):
    """A2 §4：max_tokens 被 reasoning 吃满 → content 空。必须报错，不能返回空 listing。"""
    payload = {
        "status_code": 200,
        "json": {"choices": [{"message": {"content": ""}, "finish_reason": "length"}], "usage": {}},
    }
    res = make(payload).generate_listing(product, ["EN", "TH", "VI"])
    assert not res.ok
    assert res.error.code == "LLM_EMPTY_CONTENT"
    assert res.error.kind == ErrorKind.GENERATION and res.error.retryable
    assert res.data is None


def test_truncated_output_is_flagged_with_budget_hint(product):
    payload = {
        "status_code": 200,
        "json": {"choices": [{"message": {"content": '{"langs": ["EN"'}, "finish_reason": "length"}], "usage": {}},
    }
    res = make(payload).generate_listing(product, ["EN"])
    assert not res.ok and res.error.code == "LLM_TRUNCATED"
    assert "max_tokens" in res.error.message or res.error.detail.get("max_tokens")


@pytest.mark.parametrize(
    "content,expected",
    [
        ("sorry, I cannot do that", "LLM_NOT_JSON"),
        ("{not valid json}", "LLM_JSON_DECODE"),
        ('{"versions": []}', "LLM_SCHEMA_INVALID"),
        ('{"versions": [{"lang": "EN", "title": "x", "bullets": ["a"], "description": "y"}]}', "LLM_SCHEMA_INVALID"),
    ],
)
def test_malformed_model_output_becomes_structured_error(product, content, expected):
    payload = {"status_code": 200, "json": {"choices": [{"message": {"content": content}, "finish_reason": "stop"}], "usage": {}}}
    res = make(payload).generate_listing(product, ["EN", "TH", "VI"])
    assert not res.ok and res.error.code == expected, res.error
    assert res.error.kind == ErrorKind.GENERATION


def _versions_payload(langs):
    return {
        "langs": list(langs),
        "versions": [
            {
                "lang": l,
                "title": f"Portable USB Fan {l}",
                "bullets": ["USB rechargeable", "3 speeds quiet", "Folds flat for travel"],
                "description": f"Description in {l} long enough to be genuinely useful to a shopper.",
                "advisory_flags": [],
            }
            for l in langs
        ],
    }


def _wrap(content, finish="stop"):
    return {"status_code": 200, "json": {
        "model": "deepseek-flash",
        "choices": [{"message": {"content": content}, "finish_reason": finish}],
        "usage": {"prompt_tokens": 900, "completion_tokens": 5000, "total_tokens": 5900,
                  "completion_tokens_details": {"reasoning_tokens": 0}},
    }}


def test_json_inside_code_fence_is_parsed(product):
    inner = json.dumps(_versions_payload(["EN", "TH", "VI"]))
    res = make(_wrap(f"```json\n{inner}\n```")).generate_listing(product, ["EN", "TH", "VI"])
    assert res.ok, res.error
    assert [v.lang for v in res.data.package.versions] == ["EN", "TH", "VI"]


def test_prose_wrapped_json_is_recovered(product):
    """模型常在 JSON 前后加一句解释。要能捞出 JSON，而不是整体判死。"""
    inner = json.dumps(_versions_payload(["EN"]))
    res = make(_wrap(f"Here you go:\n{inner}\nHope this helps!")).generate_listing(product, ["EN"])
    assert res.ok, res.error


def test_missing_language_is_rejected_not_silently_short(product):
    """模型少给一个语种 = 不合格产出，不能只交 2 份当 3 份。"""
    res = make(ok_payload(("EN", "TH"))).generate_listing(product, ["EN", "TH", "VI"])
    assert not res.ok and res.error.code == "LLM_LANG_MISSING"
    assert "VI" in res.error.message


def test_extra_language_returned_is_dropped_to_request_order(product):
    payload = ok_payload(("EN", "TH", "VI", "ID"))
    res = make(payload).generate_listing(product, ["EN", "TH", "VI"])
    assert res.ok and [v.lang for v in res.data.package.versions] == ["EN", "TH", "VI"]


# ------------------------------------------------------------------ 传输层失败

def test_http_error_status_degrades_to_local_template(product):
    """503 属可切备用类；无备用时回落本地模板并标 degraded（不再硬失败空交）。"""
    res = make({"status_code": 503, "json": {"error": "upstream busy"}}).generate_listing(product, ["EN"])
    assert res.ok and res.data is not None
    assert res.data.degraded is True
    assert res.data.package.compliance_report.get("engine") == "local-template"


def test_timeout_degrades_to_local_template(product):
    res = make(exc=httpx.ReadTimeout("timed out")).generate_listing(product, ["EN"])
    assert res.ok and res.data.degraded is True


def test_connection_error_degrades_to_local_template(product):
    res = make(exc=httpx.ConnectError("refused")).generate_listing(product, ["EN"])
    assert res.ok and res.data.degraded is True


def test_empty_langs_rejected_before_network(product):
    calls = []
    def transport(body):
        calls.append(body)
        return ok_payload()
    res = GatewayLLM(transport=transport).generate_listing(product, [])
    assert not res.ok and res.error.kind == ErrorKind.VALIDATION
    assert calls == [], "空语种不该白烧一次外呼"


def test_unknown_task_profile_is_validation_error(product):
    res = GatewayLLM(transport=lambda b: ok_payload(), task="teleport").generate_listing(product, ["EN"])
    assert not res.ok and res.error.code == "LLM_UNKNOWN_TASK"


# ------------------------------------------------------------------ A2 的两条硬约束落到代码上

def test_copywriting_budget_meets_measured_floor():
    """A2 实测：4000 预算 0/3 全败。缺省必须 ≥12000 且 reasoning_effort=low。"""
    p = TASK_PROFILES["copywriting"]
    assert p["max_tokens"] >= 12000, "低于实测底线会复现 0/3 全败"
    assert p["reasoning_effort"] == "low"
    assert p["timeout_s"] >= 240


def test_cost_ignores_reasoning_tokens_entirely(product):
    """网关 reasoning_tokens 恒报 0（A2 §9.3），成本只能用 completion_tokens。
    这里给一个"reasoning_tokens 被谎报为 0 但 completion 很大"的响应，成本仍应算出来。"""
    res = make(ok_payload()).generate_listing(product, ["EN", "TH", "VI"])
    assert res.ok
    assert res.data.usage.completion_tokens == 5000
    assert res.data.usage.cost_amount is not None and res.data.usage.cost_amount > 0
    assert res.data.usage.cost_currency == "USD"


def test_unknown_model_cost_is_none_not_zero(product):
    g = GatewayLLM(transport=lambda b: ok_payload(), model="some-unpriced-model")
    res = g.generate_listing(product, ["EN", "TH", "VI"])
    assert res.ok and res.data.usage.cost_amount is None, "拿不到价目必须 None，不许编 0"


def test_prompt_forbids_inventing_facts():
    facts = MockProductSource().fetch(URL).data
    prompt = build_prompt(facts, ["EN", "TH"], "TH")
    assert "Do not invent" in prompt
    assert "English" in prompt and "Thai" in prompt
    assert facts.title_raw in prompt


def test_no_plaintext_key_in_source():
    """密钥只从环境读（B1 约束延续）。"""
    import inspect
    import shopkeeper.adapters.gateway_llm as mod

    src = inspect.getsource(mod)
    assert "sk-" not in src, "源码里不得出现明文密钥"


# ------------------------------------------------------------------ 真实调用（网关在才跑）

def _gateway_alive() -> bool:
    try:
        with httpx.Client(timeout=5.0) as c:
            return c.get("http://127.0.0.1:3425/v1/models").status_code == 200
    except Exception:  # noqa: BLE001
        return False


@pytest.mark.real
@pytest.mark.skipif(not _gateway_alive(), reason="本机 magpie 网关未运行")
def test_real_gateway_three_language_generation(product):
    g = GatewayLLM()
    res = g.generate_listing(product, ["EN", "TH", "VI"], site="SEA")
    assert res.ok, res.error
    assert [v.lang for v in res.data.package.versions] == ["EN", "TH", "VI"]
    assert res.data.usage.completion_tokens > 0
    assert res.duration_ms > 1000, "真实调用必然有可观测耗时"
    for v in res.data.package.versions:
        assert len(v.bullets) >= 3 and v.title.strip()


def test_instance_snapshots_budget_against_later_global_mutation(product):
    """已构造实例的外呼参数不能被别处事后改坏全局表所影响。"""
    g = GatewayLLM(transport=lambda b: ok_payload())
    before = g.profiles["copywriting"]["max_tokens"]
    TASK_PROFILES["copywriting"]["max_tokens"] = 1
    try:
        assert g.profiles["copywriting"]["max_tokens"] == before, "实例应持有快照，不受全局改写影响"
    finally:
        TASK_PROFILES["copywriting"]["max_tokens"] = before
