<#
.SYNOPSIS
  Nova Chain 镜像一键「构建 + 打标签 + 推送 GHCR」。（GitHub Actions 已自动构建，此脚本仅本地手动推送时用）

.DESCRIPTION
  默认推送到 GHCR（ghcr.io）。首次推送需登录：
    docker login ghcr.io -u <GitHub用户名>   （密码用 GitHub Personal Access Token，权限 write:packages）
  首次构建会编译 liboqs（抗量子库），约 5~15 分钟，属正常现象。

.EXAMPLE
  .\scripts\docker_push.ps1 -Tag v0.11           # 构建并推送 v0.11 到 GHCR
  .\scripts\docker_push.ps1 -Tag dev -SkipOqs    # 跳过抗量子（回退 Ed25519），推 dev
#>
param(
    [string]$Repo = "ghcr.io/cortex-forest/novachain",
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
Write-Host "完成！镜像已推送: https://github.com/Cortex-Forest/novachain/pkgs/container/novachain" -ForegroundColor Green
Write-Host "他人拉取: docker pull $image" -ForegroundColor Green
