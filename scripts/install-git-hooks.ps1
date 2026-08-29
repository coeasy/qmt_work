# 安装本地 git 钩子：把 scripts/post-commit.hook 复制到 .git/hooks/post-commit，
# 使「git commit 即自动构建客户端」。运行一次即可（提交后仍生效）。
$ErrorActionPreference = "Stop"
$root = Resolve-Path (Join-Path $PSScriptRoot "..")
$hookSrc = Join-Path $root "scripts" "post-commit.hook"
$hookDst = Join-Path $root ".git" "hooks" "post-commit"

if (-not (Test-Path $hookSrc)) { Write-Error "缺少 $hookSrc"; exit 1 }
New-Item -ItemType Directory -Force -Path (Split-Path $hookDst) | Out-Null
Copy-Item -Path $hookSrc -Destination $hookDst -Force

# git 钩子需可执行位；Windows 上通过 attrib 去除只读即可，git 用 sh 执行。
if (Test-Path (Join-Path $root ".git" "hooks")) {
  attrib -R "$hookDst" 2>$null | Out-Null
}
Write-Host "已安装 post-commit 钩子 -> $hookDst" -ForegroundColor Green
Write-Host "提交代码后将自动后台构建客户端（scripts/build-client.ps1 -Target dir）。" -ForegroundColor Cyan
Write-Host "如需临时跳过：QMT_NO_AUTOBUILD=1 git commit ..." -ForegroundColor DarkGray
