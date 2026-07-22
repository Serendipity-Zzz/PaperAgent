[CmdletBinding()]
param([switch]$SkipExecutable, [switch]$AllowDirty)

$ErrorActionPreference = 'Stop'
$Root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path

function Resolve-Uv {
    $fromPath = Get-Command uv -ErrorAction SilentlyContinue
    if ($fromPath) { return $fromPath.Source }
    if ($env:PAPERAGENT_UV_PATH -and (Test-Path -LiteralPath $env:PAPERAGENT_UV_PATH)) {
        return (Resolve-Path -LiteralPath $env:PAPERAGENT_UV_PATH).Path
    }
    foreach ($candidate in @((Join-Path $Root 'tools\uv\uv.exe'), 'E:\App\uv\current\uv.exe')) {
        if (Test-Path -LiteralPath $candidate) { return $candidate }
    }
    throw 'uv was not found.'
}

function Invoke-Checked {
    param([string]$Program, [string[]]$Arguments)
    & $Program @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Command failed ($LASTEXITCODE): $Program $($Arguments -join ' ')" }
}

$Uv = Resolve-Uv
if (-not $AllowDirty) {
    $Dirty = git -C $Root status --porcelain
    if ($Dirty) { throw 'Release builds require a clean working tree. Commit and verify the source first.' }
}
$Version = (& $Uv run python -c "import tomllib;print(tomllib.load(open('pyproject.toml','rb'))['project']['version'])").Trim()
$Commit = (git -C $Root rev-parse HEAD).Trim()
$Release = Join-Path $Root 'dist\release'
$ExecutableDir = Join-Path $Root 'dist\pyinstaller\PaperAgent'
$Manifest = Join-Path $Root 'dist\RELEASE.json'

