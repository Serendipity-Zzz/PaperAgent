[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('P0', 'P1', 'P2', 'P3', 'P4', 'P5', 'P6', 'P7', 'P8')]
    [string]$Phase
)

$ErrorActionPreference = 'Stop'
$Root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$DevScript = Join-Path $PSScriptRoot 'dev.ps1'

function Resolve-Uv {
    $fromPath = Get-Command uv -ErrorAction SilentlyContinue
    if ($fromPath) { return $fromPath.Source }
    if ($env:PAPERAGENT_UV_PATH -and (Test-Path -LiteralPath $env:PAPERAGENT_UV_PATH)) {
        return (Resolve-Path -LiteralPath $env:PAPERAGENT_UV_PATH).Path
    }
    $candidate = 'E:\App\uv\current\uv.exe'
    if (Test-Path -LiteralPath $candidate) { return $candidate }
    throw 'uv was not found. Add it to PATH or set PAPERAGENT_UV_PATH.'
}

function Invoke-Checked {
    param([string]$Program, [string[]]$Arguments)
    & $Program @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed ($LASTEXITCODE): $Program $($Arguments -join ' ')"
    }
}

Push-Location $Root
try {
    $Uv = Resolve-Uv
    switch ($Phase) {
        'P0' {
            & $DevScript lint
            & $DevScript typecheck
            Invoke-Checked $Uv @(
                'run', 'pytest',
                'tests/unit/test_interactive_workspace_contracts.py',
                'tests/unit/test_interactive_workspace_architecture.py',
                'tests/unit/test_portable_backup.py',
                'tests/integration/test_p1_database_api.py',
                '-q'
            )
            & $DevScript check-architecture
        }
        'P1' {
            & $DevScript lint
            & $DevScript typecheck
            Invoke-Checked 'npm' @('run', 'test', '--prefix', 'frontend')
            Invoke-Checked 'npm' @('run', 'build', '--prefix', 'frontend')
            Invoke-Checked $Uv @(
                'run', 'pytest',
                'tests/unit/test_p4_preview.py',
                'tests/unit/test_p4_preview_formats.py',
                'tests/security/test_p4_active_content.py',
                '-q'
            )
            & $DevScript check-architecture
        }
        'P2' {
            & $DevScript lint
            & $DevScript typecheck
            Invoke-Checked $Uv @('run', 'pytest', 'tests/integration', '-q')
            Invoke-Checked 'npm' @('run', 'test', '--prefix', 'frontend')
            Invoke-Checked 'npm' @('run', 'build', '--prefix', 'frontend')
            & $DevScript check-architecture
        }
        'P3' {
            & $DevScript lint
            & $DevScript typecheck
            Invoke-Checked $Uv @(
                'run', 'pytest',
                'tests/integration/test_durable_runs.py',
                'tests/integration/test_agent_job_api.py',
                'tests/integration/test_conversation_engine.py',
                'tests/integration/test_p5_checkpoint.py',
                'tests/integration/test_workspace_migration.py',
                '-q'
            )
            Invoke-Checked 'npm' @('run', 'test', '--prefix', 'frontend')
            Invoke-Checked 'npm' @('run', 'build', '--prefix', 'frontend')
            & $DevScript check-architecture
        }
        'P4' {
            & $DevScript lint
            & $DevScript typecheck
            Invoke-Checked $Uv @(
                'run', 'pytest',
                'tests/integration/test_parallel_resources.py',
                'tests/integration/test_agent_job_api.py',
                'tests/integration/test_durable_runs.py',
                'tests/integration/test_workspace_migration.py',
                'tests/unit/test_p7_environment_runtime.py',
                '-q'
            )
            Invoke-Checked 'npm' @('run', 'test', '--prefix', 'frontend')
            Invoke-Checked 'npm' @('run', 'build', '--prefix', 'frontend')
            & $DevScript check-architecture
        }
        'P5' {
            & $DevScript lint
            & $DevScript typecheck
            Invoke-Checked $Uv @(
                'run', 'pytest',
                'tests/unit/test_interactive_workspace_contracts.py',
                'tests/unit/test_steering.py',
                'tests/integration/test_steering_api.py',
                'tests/integration/test_agent_loop.py',
                'tests/integration/test_durable_runs.py',
                'tests/integration/test_parallel_resources.py',
                'tests/integration/test_workspace_migration.py',
                '-q'
            )
            Invoke-Checked 'npm' @('run', 'test', '--prefix', 'frontend')
            Invoke-Checked 'npm' @('run', 'build', '--prefix', 'frontend')
            & $DevScript check-architecture
        }
        'P6' {
            & $DevScript lint
            & $DevScript typecheck
            Invoke-Checked $Uv @(
                'run', 'pytest',
                'tests/integration/test_provider_domains.py',
                'tests/integration/test_provider_settings_ui_contract.py',
                'tests/contract/test_p2_provider_contract.py',
                'tests/unit/test_interactive_workspace_architecture.py',
                '-q'
            )
            Invoke-Checked 'npm' @('run', 'test', '--prefix', 'frontend', '--', '--run')
            Invoke-Checked 'npm' @('run', 'build', '--prefix', 'frontend')
            & $DevScript check-architecture
        }
        'P7' {
            & $DevScript lint
            & $DevScript typecheck
            Invoke-Checked $Uv @(
                'run', 'pytest',
                'tests/unit/test_candidate_plan_repair.py',
                'tests/unit/test_p6_rendering.py',
                'tests/integration/test_p7_workspace_certification.py',
                'tests/integration/test_workspace_api.py',
                'tests/integration/test_p1_backup_events.py',
                'tests/integration/test_workspace_migration.py',
                'tests/integration/test_p8_recovery_api.py',
                'tests/unit/test_portable_backup.py',
                'tests/performance/test_interactive_workspace_targets.py',
                'tests/security',
                'tests/fault/test_p8_fault_matrix.py',
                '-q'
            )
            Invoke-Checked 'npm' @('run', 'test', '--prefix', 'frontend', '--', '--run')
            Invoke-Checked 'npm' @('run', 'build', '--prefix', 'frontend')
            Invoke-Checked $Uv @('run', 'pip', 'check')
            Invoke-Checked 'npm' @('audit', '--prefix', 'frontend', '--omit=dev', '--audit-level=high')
            $LiveEvidence = Join-Path $Root 'docs\test-reports\interactive-workspace\P7-live-full-paper.json'
            if (-not (Test-Path -LiteralPath $LiveEvidence)) {
                throw 'P7 live full-paper evidence is missing.'
            }
            $Live = Get-Content -LiteralPath $LiveEvidence -Raw | ConvertFrom-Json
            if ($Live.status -ne 'passed' -or $Live.output.formats.Count -lt 5) {
                throw 'P7 live full-paper evidence did not pass the artifact gate.'
            }
            $ArtifactManifest = Join-Path $Root 'docs\test-reports\interactive-workspace\P7-artifact-manifest.json'
            if (-not (Test-Path -LiteralPath $ArtifactManifest)) {
                throw 'P7 certified artifact manifest is missing.'
            }
            & $DevScript check-architecture
        }
        'P8' {
            & $DevScript lint
            & $DevScript typecheck
            Invoke-Checked $Uv @(
                'run', 'pytest',
                'tests/unit/test_p1_launcher.py',
                'tests/unit/test_p8_onboarding_backup.py',
                'tests/unit/test_p8_release_payload.py',
                'tests/integration/test_p8_recovery_api.py',
                'tests/integration/test_p8_installer_scripts.py',
                'tests/e2e/test_p8_release_workflow.py',
                'tests/performance/test_p8_targets.py',
                'tests/fault/test_p8_fault_matrix.py',
                '-q'
            )
            Invoke-Checked 'npm' @('run', 'test', '--prefix', 'frontend', '--', '--run')
            Invoke-Checked 'npm' @('run', 'build', '--prefix', 'frontend')
            & (Join-Path $PSScriptRoot 'check-release-candidate.ps1')
            & $DevScript check-architecture
        }
        default {
            throw "Phase $Phase gate is not implemented yet; complete its gate task first."
        }
    }
    Write-Host "Interactive workspace $Phase gate passed"
}
finally {
    Pop-Location
}
