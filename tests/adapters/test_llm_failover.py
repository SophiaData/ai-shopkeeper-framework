"""LLM 主备切换与本地模板降级（mock transport，不外网）。"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from pydantic import BaseModel, Field

from shopkeeper.adapters.gateway_llm import GatewayLLM
from shopkeeper.adapters.llm_failover import (
    CODE_LLM_FALLBACK_EXHAUSTED,
    is_model_failover_error,
    model_chain,
    template_listing_outcome,
)
from shopkeeper.adapters.mock import MockProductSource
from shopkeeper.errors import AdapterError, ErrorKind
from shopkeeper.orchestrator.definition import PIPELINE_V1
from shopkeeper.orchestrator.llm import OrchestratorLLM, StructuredLLMOutcome
from shopkeeper.orchestrator.store import OrchestratorStore
from shopkeeper.security.error_codes import resolve
from shopkeeper.workflow.providers import OpenAIChatProvider
from shopkeeper.workflow.store import WorkflowStore

URL = "https://detail.1688.com/offer/600123456789.html"


@pytest.fixture()
def product():
    return MockProductSource().fetch(URL).data


def _ok_versions(langs=("EN", "TH", "VI")):
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


def _wrap(content, *, model="deepseek-flash"):
    return {
        "status_code": 200,
        "json": {
            "model": model,
            "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 200},
        },
    }


def test_model_chain_dedupes_and_orders():
    chain = model_chain(
        {
            "SHOPKEEPER_LLM_MODEL": "primary",
            "LLM_FALLBACK_MODEL": "backup-a",
            "LLM_FALLBACK_MODELS": "backup-b,primary,backup-a",
        }
    )
    assert chain == ["primary", "backup-a", "backup-b"]


@pytest.mark.parametrize(
    "code,expect",
    [
        ("LLM_TIMEOUT", True),
        ("LLM_TRANSPORT", True),
        ("LLM_HTTP_429", True),
        ("LLM_HTTP_503", True),
        ("LLM_HTTP_502", True),
        ("LLM_HTTP_500", True),
        ("LLM_SCHEMA_INVALID", False),
        ("LLM_EMPTY_CONTENT", False),
        ("LLM_BUDGET_EXCEEDED", False),
    ],
)
def test_failover_error_classifier(code, expect):
    err = AdapterError.generation(code, "x") if code != "LLM_BUDGET_EXCEEDED" else AdapterError.policy(code, "x")
    assert is_model_failover_error(err) is expect


def test_fallback_exhausted_code_registered():
    spec = resolve(CODE_LLM_FALLBACK_EXHAUSTED)
    assert spec is not None
    assert spec.kind == ErrorKind.GENERATION
    assert spec.human_todo is True


def test_gateway_switches_to_fallback_model(product):
    calls: list[str] = []

    def transport(body):
        calls.append(body["model"])
        if body["model"] == "primary-model":
            return {"status_code": 503, "json": {"error": "busy"}}
        return _wrap(json.dumps(_ok_versions(["EN"])), model=body["model"])

    g = GatewayLLM(
        transport=transport,
        model="primary-model",
        env={
            "SHOPKEEPER_LLM_MODEL": "primary-model",
            "LLM_FALLBACK_MODEL": "backup-model",
        },
    )
    res = g.generate_listing(product, ["EN"])
    assert res.ok, res.error
    assert calls == ["primary-model", "backup-model"]
    assert res.data.degraded is False
    assert res.data.package.compliance_report.get("requested_model") == "backup-model"


def test_gateway_all_failover_degrades_to_template(product):
    def transport(body):
        if body["model"] == "primary-model":
            raise httpx.ReadTimeout("timed out")
        return {"status_code": 429, "json": {"error": "rate limited"}}

    g = GatewayLLM(
        transport=transport,
        model="primary-model",
        env={
            "SHOPKEEPER_LLM_MODEL": "primary-model",
            "LLM_FALLBACK_MODEL": "backup-model",
        },
    )
    res = g.generate_listing(product, ["EN", "TH"])
    assert res.ok, res.error
    assert res.data.degraded is True
    assert res.data.degrade_code == CODE_LLM_FALLBACK_EXHAUSTED
    assert res.data.package.compliance_report["engine"] == "local-template"
    assert "Local template" in res.data.package.versions[0].bullets[1]
    assert res.data.usage.cost_amount == 0.0


def test_gateway_schema_error_does_not_failover_or_template(product):
    calls: list[str] = []

    def transport(body):
        calls.append(body["model"])
        return _wrap('{"versions": []}')

    g = GatewayLLM(
        transport=transport,
        model="primary-model",
        env={"SHOPKEEPER_LLM_MODEL": "primary-model", "LLM_FALLBACK_MODEL": "backup-model"},
    )
    res = g.generate_listing(product, ["EN"])
    assert not res.ok
    assert res.error.code == "LLM_SCHEMA_INVALID"
    assert calls == ["primary-model"], "结构错误不切备用、不回落模板"


def test_gateway_503_without_fallback_still_templates(product):
    g = GatewayLLM(
        transport=lambda b: {"status_code": 503, "json": {"error": "busy"}},
        model="only-model",
        env={"SHOPKEEPER_LLM_MODEL": "only-model", "LLM_FALLBACK_MODEL": ""},
    )
    res = g.generate_listing(product, ["EN"])
    assert res.ok and res.data.degraded is True


def test_template_listing_marks_advisory():
    product = MockProductSource().fetch(URL).data
    out = template_listing_outcome(product, ["EN"], "SEA")
    assert out.degraded and out.degrade_code == CODE_LLM_FALLBACK_EXHAUSTED
    assert "llm_degraded_template" in out.package.versions[0].advisory_flags


class _TinyDecision(BaseModel):
    rationale: str = ""
    recommend: bool = True
    risk_notes: list[str] = Field(default_factory=list)


def test_orchestrator_failover_and_budget_counts_fallback(tmp_path: Path):
    """备用成功也记账；主 503 → 备用 200。"""
    wf = WorkflowStore(tmp_path / "wf.db")
    store = OrchestratorStore(wf)
    calls: list[str] = []

    def transport(body):
        calls.append(body["model"])
        if body["model"] == "orch-primary":
            return {"status_code": 503, "json": {"error": "busy"}}
        payload = {"rationale": "backup ok", "recommend": True, "risk_notes": []}
        return {
            "status_code": 200,
            "json": {
                "model": body["model"],
                "choices": [{"message": {"content": json.dumps(payload)}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1000, "completion_tokens": 500},
            },
        }

    env = {
        "LLM_CHANNEL": "gateway",
        "SHOPKEEPER_LLM_API_KEY": "test-not-placeholder-key",
        "SHOPKEEPER_LLM_MODEL": "orch-primary",
        "LLM_FALLBACK_MODEL": "group/auto-deepseek-v4-flash",
        "LLM_DAILY_BUDGET_USD": "10",
    }
    llm = OrchestratorLLM(store, env=env, transport=transport, sleep_fn=lambda _s: None)
    assert llm.mock is False
    run = store.create_run(PIPELINE_V1, {"url": URL})
    res = llm.complete_json(
        run_id=run["id"],
        step_id="scout",
        system="sys",
        user="user",
        schema=_TinyDecision,
        mock_payload={"rationale": "tpl", "recommend": True, "risk_notes": []},
        max_retries=0,
    )
    assert res.ok and res.data is not None
    assert calls == ["orch-primary", "group/auto-deepseek-v4-flash"]
    assert res.data.degraded is False
    assert res.data.model == "group/auto-deepseek-v4-flash"
    spent = store.spent_usd_today()
    assert spent > 0, "备用调用必须计入日预算"


def test_orchestrator_all_fail_degrades_template(tmp_path: Path):
    wf = WorkflowStore(tmp_path / "wf.db")
    store = OrchestratorStore(wf)

    def transport(_body):
        return {"status_code": 429, "json": {"error": "rate"}}

    env = {
        "LLM_CHANNEL": "gateway",
        "SHOPKEEPER_LLM_API_KEY": "test-not-placeholder-key",
        "SHOPKEEPER_LLM_MODEL": "orch-primary",
        "LLM_FALLBACK_MODEL": "orch-backup",
    }
    llm = OrchestratorLLM(store, env=env, transport=transport, sleep_fn=lambda _s: None)
    run = store.create_run(PIPELINE_V1, {"url": URL})
    res = llm.complete_json(
        run_id=run["id"],
        step_id="scout",
        system="sys",
        user="user",
        schema=_TinyDecision,
        mock_payload={"rationale": "tpl", "recommend": True, "risk_notes": ["x"]},
        max_retries=0,
    )
    assert res.ok and res.data is not None
    assert res.data.degraded is True
    assert res.data.degrade_code == CODE_LLM_FALLBACK_EXHAUSTED
    assert res.data.model == "local_template"
    assert res.data.cost_usd == 0.0
    assert isinstance(res.data, StructuredLLMOutcome)


def test_orchestrator_budget_blocks_before_fallback(tmp_path: Path):
    wf = WorkflowStore(tmp_path / "wf.db")
    store = OrchestratorStore(wf)
    run = store.create_run(PIPELINE_V1, {"url": URL})
    store.record_llm_usage(
        run_id=run["id"],
        step_id="seed",
        model="seed",
        prompt_version="t",
        prompt_tokens=1,
        completion_tokens=1,
        cost_usd=1.0,
        rationale="seed",
    )
    calls: list[str] = []

    def transport(body):
        calls.append(body["model"])
        return {"status_code": 503, "json": {}}

    env = {
        "LLM_CHANNEL": "gateway",
        "SHOPKEEPER_LLM_API_KEY": "test-not-placeholder-key",
        "SHOPKEEPER_LLM_MODEL": "orch-primary",
        "LLM_FALLBACK_MODEL": "orch-backup",
        "LLM_DAILY_BUDGET_USD": "0.5",
    }
    llm = OrchestratorLLM(store, env=env, transport=transport, sleep_fn=lambda _s: None)
    res = llm.complete_json(
        run_id=run["id"],
        step_id="scout",
        system="s",
        user="u",
        schema=_TinyDecision,
        max_retries=0,
    )
    assert not res.ok
    assert res.error.code == "LLM_BUDGET_EXCEEDED"
    assert calls == [], "预算已超不得再外呼主/备"


def test_openai_provider_failover_then_success():
    models: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        models.append(body["model"])
        if body["model"] == "primary":
            return httpx.Response(503, json={"error": "busy"})
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"title_th":"กล่อง"}'}}]})

    client = OpenAIChatProvider(
        base_url="http://llm.test/v1",
        api_key="k",
        model="primary",
        transport=httpx.MockTransport(handler),
        env={"SHOPKEEPER_LLM_MODEL": "primary", "LLM_FALLBACK_MODEL": "backup"},
    )
    result = client.complete("sys", "user")
    assert result.ok
    assert models == ["primary", "backup"]
    assert "กล่อง" in result.data
