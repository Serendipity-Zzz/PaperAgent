[CmdletBinding()]
param(
    [ValidateSet('Quick', 'Phase', 'Full', 'LiveProvider')]
    [string]$Mode = 'Quick',
    [ValidateSet('P0', 'P1', 'P2', 'P3', 'P4', 'P5', 'P6', 'P7', 'P8')]
    [string]$Phase = 'P7'
)

$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Push-Location $root
try {
    switch ($Mode) {
        'Quick' {
            & .\scripts\dev.ps1 lint
            & .\scripts\dev.ps1 typecheck
            & .\scripts\dev.ps1 test-unit
        }
        'Phase' { & .\scripts\check.ps1 -Phase $Phase }
        'Full' { & .\scripts\check.ps1 -Full }
        'LiveProvider' { & .\scripts\test-live-provider.ps1 }
    }
    if ($LASTEXITCODE -ne 0) { throw "Local test gate failed: $LASTEXITCODE" }
}
finally {
    Pop-Location
}
