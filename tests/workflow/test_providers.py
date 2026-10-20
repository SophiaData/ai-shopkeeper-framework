"""文案提供方与泰语质检。不访问外网。"""

from __future__ import annotations

import httpx

from shopkeeper.workflow.providers import (
    CozeChatProvider,
    DifyChatProvider,
    OpenAIChatProvider,
    build_provider,
    parse_model_json,
)
from shopkeeper.workflow.thai_qa import review_copy


def test_thai_copy_requires_thai_script_and_rejects_unverified_claims():
    ok = review_copy("กล่องจัดเก็บ", "รายละเอียดตามที่กรอก", require_thai=True)
    assert ok["ok"] is True
    missing = review_copy("Storage box", "facts only", require_thai=True)
    assert missing["ok"] is False
    banned = review_copy("กล่อง", "供应商写了100%防水", require_thai=True)
    assert banned["ok"] is False
    refund = review_copy("ร่าง", "ขอบคุณ", require_thai=True, extra="我要退款")
    assert refund["escalate"] is True
    assert refund["ok"] is False


def test_thai_qa_catches_spaced_and_fullwidth_claims():
    for sample in ("100% 防水", "１００％防水", "食品 级", "กันน้ำ 100%"):
        result = review_copy("กล่อง", sample, require_thai=True)
        assert result["ok"] is False, sample
        assert any(issue.startswith("合规词[") for issue in result["issues"])
        assert result["compliance"]["blocked"] is True
        assert result["hits"]


def test_thai_qa_warn_only_still_ok_for_approval_path():
    """warn 不阻断审批；block 才 ok=False。"""
    warn_only = review_copy(
        "กล่องจัดเก็บ",
        "This is the best ever drawer organizer for home",
        require_thai=True,
    )
    assert warn_only["ok"] is True
    assert warn_only["warnings"]
    assert warn_only["compliance"]["blocked"] is False

    blocked = review_copy("กล่อง", "cure mold with this box", require_thai=True)
    assert blocked["ok"] is False
    assert blocked["compliance"]["block_hits"]
    hit = blocked["compliance"]["block_hits"][0]
    assert "start" in hit and "end" in hit


def test_openai_without_key_degrades_to_labeled_template():
    provider = build_provider({"WORKFLOW_LLM_PROVIDER": "openai"})
    assert provider.degraded is True
    assert provider.name == "mock"
    assert "本地模板" in provider.reason


def test_openai_compatible_client_returns_message_content():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/chat/completions")
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"title_th":"กล่องทดสอบ"}'}}]},
        )

    client = OpenAIChatProvider(
        base_url="http://llm.test/v1",
        api_key="test-key",
        model="test-model",
        transport=httpx.MockTransport(handler),
    )
    result = client.complete("sys", "user")
    assert result.ok
    assert parse_model_json(result.data)["title_th"] == "กล่องทดสอบ"

    selected = build_provider(
        {
            "WORKFLOW_LLM_PROVIDER": "openai",
            "WORKFLOW_LLM_API_KEY": "test-key",
            "WORKFLOW_LLM_BASE_URL": "http://llm.test/v1",
            "WORKFLOW_LLM_MODEL": "test-model",
        }
    )
    assert selected.name == "openai"


def test_dify_and_coze_clients_read_the_answer_field():
    dify = DifyChatProvider(
        base_url="http://dify.test/v1",
        api_key="k",
        user="shopkeeper-th",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"answer": '{"title_th":"กล่อง"}'})),
    )
    assert "กล่อง" in dify.complete("s", "u").data

    def coze(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/v3/chat"):
            return httpx.Response(
                200,
                json={"code": 0, "data": {"id": "c1", "conversation_id": "v1", "status": "completed"}},
            )
        return httpx.Response(200, json={"code": 0, "data": [{"type": "answer", "content": '{"title_th":"กล่อง"}'}]})

    client = CozeChatProvider(
        base_url="http://coze.test",
        api_key="k",
        bot_id="bot",
        transport=httpx.MockTransport(coze),
    )
    assert "กล่อง" in client.complete("s", "u").data


def test_non_json_provider_body_is_an_adapter_failure():
    def html(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>gateway</html>")

    cases = [
        (OpenAIChatProvider(base_url="http://llm.test/v1", api_key="k", model="m", transport=httpx.MockTransport(html)), "LLM_HTTP_ERROR"),
        (DifyChatProvider(base_url="http://dify.test/v1", api_key="k", user="u", transport=httpx.MockTransport(html)), "DIFY_HTTP_ERROR"),
        (CozeChatProvider(base_url="http://coze.test", api_key="k", bot_id="bot", transport=httpx.MockTransport(html)), "COZE_HTTP_ERROR"),
    ]
    for provider, code in cases:
        result = provider.complete("sys", "user")
        assert result.ok is False
        assert result.error.code == code
        assert result.error.kind.value == "generation"
        assert "不是 JSON" in result.error.message


def test_outbound_prompt_redacts_phone_but_keeps_product_name():
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = request.content.decode()
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    user = '{"name":"抽屉分隔盒","specs":"联系 13812345678","message":"请打 13812345678"}'
    client = OpenAIChatProvider(
        base_url="http://llm.test/v1",
        api_key="k",
        model="m",
        transport=httpx.MockTransport(handler),
    )
    assert client.complete("sys", user).ok
    assert "13812345678" not in seen["body"]
    assert "抽屉分隔盒" in seen["body"]
    assert "[REDACTED]" in seen["body"]


def test_thai_qa_flags_supplier_leakage():
    leaked = review_copy("กล่อง", "一件代发，MOQ 100", require_thai=True)
    assert leaked["ok"] is False
    assert any(issue.startswith("供给侧：") for issue in leaked["issues"])
