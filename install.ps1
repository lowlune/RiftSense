#Requires -Version 5.1
<#
.SYNOPSIS
    Installs or updates RiftSense for the current user. No admin required.

.DESCRIPTION
    Resolves the latest stable release tag from the GitHub releases Atom feed
    (not subject to the unauthenticated REST rate limit), downloads the stable
    RiftSense-win-x64.zip asset, verifies its SHA-256 against the GitHub release
    asset digest (falling back to the release's SHA256SUMS.txt), checks the
    archive for unsafe paths, and extracts it to %LOCALAPPDATA%\RiftSense,
    preserving an existing build_intent.txt.

    This script never uses Invoke-Expression and never executes anything from
    the downloaded archive.

.PARAMETER InstallDir
    Target directory. Defaults to %LOCALAPPDATA%\RiftSense.

.PARAMETER Force
    Allow installing over an existing directory that does not look like a
    RiftSense install.

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1

.EXAMPLE
    irm https://github.com/lowlune/RiftSense/releases/latest/download/install.ps1 | iex
#>
[CmdletBinding()]
param(
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA 'RiftSense'),
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$Repository = 'lowlune/RiftSense'
$AssetName = 'RiftSense-win-x64.zip'
$AtomUrl = "https://github.com/$Repository/releases.atom"
$DownloadUrl = "https://github.com/$Repository/releases/latest/download/$AssetName"
$UserAgent = 'RiftSense-installer'

function Get-LatestStableTag {
    $response = Invoke-WebRequest -Uri $AtomUrl -UseBasicParsing -Headers @{ 'User-Agent' = $UserAgent }
    $feed = [xml]$response.Content
    foreach ($entry in @($feed.feed.entry)) {
        $link = @($entry.link) | Where-Object { $_.rel -eq 'alternate' } | Select-Object -First 1
        if (-not $link) { continue }
        $tag = ($link.href -split '/tag/')[-1]
        if ($tag -match '-') { continue }
        if ($tag -match '^v\d+\.\d+\.\d+$') { return $tag }
    }
    throw "Could not find a stable release in the Atom feed at $AtomUrl. Check your network connection and try again."
}

function Get-ExpectedSha256([string]$Tag) {
    try {
        $headers = @{
            'Accept'               = 'application/vnd.github+json'
            'User-Agent'           = $UserAgent
            'X-GitHub-Api-Version' = '2022-11-28'
        }
        $release = Invoke-RestMethod -Uri "https://api.github.com/repos/$Repository/releases/tags/$Tag" -Headers $headers -UseBasicParsing
        foreach ($asset in @($release.assets)) {
            $hasDigest = $asset.PSObject.Properties['digest'] -and ($asset.PSObject.Properties['digest'].Value -match '^sha256:([0-9a-fA-F]{64})$')
            if (($asset.name -eq $AssetName) -and $hasDigest) {
                return $Matches[1].ToLowerInvariant()
            }
        }
    }
    catch {
        Write-Warning "GitHub API digest unavailable ($($_.Exception.Message)); falling back to SHA256SUMS.txt."
    }

    $sumsUrl = "https://github.com/$Repository/releases/download/$Tag/SHA256SUMS.txt"
    try {
        $sums = (Invoke-WebRequest -Uri $sumsUrl -UseBasicParsing -Headers @{ 'User-Agent' = $UserAgent }).Content
    }
    catch {
        throw "Could not verify the download: no asset digest and no SHA256SUMS.txt for release $Tag. Nothing was installed."
    }
    foreach ($line in ($sums -split "`n")) {
        if ($line -match ('^([0-9a-fA-F]{64})\s+\*?' + [regex]::Escape($AssetName) + '\s*$')) {
            return $Matches[1].ToLowerInvariant()
        }
    }
    throw "SHA256SUMS.txt for release $Tag has no entry for $AssetName. Nothing was installed."
}

function Assert-SafeArchive([string]$ZipPath) {
    if (-not ('System.IO.Compression.ZipFile' -as [type])) {
        Add-Type -AssemblyName System.IO.Compression.FileSystem
    }
    $archive = [System.IO.Compression.ZipFile]::OpenRead($ZipPath)
    try {
        $names = @()
        foreach ($entry in $archive.Entries) {
            $name = [string]$entry.FullName
            if ($name -match '(^|/)\.\.(/|$)' -or $name.StartsWith('/') -or $name.StartsWith('\') -or $name -match '^[A-Za-z]:') {
                throw "The downloaded archive contains an unsafe path ('$name'); refusing to install."
            }
            $names += $name
        }
        foreach ($required in @('RiftSense/VERSION', 'RiftSense/ui/server.py', 'RiftSense/Start-Ui.cmd')) {
            if ($names -notcontains $required) {
                throw "The downloaded archive is missing $required; refusing to install."
            }
        }
    }
    finally {
        $archive.Dispose()
    }
}

$tag = Get-LatestStableTag
Write-Host "Latest stable release: $tag"

$expected = Get-ExpectedSha256 -Tag $tag

$tempRoot = Join-Path $env:TEMP ("RiftSense-install-" + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Force -Path $tempRoot | Out-Null
try {
    $zipPath = Join-Path $tempRoot $AssetName
    Write-Host "Downloading $DownloadUrl"
    Invoke-WebRequest -Uri $DownloadUrl -OutFile $zipPath -UseBasicParsing -Headers @{ 'User-Agent' = $UserAgent }

    $actual = (Get-FileHash -LiteralPath $zipPath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actual -ne $expected) {
        throw "SHA-256 mismatch for ${AssetName}.`n  expected: $expected`n  actual:   $actual`nThe download may be corrupted or tampered with. Nothing was installed."
    }
    Write-Host "SHA-256 verified: $actual"

    Assert-SafeArchive -ZipPath $zipPath

    if (-not ('System.IO.Compression.ZipFile' -as [type])) {
        Add-Type -AssemblyName System.IO.Compression.FileSystem
    }
    $staging = Join-Path $tempRoot 'staging'
    [System.IO.Compression.ZipFile]::ExtractToDirectory($zipPath, $staging)
    $payload = Join-Path $staging 'RiftSense'
    if (-not (Test-Path -LiteralPath $payload)) {
        throw 'The archive did not contain the expected RiftSense folder; nothing was installed.'
    }
    $newVersion = (Get-Content -LiteralPath (Join-Path $payload 'VERSION') -Raw).Trim()

    $intentPath = Join-Path $InstallDir 'build_intent.txt'
    $savedIntent = $null
    if (Test-Path -LiteralPath $intentPath) {
        $savedIntent = [System.IO.File]::ReadAllText($intentPath)
        Write-Host 'Preserving your existing build_intent.txt'
    }

    if ((Test-Path -LiteralPath $InstallDir) -and (-not $Force)) {
        if (-not (Test-Path -LiteralPath (Join-Path $InstallDir 'ui\server.py'))) {
            throw "'$InstallDir' exists but does not look like a RiftSense install. Re-run with -Force to install anyway, or pass a different -InstallDir."
        }
    }

    New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
    foreach ($item in Get-ChildItem -LiteralPath $payload -Force) {
        Copy-Item -LiteralPath $item.FullName -Destination $InstallDir -Recurse -Force
    }

    if ($null -ne $savedIntent) {
        [System.IO.File]::WriteAllText($intentPath, $savedIntent, (New-Object -TypeName System.Text.UTF8Encoding -ArgumentList $false))
    }

    Write-Host ''
    Write-Host "RiftSense $newVersion installed to $InstallDir"
    Write-Host ''
    Write-Host 'Next steps:'
    Write-Host "  1. Copy the coach agent into opencode: $InstallDir\agent\lol-coach.md  ->  $env:USERPROFILE\.config\opencode\agent\lol-coach.md"
    Write-Host "  2. Start the dashboard:  $InstallDir\Start-Ui.cmd"
    Write-Host "  3. Start coaching:       $InstallDir\Start-AutoCoach.cmd"
    Write-Host '  Updates: re-run this installer, or use the dashboard update check between games.'
}
finally {
    Remove-Item -LiteralPath $tempRoot -Recurse -Force -ErrorAction SilentlyContinue
}
