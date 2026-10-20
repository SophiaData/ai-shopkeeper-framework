"""真实 LLM 适配器：OpenAI 兼容网关（A2 spike 实测通道）。

**两条 A2 实测结论直接写进默认配置**，不是注释里的建议：
1. `reasoning_content` 与 `content` 共享 `max_tokens` 预算
   → 文案任务缺省 `max_tokens=12000`（4000 实测 0/3 全败）、`reasoning_effort="low"`、`timeout≥240s`。
2. 网关的 `completion_tokens_details.reasoning_tokens` **恒报 0**（同批 run 思考流实测 7k–28k 字符）
   → 成本只用 `completion_tokens` 总量折算，**绝不读 reasoning_tokens**。

预算（max_tokens / temperature / reasoning_effort）**按任务路由**，不做全局常量：
文案要 12000，分类小任务 900 就够——但分类任务同样会被思考吃满，所以也给了下限。
"""

from __future__ import annotations

import copy
import json
import os
import re
import time
from typing import Any

import httpx
from pydantic import BaseModel, Field, ValidationError

from shopkeeper.adapters.base import LlmClient, _build_http_client
from shopkeeper.adapters.llm_failover import (
    fallback_base_url,
    is_model_failover_error,
    model_chain,
    template_listing_outcome,
)
from shopkeeper.errors import AdapterError
from shopkeeper.models import (
    AdapterResult,
    LangVersion,
    ListingPackage,
    LlmListingOutcome,
    LlmUsage,
    ProductFacts,
)

#: 按任务路由的调用预算（A2 §4/§11 实测底线）
TASK_PROFILES: dict[str, dict[str, Any]] = {
    "copywriting": {"max_tokens": 12000, "temperature": 0.3, "reasoning_effort": "low", "timeout_s": 240.0},
    "classification": {"max_tokens": 2000, "temperature": 0.0, "reasoning_effort": "low", "timeout_s": 90.0},
}

#: 厂商公开牌价（USD / 1M tokens）。**只用于折算，非网关实扣**；拿不到价目就不折算。
#: 注意：网关无费用接口（A2 实测 /pricing、/v1/usage 全 404）。
PRICE_USD_PER_MTOK: dict[str, tuple[float, float]] = {
    "deepseek-flash": (0.14, 0.28),
    "deepseek-v4-flash": (0.14, 0.28),
    "group/auto-deepseek-v4-1-flash": (0.14, 0.28),
    # 旧 id 保留为**查价别名**：它在本机网关上不存在（调用会 404），但用户 .env 里
    # 可能还写着它，历史审计记录也用它。删掉会让那些调用折算成 0 成本（实测把
    # test_orchestrator_failover_and_budget_counts_fallback 打红）。
    "group/auto-deepseek-v4-flash": (0.14, 0.28),
    "deepseek-v4-pro": (0.55, 2.19),
    "group/auto-deepseek-v4-pro": (0.55, 2.19),
}

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


class _ModelVersion(BaseModel):
    lang: str
    title: str = Field(min_length=1)
    bullets: list[str] = Field(min_length=3)
    description: str = Field(min_length=1)
    advisory_flags: list[str] = Field(default_factory=list)


class _ModelOutput(BaseModel):
    langs: list[str] = Field(default_factory=list)
    versions: list[_ModelVersion] = Field(min_length=1)


