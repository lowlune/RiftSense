#Requires -Version 5.1
<#
.SYNOPSIS
    Refreshes packaging/scoop/riftsense.json from the latest GitHub release.

.DESCRIPTION
    Fetches the latest stable release from the GitHub REST API, reads the
    SHA-256 digest GitHub computed for the versioned release asset, and writes
    version / url / hash into the Scoop manifest. Falls back to the release's
    SHA256SUMS.txt asset when the API does not expose a digest. Safe to run
    repeatedly: if the manifest already matches the latest release it is left
    untouched.

    The manifest keeps `checkver: "github"` and `autoupdate`, so Scoop users
    pick up later versions automatically; this script only keeps the committed
    baseline honest.

.PARAMETER Repository
    owner/name of the GitHub repository. Defaults to lowlune/RiftSense.

.PARAMETER Token
    Optional GitHub token (raises the API rate limit).

.PARAMETER DryRun
    Print the rendered manifest instead of writing it.

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File tools\update-scoop.ps1

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File tools\update-scoop.ps1 -DryRun
#>
[CmdletBinding()]
param(
    [string]$Repository = 'lowlune/RiftSense',
    [string]$Token,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$repoRoot = Split-Path -Parent $PSScriptRoot
$manifestPath = Join-Path $repoRoot (Join-Path 'packaging\scoop' 'riftsense.json')
$stableName = 'RiftSense-win-x64.zip'

$headers = @{
    'Accept'               = 'application/vnd.github+json'
    'User-Agent'           = 'RiftSense-update-scoop'
    'X-GitHub-Api-Version' = '2022-11-28'
}
if ($Token) { $headers['Authorization'] = "Bearer $Token" }

Write-Host "Fetching the latest release of $Repository..."
$release = Invoke-RestMethod -Uri "https://api.github.com/repos/$Repository/releases/latest" -Headers $headers -UseBasicParsing
if (-not $release.tag_name) {
    throw "GitHub returned no tag_name for $Repository; is the repository public?"
}
if ($release.prerelease) {
    throw "The latest release $($release.tag_name) is a prerelease; refusing to update the stable Scoop manifest."
}
$tag = [string]$release.tag_name
$version = $tag.TrimStart('v')
if ($version -notmatch '^\d+\.\d+\.\d+$') {
    throw "Unexpected release tag '$tag': expected vMAJOR.MINOR.PATCH."
}

$versionedName = "RiftSense-win-x64-v$version.zip"
$asset = @($release.assets) | Where-Object { $_.name -eq $versionedName } | Select-Object -First 1
if (-not $asset) {
    Write-Warning "Release $tag has no $versionedName; falling back to $stableName."
    $asset = @($release.assets) | Where-Object { $_.name -eq $stableName } | Select-Object -First 1
}
if (-not $asset) {
    throw "Release $tag has neither $versionedName nor $stableName."
}

$hash = $null
$digestProperty = $asset.PSObject.Properties['digest']
if ($digestProperty -and ($digestProperty.Value -match '^sha256:([0-9a-fA-F]{64})$')) {
    $hash = $Matches[1].ToLowerInvariant()
}
if (-not $hash) {
    Write-Warning "No asset digest available for $($asset.name); falling back to SHA256SUMS.txt."
    $sumsUrl = "https://github.com/$Repository/releases/download/$tag/SHA256SUMS.txt"
    $sums = (Invoke-WebRequest -Uri $sumsUrl -UseBasicParsing -Headers @{ 'User-Agent' = 'RiftSense-update-scoop' }).Content
    foreach ($line in ($sums -split "`n")) {
        if ($line -match ('^([0-9a-fA-F]{64})\s+\*?' + [regex]::Escape($asset.name) + '\s*$')) {
            $hash = $Matches[1].ToLowerInvariant()
            break
        }
    }
}
if (-not $hash) {
    throw "Could not determine a SHA-256 for $($asset.name) in release $tag; manifest left unchanged."
}

$url = "https://github.com/$Repository/releases/download/$tag/$($asset.name)"

$literalVersion = '$version'
$autoupdateUrl = "https://github.com/$Repository/releases/download/v$literalVersion/RiftSense-win-x64-v$literalVersion.zip"
$autoupdateApi = "https://api.github.com/repos/$Repository/releases/tags/v$literalVersion"
$autoupdateJsonPath = '$.assets[?(@.name == ''RiftSense-win-x64-v$version.zip'')].digest'

if (Test-Path -LiteralPath $manifestPath) {
    $manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
}
else {
    Write-Warning "Manifest not found at $manifestPath; creating a new one."
    $manifest = [pscustomobject]@{
        version      = ''
        description  = 'Local League of Legends coaching dashboard (portable).'
        homepage     = "https://github.com/$Repository"
        license      = 'MIT'
        architecture = [pscustomobject]@{ '64bit' = [pscustomobject]@{ url = ''; hash = '' } }
        extract_dir  = 'RiftSense'
        shortcuts    = @(@('Start-Ui.cmd', 'RiftSense UI'), @('Start-AutoCoach.cmd', 'RiftSense AutoCoach'))
        checkver     = 'github'
        autoupdate   = [pscustomobject]@{
            architecture = [pscustomobject]@{
                '64bit' = [pscustomobject]@{
                    url  = $autoupdateUrl
                    hash = [pscustomobject]@{ url = $autoupdateApi; jsonpath = $autoupdateJsonPath }
                }
            }
        }
    }
}

foreach ($name in @('architecture')) {
    if (-not $manifest.PSObject.Properties[$name]) {
        $manifest | Add-Member -MemberType NoteProperty -Name $name -Value ([pscustomobject]@{})
    }
}
if (-not $manifest.architecture.PSObject.Properties['64bit']) {
    $manifest.architecture | Add-Member -MemberType NoteProperty -Name '64bit' -Value ([pscustomobject]@{})
}

$arch = $manifest.architecture.'64bit'
$alreadyCurrent = ($manifest.version -eq $version) -and ($arch.url -eq $url) -and ($arch.hash -eq $hash)
if ($alreadyCurrent) {
    Write-Host "Already current: riftsense $version ($hash)"
    return
}

$manifest.version = $version
$arch.url = $url
$arch.hash = $hash

$json = $manifest | ConvertTo-Json -Depth 16
if ($DryRun) {
    Write-Host $json
    return
}

[System.IO.File]::WriteAllText($manifestPath, ($json + "`n"), (New-Object -TypeName System.Text.UTF8Encoding -ArgumentList $false))
Write-Host "Updated $manifestPath"
Write-Host "  version: $version"
Write-Host "  url:     $url"
Write-Host "  hash:    $hash"
Write-Host ''
Write-Host "Commit the manifest change (e.g. git add packaging/scoop/riftsense.json) once the release is live."
