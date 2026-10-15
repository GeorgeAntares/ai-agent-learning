from fastapi import Request
from fastapi.responses import JSONResponse


class AppError(Exception):
    """表示需要按项目约定返回给客户端的业务错误。

    做什么：保存错误码、提示消息和 HTTP 状态码。
    为什么：业务代码不应各自拼装不同格式的错误响应。
    结果：调用方始终能收到 code、message 和 request_id 三个字段。
    """
    def __init__(self, code: str, message: str, status_code: int = 400) -> None:
        self.code = code
        self.message = message
        self.status_code = status_code


def error_response(request: Request, code: str, message: str, status_code: int) -> JSONResponse:
    # 做什么：将错误信息转换成统一的 JSON 响应。
    # 为什么：前端和调用方需要稳定的错误字段，而不是依赖每个接口的不同格式。
    # 结果：所有受控错误都会返回 code、message 与当前请求的 request_id。
    return JSONResponse(
        status_code=status_code,
        content={
            "code": code,
            "message": message,
            "request_id": request.state.request_id,
        },
    )


async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    # 做什么：捕获业务代码抛出的 AppError。
    # 为什么：路由只需要说明业务失败原因，不需要重复构造 JSON 响应。
    # 结果：FastAPI 自动调用此处理器，并使用统一错误格式返回客户端。
    return error_response(request, exc.code, exc.message, exc.status_code)
