"""草稿生成的可插拔对话客户端。

WORKFLOW_LLM_PROVIDER=mock|openai|coze|dify。
openai 走 OpenAI 兼容的 /chat/completions，可指向本机网关。
coze 走开放平台 /v3/chat（非流式，最多轮询两次）。
dify 走 /chat-messages 的 blocking 模式。

选定的提供方缺少令牌或地址时，退回本地模板，并在草稿上标记 degraded。
openai 主模型超时/5xx/限流时按 LLM_FALLBACK_MODEL 切换备用；全部失败同样回落模板。
退回不是失败，也不会把模板说成模型输出。
"""

from __future__ import annotations

import json
import os
from typing import Any

import httpx

from shopkeeper.adapters.base import _build_http_client
from shopkeeper.adapters.llm_failover import fallback_base_url, is_model_failover_error, model_chain
from shopkeeper.errors import AdapterError
from shopkeeper.models import AdapterResult
from shopkeeper.security.redaction import redact_spans

_MOCK_NOTE = "本地模板只复述你提供的事实，不是模型撰写，也不是销量结论。"


def _redact_prompt(text: str) -> str:
    """外呼前只抹自由文本里的手机号、邮箱等片段。不按字段名整段丢掉商品名。"""
    cleaned, _hit = redact_spans(text)
    return cleaned


def _read_json(response: httpx.Response, code: str, label: str) -> dict[str, Any] | AdapterError:
    """网关返回 HTML 或空壳时必须是 AdapterResult 失败，不能抛 JSONDecodeError。"""
    if not response.content:
        return {}
    try:
        payload = response.json()
    except (json.JSONDecodeError, ValueError):
        return AdapterError.generation(code, f"{label}响应不是 JSON")
    if not isinstance(payload, dict):
        return AdapterError.generation(code, f"{label}响应不是 JSON")
    return payload


def render_mock(kind: str, item: dict[str, str]) -> dict[str, str]:
    name = (item.get("name") or "สินค้าจัดเก็บ").strip()
    specs = (item.get("specs") or item.get("message") or "ไม่มีข้อมูลที่ผู้ขายกรอก").strip()
    if kind == "video_script":
        return {
            "title_th": f"สคริปต์ 30 วินาที {name}",
            "title_en": f"30s script draft: {name}",
            "body_th": (
                "0-3 วินาที: ภาพลิ้นชักรก\n"
                f"3-20 วินาที: แสดง {name} ตามข้อเท็จจริงที่ผู้ขายกรอก: {specs}\n"
                "ท้ายคลิป: ราคาตามหน้าร้าน อย่าพูดคำสัญญาที่ยังไม่ได้ตรวจ"
            ),
            "body_en": f"Show only seller-provided facts for {name}: {specs}. End on the live product page price.",
        }
    if kind == "customer_reply":
        message = (item.get("message") or specs).strip()
        return {
            "title_th": "ร่างตอบลูกค้า",
            "title_en": "Customer reply draft",
            "body_th": (
                "ขอบคุณที่ติดต่อมา เจ้าหน้าที่จะตรวจข้อความนี้ก่อนส่ง\n"
                f"ข้อความลูกค้า: {message}\n"
                "ยังไม่ยืนยันวันจัดส่ง เงินคืน หรือที่อยู่ใหม่"
            ),
            "body_en": f"Draft only. Customer said: {message}. No delivery promise, refund, or address change.",
        }
    return {
        "title_th": f"กล่องจัดเก็บ {name}",
        "title_en": f"Home storage draft: {name}",
        "body_th": f"รายละเอียดตามที่ผู้ขายกรอกเท่านั้น: {specs}",
        "body_en": f"Seller-provided facts only: {specs}",
    }


class ChatProvider:
    name = "mock"
    degraded = False
    reason = ""

    def complete(self, system: str, user: str) -> AdapterResult[str]:
        raise NotImplementedError


class MockChatProvider(ChatProvider):
    name = "mock"
    reason = _MOCK_NOTE

    def complete(self, system: str, user: str) -> AdapterResult[str]:
        return AdapterResult.success("", self.name)