def build_prompt(product: ProductFacts, langs: list[str], site: str) -> str:
    lang_names = {"EN": "English", "TH": "Thai", "VI": "Vietnamese", "ID": "Indonesian", "MS": "Malay"}
    listed = ", ".join(f"{l}({lang_names.get(l.upper(), l)})" for l in langs)
    # **不把成本价和 MOQ 写进 prompt**。之前写了，模型就照抄进买家文案
    # （实测 3 次里 2 次冒出 "MOQ: 1"）——等于我们自己把供给侧事实递过去，
    # 再指望它别说。供给侧信息对写文案没有任何必要。
    return (
        "You are a TikTok Shop listing copywriter for Southeast Asia.\n"
        f"Write product listing copy in these languages ONLY: {listed}.\n\n"
        f"Product title (source, Chinese): {product.title_raw}\n"
        "Source facts are the ONLY product facts you may use. Do not invent certifications, "
        "UPF/ratings, clinical claims, sales numbers, or brand names not present above. "
        "Do not use medical or absolute-efficacy wording.\n\n"
        "SUPPLIER-SIDE INFORMATION MUST NOT APPEAR IN THE COPY: no MOQ, minimum order quantity, "
        "wholesale, bulk order, unit price, cost, stock level, currency, ordering terms, or "
        "supplier/shop identity. This is buyer-facing retail copy.\n\n"
        "Return STRICT JSON only, no prose, matching exactly:\n"
        '{"langs": [...], "versions": [{"lang": "...", "title": "...", '
        '"bullets": ["...", "...", "..."], "description": "...", '
        '"advisory_flags": ["..."]}]}\n'
        "One entry per requested language, same order. advisory_flags holds wording a human should "
        "double-check (origin / health / superlative claims); use an empty array if none. "
        "advisory_flags are notes ABOUT the copy, never part of it."
    )


