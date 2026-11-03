# 做什么：定位仓库根目录，并验证启动所需的本地文件和 Docker CLI。
# 为什么：开发者可能从任意 PowerShell 工作目录调用脚本，缺失依赖时应尽早给出明确错误。
# 结果：后续 Compose 命令始终在正确目录并使用项目自己的 .env 配置执行。
# 做什么：允许调用方按需请求重新构建 API 镜像。
# 为什么：日常重启复用本地镜像，可避免 Docker Hub 暂时不可达时阻塞启动。
# 结果：默认直接启动现有服务；仅传入 -Build 时执行 docker compose up --build。
param(
    [switch]$Build
)

$projectRoot = Split-Path -Parent $PSScriptRoot
$envFile = Join-Path $projectRoot ".env"
$dockerCommand = Get-Command docker -ErrorAction SilentlyContinue
$dockerIsReady = $false

if ($dockerCommand) {
    & $dockerCommand.Source info --format "{{.ServerVersion}}" 2>$null | Out-Null
    $dockerIsReady = $LASTEXITCODE -eq 0
}

# 做什么：在 Docker CLI 未运行时启动 Docker Desktop，并等待引擎准备完成。
# 为什么：首次启动 Docker Desktop 需要一定时间，直接执行 Compose 会在守护进程未就绪时失败。
# 结果：已安装 Docker Desktop 的开发者可用同一脚本完成桌面程序启动与服务编排。
if (-not $dockerIsReady) {
    $dockerDesktopCandidates = @(
        "D:\docker\DockerDesktop\Docker Desktop.exe",
        (Join-Path $env:ProgramFiles "Docker\Docker\Docker Desktop.exe"),
        (Join-Path $env:LOCALAPPDATA "Docker\Docker Desktop.exe")
    )
    $dockerDesktop = $dockerDesktopCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1

    if (-not $dockerDesktop) {
        throw "未找到 Docker Desktop。请安装 Docker Desktop 后重试。"
    }

    Start-Process -FilePath $dockerDesktop

    $dockerCliDirectory = Join-Path (Split-Path -Parent $dockerDesktop) "resources\bin"
    if (Test-Path -LiteralPath $dockerCliDirectory) {
        $env:Path = "$dockerCliDirectory;$env:Path"
    }

    for ($attempt = 1; $attempt -le 90; $attempt++) {
        $dockerCommand = Get-Command docker -ErrorAction SilentlyContinue
        if ($dockerCommand) {
            & $dockerCommand.Source info --format "{{.ServerVersion}}" 2>$null | Out-Null
            if ($LASTEXITCODE -eq 0) {
                break
            }
        }
        Start-Sleep -Seconds 1
    }

    if (-not $dockerCommand -or $LASTEXITCODE -ne 0) {
        throw "Docker Desktop 在 90 秒内未准备就绪。请确认它已成功启动后重试。"
    }
}

if (-not (Test-Path -LiteralPath $envFile)) {
    throw "未找到 .env。请从 .env.example 创建本地配置后重试。"
}

# 做什么：以附加日志模式启动 API、MySQL 和 Redis，并按需重新构建镜像。
# 为什么：Compose 会等待健康检查，并让 API 在启动时自动执行 Alembic 迁移。
# 结果：按 Ctrl+C 可停止前台日志；默认复用本地镜像，-Build 时才重新构建。
Push-Location $projectRoot
try {
    $composeArguments = @("compose", "--env-file", ".env", "up")
    if ($Build) {
        $composeArguments += "--build"
    }

    & docker @composeArguments
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }
}
finally {
    Pop-Location
}