class DegradedChatProvider(MockChatProvider):
    degraded = True

    def __init__(self, reason: str) -> None:
        self.reason = reason


class OpenAIChatProvider(ChatProvider):
    name = "openai"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        transport: httpx.BaseTransport | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.env = env if env is not None else os.environ
        self._fallback_base = fallback_base_url(dict(self.env), default=self.base_url)
        self._client = _build_http_client(transport=transport, timeout=120.0)
        #: usage 累计（P0-08）。此前 workflow 侧**完全没有 token 计量**，
        #: 于是「这次生成花了多少」永远是缺数据。每成功一次就累加。
        self.usage: dict[str, int] = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        #: 最近一次成功调用用的模型名（已发生主备切换时能看出实际是哪条）。
        self.last_model: str = "" 

    def complete(self, system: str, user: str) -> AdapterResult[str]:
        system = _redact_prompt(system)
        user = _redact_prompt(user)
        chain = model_chain(dict(self.env), primary=self.model)
        if not chain:
            chain = [self.model]
        last: AdapterResult[str] | None = None
        for idx, model_name in enumerate(chain):
            base = self.base_url if idx == 0 else (self._fallback_base or self.base_url)
            once = self._complete_once(system, user, model_name, base)
            if once.ok:
                return once
            last = once
            if is_model_failover_error(once.error):
                continue
            return once
        return last or AdapterResult.failure(
            AdapterError.generation("LLM_HTTP_ERROR", "对话接口失败"),
            self.name,
        )

    def _complete_once(self, system: str, user: str, model_name: str, base_url: str) -> AdapterResult[str]:
        try:
            response = self._client.post(
                f"{base_url.rstrip('/')}/chat/completions",
                headers={"authorization": f"Bearer {self.api_key}", "content-type": "application/json"},
                json={
                    "model": model_name,
                    "temperature": 0.3,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                },
            )
        except httpx.TimeoutException:
            return AdapterResult.failure(
                AdapterError.generation("LLM_TIMEOUT", "对话接口超时"),
                self.name,
            )
        except httpx.HTTPError as exc:
            return AdapterResult.failure(
                AdapterError.generation("LLM_HTTP_ERROR", f"对话接口失败：{type(exc).__name__}"),
                self.name,
            )
        payload = _read_json(response, "LLM_HTTP_ERROR", "对话接口")
        if isinstance(payload, AdapterError):
            return AdapterResult.failure(payload, self.name)
        if response.status_code >= 400:
            # 精确码便于主备切换（429/5xx）；其余仍用笼统码
            code = (
                f"LLM_HTTP_{response.status_code}"
                if response.status_code == 429 or response.status_code >= 500
                else "LLM_HTTP_ERROR"
            )
            return AdapterResult.failure(
                AdapterError.generation(code, f"对话接口 HTTP {response.status_code}"),
                self.name,
            )
        choices = payload.get("choices") or []
        content = ""
        if choices and isinstance(choices[0], dict):
            content = str((choices[0].get("message") or {}).get("content") or "")
        if not content.strip():
            return AdapterResult.failure(AdapterError.generation("LLM_EMPTY", "模型返回空内容。"), self.name)
        self._record_usage(payload, model_name)
        return AdapterResult.success(content, self.name)

    def _record_usage(self, payload: dict[str, Any], model_name: str) -> None:
        """累计一次成功调用的 token 用量。

        **只读 `completion_tokens` 总量，绝不读 `reasoning_tokens`** ——
        网关那个字段恒报 0（见 `gateway_llm` 模块头注释），拿它折算会严重低估。
        拿不到 usage 就只加 calls，不编造数字。
        """
        self.usage["calls"] += 1
        self.last_model = model_name
        usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
        for src, dst in (
            ("prompt_tokens", "prompt_tokens"),
            ("completion_tokens", "completion_tokens"),
            ("total_tokens", "total_tokens"),
        ):
            try:
                self.usage[dst] += int(usage.get(src) or 0)
            except (TypeError, ValueError):
                continue

    def cost_usd(self) -> float | None:
        """按厂商公开牌价折算（**非网关实扣**）。拿不到价目返回 None，不猜。"""
        from shopkeeper.adapters.gateway_llm import PRICE_USD_PER_MTOK

        price = PRICE_USD_PER_MTOK.get(self.last_model or self.model)
        if price is None:
            return None
        prompt_price, completion_price = price
        return (
            self.usage["prompt_tokens"] * prompt_price
            + self.usage["completion_tokens"] * completion_price
        ) / 1_000_000


