param(
    [switch]$Full,
    [string]$LiveEvidence = ""
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Python)) {
    & "$PSScriptRoot\dev.ps1" bootstrap
}

Push-Location $Root
try {
    & "$PSScriptRoot\dev.ps1" lint
    if ($LASTEXITCODE -ne 0) { throw "lint gate failed: $LASTEXITCODE" }
    & "$PSScriptRoot\dev.ps1" typecheck
    if ($LASTEXITCODE -ne 0) { throw "typecheck gate failed: $LASTEXITCODE" }
    & $Python scripts/check_agent_kernel.py
    if ($LASTEXITCODE -ne 0) { throw "anti-fake gate failed: $LASTEXITCODE" }
    & $Python -m pytest tests/unit tests/integration tests/contract -q
    if ($LASTEXITCODE -ne 0) { throw "core test gate failed: $LASTEXITCODE" }
    & "$PSScriptRoot\dev.ps1" check-architecture
    if ($LASTEXITCODE -ne 0) { throw "architecture gate failed: $LASTEXITCODE" }
    if ($Full) {
        npm run build --prefix frontend
        if ($LASTEXITCODE -ne 0) { throw "frontend build gate failed: $LASTEXITCODE" }
        & $Python -m pytest tests/security tests/fault tests/performance tests/e2e -q
        if ($LASTEXITCODE -ne 0) { throw "extended test gate failed: $LASTEXITCODE" }
        npm test --prefix frontend -- --run
        if ($LASTEXITCODE -ne 0) { throw "frontend test gate failed: $LASTEXITCODE" }
    }
    if ($LiveEvidence) {
        & "$PSScriptRoot\test-live-provider.ps1" -EvidencePath $LiveEvidence
    }
}
finally {
    Pop-Location
}
