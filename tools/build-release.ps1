#Requires -Version 5.1
<#
.SYNOPSIS
    Builds the RiftSense release ZIP locally from tracked files only.

.DESCRIPTION
    Local equivalent of the CI `build` job. Archives the current HEAD commit
    with `git archive` (tracked files only, so runtime state such as
    champion.json/items.json can never leak into a release), stamps the
    version into the VERSION file inside the archive, and writes
    SHA256SUMS.txt next to the artifacts.

.PARAMETER Version
    Version to stamp, without a leading `v`. Defaults to the VERSION file.

.PARAMETER OutputDirectory
    Where to write the artifacts. Defaults to <repo>/dist.

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File tools\build-release.ps1

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File tools\build-release.ps1 -Version 1.0.1
#>
[CmdletBinding()]
param(
    [string]$Version,
    [string]$OutputDirectory
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$repoRoot = Split-Path -Parent $PSScriptRoot
if (-not $OutputDirectory) {
    $OutputDirectory = Join-Path $repoRoot 'dist'
}

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    throw 'git was not found on PATH. Install Git for Windows and retry.'
}

if (-not $Version) {
    $versionFile = Join-Path $repoRoot 'VERSION'
    if (-not (Test-Path -LiteralPath $versionFile)) {
        throw "VERSION file not found at $versionFile. Pass -Version explicitly."
    }
    $Version = (Get-Content -LiteralPath $versionFile -Raw).Trim()
}
$Version = $Version.TrimStart('v')
if ($Version -notmatch '^\d+\.\d+\.\d+') {
    throw "Version '$Version' does not look like MAJOR.MINOR.PATCH."
}

$stableName = 'RiftSense-win-x64.zip'
$versionedName = "RiftSense-win-x64-v$Version.zip"
$zipPath = Join-Path $OutputDirectory $versionedName

New-Item -ItemType Directory -Force -Path $OutputDirectory | Out-Null

Push-Location $repoRoot
try {
    $dirty = @(git status --porcelain)
    if ($dirty.Count -gt 0) {
        Write-Warning 'Worktree has uncommitted changes. The release is built from committed HEAD only.'
    }

    if (Test-Path -LiteralPath $zipPath) {
        Remove-Item -LiteralPath $zipPath -Force
    }
    git archive --format=zip --prefix=RiftSense/ -o $zipPath HEAD
    if ($LASTEXITCODE -ne 0) {
        throw "git archive failed with exit code $LASTEXITCODE."
    }
}
finally {
    Pop-Location
}

# Stamp the requested version into the copy of VERSION inside the archive.
if (-not ('System.IO.Compression.ZipFile' -as [type])) {
    Add-Type -AssemblyName System.IO.Compression.FileSystem
}
$archive = [System.IO.Compression.ZipFile]::Open($zipPath, [System.IO.Compression.ZipArchiveMode]::Update)
try {
    $entry = $archive.GetEntry('RiftSense/VERSION')
    if (-not $entry) {
        throw 'git archive produced no RiftSense/VERSION entry; is VERSION tracked?'
    }
    $entry.Delete()
    $newEntry = $archive.CreateEntry('RiftSense/VERSION')
    $writer = New-Object -TypeName System.IO.StreamWriter -ArgumentList @($newEntry.Open(), (New-Object -TypeName System.Text.UTF8Encoding -ArgumentList $false))
    try {
        $writer.WriteLine($Version)
    }
    finally {
        $writer.Dispose()
    }
}
finally {
    $archive.Dispose()
}

Copy-Item -LiteralPath $zipPath -Destination (Join-Path $OutputDirectory $stableName) -Force
$installerSource = Join-Path $repoRoot 'install.ps1'
if (Test-Path -LiteralPath $installerSource) {
    Copy-Item -LiteralPath $installerSource -Destination (Join-Path $OutputDirectory 'install.ps1') -Force
}

$sumLines = @()
foreach ($name in @($stableName, $versionedName, 'install.ps1')) {
    $candidate = Join-Path $OutputDirectory $name
    if (-not (Test-Path -LiteralPath $candidate)) { continue }
    $hash = (Get-FileHash -LiteralPath $candidate -Algorithm SHA256).Hash.ToLowerInvariant()
    $sumLines += "$hash  $name"
}
if ($sumLines.Count -eq 0) {
    throw 'no artifacts were produced; nothing to checksum.'
}
$sumsPath = Join-Path $OutputDirectory 'SHA256SUMS.txt'
[System.IO.File]::WriteAllText($sumsPath, (($sumLines -join "`n") + "`n"), (New-Object -TypeName System.Text.UTF8Encoding -ArgumentList $false))

Write-Host ''
Write-Host "Built RiftSense $Version"
Write-Host "  $zipPath"
Write-Host "  $(Join-Path $OutputDirectory $stableName)"
Write-Host "  $sumsPath"
Write-Host ''
Write-Host 'Next steps:'
Write-Host "  1. Inspect dist\SHA256SUMS.txt and smoke-test the extracted ZIP."
Write-Host "  2. git tag -a v$Version -m `"RiftSense v$Version`""
Write-Host "  3. git push origin v$Version   # CI runs tests, builds, attests and publishes"
Write-Host "  4. After the release: powershell -File tools\update-scoop.ps1"
Write-Host ''
Write-Host "Manual fallback: gh release create v$Version dist\RiftSense-win-x64-v$Version.zip dist\SHA256SUMS.txt --title `"RiftSense v$Version`""
