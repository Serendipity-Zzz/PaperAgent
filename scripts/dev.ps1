[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet('bootstrap', 'lint', 'typecheck', 'test-unit', 'test-integration', 'test-contract', 'test-security', 'test-performance', 'test-golden', 'test-fault', 'test-e2e', 'build', 'validate-fixtures', 'check-architecture')]
    [string]$Action = 'bootstrap'
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
    $candidates = @(
        (Join-Path $Root 'tools\uv\uv.exe'),
        'E:\App\uv\current\uv.exe',
        (Join-Path $env:USERPROFILE '.local\bin\uv.exe'),
        (Join-Path $env:LOCALAPPDATA 'uv\uv.exe')
    )
    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path -LiteralPath $candidate)) { return $candidate }
    }
    throw 'uv was not found. Add it to PATH or set PAPERAGENT_UV_PATH.'
}

function Invoke-Checked {
    param([string]$Program, [string[]]$Arguments, [string]$WorkingDirectory = $Root)
    Push-Location $WorkingDirectory
    try {
        & $Program @Arguments
        if ($LASTEXITCODE -ne 0) {
            throw "Command failed ($LASTEXITCODE): $Program $($Arguments -join ' ')"
        }
    }
    finally {
        Pop-Location
    }
}

function Test-HasPythonTests {
    param([string]$Directory)
    return [bool](Get-ChildItem -LiteralPath $Directory -Filter 'test_*.py' -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1)
}

$Uv = Resolve-Uv

switch ($Action) {
    'bootstrap' {
        Invoke-Checked $Uv @('sync', '--all-extras', '--dev')
        $lock = Join-Path $Frontend 'package-lock.json'
        if (Test-Path -LiteralPath $lock) {
            Invoke-Checked 'npm' @('ci') $Frontend
        }
        else {
            Invoke-Checked 'npm' @('install') $Frontend
        }
    }
    'lint' {
        Invoke-Checked $Uv @('run', 'ruff', 'check', '.')
        Invoke-Checked 'npm' @('run', 'lint') $Frontend
    }
    'typecheck' {
        Invoke-Checked $Uv @('run', 'mypy')
        Invoke-Checked 'npm' @('run', 'typecheck') $Frontend
    }
    'test-unit' {
        Invoke-Checked $Uv @('run', 'pytest', 'tests/unit')
        Invoke-Checked 'npm' @('test', '--', '--run') $Frontend
    }
    'test-integration' {
        if (Test-HasPythonTests (Join-Path $Root 'tests\integration')) {
            Invoke-Checked $Uv @('run', 'pytest', 'tests/integration')
        }
        else { Write-Host 'No integration tests are defined for this phase.' }
    }
    'test-contract' {
        if (Test-HasPythonTests (Join-Path $Root 'tests\contract')) {
            Invoke-Checked $Uv @('run', 'pytest', 'tests/contract')
        }
        else { Write-Host 'No contract tests are defined for this phase.' }
    }
    'test-security' {
        if (Test-HasPythonTests (Join-Path $Root 'tests\security')) {
            Invoke-Checked $Uv @('run', 'pytest', 'tests/security')
        }
        else { Write-Host 'No security tests are defined for this phase.' }
    }
    'test-performance' {
        if (Test-HasPythonTests (Join-Path $Root 'tests\performance')) {
            Invoke-Checked $Uv @('run', 'pytest', 'tests/performance')
        }
        else { Write-Host 'No performance tests are defined for this phase.' }
    }
    'test-golden' {
        if (Test-HasPythonTests (Join-Path $Root 'tests\golden')) {
            Invoke-Checked $Uv @('run', 'pytest', 'tests/golden')
        }
        else { Write-Host 'No golden tests are defined for this phase.' }
    }
    'test-fault' {
        if (Test-HasPythonTests (Join-Path $Root 'tests\fault')) {
            Invoke-Checked $Uv @('run', 'pytest', 'tests/fault')
        }
        else { Write-Host 'No fault-injection tests are defined for this phase.' }
    }
    'test-e2e' {
        if (Test-HasPythonTests (Join-Path $Root 'tests\e2e')) {
            Invoke-Checked $Uv @('run', 'pytest', 'tests/e2e')
        }
        else { Write-Host 'No E2E tests are defined for this phase.' }
    }
    'build' {
        Invoke-Checked $Uv @('build')
        Invoke-Checked 'npm' @('run', 'build') $Frontend
    }
    'validate-fixtures' {
        Invoke-Checked $Uv @('run', 'python', 'scripts/validate_fixtures.py')
    }
    'check-architecture' {
        Invoke-Checked $Uv @('run', 'python', 'scripts/check_architecture.py')
        Invoke-Checked $Uv @('run', 'python', 'scripts/check_repo_hygiene.py')
    }
}
