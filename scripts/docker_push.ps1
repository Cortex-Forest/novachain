<#
.SYNOPSIS
  Nova Chain 镜像一键「构建 + 打标签 + 推送 Docker Hub」。

.DESCRIPTION
  推送到 Docker Hub 需要：
    1. 已有 Docker Hub 账号（https://hub.docker.com 注册）
    2. 已登录：docker login
  首次构建会编译 liboqs（抗量子库），约 5~15 分钟，属正常现象。

.EXAMPLE
  .\scripts\docker_push.ps1                      # 构建并推送 latest
  .\scripts\docker_push.ps1 -Tag v0.11           # 构建并推送 v0.11
  .\scripts\docker_push.ps1 -SkipOqs -Tag dev    # 跳过抗量子（回退 Ed25519），推 dev
#>
param(
    [string]$Repo = "spurtniwa/nova",
    [string]$Tag  = "latest",
    [switch]$SkipOqs
)
$ErrorActionPreference = "Stop"

$image = "${Repo}:${Tag}"
$arg = @("build", "-t", $image)
if ($SkipOqs) { $arg += @("--build-arg", "NOVA_OQS=0") }
$arg += "."

Write-Host "==> docker $($arg -join ' ')" -ForegroundColor Cyan
& docker @arg
if ($LASTEXITCODE -ne 0) { throw "docker build 失败" }

Write-Host "==> docker push ${image}" -ForegroundColor Cyan
& docker push $image
if ($LASTEXITCODE -ne 0) { throw "docker push 失败" }

Write-Host ""
Write-Host "完成！镜像已推送: https://hub.docker.com/r/$Repo/tags" -ForegroundColor Green
Write-Host "他人拉取: docker pull $image" -ForegroundColor Green
