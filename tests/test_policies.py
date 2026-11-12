from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import app
from app.policies import PolicyQuery, PolicySearchResult, PolicyService


def test_policy_service_returns_refund_time_limit_policy() -> None:
    # 做什么：通过政策服务检索包含退款时限的用户咨询。
    # 为什么：退款到账时间必须引用本地政策原文，不能由服务自行猜测规则。
    # 结果：检索结果包含退款时限政策的唯一标识、标题和原文片段。
    results = PolicyService().search_policy(PolicyQuery(query="退款时限"))

    assert results[0].model_dump() == {
        "policy_id": "REFUND_TIME_LIMIT",
        "title": "退款时限规则",
        "snippet": "退款审核通过后，原支付渠道通常会在 3 至 10 个工作日内到账。",
    }


def test_policy_service_returns_return_shipping_fee_policy() -> None:
    # 做什么：通过政策服务检索退货运费承担问题。
    # 为什么：用户需要可追溯的运费承担依据，而不是泛化的售后建议。
    # 结果：结果包含退货运费规则的编号、相关标题和说明承担方的原文片段。
    results = PolicyService().search_policy(PolicyQuery(query="退货运费由谁承担"))

    assert results[0].model_dump() == {
        "policy_id": "RETURN_SHIPPING_FEE",
        "title": "退货运费规则",
        "snippet": "非质量问题退货，退货运费由用户承担；商品质量问题由商家承担。",
    }


def test_policy_service_returns_empty_results_for_unmatched_question() -> None:
    # 做什么：通过政策服务检索知识库中不存在的火星退货问题。
    # 为什么：服务只应返回保存的政策依据，不能因为通用词退货而编造或错误匹配政策。
    # 结果：查询成功但 results 为空列表，调用方可据此决定后续处理方式。
    results = PolicyService().search_policy(PolicyQuery(query="火星退货规则"))

    assert results == []


def test_internal_policy_route_delegates_to_policy_service() -> None:
    # 做什么：替换政策服务的检索方法后访问内部政策路由。
    # 为什么：路由只能调用 PolicyService，不能自行读取政策文件或实现另一套检索规则。
    # 结果：路由返回服务提供的结果，并以经过校验的查询模型调用服务方法。
    expected_result = PolicySearchResult(
        policy_id="RETURN_SHIPPING_FEE",
        title="退货运费规则",
        snippet="非质量问题退货，退货运费由用户承担；商品质量问题由商家承担。",
    )
    with patch.object(PolicyService, "search_policy", return_value=[expected_result]) as search_policy:
        with TestClient(app) as client:
            response = client.get("/internal/policies/search?query=退货运费由谁承担")

    assert response.status_code == 200
    assert response.json() == {
        "query": "退货运费由谁承担",
        "results": [expected_result.model_dump()],
    }
    search_policy.assert_called_once_with(PolicyQuery(query="退货运费由谁承担"))


def test_internal_policy_route_returns_empty_results_without_guessing() -> None:
    # 做什么：通过内部路由检索没有对应政策依据的问题。
    # 为什么：开发验收入口也必须严格返回服务的空结果，不能组织猜测性答案。
    # 结果：响应保留查询文本且 results 是空列表。
    with TestClient(app) as client:
        response = client.get("/internal/policies/search?query=火星退货规则")

    assert response.status_code == 200
    assert response.json() == {"query": "火星退货规则", "results": []}
