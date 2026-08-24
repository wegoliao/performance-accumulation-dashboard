param(
    [switch]$NoOpen
)

$ErrorActionPreference = 'Stop'
$SideRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent $SideRoot
$Python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'

if (-not (Test-Path -LiteralPath $Python)) {
    throw "找不到專案 Python：$Python"
}

& $Python (Join-Path $SideRoot 'scripts\build_dashboard.py')
if ($LASTEXITCODE -ne 0) {
    throw "績效累積圖重算失敗，exit code=$LASTEXITCODE"
}

$Dashboard = Join-Path $SideRoot 'index.html'
Write-Host "績效累積圖已重算：$Dashboard"
if (-not $NoOpen) {
    Start-Process -FilePath $Dashboard
}
