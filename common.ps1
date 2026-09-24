[System.Net.ServicePointManager]::ServerCertificateValidationCallback = { $true }
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
    $parts = (Get-Content -LiteralPath $lf -Raw).Trim() -split ':'
    if ($parts.Count -lt 4) { return $null }
    return [pscustomobject]@{ Path = $lf; Port = $parts[2]; Password = $parts[3]; Protocol = $parts[4] }
}

function Invoke-LcuApi {
    param(
        [Parameter(Mandatory = $true)][string]$ApiPath,
        [Parameter(Mandatory = $true)]$Lock
    )
    $uri = '{0}://127.0.0.1:{1}{2}' -f $Lock.Protocol, $Lock.Port, $ApiPath
    $tmp = Join-Path $env:TEMP ("lcu_" + [guid]::NewGuid().ToString('N') + ".json")
    $code = curl.exe -k -s -u "riot:$($Lock.Password)" -o $tmp -w '%{http_code}' --max-time 15 $uri
    $raw = ''
    if (Test-Path -LiteralPath $tmp) {
        $raw = Get-Content -LiteralPath $tmp -Raw -Encoding UTF8
        Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
    }
    if ($code -ne '200') { throw "LCU HTTP $code for $ApiPath" }
    if (-not $raw) { return $null }
    return ($raw | ConvertFrom-Json)
}

function Get-ChampMap {
    $cache = Join-Path $PSScriptRoot 'champion.json'
    $needDownload = $true
    if (Test-Path -LiteralPath $cache) {
        if ((Get-Item -LiteralPath $cache).LastWriteTime -gt (Get-Date).AddDays(-14)) { $needDownload = $false }
    }
    if ($needDownload) {
        try {
            $vers = Invoke-RestMethod -Uri 'https://ddragon.leagueoflegends.com/api/versions.json' -TimeoutSec 20
            Invoke-WebRequest -Uri "https://ddragon.leagueoflegends.com/cdn/$($vers[0])/data/en_US/champion.json" -OutFile $cache -TimeoutSec 60 -UseBasicParsing
        } catch { }
    }
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
