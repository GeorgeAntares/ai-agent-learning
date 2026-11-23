import json

import httpx
import pytest

from app.agent import LLMAgent, ToolCallDecision
from app.config import Settings
from app.errors import AppError


def _openai_response(content: object, status_code: int = 200) -> httpx.Response:
    """构造 OpenAI 兼容聊天接口的最小完整响应。"""
    return httpx.Response(
        status_code,
        json={"choices": [{"message": {"content": json.dumps(content)}}]},
        request=httpx.Request("POST", "https://llm.example.test/v1/chat/completions"),
    )


def test_llm_agent_returns_raw_structured_decision(monkeypatch: pytest.MonkeyPatch) -> None:
    # 做什么：模拟模型返回单个结构化物流工具申请。
    # 为什么：LLMAgent 只能产出待校验的 AgentDecision，不能执行工具或生成工具结果。
    # 结果：适配器返回可由 AgentService 后续解析的原始决策字典。
    expected = {
        "action": "TOOL_CALL",
        "tool_name": "get_logistics",
        "arguments": {"order_no": "202511150001"},
    }

    def fake_post(self: httpx.Client, *args: object, **kwargs: object) -> httpx.Response:
        return _openai_response(expected)

    monkeypatch.setattr(httpx.Client, "post", fake_post)

    decision = LLMAgent("https://llm.example.test/v1", "secret-key", "test-model", 15).decide(
        "查询订单 202511150001 的物流"
    )

    assert isinstance(decision, ToolCallDecision)
    assert decision.model_dump() == expected


def test_llm_agent_rejects_invalid_decision_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    # 做什么：模拟模型输出带未允许 SQL 字段且缺少 arguments 的工具申请。
    # 为什么：模型输出必须先受 AgentDecision Schema 限制，不能把任意内容交给 Gateway。
    # 结果：适配器返回 invalid_agent_decision，不会暴露或执行模型输出中的 SQL。
    def fake_post(self: httpx.Client, *args: object, **kwargs: object) -> httpx.Response:
        return _openai_response({"action": "TOOL_CALL", "tool_name": "get_order", "sql": "SELECT 1"})

    monkeypatch.setattr(httpx.Client, "post", fake_post)

    with pytest.raises(AppError) as error:
        LLMAgent("https://llm.example.test/v1", "secret-key", "test-model", 15).decide("查询订单")

    assert error.value.code == "invalid_agent_decision"
    assert error.value.status_code == 422


def test_llm_agent_rejects_unregistered_tool_name(monkeypatch: pytest.MonkeyPatch) -> None:
    # 做什么：模拟模型请求未登记的 delete_order 写工具。
    # 为什么：真实模型输出除符合字段结构外，还必须只申请 N10 集中白名单中的只读工具。
    # 结果：LLMAgent 在 Gateway 前返回 invalid_agent_decision，禁止模型申请未定义能力。
    def fake_post(self: httpx.Client, *args: object, **kwargs: object) -> httpx.Response:
        return _openai_response(
            {"action": "TOOL_CALL", "tool_name": "delete_order", "arguments": {"order_no": "202511150001"}}
        )

    monkeypatch.setattr(httpx.Client, "post", fake_post)

    with pytest.raises(AppError) as error:
        LLMAgent("https://llm.example.test/v1", "secret-key", "test-model", 15).decide("删除订单")

    assert error.value.code == "invalid_agent_decision"


def test_llm_provider_configuration_requires_complete_credentials() -> None:
    # 做什么：构造选择 llm 但缺少模型地址、密钥和模型名的应用配置。
    # 为什么：不完整配置应在启动时失败，不能等到处理工单时才暴露为运行时错误。
    # 结果：Settings 拒绝该配置，默认 fake 模式仍不要求任何真实模型凭据。
    with pytest.raises(ValueError):
        Settings(app_name="afterflow-test", agent_provider="llm", _env_file=None)


def test_llm_agent_translates_request_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    # 做什么：模拟模型 HTTP 请求超时。
    # 为什么：超时不能泄露请求细节或使工作流误标记成功，应保留 PROCESSING 供重试。
    # 结果：适配器抛出受控 llm_timeout 错误。
    def fake_post(self: httpx.Client, *args: object, **kwargs: object) -> httpx.Response:
        raise httpx.TimeoutException("timed out")

    monkeypatch.setattr(httpx.Client, "post", fake_post)

    with pytest.raises(AppError) as error:
        LLMAgent("https://llm.example.test/v1", "secret-key", "test-model", 15).decide("查询订单")

    assert error.value.code == "llm_timeout"


def test_llm_agent_translates_non_success_response(monkeypatch: pytest.MonkeyPatch) -> None:
    # 做什么：模拟模型服务返回 503 非成功状态。
    # 为什么：上游失败不能被误当作模型合法决策或包含上游响应详情返回给客户端。
    # 结果：适配器抛出受控 llm_request_failed 错误。
    def fake_post(self: httpx.Client, *args: object, **kwargs: object) -> httpx.Response:
        return _openai_response({"error": "unavailable"}, status_code=503)

    monkeypatch.setattr(httpx.Client, "post", fake_post)

    with pytest.raises(AppError) as error:
        LLMAgent("https://llm.example.test/v1", "secret-key", "test-model", 15).decide("查询订单")

    assert error.value.code == "llm_request_failed"
