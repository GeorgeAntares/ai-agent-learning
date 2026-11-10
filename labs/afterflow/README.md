# AfterFlow Agent

## 项目简介

AfterFlow Agent 是面向中小型电商售后团队的工单智能处置平台后端项目。平台的目标是将售后咨询转化为可追踪的工单，并在受控范围内自动处理低风险问题，将退款、投诉、信息不足等高风险或不确定场景转交人工审核。

当前仓库已提供 FastAPI 服务、统一请求日志与错误响应、本地 Docker 开发环境，以及 MySQL 数据库迁移基础设施。订单、工单、退款和 Agent 业务能力仍处于后续开发范围。

## 功能

- 提供 `GET /health` 健康检查接口，验证 API 进程可访问。
- 为 HTTP 请求生成或沿用 `X-Request-ID`，并写入统一日志与响应头。
- 使用统一 JSON 结构返回 API 错误，便于客户端处理与问题追踪。
- 通过 Docker Compose 启动 API、MySQL 8 和 Redis 7。
- 使用命名卷持久化 MySQL 与 Redis 数据。
- 使用 Alembic 管理数据库迁移；当前首个迁移创建最小验证表 `schema_version`。
- API 启动前自动执行 `alembic upgrade head`，重复执行迁移不会重复建表。

## 业务场景

项目规划支持以下电商售后场景：

- 订单查询：查询订单状态、商品、支付和收货信息，并对敏感字段脱敏。
- 物流查询：返回物流状态、最近轨迹和异常提示。
- 售后政策问答：回答退款时限、退货条件和运费规则等问题。
- 退款申请：收集必要信息后创建待审核申请，不直接执行退款。
- 转人工：将投诉、退款、物流异常、信息不足或无法可靠判断的请求转给客服处理。

上述业务场景是项目目标，当前版本尚未实现对应的业务表、接口、Agent 调用或异步任务。

## 启动方式

### 前置条件

- Docker Desktop 已启动。
- PowerShell 可执行项目中的启动脚本。
- 本地存在 `.env` 文件；首次使用可从 `.env.example` 创建：

```powershell
Copy-Item .env.example .env
```

### 日常启动

在项目根目录执行：

```powershell
.\scripts\start.ps1
```

脚本会启动 Docker Desktop（如尚未运行），然后复用本地镜像启动 API、MySQL 和 Redis。命令会附加显示容器日志；按 `Ctrl+C` 会停止前台 Compose 服务。

### 重新构建镜像

修改 `Dockerfile`、`pyproject.toml`、`app` 或 `alembic` 后执行：

```powershell
.\scripts\start.ps1 -Build
```

也可以直接使用 Compose：

```powershell
docker compose up --build
```

### 验证服务

服务启动后执行：

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health
docker compose exec api alembic upgrade head
```

健康检查应返回：

```json
{"status": "ok"}
```