Push-Location $Root
try {
    Invoke-Checked 'npm' @('run', 'build', '--prefix', 'frontend')
    Invoke-Checked $Uv @('run', 'python', 'scripts/generate_sbom.py')
    Invoke-Checked $Uv @('run', 'python', 'scripts/generate_release_manifest.py', $Manifest)

    if (-not $SkipExecutable) {
        Invoke-Checked $Uv @(
            'run', '--with', 'pyinstaller>=6.14,<7', 'pyinstaller',
            '--noconfirm', '--clean', '--onedir', '--windowed',
            '--name', 'PaperAgent', '--paths', (Join-Path $Root 'backend'),
            '--collect-submodules', 'paperagent',
            '--add-data', "$(Join-Path $Root 'frontend\dist');frontend\dist",
            '--add-data', "$(Join-Path $Root 'migrations');migrations",
            '--add-data', "$(Join-Path $Root 'alembic.ini');.",
            '--add-data', "$(Join-Path $Root 'knowledge');knowledge",
            '--add-data', "$(Join-Path $Root 'skills');skills",
            '--add-data', "$(Join-Path $Root 'third_party');third_party",
            '--distpath', (Join-Path $Root 'dist\pyinstaller'),
            '--workpath', (Join-Path $Root 'dist\pyinstaller-work'),
            '--specpath', (Join-Path $Root 'dist'),
            (Join-Path $Root 'launcher\main.py')
        )
    }

    if (-not (Test-Path -LiteralPath (Join-Path $ExecutableDir 'PaperAgent.exe'))) {
        throw 'Windows executable is missing; omit -SkipExecutable or restore the controlled build output.'
    }
    New-Item -ItemType Directory -Force -Path $Release | Out-Null
    $ReleasePrefix = [IO.Path]::GetFullPath($Release).TrimEnd('\') + '\'
    foreach ($Existing in Get-ChildItem -LiteralPath $Release -Force) {
        $ResolvedExisting = [IO.Path]::GetFullPath($Existing.FullName)
        if (-not $ResolvedExisting.StartsWith($ReleasePrefix, [StringComparison]::OrdinalIgnoreCase)) {
            throw "Release cleanup escaped the release directory: $ResolvedExisting"
        }
        Remove-Item -LiteralPath $ResolvedExisting -Recurse -Force
    }
    $PortableName = "PaperAgent-$Version-windows-x64"
    $Portable = Join-Path $Release $PortableName
    if (Test-Path -LiteralPath $Portable) {
        $ResolvedRelease = (Resolve-Path $Release).Path.TrimEnd('\') + '\'
        $ResolvedPortable = (Resolve-Path $Portable).Path
        if (-not $ResolvedPortable.StartsWith($ResolvedRelease, [StringComparison]::OrdinalIgnoreCase)) {
            throw 'Portable cleanup target escaped the release directory.'
        }
        Remove-Item -LiteralPath $Portable -Recurse -Force
    }
    New-Item -ItemType Directory -Force -Path $Portable | Out-Null
    Copy-Item -Path (Join-Path $ExecutableDir '*') -Destination $Portable -Recurse
    foreach ($file in @('LICENSE', 'THIRD_PARTY_NOTICES.md')) { Copy-Item (Join-Path $Root $file) $Portable }
    Copy-Item $Manifest (Join-Path $Portable 'RELEASE.json')
    Copy-Item (Join-Path $Root 'docs\release\sbom.cdx.json') $Portable
    Copy-Item (Join-Path $Root 'docs\user-guide.md') $Portable
    foreach ($script in @('install-user.ps1', 'uninstall-user.ps1', 'rollback-user.ps1')) {
        Copy-Item (Join-Path $Root "scripts\$script") $Portable
    }
    $DependencyTests = Get-ChildItem -LiteralPath (Join-Path $Portable '_internal') `
        -Recurse -Directory -Filter 'tests' -ErrorAction SilentlyContinue
    foreach ($Directory in $DependencyTests) {
        $PortablePrefix = [IO.Path]::GetFullPath($Portable).TrimEnd('\') + '\'
        $ResolvedDirectory = [IO.Path]::GetFullPath($Directory.FullName)
        if (-not $ResolvedDirectory.StartsWith($PortablePrefix, [StringComparison]::OrdinalIgnoreCase)) {
            throw "Dependency test cleanup escaped the portable root: $ResolvedDirectory"
        }
        Remove-Item -LiteralPath $ResolvedDirectory -Recurse -Force
    }
    Invoke-Checked $Uv @('run', 'python', 'scripts/verify_release.py', $Portable)

    $Zip = Join-Path $Release "$PortableName.zip"
    if (Test-Path -LiteralPath $Zip) { Remove-Item -LiteralPath $Zip -Force }
    Compress-Archive -Path $Portable -DestinationPath $Zip -CompressionLevel Optimal
    $Files = @($Zip)
    $IsccCommand = Get-Command ISCC.exe -ErrorAction SilentlyContinue
    $IsccPath = if ($IsccCommand) { $IsccCommand.Source } else {
        @(
            'E:\App\InnoSetup\ISCC.exe',
            (Join-Path $env:LOCALAPPDATA 'Programs\Inno Setup 6\ISCC.exe'),
            (Join-Path $env:ProgramFiles 'Inno Setup 6\ISCC.exe')
        ) | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -First 1
    }
    if ($IsccPath) {
        Invoke-Checked $IsccPath @("/DMyAppVersion=$Version", "/DMyCommit=$Commit", (Join-Path $Root 'installer\paperagent.iss'))
        $Setup = Join-Path $Release "PaperAgent-$Version-Setup.exe"
        if (Test-Path -LiteralPath $Setup) { $Files += $Setup }
    }
    $Sums = foreach ($file in $Files) {
        $hash = (Get-FileHash -Algorithm SHA256 $file).Hash.ToLowerInvariant()
        "$hash  $(Split-Path $file -Leaf)"
    }
    $Sums | Set-Content -Encoding utf8 (Join-Path $Release 'SHA256SUMS.txt')
    [ordered]@{
        schema_version = 1
        version = $Version
        commit = $Commit
        built_at = (Get-Date).ToUniversalTime().ToString('o')
        artifacts = @($Files | ForEach-Object {
            [ordered]@{ name = (Split-Path $_ -Leaf); size = (Get-Item $_).Length; sha256 = (Get-FileHash $_ -Algorithm SHA256).Hash.ToLowerInvariant() }
        })
    } | ConvertTo-Json -Depth 6 | Set-Content -Encoding utf8 (Join-Path $Release 'release-index.json')
    Write-Host "Release: $Zip"
    Write-Host "Source commit: $Commit"
}
finally { Pop-Location }
