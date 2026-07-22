[CmdletBinding()]
param(
    [ValidateSet('P0', 'P1', 'P2', 'P3', 'P4', 'P5', 'P6', 'P7', 'P8')]
    [string]$Phase,
    [switch]$Full
)

$ErrorActionPreference = 'Stop'
$Dev = Join-Path $PSScriptRoot 'dev.ps1'
$started = Get-Date

function Invoke-PhaseStep {
    param([string]$Name)
    Write-Host "==> $Name" -ForegroundColor Cyan
    & $Dev $Name
    if ($LASTEXITCODE -ne 0) { throw "Phase check failed at $Name" }
}

$baseSteps = @('lint', 'typecheck', 'test-unit', 'validate-fixtures', 'check-architecture', 'build')
foreach ($step in $baseSteps) { Invoke-PhaseStep $step }

if ($Phase -ne 'P0' -or $Full) {
    Invoke-PhaseStep 'test-integration'
}
if ($Phase -in @('P2', 'P3', 'P4', 'P5', 'P6', 'P7', 'P8') -or $Full) {
    Invoke-PhaseStep 'test-contract'
}
if ($Phase -in @('P4', 'P5', 'P6', 'P7', 'P8') -or $Full) {
    Invoke-PhaseStep 'test-security'
    Invoke-PhaseStep 'test-performance'
}
if ($Phase -in @('P6', 'P7', 'P8') -or $Full) {
    Invoke-PhaseStep 'test-golden'
}
if ($Phase -eq 'P8' -or $Full) {
    Invoke-PhaseStep 'test-fault'
}
if ($Phase -ne 'P0' -or $Full) {
    Invoke-PhaseStep 'test-e2e'
}

$duration = (Get-Date) - $started
Write-Host ("Phase {0} checks passed in {1:n1}s" -f $Phase, $duration.TotalSeconds) -ForegroundColor Green
