"""编排器 LLM 调用：结构化输出校验、决策理由留痕、重试退避、日预算记账。

无真实 key / LLM_CHANNEL=mock 时走 MockOrchestratorLLM，保证 CI 可跑。
真实通道复用 gateway_llm 的 OpenAI 兼容 HTTP 与牌价折算纪律。
主模型超时/5xx/限流时按 LLM_FALLBACK_MODEL 等切换备用；全部失败回落本地模板并标 degraded。
备用调用同样计入 LLM_DAILY_BUDGET_USD。
"""

from __future__ import annotations

import copy
import json
import os
import re
import time
from typing import Any, Callable, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from shopkeeper.adapters.base import _build_http_client
from shopkeeper.adapters.gateway_llm import PRICE_USD_PER_MTOK, TASK_PROFILES
from shopkeeper.adapters.llm_failover import (
    CODE_LLM_FALLBACK_EXHAUSTED,
    fallback_base_url,
    is_model_failover_error,
    model_chain,
    template_orchestrator_payload,
)
from shopkeeper.errors import AdapterError, ErrorKind
from shopkeeper.models import AdapterResult
from shopkeeper.orchestrator.budget import check_budget
from shopkeeper.orchestrator.store import OrchestratorStore
from shopkeeper.retry import RetryConfig, call_with_retry, compute_backoff
from shopkeeper.workflow.alerts import emit_alert

PROMPT_VERSION = "orch-structured-v1"
CODE_ORCH_STEP_RETRIES_EXCEEDED = "ORCH_STEP_RETRIES_EXCEEDED"

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)
T = TypeVar("T", bound=BaseModel)


def use_mock_llm(env: dict[str, str] | None = None) -> bool:
    source = env if env is not None else os.environ
    channel = str(source.get("LLM_CHANNEL", "") or source.get("ORCHESTRATOR_LLM", "") or "mock").strip().lower()
    if channel in {"mock", "offline", "0", "false"}:
        return True
    key = str(source.get("SHOPKEEPER_LLM_API_KEY", "") or "").strip()
    # 本机网关占位不算「有真实 key」
    if not key or key in {"local-gateway-no-auth", "changeme", "your-key"}:
        # 若显式 gateway/openai 且有 base_url，仍允许；否则 mock
        if channel in {"gateway", "openai", "real"}:
            return False
        return True
    return False


class StructuredLLMOutcome(BaseModel):
    data: dict[str, Any]
    rationale: str = ""
    model: str = "mock"
    prompt_version: str = PROMPT_VERSION
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0
    attempts: int = 1
    degraded: bool = False
    degrade_code: str | None = None