class GatewayLLM(LlmClient):
    """真实网关客户端。失败一律 AdapterResult.failure，不裸抛。"""

    name = "gateway_llm"
    deterministic = False  # 真实模型做不到逐字节复现（契约套件据此放宽该断言）

    def __init__(
        self,
        *,
        base_url: str | None = None,
        model: str | None = None,
        api_key: str | None = None,
        task: str = "copywriting",
        profiles: dict[str, dict[str, Any]] | None = None,
        prices: dict[str, tuple[float, float]] | None = None,
        transport: Any | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        self.env = env if env is not None else os.environ
        self.base_url = (
            base_url or self.env.get("SHOPKEEPER_LLM_BASE_URL") or "http://127.0.0.1:3425/v1"
        ).rstrip("/")
        self.model = model or self.env.get("SHOPKEEPER_LLM_MODEL") or "group/auto-deepseek-v4-1-flash"
        # 密钥只从环境读；本机网关免鉴权时给占位串，代码里不出现明文
        self.api_key = api_key or self.env.get("SHOPKEEPER_LLM_API_KEY") or "local-gateway-no-auth"
        self.task = task
        # 深拷贝：调用方或别处改了全局预算表，不该悄悄改变已构造实例的外呼参数
        self.profiles = copy.deepcopy(profiles or TASK_PROFILES)
        self.prices = copy.deepcopy(prices) if prices is not None else copy.deepcopy(PRICE_USD_PER_MTOK)
        self._transport = transport  # 测试注入用；None 时走真 httpx
        self._fallback_base = fallback_base_url(dict(self.env), default=self.base_url)

    # ------------------------------------------------------------------

    def generate_listing(self, product, langs, site="SEA") -> AdapterResult[LlmListingOutcome]:
        if not langs:
            return AdapterResult.failure(AdapterError.validation("LLM_NO_LANGS", "语种列表为空"), self.name)
        profile = self.profiles.get(self.task)
        if profile is None:
            return AdapterResult.failure(
                AdapterError.validation("LLM_UNKNOWN_TASK", f"未配置任务预算 {self.task}"), self.name
            )

        chain = model_chain(dict(self.env), primary=self.model)
        if not chain:
            chain = [self.model]
        failover_errors: list[dict[str, Any]] = []
        last_non_failover: AdapterResult[LlmListingOutcome] | None = None
        total_ms = 0

        for idx, model_name in enumerate(chain):
            base = self.base_url if idx == 0 else (self._fallback_base or self.base_url)
            once = self._generate_once(product, list(langs), site, profile, model_name, base)
            total_ms += once.duration_ms
            if once.ok and once.data is not None:
                once.data.attempts = idx + 1
                return AdapterResult.success(once.data, self.name, total_ms)
            err = once.error
            if is_model_failover_error(err):
                failover_errors.append(
                    {"model": model_name, "code": err.code if err else "?", "message": err.message if err else ""}
                )
                continue
            last_non_failover = once
            break

        if last_non_failover is not None:
            return AdapterResult.failure(
                last_non_failover.error or AdapterError.generation("LLM_NO_RESULT", "文案失败"),
                self.name,
                total_ms,
            )

        # 主备均因超时/5xx/限流失败 → 本地模板 + degraded（不冒充模型）
        outcome = template_listing_outcome(product, list(langs), site, failover_errors=failover_errors)
        outcome.attempts = max(1, len(failover_errors))
        return AdapterResult.success(outcome, self.name, total_ms)

    def _generate_once(
        self,
        product: ProductFacts,
        langs: list[str],
        site: str,
        profile: dict[str, Any],
        model_name: str,
        base_url: str,
    ) -> AdapterResult[LlmListingOutcome]:
        body = {
            "model": model_name,
            "messages": [{"role": "user", "content": build_prompt(product, langs, site)}],
            "temperature": profile["temperature"],
            "max_tokens": profile["max_tokens"],
            "reasoning_effort": profile["reasoning_effort"],
        }
        t0 = time.monotonic()
        try:
            if self._transport is not None:
                raw = self._transport(body)
            else:
                raw = self._post(body, profile["timeout_s"], base_url=base_url)
        except httpx.TimeoutException:
            ms = int((time.monotonic() - t0) * 1000)
            return AdapterResult.failure(
                AdapterError.generation("LLM_TIMEOUT", f"网关超时 >{profile['timeout_s']}s"), self.name, ms
            )
        except (httpx.HTTPError, OSError) as exc:
            ms = int((time.monotonic() - t0) * 1000)
            return AdapterResult.failure(
                AdapterError.generation("LLM_TRANSPORT", f"{type(exc).__name__}: {exc}"[:200]), self.name, ms
            )

        ms = int((time.monotonic() - t0) * 1000)
        status = raw.get("status_code")
        if status and status >= 400:
            body_snip = raw.get("body")
            if body_snip is None:
                body_snip = raw.get("json")
            return AdapterResult.failure(
                AdapterError.generation(f"LLM_HTTP_{status}", str(body_snip)[:200]), self.name, ms
            )

        data = raw.get("json") or {}
        choices = data.get("choices") or []
        if not choices:
            return AdapterResult.failure(
                AdapterError.generation("LLM_NO_CHOICES", "网关返回无 choices"), self.name, ms
            )
        msg = choices[0].get("message") or {}
        content = msg.get("content") or ""
        finish = choices[0].get("finish_reason")
        usage_raw = data.get("usage") or {}

        # 思考吃满预算的典型症状：content 空 + finish_reason=length（A2 §4 实测）
        if not content.strip():
            return AdapterResult.failure(
                AdapterError.generation(
                    "LLM_EMPTY_CONTENT",
                    f"content 为空（finish_reason={finish}，疑思考吃满 max_tokens 预算）",
                    finish_reason=finish,
                    max_tokens=profile["max_tokens"],
                ),
                self.name,
                ms,
            )
        if finish == "length":
            return AdapterResult.failure(
                AdapterError.generation(
                    "LLM_TRUNCATED", "输出被截断（finish_reason=length）→ 需提高该任务 max_tokens 预算",
                    max_tokens=profile["max_tokens"],
                ),
                self.name,
                ms,
            )

        parsed = self._parse(content)
        if isinstance(parsed, AdapterError):
            return AdapterResult.failure(parsed, self.name, ms)

        ordered = self._order_by_request(parsed, langs)
        if isinstance(ordered, AdapterError):
            return AdapterResult.failure(ordered, self.name, ms)

        prompt_tokens = int(usage_raw.get("prompt_tokens") or 0)
        completion_tokens = int(usage_raw.get("completion_tokens") or 0)
        used_model = str(data.get("model") or model_name)
        outcome = LlmListingOutcome(
            package=ListingPackage(
                product_id=product.source_item_id,
                site=site,
                langs=list(langs),
                versions=ordered,
                compliance_report={
                    "engine": "model-self-check(辅轨)+lexicon(主轨在 Stylist 合并)",
                    "model": used_model,
                    "requested_model": model_name,
                },
            ),
            usage=LlmUsage(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                latency_ms=ms,
                cost_amount=self._cost(prompt_tokens, completion_tokens, model_name=model_name),
                cost_currency="USD",
            ),
            attempts=1,
            degraded=False,
        )
        return AdapterResult.success(outcome, self.name, ms)

    # ------------------------------------------------------------------

    def _post(self, body: dict[str, Any], timeout_s: float, *, base_url: str | None = None) -> dict[str, Any]:
        root = (base_url or self.base_url).rstrip("/")
        with _build_http_client(timeout=timeout_s) as client:
            r = client.post(
                f"{root}/chat/completions",
                json=body,
                headers={"Authorization": f"Bearer {self.api_key}"},
            )
            try:
                payload = r.json()
            except Exception:  # noqa: BLE001
                payload = {"raw_text": r.text[:500]}
            return {"status_code": r.status_code, "json": payload}

    @staticmethod
    def _parse(content: str) -> list[LangVersion] | AdapterError:
        text = _FENCE.sub("", content.strip())
        # 容忍模型在 JSON 前后加解释文字：抓第一个 { 到最后一个 }
        if not text.lstrip().startswith("{"):
            s, e = text.find("{"), text.rfind("}")
            if s == -1 or e == -1:
                return AdapterError.generation("LLM_NOT_JSON", f"响应不是 JSON：{text[:120]!r}")
            text = text[s : e + 1]
        try:
            obj = json.loads(text)
        except json.JSONDecodeError as exc:
            return AdapterError.generation("LLM_JSON_DECODE", f"JSON 解析失败：{exc.msg} @pos {exc.pos}")
        try:
            model_out = _ModelOutput.model_validate(obj)
        except ValidationError as exc:
            bad = "; ".join(f"{'.'.join(str(x) for x in e['loc'])}: {e['type']}" for e in exc.errors()[:4])
            return AdapterError.generation("LLM_SCHEMA_INVALID", f"结构校验失败 → {bad}")
        return [
            LangVersion(
                lang=v.lang.upper(),
                title=v.title.strip(),
                bullets=[b.strip() for b in v.bullets if b.strip()],
                description=v.description.strip(),
                compliance_flags=[],  # 硬阻断只由词表在主轨写入
                advisory_flags=[str(f) for f in v.advisory_flags],
            )
            for v in model_out.versions
        ]

    @staticmethod
    def _order_by_request(versions: list[LangVersion], langs: list[str]) -> list[LangVersion] | AdapterError:
        """语种必须与请求一一对应：缺语种就是不合格产出，不能默默少给。"""
        by_lang: dict[str, LangVersion] = {}
        for v in versions:
            by_lang.setdefault(v.lang.upper(), v)
        missing = [l for l in langs if l.upper() not in by_lang]
        if missing:
            return AdapterError.generation("LLM_LANG_MISSING", f"缺语种 {missing}（给了 {sorted(by_lang)}）")
        return [by_lang[l.upper()] for l in langs]

    def _cost(self, prompt_tokens: int, completion_tokens: int, *, model_name: str | None = None) -> float | None:
        """只用 prompt/completion 总量。**不读 reasoning_tokens**——该网关恒报 0（A2 §9.3）。"""
        price = self.prices.get(model_name or self.model)
        if price is None:
            return None
        in_p, out_p = price
        return round((prompt_tokens / 1e6 * in_p) + (completion_tokens / 1e6 * out_p), 6)
