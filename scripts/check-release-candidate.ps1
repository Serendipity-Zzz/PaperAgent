[CmdletBinding()]
param([switch]$RequireArtifacts)

$ErrorActionPreference = 'Stop'
$Root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$Scripts = @(
    'start-local.ps1',
    'install-source-shortcut.ps1',
    'install-user.ps1',
    'uninstall-user.ps1',
    'rollback-user.ps1',
    'build_release.ps1'
)
foreach ($Name in $Scripts) {
    $Tokens = $null
    $Errors = $null
    [System.Management.Automation.Language.Parser]::ParseFile(
        (Join-Path $PSScriptRoot $Name), [ref]$Tokens, [ref]$Errors
    ) | Out-Null
    if ($Errors.Count) { throw "$Name PowerShell parse failed: $($Errors[0].Message)" }
}
if (-not $RequireArtifacts) {
    Write-Host 'Release script syntax passed'
    exit 0
}

$IndexPath = Join-Path $Root 'dist\release\release-index.json'
if (-not (Test-Path -LiteralPath $IndexPath)) { throw 'release-index.json is missing' }
$Index = Get-Content -LiteralPath $IndexPath -Raw | ConvertFrom-Json
$Commit = (git -C $Root rev-parse HEAD).Trim()
if ($Index.commit -ne $Commit) { throw "Release commit $($Index.commit) does not match HEAD $Commit" }
foreach ($Artifact in $Index.artifacts) {
    $Path = Join-Path $Root "dist\release\$($Artifact.name)"
    if (-not (Test-Path -LiteralPath $Path)) { throw "Missing release artifact: $($Artifact.name)" }
    $Actual = (Get-FileHash -Algorithm SHA256 $Path).Hash.ToLowerInvariant()
    if ($Actual -ne $Artifact.sha256) { throw "Release hash mismatch: $($Artifact.name)" }
}
$Portable = Join-Path $Root "dist\release\PaperAgent-$($Index.version)-windows-x64"
$Uv = if (Get-Command uv -ErrorAction SilentlyContinue) {
    (Get-Command uv).Source
} elseif (Test-Path 'E:\App\uv\current\uv.exe') {
    'E:\App\uv\current\uv.exe'
} else { throw 'uv was not found' }
& $Uv run python (Join-Path $PSScriptRoot 'verify_release.py') $Portable
if ($LASTEXITCODE -ne 0) { throw 'Release payload verification failed' }
$SmokeData = Join-Path $Root "tmp\release-smoke-$([guid]::NewGuid().ToString('N'))"
$PreviousData = $env:PAPERAGENT_DATA_DIR
$env:PAPERAGENT_DATA_DIR = $SmokeData
try {
    $Smoke = Start-Process -FilePath (Join-Path $Portable 'PaperAgent.exe') `
        -ArgumentList @('--smoke-test', '--no-browser', '--no-tray') `
        -WindowStyle Hidden -Wait -PassThru
    if ($Smoke.ExitCode -ne 0) { throw "Packaged smoke failed: $($Smoke.ExitCode)" }
}
finally { $env:PAPERAGENT_DATA_DIR = $PreviousData }
Write-Host 'Release candidate hash, payload and packaged smoke passed'
