# 本地开发环境设计

## 目标

通过一条 `docker compose up --build` 命令启动 FastAPI API、MySQL 8 和 Redis，
并在 API 启动前自动执行可重复的数据库迁移。

## 架构

`docker-compose.yml` 定义三个服务：

- `mysql` 使用 MySQL 8，配置健康检查，并用命名卷保存 `/var/lib/mysql`。
- `redis` 使用 Redis 7 稳定镜像，配置健康检查，并用命名卷保存 `/data`。
- `api` 由项目 Dockerfile 构建，发布主机 `8000` 到容器 `8000`，等待 MySQL 和 Redis 健康后启动。

API 容器入口命令依次执行 `alembic upgrade head` 和 Uvicorn。迁移是 Alembic
版本化操作，因此容器重启及手工重复执行迁移都只会应用尚未记录的版本。

## 配置与依赖

`Settings` 增加 MySQL 与 Redis 的连接配置。`.env.example` 提供本地容器网络可用的
连接地址和账号模板；`.env` 保持被 Git 忽略。SQLAlchemy 使用 PyMySQL 连接 MySQL，
Alembic 从同一个 `DATABASE_URL` 读取迁移连接地址。

Dockerfile 基于 Python 3.13 slim，安装 `pyproject.toml` 的运行依赖，复制应用和迁移文件，
并暴露 8000 端口。

## 数据库迁移

Alembic 初始化在仓库根目录，环境脚本从应用配置读取数据库 URL。首个迁移创建最小的
`schema_version` 表；表只用于验证迁移链路，不承载业务数据。迁移的 `upgrade` / `downgrade`
使用明确的建表与删表操作，遵循 Alembic 版本表以保证重复 `upgrade head` 无副作用。

## 测试与验收

迁移测试为每次测试创建独立 SQLite 文件数据库，调用 Alembic 的 `upgrade head`，然后通过
SQLAlchemy 检查 `schema_version` 表存在。这样普通 `pytest` 不依赖 Docker 也能验证迁移可执行；
Compose 手工验收覆盖 MySQL 实际运行路径。

验收步骤：

1. `docker compose up --build` 启动三项服务。
2. 请求 `http://127.0.0.1:8000/health` 获得 `{"status":"ok"}`。
3. 执行 `docker compose exec api alembic upgrade head` 并成功返回。
4. 运行全部 pytest，N01-N02 与新增迁移测试均通过。

## 非目标

不创建业务表或 CRUD 接口，不接入 Agent、模型 API 或异步任务；Redis 只作为可用的本地依赖启动，
不实现缓存或队列逻辑。
