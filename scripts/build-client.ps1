# 一键打包客户端（Windows 桌面端）。
# 用法：
#   pwsh scripts/build-client.ps1                  # 全流程：前端 -> 后端(PyInstaller) -> Electron 安装包(nsis)
#   pwsh scripts/build-client.ps1 -Target dir      # 仅产出解包目录 win-unpacked（快速本地验证）
#   pwsh scripts/build-client.ps1 -Target zip
#   pwsh scripts/build-client.ps1 -SkipFrontend    # 不重新构建前端（复用 backend/static 现有内容）
#   pwsh scripts/build-client.ps1 -SkipBackend     # 不重新 PyInstaller（复用 backend/dist/qmt_work）
#
# 说明：
#   - 自动探测 Python/PyInstaller（PATH -> 常见 miniforge -> python -m PyInstaller）。
#   - 产物位于 frontend/release/。
[CmdletBinding()]
param(
  [ValidateSet("nsis", "zip", "dir")] [string]$Target = "nsis",
  [switch]$SkipFrontend,
  [switch]$SkipBackend
)

$ErrorActionPreference = "Stop"
$root = Resolve-Path (Join-Path $PSScriptRoot "..")
$frontend = Join-Path $root "frontend"
$backend = Join-Path $root "backend"

function Test-Command {
  param([string]$Name)
  return [bool](Get-Command $Name -ErrorAction SilentlyContinue)
}

Push-Location $frontend

# 1) 前端
if (-not $SkipFrontend) {
  Write-Host "[build] vite build 前端..." -ForegroundColor Cyan
  if (-not (Test-Path node_modules)) { npm install }
  npm run build
} else {
  Write-Host "[build] 跳过前端构建（复用 backend/static）" -ForegroundColor DarkGray
}

# 2) 后端（PyInstaller 单目录）
if (-not $SkipBackend) {
  Write-Host "[build] PyInstaller 打包后端..." -ForegroundColor Cyan
  if (-not (Test-Path node_modules)) { npm install }
  npm run backend:build
} else {
  Write-Host "[build] 跳过后端打包（复用 backend/dist/qmt_work）" -ForegroundColor DarkGray
}

# 3) Electron 打包
Write-Host "[build] electron-builder 打包 ($Target)..." -ForegroundColor Cyan
if (-not (Test-Path node_modules/.bin/electron-builder)) { npm install }
if ($Target -eq "dir") {
  npx electron-builder --dir
} else {
  npx electron-builder --win $Target
}

Pop-Location
Write-Host "[build] 完成。产物目录: frontend/release/" -ForegroundColor Green