class DifyChatProvider(ChatProvider):
    name = "dify"

    def __init__(self, *, base_url: str, api_key: str, user: str, transport: httpx.BaseTransport | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.user = user
        self._client = _build_http_client(transport=transport, timeout=120.0)

    def complete(self, system: str, user: str) -> AdapterResult[str]:
        system = _redact_prompt(system)
        user = _redact_prompt(user)
        try:
            response = self._client.post(
                f"{self.base_url}/chat-messages",
                headers={"authorization": f"Bearer {self.api_key}", "content-type": "application/json"},
                json={
                    "inputs": {},
                    "query": f"{system}\n\n{user}",
                    "response_mode": "blocking",
                    "user": self.user,
                },
            )
        except httpx.HTTPError as exc:
            return AdapterResult.failure(
                AdapterError.generation("DIFY_HTTP_ERROR", f"Dify 请求失败：{type(exc).__name__}"),
                self.name,
            )
        payload = _read_json(response, "DIFY_HTTP_ERROR", "Dify")
        if isinstance(payload, AdapterError):
            return AdapterResult.failure(payload, self.name)
        if response.status_code >= 400:
            return AdapterResult.failure(
                AdapterError.generation("DIFY_HTTP_ERROR", f"Dify HTTP {response.status_code}"),
                self.name,
            )
        answer = str(payload.get("answer") or "")
        if not answer.strip():
            return AdapterResult.failure(AdapterError.generation("DIFY_EMPTY", "Dify 没有返回答案。"), self.name)
        return AdapterResult.success(answer, self.name)


class CozeChatProvider(ChatProvider):
    name = "coze"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        bot_id: str,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.bot_id = bot_id
        self._client = _build_http_client(transport=transport, timeout=30.0)

    def complete(self, system: str, user: str) -> AdapterResult[str]:
        system = _redact_prompt(system)
        user = _redact_prompt(user)
        headers = {"authorization": f"Bearer {self.api_key}", "content-type": "application/json"}
        try:
            created = self._client.post(
                f"{self.base_url}/v3/chat",
                headers=headers,
                json={
                    "bot_id": self.bot_id,
                    "user_id": "shopkeeper-th",
                    "stream": False,
                    "auto_save_history": False,
                    "additional_messages": [
                        {"role": "user", "content": f"{system}\n\n{user}", "content_type": "text"}
                    ],
                },
            )
        except httpx.HTTPError as exc:
            return AdapterResult.failure(
                AdapterError.generation("COZE_HTTP_ERROR", f"Coze 请求失败：{type(exc).__name__}"),
                self.name,
            )
        payload = _read_json(created, "COZE_HTTP_ERROR", "Coze")
        if isinstance(payload, AdapterError):
            return AdapterResult.failure(payload, self.name)
        if created.status_code >= 400 or payload.get("code") not in (0, "0", None):
            if created.status_code >= 400 or (payload.get("code") not in (0, "0") and "data" not in payload):
                return AdapterResult.failure(
                    AdapterError.generation("COZE_HTTP_ERROR", f"Coze 返回 code={payload.get('code')}"),
                    self.name,
                )
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        status = str(data.get("status") or "")
        conversation_id = str(data.get("conversation_id") or "")
        chat_id = str(data.get("id") or "")
        if status and status != "completed" and conversation_id and chat_id:
            retrieved = self._client.get(
                f"{self.base_url}/v3/chat/retrieve",
                headers=headers,
                params={"conversation_id": conversation_id, "chat_id": chat_id},
            )
            retrieved_payload = _read_json(retrieved, "COZE_HTTP_ERROR", "Coze")
            if isinstance(retrieved_payload, AdapterError):
                return AdapterResult.failure(retrieved_payload, self.name)
            data = retrieved_payload.get("data") if isinstance(retrieved_payload.get("data"), dict) else data
            status = str(data.get("status") or status)
        if status and status != "completed":
            return AdapterResult.failure(
                AdapterError.generation("COZE_PENDING", "Coze 还没有完成，本次不编造文案。"),
                self.name,
            )
        if conversation_id and chat_id:
            messages = self._client.get(
                f"{self.base_url}/v3/chat/message/list",
                headers=headers,
                params={"conversation_id": conversation_id, "chat_id": chat_id},
            )
            message_payload = _read_json(messages, "COZE_HTTP_ERROR", "Coze")
            if isinstance(message_payload, AdapterError):
                return AdapterResult.failure(message_payload, self.name)
            rows = message_payload.get("data") if isinstance(message_payload.get("data"), list) else []
            for row in rows:
                if isinstance(row, dict) and row.get("type") == "answer" and row.get("content"):
                    return AdapterResult.success(str(row["content"]), self.name)
        answer = str(data.get("content") or payload.get("answer") or "")
        if not answer.strip():
            return AdapterResult.failure(AdapterError.generation("COZE_EMPTY", "Coze 没有返回回答。"), self.name)
        return AdapterResult.success(answer, self.name)


def build_provider(env: dict[str, str] | None = None, *, transport: httpx.BaseTransport | None = None) -> ChatProvider:
    source = env if env is not None else os.environ
    kind = source.get("WORKFLOW_LLM_PROVIDER", "mock").strip().lower() or "mock"
    if kind in ("", "mock"):
        return MockChatProvider()
    if kind == "openai":
        api_key = source.get("WORKFLOW_LLM_API_KEY", "").strip() or source.get("SHOPKEEPER_LLM_API_KEY", "").strip()
        base_url = source.get("WORKFLOW_LLM_BASE_URL", "").strip() or source.get("SHOPKEEPER_LLM_BASE_URL", "").strip()
        model = source.get("WORKFLOW_LLM_MODEL", "").strip() or source.get("SHOPKEEPER_LLM_MODEL", "").strip()
        if not api_key or not base_url or not model:
            return DegradedChatProvider("已选择 OpenAI 兼容接口，但地址、模型或密钥为空，改用本地模板。")
        return OpenAIChatProvider(
            base_url=base_url,
            api_key=api_key,
            model=model,
            transport=transport,
            env=dict(source),
        )
    if kind == "dify":
        api_key = source.get("DIFY_API_KEY", "").strip()
        base_url = source.get("DIFY_API_BASE", "").strip()
        if not api_key or not base_url:
            return DegradedChatProvider("已选择 Dify，但 DIFY_API_KEY 或 DIFY_API_BASE 为空，改用本地模板。")
        return DifyChatProvider(
            base_url=base_url,
            api_key=api_key,
            user=source.get("DIFY_APP_USER", "shopkeeper-th").strip() or "shopkeeper-th",
            transport=transport,
        )
    if kind == "coze":
        api_key = source.get("COZE_API_TOKEN", "").strip()
        bot_id = source.get("COZE_BOT_ID", "").strip()
        base_url = source.get("COZE_API_BASE", "https://api.coze.com").strip() or "https://api.coze.com"
        if not api_key or not bot_id:
            return DegradedChatProvider("已选择 Coze，但 COZE_API_TOKEN 或 COZE_BOT_ID 为空，改用本地模板。")
        return CozeChatProvider(base_url=base_url, api_key=api_key, bot_id=bot_id, transport=transport)
    return DegradedChatProvider(f"未知的 WORKFLOW_LLM_PROVIDER={kind}，改用本地模板。")


def parse_model_json(raw: str) -> dict[str, Any] | None:
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None
