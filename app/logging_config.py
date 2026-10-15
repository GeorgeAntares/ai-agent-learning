import json
import logging


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        # 做什么：把 Python 日志记录转换为 JSON 字符串。
        # 为什么：普通文本日志难以按 request_id、路径或状态码进行搜索和统计。
        # 结果：每一行日志都是包含关键字段的 JSON，便于后续排错和接入日志平台。
        payload = {
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for field in (
            "event",
            "method",
            "path",
            "request_id",
            "status_code",
            "duration_ms",
        ):
            value = getattr(record, field, None)
            if value is not None:
                payload[field] = value
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(log_level: str) -> logging.Logger:
    # 做什么：创建并配置 AfterFlow 专用日志器。
    # 为什么：应用日志需要按 .env 中的日志级别输出，且要使用统一 JSON 格式。
    # 结果：调用 logger.info(...) 时会输出结构化日志，而不是默认文本日志。
    logger = logging.getLogger("afterflow")
    logger.setLevel(log_level.upper())
    logger.propagate = False

    # 做什么：仅在日志器没有处理器时添加输出处理器。
    # 为什么：Uvicorn 的 --reload 会重新导入模块，重复添加会导致同一日志打印多次。
    # 结果：无论服务重载多少次，每条应用日志只输出一次。
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)

    return logger
