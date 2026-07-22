[CmdletBinding()]
param(
    [string]$BaseUrl = 'https://api.deepseek.com',
    [string]$Model = 'deepseek-v4-pro',
    [string]$EvidencePath = ''
)

$ErrorActionPreference = 'Stop'
$secure = Read-Host 'DeepSeek API Key（仅保留在当前测试进程）' -AsSecureString
$pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
try {
    $env:PAPERAGENT_LIVE_API_KEY = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
    $env:PAPERAGENT_LIVE_BASE_URL = $BaseUrl
    $env:PAPERAGENT_LIVE_MODEL = $Model
    if ($EvidencePath) { $env:PAPERAGENT_LIVE_EVIDENCE = $EvidencePath }
    $uv = if ($env:PAPERAGENT_UV_PATH) {
        $env:PAPERAGENT_UV_PATH
    }
    elseif (Get-Command uv -ErrorAction SilentlyContinue) {
        (Get-Command uv).Source
    }
    elseif (Test-Path 'E:\App\uv\current\uv.exe') {
        'E:\App\uv\current\uv.exe'
    }
    else {
        throw 'uv not found; configure PAPERAGENT_UV_PATH or add uv to PATH'
    }
    & $uv run python scripts/live_provider_test.py
    $providerExitCode = $LASTEXITCODE
    if ($providerExitCode -ne 0) { throw "Live provider gate failed: $providerExitCode" }
}
finally {
    Remove-Item Env:PAPERAGENT_LIVE_API_KEY -ErrorAction SilentlyContinue
    Remove-Item Env:PAPERAGENT_LIVE_BASE_URL -ErrorAction SilentlyContinue
    Remove-Item Env:PAPERAGENT_LIVE_MODEL -ErrorAction SilentlyContinue
    Remove-Item Env:PAPERAGENT_LIVE_EVIDENCE -ErrorAction SilentlyContinue
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
}
