import asyncio

from fastapi.testclient import TestClient
from starlette.requests import Request

from app.errors import AppError, app_error_handler
from app.main import app


def test_missing_resource_has_uniform_error_response() -> None:
    # 做什么：携带固定请求 ID 访问不存在的路径。
    # 为什么：既要验证 404 的统一错误格式，也要验证请求 ID 被原样保留。
    # 结果：响应包含 not_found、Resource not found 和传入的 test-request-id。
    client = TestClient(app)

    response = client.get("/missing-resource", headers={"X-Request-ID": "test-request-id"})

    assert response.status_code == 404
    assert response.json() == {
        "code": "not_found",
        "message": "Resource not found",
        "request_id": "test-request-id",
    }


def test_request_id_is_returned_when_generated() -> None:
    # 做什么：不传 X-Request-ID，访问正常的 /health 接口。
    # 为什么：客户端未提供请求 ID 时，服务必须自行生成一个用于排错。
    # 结果：响应头中存在非空的 X-Request-ID。
    client = TestClient(app)

    response = client.get("/health")

    assert response.status_code == 200
    assert response.headers["X-Request-ID"]


def test_app_error_has_uniform_error_response() -> None:
    # 做什么：直接调用 AppError 的异常处理器，模拟一条已带请求 ID 的请求。
    # 为什么：不必为了测试而增加无业务意义的 API 路由，也能验证业务错误契约。
    # 结果：ticket_not_found 会按统一 JSON 格式返回，并保留 test-request-id。
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": [],
            "state": {"request_id": "test-request-id"},
        }
    )

    response = asyncio.run(
        app_error_handler(request, AppError("ticket_not_found", "Ticket not found", 404))
    )

    assert response.status_code == 404
    assert response.body == (
        b'{"code":"ticket_not_found","message":"Ticket not found",'
        b'"request_id":"test-request-id"}'
    )
