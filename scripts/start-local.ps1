[CmdletBinding()]
param(
    [ValidateSet('Start', 'Stop', 'Status', 'Logs')]
    [string]$Action = 'Start',
    [switch]$NoBrowser,
    [switch]$NoTray,
    [switch]$RebuildFrontend
)

$ErrorActionPreference = 'Stop'
$Root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$Frontend = Join-Path $Root 'frontend'

function Resolve-Uv {
    $fromPath = Get-Command uv -ErrorAction SilentlyContinue
    if ($fromPath) { return $fromPath.Source }
    if ($env:PAPERAGENT_UV_PATH -and (Test-Path -LiteralPath $env:PAPERAGENT_UV_PATH)) {
        return (Resolve-Path -LiteralPath $env:PAPERAGENT_UV_PATH).Path
    }
    foreach ($candidate in @(
        (Join-Path $Root 'tools\uv\uv.exe'),
        'E:\App\uv\current\uv.exe',
        (Join-Path $env:LOCALAPPDATA 'uv\uv.exe')
    )) {
        if ($candidate -and (Test-Path -LiteralPath $candidate)) { return $candidate }
    }
    throw '未找到 uv。请先安装 uv，或设置 PAPERAGENT_UV_PATH。'
}

function Invoke-Checked {
    param([string]$Program, [string[]]$Arguments, [string]$WorkingDirectory = $Root)
    Push-Location $WorkingDirectory
    try {
        & $Program @Arguments
        if ($LASTEXITCODE -ne 0) {
            throw "命令失败 ($LASTEXITCODE): $Program $($Arguments -join ' ')"
        }
    }
    finally { Pop-Location }
}

$Uv = Resolve-Uv
# `start-local.ps1` is intentionally callable from any working directory.  Tell
# uv to enter the repository before resolving the project environment; without
# this, `uv run` can inherit an active Conda interpreter and fail to import the
# repository-local `launcher` package during Status or startup polling.
$LauncherArgs = @('--directory', $Root, 'run', 'python', '-m', 'launcher.main')

if ($Action -eq 'Stop') {
    Invoke-Checked $Uv ($LauncherArgs + '--stop')
    Write-Host 'PaperAgent 已安全停止。' -ForegroundColor Green
    exit 0
}
if ($Action -eq 'Status') {
    & $Uv @LauncherArgs '--status'
    exit $LASTEXITCODE
}
if ($Action -eq 'Logs') {
    Invoke-Checked $Uv ($LauncherArgs + '--logs')
    exit 0
}

$VenvPython = Join-Path $Root '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $VenvPython)) {
    Write-Host '首次运行：正在创建项目环境…' -ForegroundColor Cyan
    Invoke-Checked $Uv @('sync', '--all-extras', '--dev')
}

$Index = Join-Path $Frontend 'dist\index.html'
$NeedsBuild = $RebuildFrontend -or -not (Test-Path -LiteralPath $Index)
if (-not $NeedsBuild) {
    $NewestSource = Get-ChildItem -LiteralPath (Join-Path $Frontend 'src') -Recurse -File |
        Sort-Object LastWriteTimeUtc -Descending | Select-Object -First 1
    $NeedsBuild = $NewestSource -and $NewestSource.LastWriteTimeUtc -gt (Get-Item $Index).LastWriteTimeUtc
}
if ($NeedsBuild) {
    if (-not (Test-Path -LiteralPath (Join-Path $Frontend 'node_modules'))) {
        $NpmAction = if (Test-Path -LiteralPath (Join-Path $Frontend 'package-lock.json')) { 'ci' } else { 'install' }
        Invoke-Checked 'npm' @($NpmAction) $Frontend
    }
    Write-Host '正在构建本地前端…' -ForegroundColor Cyan
    Invoke-Checked 'npm' @('run', 'build') $Frontend
}

$Arguments = $LauncherArgs.Clone()
if ($NoBrowser) { $Arguments += '--no-browser' }
if ($NoTray) { $Arguments += '--no-tray' }
$Process = Start-Process -FilePath $Uv -ArgumentList $Arguments -WorkingDirectory $Root -WindowStyle Hidden -PassThru

$Deadline = (Get-Date).AddSeconds(35)
do {
    Start-Sleep -Milliseconds 250
    & $Uv @LauncherArgs '--status' *> $null
    if ($LASTEXITCODE -eq 0) {
        $StatusJson = (& $Uv @LauncherArgs '--status' | Out-String).Trim()
        $CurrentUrl = $null
        try {
            $StatusObject = $StatusJson | ConvertFrom-Json
            $CurrentUrl = $StatusObject.state.url
        }
        catch {
            # The launcher is already healthy; failure to format the convenience URL
            # must not turn a successful start into a false failure.
        }
        Write-Host "PaperAgent 已启动（launcher PID $($Process.Id)）。" -ForegroundColor Green
        if ($CurrentUrl) {
            Write-Host "当前地址：$CurrentUrl" -ForegroundColor Cyan
            Write-Host '请新开浏览器标签使用该地址；重启后端口可能变化，请以 Status 为准。'
        }
        Write-Host '停止：.\scripts\start-local.ps1 Stop；日志：.\scripts\start-local.ps1 Logs'
        exit 0
    }
    if ($Process.HasExited) {
        throw 'PaperAgent 启动器提前退出。请运行 .\scripts\start-local.ps1 Logs 查看原因。'
    }
} while ((Get-Date) -lt $Deadline)

throw 'PaperAgent 在 35 秒内未通过健康检查。请打开日志定位。'
