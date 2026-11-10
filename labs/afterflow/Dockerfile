# 做什么：使用满足项目版本要求的轻量 Python 运行环境构建 API 镜像。
# 为什么：开发者在任意支持 Docker 的机器上都应得到相同的 Python 依赖与启动方式。
# 结果：Compose 可以稳定构建并运行 FastAPI 与 Alembic 命令。
FROM python:3.13-slim

WORKDIR /app

# 做什么：复制项目源码和依赖声明后安装运行所需的包。
# 为什么：Docker 构建出的镜像必须以 pyproject.toml 作为唯一依赖来源。
# 结果：容器中的 FastAPI、SQLAlchemy、Alembic 与 MySQL 驱动版本和项目保持一致。
COPY pyproject.toml ./
COPY app ./app
COPY alembic.ini ./
COPY alembic ./alembic
RUN pip install --no-cache-dir .

EXPOSE 8000

# 做什么：先应用尚未执行的迁移，再启动 Uvicorn 服务。
# 为什么：API 进程不应在数据库结构落后于代码时开始接收请求。
# 结果：首次启动自动建表，后续重启只检查并跳过已应用的迁移。
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
