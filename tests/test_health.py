from fastapi.testclient import TestClient

from app.main import app


def test_health_check_returns_ok() -> None:
    # 做什么：创建 FastAPI 的测试客户端并请求 /health。
    # 为什么：自动化测试不应依赖手动启动 Uvicorn，也要验证接口返回约定。
    # 结果：测试会确认状态码为 200，且 JSON 完全等于 {"status": "ok"}。
    client = TestClient(app)

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
