try { [System.Net.ServicePointManager]::SecurityProtocol = [System.Net.SecurityProtocolType]::Tls12 } catch {}

$script:SpellMap = @{
    1 = 'Cleanse'; 3 = 'Exhaust'; 4 = 'Flash'; 6 = 'Ghost'; 7 = 'Heal';
    11 = 'Smite'; 12 = 'Teleport'; 13 = 'Clarity'; 14 = 'Ignite';
    21 = 'Barrier'; 32 = 'Snowball'; 39 = 'Mark'
}

$script:QueueMap = @{
    400 = 'Normal Draft'; 420 = 'Ranked Solo'; 430 = 'Normal Blind'; 440 = 'Ranked Flex';
    450 = 'ARAM'; 700 = 'Clash'; 720 = 'ARAM Clash'; 900 = 'URF'; 1700 = 'Arena'; 1900 = 'URF'
}

function Get-SpellName {
    param($Id)
    if ($null -eq $Id) { return '-' }
    $i = [int]$Id
    if ($script:SpellMap.ContainsKey($i)) { return $script:SpellMap[$i] }
    return "#$Id"
}

function Get-QueueName {
    param($Id)
    if ($null -eq $Id) { return '?' }
    $i = [int]$Id
    if ($script:QueueMap.ContainsKey($i)) { return $script:QueueMap[$i] }
    return "Queue $Id"
}