class OrchestratorLLM:
    """统一入口：complete_json + 预算检查 + 用量落库。"""

    def __init__(
        self,
        store: OrchestratorStore,
        *,
        env: dict[str, str] | None = None,
        transport: Any | None = None,
        sleep_fn: Callable[[float], None] | None = None,
    ) -> None:
        self.store = store
        self.env = env if env is not None else os.environ
        self._transport = transport
        self._sleep = sleep_fn or time.sleep
        self.mock = use_mock_llm(self.env)
        self.base_url = (
            str(self.env.get("SHOPKEEPER_LLM_BASE_URL") or "http://127.0.0.1:3425/v1")
        ).rstrip("/")
        self.model = str(self.env.get("SHOPKEEPER_LLM_MODEL") or "group/auto-deepseek-v4-1-flash")
        self.api_key = str(self.env.get("SHOPKEEPER_LLM_API_KEY") or "local-gateway-no-auth")
        self.profiles = copy.deepcopy(TASK_PROFILES)
        self.prices = copy.deepcopy(PRICE_USD_PER_MTOK)
        self._fallback_base = fallback_base_url(dict(self.env), default=self.base_url)

    def complete_json(
        self,
        *,
        run_id: str,
        step_id: str,
        system: str,
        user: str,
        schema: type[T],
        task: str = "classification",
        mock_payload: dict[str, Any] | None = None,
        max_retries: int = 2,
        backoff_base_s: float = 0.05,
        backoff_max_s: float = 2.0,
    ) -> AdapterResult[StructuredLLMOutcome]:
        spent = self.store.spent_usd_today()
        gate = check_budget(spent, env=dict(self.env), emit=True, run_id=run_id)
        if gate["exceeded"]:
            return AdapterResult.failure(
                AdapterError.policy(
                    "LLM_BUDGET_EXCEEDED",
                    f"日预算已用尽 spent={gate['spent']} limit={gate['limit']}",
                    spent=gate["spent"],
                    limit=gate["limit"],
                ),
                "orchestrator_llm",
            )

        def once() -> AdapterResult[StructuredLLMOutcome]:
            if self.mock:
                return self._mock_once(schema, mock_payload)
            return self._gateway_with_failover(system, user, schema, task, mock_payload)

        cfg = RetryConfig(
            max_retries=max_retries,
            backoff_base_s=backoff_base_s,
            backoff_max_s=backoff_max_s,
        )

        def on_retry(attempt: int, last: AdapterResult[StructuredLLMOutcome], delay: float) -> None:
            wait = delay if delay > 0 else compute_backoff(
                attempt, base_s=backoff_base_s, max_s=backoff_max_s
            )
            self._sleep(wait)

        result, attempts = call_with_retry(
            once,
            config=cfg,
            on_retry=on_retry,
            is_retryable=lambda r: bool(r.error and r.error.kind == ErrorKind.GENERATION),
        )
        if not result.ok or result.data is None:
            if attempts > max_retries:
                emit_alert(
                    CODE_ORCH_STEP_RETRIES_EXCEEDED,
                    job_type="orchestrator",
                    order_id=run_id,
                )
            return result

        data = result.data
        data.attempts = attempts
        # 预估本笔会不会撞预算（mock / 本地模板成本可为 0）
        projected = spent + float(data.cost_usd or 0.0)
        post = check_budget(projected, env=dict(self.env), emit=False, run_id=run_id)
        if post["exceeded"] and float(data.cost_usd or 0.0) > 0:
            # 已拿到结果但记账后会超：仍记账并标预算失败，由引擎暂停
            self.store.record_llm_usage(
                run_id=run_id,
                step_id=step_id,
                model=data.model,
                prompt_version=data.prompt_version,
                prompt_tokens=data.prompt_tokens,
                completion_tokens=data.completion_tokens,
                cost_usd=data.cost_usd,
                rationale=data.rationale,
            )
            check_budget(self.store.spent_usd_today(), env=dict(self.env), emit=True, run_id=run_id)
            return AdapterResult.failure(
                AdapterError.policy(
                    "LLM_BUDGET_EXCEEDED",
                    f"本笔调用后日预算将超限 spent={projected} limit={post['limit']}",
                    spent=projected,
                    limit=post["limit"],
                ),
                "orchestrator_llm",
            )

        self.store.record_llm_usage(
            run_id=run_id,
            step_id=step_id,
            model=data.model,
            prompt_version=data.prompt_version,
            prompt_tokens=data.prompt_tokens,
            completion_tokens=data.completion_tokens,
            cost_usd=data.cost_usd,
            rationale=data.rationale,
        )
        return AdapterResult.success(data, "orchestrator_llm", data.latency_ms)

    def _mock_once(
        self,
        schema: type[T],
        mock_payload: dict[str, Any] | None,
    ) -> AdapterResult[StructuredLLMOutcome]:
        payload = dict(mock_payload or {"rationale": "mock decision", "ok": True})
        try:
            parsed = schema.model_validate(payload)
        except ValidationError as exc:
            return AdapterResult.failure(
                AdapterError.generation("LLM_SCHEMA_INVALID", str(exc.errors())[:200]),
                "mock_orchestrator_llm",
            )
        data = parsed.model_dump()
        rationale = str(data.get("rationale") or "mock")
        # mock 计费可配置，便于测预算
        cost = float(self.env.get("ORCHESTRATOR_MOCK_LLM_COST_USD", "0") or 0)
        tokens_p = int(self.env.get("ORCHESTRATOR_MOCK_PROMPT_TOKENS", "100") or 100)
        tokens_c = int(self.env.get("ORCHESTRATOR_MOCK_COMPLETION_TOKENS", "50") or 50)
        outcome = StructuredLLMOutcome(
            data=data,
            rationale=rationale,
            model="mock_orchestrator_llm",
            prompt_version=PROMPT_VERSION,
            prompt_tokens=tokens_p,
            completion_tokens=tokens_c,
            cost_usd=cost,
            latency_ms=0,
            degraded=False,
        )
        return AdapterResult.success(outcome, "mock_orchestrator_llm", 0)

    def _gateway_with_failover(
        self,
        system: str,
        user: str,
        schema: type[T],
        task: str,
        mock_payload: dict[str, Any] | None,
    ) -> AdapterResult[StructuredLLMOutcome]:
        chain = model_chain(dict(self.env), primary=self.model)
        if not chain:
            chain = [self.model]
        failover_errors: list[dict[str, Any]] = []
        last_non_failover: AdapterResult[StructuredLLMOutcome] | None = None
        total_ms = 0

        for idx, model_name in enumerate(chain):
            # 每换模型前再查预算：备用调用也计入同一日限额
            spent = self.store.spent_usd_today()
            gate = check_budget(spent, env=dict(self.env), emit=True)
            if gate["exceeded"]:
                return AdapterResult.failure(
                    AdapterError.policy(
                        "LLM_BUDGET_EXCEEDED",
                        f"日预算已用尽 spent={gate['spent']} limit={gate['limit']}",
                        spent=gate["spent"],
                        limit=gate["limit"],
                    ),
                    "orchestrator_llm",
                )
            base = self.base_url if idx == 0 else (self._fallback_base or self.base_url)
            once = self._gateway_once(system, user, schema, task, model_name, base)
            total_ms += once.duration_ms
            if once.ok and once.data is not None:
                once.data.latency_ms = total_ms
                return once
            err = once.error
            if is_model_failover_error(err):
                failover_errors.append(
                    {"model": model_name, "code": err.code if err else "?", "message": err.message if err else ""}
                )
                continue
            last_non_failover = once
            break

        if last_non_failover is not None:
            return last_non_failover

        # 全部超时/5xx/限流 → 本地模板 + degraded
        payload = template_orchestrator_payload(mock_payload)
        try:
            parsed = schema.model_validate(payload)
        except ValidationError:
            # 模板对不齐 schema 时仍尽量交最小合法字段
            payload = {"rationale": "local template after LLM failover exhausted"}
            try:
                parsed = schema.model_validate(payload)
            except ValidationError as exc:
                return AdapterResult.failure(
                    AdapterError.generation(
                        CODE_LLM_FALLBACK_EXHAUSTED,
                        f"备用模型耗尽且本地模板无法满足 schema：{str(exc.errors())[:160]}",
                        failover_errors=failover_errors,
                    ),
                    "orchestrator_llm",
                    total_ms,
                )
        data = parsed.model_dump()
        outcome = StructuredLLMOutcome(
            data=data,
            rationale=str(data.get("rationale") or "local template"),
            model="local_template",
            prompt_version=PROMPT_VERSION,
            prompt_tokens=0,
            completion_tokens=0,
            cost_usd=0.0,
            latency_ms=total_ms,
            attempts=max(1, len(failover_errors)),
            degraded=True,
            degrade_code=CODE_LLM_FALLBACK_EXHAUSTED,
        )
        return AdapterResult.success(outcome, "orchestrator_llm", total_ms)

    def _gateway_once(
        self,
        system: str,
        user: str,
        schema: type[T],
        task: str,
        model_name: str,
        base_url: str,
    ) -> AdapterResult[StructuredLLMOutcome]:
        profile = self.profiles.get(task) or self.profiles["classification"]
        body = {
            "model": model_name,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": profile["temperature"],
            "max_tokens": profile["max_tokens"],
            "reasoning_effort": profile.get("reasoning_effort", "low"),
        }
        t0 = time.monotonic()
        try:
            if self._transport is not None:
                raw = self._transport(body)
            else:
                with _build_http_client(timeout=float(profile["timeout_s"])) as client:
                    r = client.post(
                        f"{base_url.rstrip('/')}/chat/completions",
                        json=body,
                        headers={"Authorization": f"Bearer {self.api_key}"},
                    )
                    try:
                        payload = r.json()
                    except Exception:  # noqa: BLE001
                        payload = {"raw_text": r.text[:500]}
                    raw = {"status_code": r.status_code, "json": payload}
        except httpx.TimeoutException:
            return AdapterResult.failure(
                AdapterError.generation("LLM_TIMEOUT", "编排器网关超时"),
                "gateway_llm",
            )
        except (httpx.HTTPError, OSError) as exc:
            return AdapterResult.failure(
                AdapterError.generation("LLM_TRANSPORT", f"{type(exc).__name__}: {exc}"[:200]),
                "gateway_llm",
            )

        ms = int((time.monotonic() - t0) * 1000)
        status = raw.get("status_code")
        if status and int(status) >= 400:
            return AdapterResult.failure(
                AdapterError.generation(f"LLM_HTTP_{status}", str(raw.get("json"))[:200]),
                "gateway_llm",
                ms,
            )
        data = raw.get("json") or {}
        choices = data.get("choices") or []
        if not choices:
            return AdapterResult.failure(
                AdapterError.generation("LLM_NO_CHOICES", "无 choices"),
                "gateway_llm",
                ms,
            )
        content = str((choices[0].get("message") or {}).get("content") or "")
        if not content.strip():
            return AdapterResult.failure(
                AdapterError.generation("LLM_EMPTY_CONTENT", "content 为空"),
                "gateway_llm",
                ms,
            )
        parsed_obj = _extract_json(content)
        if isinstance(parsed_obj, AdapterError):
            return AdapterResult.failure(parsed_obj, "gateway_llm", ms)
        try:
            model_out = schema.model_validate(parsed_obj)
        except ValidationError as exc:
            bad = "; ".join(f"{'.'.join(str(x) for x in e['loc'])}: {e['type']}" for e in exc.errors()[:4])
            return AdapterResult.failure(
                AdapterError.generation("LLM_SCHEMA_INVALID", f"结构校验失败 → {bad}"),
                "gateway_llm",
                ms,
            )
        usage = data.get("usage") or {}
        pt = int(usage.get("prompt_tokens") or 0)
        ct = int(usage.get("completion_tokens") or 0)
        dumped = model_out.model_dump()
        outcome = StructuredLLMOutcome(
            data=dumped,
            rationale=str(dumped.get("rationale") or ""),
            model=str(data.get("model") or model_name),
            prompt_version=PROMPT_VERSION,
            prompt_tokens=pt,
            completion_tokens=ct,
            cost_usd=self._cost(pt, ct, model_name=model_name) or 0.0,
            latency_ms=ms,
            degraded=False,
        )
        return AdapterResult.success(outcome, "gateway_llm", ms)

    def _cost(self, prompt_tokens: int, completion_tokens: int, *, model_name: str | None = None) -> float | None:
        price = self.prices.get(model_name or self.model)
        if price is None:
            return None
        in_p, out_p = price
        return round((prompt_tokens / 1e6 * in_p) + (completion_tokens / 1e6 * out_p), 6)


def _extract_json(content: str) -> dict[str, Any] | AdapterError:
    text = _FENCE.sub("", content.strip())
    if not text.lstrip().startswith("{"):
        s, e = text.find("{"), text.rfind("}")
        if s == -1 or e == -1:
            return AdapterError.generation("LLM_NOT_JSON", f"响应不是 JSON：{text[:120]!r}")
        text = text[s : e + 1]
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        return AdapterError.generation("LLM_JSON_DECODE", f"JSON 解析失败：{exc.msg}")
    if not isinstance(obj, dict):
        return AdapterError.generation("LLM_SCHEMA_INVALID", "顶层必须是对象")
    return obj


def is_budget_error(err: AdapterError | None) -> bool:
    return bool(err and err.code == "LLM_BUDGET_EXCEEDED" and err.kind == ErrorKind.POLICY)
