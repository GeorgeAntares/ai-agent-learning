import time
import uuid

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.config import get_settings
from app.errors import AppError, app_error_handler, error_response
from app.logging_config import configure_logging
from app.logistics import router as logistics_router
from app.orders import router as orders_router
from app.policies import router as policies_router
from app.tickets import router as tickets_router

# 做什么：在应用启动时读取配置并初始化日志器。
# 为什么：路由和中间件都需要相同的配置与日志规则，不能分散初始化。
# 结果：应用标题、日志等级和日志格式在整个进程内保持一致。
settings = get_settings()
logger = configure_logging(settings.log_level)


class UTF8JSONResponse(JSONResponse):
    """为 JSON 响应显式声明 UTF-8 字符集。

    做什么：将默认 Content-Type 设置为 application/json; charset=utf-8。
    为什么：Windows PowerShell 需要该声明才能稳定按 UTF-8 解码中文 JSON 字段。
    结果：Invoke-RestMethod 显示订单中文字段时不会按本地代码页产生乱码。
    """

    media_type = "application/json; charset=utf-8"


# 做什么：创建 FastAPI 应用并注册业务异常处理器。
# 为什么：后续所有接口都应使用同一个应用实例与统一错误契约。
# 结果：路由抛出 AppError 时会自动转换为项目规定的 JSON 响应。
app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    default_response_class=UTF8JSONResponse,
)
app.add_exception_handler(AppError, app_error_handler)

# 做什么：注册订单查询路由到全局 FastAPI 应用。
# 为什么：订单接口需要复用全局请求 ID、日志和统一 AppError 响应处理。
# 结果：GET /api/v1/orders/{order_no} 可由同一应用实例处理并遵循既有 API 契约。
app.include_router(orders_router)

# 做什么：注册工单创建与查询路由到全局 FastAPI 应用。
# 为什么：工单接口需要复用全局请求 ID、日志和统一 AppError 响应处理。
# 结果：POST /api/v1/tickets 与 GET /api/v1/tickets/{id} 使用项目一致的 API 契约。
app.include_router(tickets_router)

# 做什么：注册仅供本地开发验证的物流查询路由到全局 FastAPI 应用。
# 为什么：Swagger 和手动验收需要调用 LogisticsService，但路由不得直接访问物流事件表。
# 结果：GET /internal/logistics/{order_no} 使用项目统一的请求 ID 和错误响应机制。
app.include_router(logistics_router)

# 做什么：注册仅供本地开发验证的政策检索路由到全局 FastAPI 应用。
# 为什么：Swagger 和后续 Agent 必须通过 PolicyService 获取政策依据，不能直接读取知识文件。
# 结果：GET /internal/policies/search 返回类型固定的政策编号、标题和原文片段。
app.include_router(policies_router)


@app.middleware("http")
async def add_request_context(request: Request, call_next):
    # 做什么：优先沿用客户端传入的请求 ID；未传入时生成新的唯一 ID。
    # 为什么：同一请求跨多个服务时需要同一个标识，才能把相关日志串联起来。
    # 结果：request.state、响应头和应用日志都会使用相同的 X-Request-ID。
    request_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))
    request.state.request_id = request_id
    started_at = time.perf_counter()
    logger.info(
        "request_started",
        extra={
            "event": "request_started",
            "method": request.method,
            "path": request.url.path,
            "request_id": request_id,
        },
    )

    # 做什么：记录开始时间后交由 FastAPI 执行实际路由。
    # 为什么：中间件不处理具体业务，只负责包裹每一次 HTTP 请求并收集通用信息。
    # 结果：无论访问 /health 还是未来接口，都能得到统一的日志和请求 ID。
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    logger.info(
        "request_completed",
        extra={
            "event": "request_completed",
            "method": request.method,
            "path": request.url.path,
            "request_id": request_id,
            "status_code": response.status_code,
            "duration_ms": round((time.perf_counter() - started_at) * 1000, 2),
        },
    )
    return response


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    # 做什么：将 FastAPI/Starlette 的 HTTP 异常转换为项目统一错误 JSON。
    # 为什么：框架默认 404 响应格式不包含项目要求的 code 和 request_id。
    # 结果：访问不存在的路径时也能得到可预测、可追踪的错误响应。
    if exc.status_code == 404:
        return error_response(request, "not_found", "Resource not found", 404)
    return error_response(request, "http_error", str(exc.detail), exc.status_code)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    # 做什么：捕获请求参数校验失败的异常。
    # 为什么：不能把框架内部的错误格式直接暴露给客户端，避免接口契约随框架变化。
    # 结果：所有参数校验错误都返回 validation_error 和对应 request_id。
    return error_response(request, "validation_error", "Request validation failed", 422)


@app.get("/health")
def health_check() -> dict[str, str]:
    """检查 API 进程是否可访问。

    做什么：返回固定的健康状态 JSON。
    为什么：N01 只验证 FastAPI 服务是否成功启动，尚未引入数据库或 Redis。
    结果：访问 GET /health 并收到 {"status": "ok"}，即可确认 API 进程可响应请求。
    """
    return {"status": "ok"}