function Get-LcuLockfile {
    $candidates = @()
    if ($env:LEAGUE_LOCKFILE) { $candidates += $env:LEAGUE_LOCKFILE }
    Get-ItemProperty -Path @(
        'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*',
        'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*',
        'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*'
    ) -ErrorAction SilentlyContinue |
        Where-Object { $_.DisplayName -eq 'League of Legends' -and $_.InstallLocation } |
        ForEach-Object { $candidates += (($_.InstallLocation -replace '/', '\').TrimEnd('\') + '\lockfile') }
    foreach ($base in @('HKLM:\SOFTWARE\WOW6432Node\Riot Games\League of Legends', 'HKLM:\SOFTWARE\Riot Games\League of Legends', 'HKCU:\Software\Riot Games\League of Legends')) {
        $p = (Get-ItemProperty -Path $base -ErrorAction SilentlyContinue).Path
        if ($p) { $candidates += (($p -replace '/', '\').TrimEnd('\') + '\lockfile') }
    }
    Get-PSDrive -PSProvider FileSystem -ErrorAction SilentlyContinue | Where-Object { $_.Free -ne $null } | ForEach-Object {
        foreach ($sub in @('Riot Games\League of Legends', 'Games\Riot Games\League of Legends', 'Program Files\Riot Games\League of Legends', 'Program Files (x86)\Riot Games\League of Legends')) {
            $candidates += ((Join-Path $_.Root $sub) + '\lockfile')
        }
    }
    $lf = $candidates | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -First 1
    if (-not $lf) { return $null }
    $raw = ''
    try { $raw = (Get-Content -LiteralPath $lf -Raw -Encoding UTF8 -ErrorAction Stop).Trim() } catch { return $null }
    if (-not $raw) { return $null }
    $parts = $raw -split ':'
    if ($parts.Count -ne 5) { return $null }
    $port = 0
    if (-not [int]::TryParse("$($parts[2])".Trim(), [ref]$port)) { return $null }
    if ($port -lt 1 -or $port -gt 65535) { return $null }
    $protocol = "$($parts[4])".Trim().ToLowerInvariant()
    if (($protocol -ne 'http') -and ($protocol -ne 'https')) { return $null }
    if (-not "$($parts[3])") { return $null }
    return [pscustomobject]@{ Path = $lf; Port = $port; Password = "$($parts[3])"; Protocol = $protocol }
}

function Invoke-LcuApi {
    param(
        [Parameter(Mandatory = $true)][string]$ApiPath,
        [Parameter(Mandatory = $true)]$Lock
    )
    $uri = '{0}://127.0.0.1:{1}{2}' -f $Lock.Protocol, $Lock.Port, $ApiPath
    $tmp = Join-Path $env:TEMP ("lcu_" + [guid]::NewGuid().ToString('N') + ".json")
    $netrc = Join-Path $env:TEMP ("lcu_netrc_" + [guid]::NewGuid().ToString('N') + ".txt")
    $raw = ''
    $code = '000'
    try {
        Set-Content -LiteralPath $netrc -Value ("machine 127.0.0.1 login riot password {0}" -f $Lock.Password) -Encoding ASCII
        if (Test-Path -LiteralPath $tmp) { Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue }
        $code = curl.exe -k -s --netrc-file $netrc -o $tmp -w '%{http_code}' --max-time 15 $uri
        $curlExit = $LASTEXITCODE
        if ($curlExit -eq 0 -and $code -eq '200' -and (Test-Path -LiteralPath $tmp)) {
            $raw = Get-Content -LiteralPath $tmp -Raw -Encoding UTF8
        }
    } finally {
        Remove-Item -LiteralPath $netrc -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
    }
    if ($code -ne '200') { throw "LCU HTTP $code for $ApiPath" }
    if (-not $raw) { return $null }
    return ($raw | ConvertFrom-Json)
}

function Update-DdragonAsset {
    param(
        [Parameter(Mandatory = $true)][string[]]$Names,
        [switch]$Force
    )
    $todo = @()
    foreach ($name in $Names) {
        $dest = Join-Path $PSScriptRoot ($name + '.json')
        if ($Force -or -not (Test-Path -LiteralPath $dest)) { $todo += $name }
    }
    if (-not $todo) { return }
    $ver = $null
    try { $ver = (Invoke-RestMethod -Uri 'https://ddragon.leagueoflegends.com/api/versions.json' -TimeoutSec 20)[0] } catch { return }
    if (-not $ver) { return }
    foreach ($name in $todo) {
        $dest = Join-Path $PSScriptRoot ($name + '.json')
        $tmp = Join-Path $env:TEMP ("riftsense_" + $name + "_" + [guid]::NewGuid().ToString('N') + ".json")
        try {
            Invoke-WebRequest -Uri ("https://ddragon.leagueoflegends.com/cdn/{0}/data/en_US/{1}.json" -f $ver, $name) -OutFile $tmp -TimeoutSec 60 -UseBasicParsing -ErrorAction Stop
            if ((Test-Path -LiteralPath $tmp) -and ((Get-Item -LiteralPath $tmp).Length -gt 0)) {
                Move-Item -LiteralPath $tmp -Destination $dest -Force -ErrorAction Stop
            }
        } catch { }
        if (Test-Path -LiteralPath $tmp) { Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue }
    }
}

function Get-ChampMap {
    $cache = Join-Path $PSScriptRoot 'champion.json'
    $needDownload = $true
    if (Test-Path -LiteralPath $cache) {
        try {
            if ((Get-Item -LiteralPath $cache).LastWriteTime -gt (Get-Date).AddDays(-14)) { $needDownload = $false }
        } catch { }
    }
    if ($needDownload) { Update-DdragonAsset -Names @('champion') -Force }
    if (-not (Test-Path -LiteralPath $cache)) { return $null }
    try {
        $json = Get-Content -LiteralPath $cache -Raw | ConvertFrom-Json
        $map = @{}
        foreach ($prop in $json.data.PSObject.Properties) { $map[[int]$prop.Value.key] = $prop.Value.name }
        return $map
    } catch { return $null }
}

function Get-ChampName {
    param($Map, $Id)
    if ($null -eq $Id -or [int]$Id -eq 0) { return '-' }
    if ($Map -and $Map.ContainsKey([int]$Id)) { return $Map[[int]$Id] }
    return "#$Id"
}

function Get-PStat {
    param($Obj, $Name)
    if (-not $Obj) { return $null }
    if ($Obj.PSObject.Properties.Name -contains $Name) { return $Obj.$Name }
    if ($Obj.scores -and ($Obj.scores.PSObject.Properties.Name -contains $Name)) { return $Obj.scores.$Name }
    return $null
}
