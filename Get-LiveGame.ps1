param([string]$Source)

. (Join-Path $PSScriptRoot 'common.ps1')

function Fmt-Time {
    param([double]$Seconds)
    $t = [int]$Seconds
    return ('{0}:{1:00}' -f [int][math]::Floor($t / 60), ($t % 60))
}

$tmp = Join-Path $env:TEMP 'lol_live_data.json'
$d = $null
if ($Source -and (Test-Path -LiteralPath $Source)) {
    $raw = Get-Content -LiteralPath $Source -Raw -Encoding UTF8
    if ($raw) { try { $d = $raw | ConvertFrom-Json } catch { } }
}
if (-not $d) {
    $null = curl.exe -k -s -o $tmp --max-time 8 'https://127.0.0.1:2999/liveclientdata/allgamedata'
    if (Test-Path -LiteralPath $tmp) {
        $raw2 = Get-Content -LiteralPath $tmp -Raw -Encoding UTF8
        if ($raw2) { try { $d = $raw2 | ConvertFrom-Json } catch { } }
    }
}
if (-not $d -or -not $d.gameData) {
    Write-Output 'Not in a live game (Live Client Data API not reachable).'
    exit 0
}

$itemGold = @{}
$itemsCache = Join-Path $PSScriptRoot 'items.json'
if (-not (Test-Path -LiteralPath $itemsCache)) {
    try {
        $vers = Invoke-RestMethod -Uri 'https://ddragon.leagueoflegends.com/api/versions.json' -TimeoutSec 20
        Invoke-WebRequest -Uri ("https://ddragon.leagueoflegends.com/cdn/$($vers[0])/data/en_US/item.json") -OutFile $itemsCache -TimeoutSec 60 -UseBasicParsing
    } catch { }
}
if (Test-Path -LiteralPath $itemsCache) {
    try {
        $ij = (Get-Content -LiteralPath $itemsCache -Raw -Encoding UTF8) | ConvertFrom-Json
        foreach ($prop in $ij.data.PSObject.Properties) { $itemGold[[int]$prop.Name] = [int]$prop.Value.gold.total }
    } catch { }
}

function Short-Item {
    param([string]$Name)
    if (-not $Name) { return '' }
    if ($Name -like 'Stealth Ward*') { return '' }
    if ($Name -like '*Potion*') { return 'Potion' }
    if ($Name -match "^Liandry") { return "Liandry's" }
    if ($Name -match "^Rylai") { return "Rylai's" }
    if ($Name -match "^Zhonya") { return "Zhonya's" }
    if ($Name -match '^Plated Steelcaps') { return 'Steelcaps' }
    if ($Name -match "^Mercury's") { return 'Mercs' }
    if ($Name -match '^Sorcerer') { return 'Sorcs' }
    if ($Name -match '^Gustwalker') { return 'Gustwalker' }
    if ($Name -match '^Mosstomper') { return 'Mosstomper' }
    if ($Name -match '^Scorchclaw') { return 'Scorchclaw' }
    if ($Name -match '^Abyssal') { return 'Abyssal' }
    if ($Name -match '^Kaenic') { return 'Rookern' }
    if ($Name -match '^Spirit Visage') { return 'Visage' }
    if ($Name -match '^Force of Nature') { return 'FoN' }
    if ($Name -match '^Blackfire') { return 'Blackfire' }
    if ($Name -match '^Kraken') { return 'Kraken' }
    if ($Name -match '^Heartsteel') { return 'Heartsteel' }
    if ($Name -match '^Youmuu') { return "Youmuu's" }
    if ($Name -match '^Mortal Reminder') { return 'Mortal' }
    if ($Name -match '^Blade of the Ruined') { return 'BotRK' }
    return ($Name -split ' ')[0]
}

function Pos-Short {
    param([string]$P)
    switch ("$P") {
        'TOP' { return 'T' }
        'JUNGLE' { return 'J' }
        'MIDDLE' { return 'M' }
        'BOTTOM' { return 'B' }
        'UTILITY' { return 'S' }
        default { return '' }
    }
}

$g = $d.gameData
$ap = $d.activePlayer
$me = $null
foreach ($p in $d.allPlayers) {
    if ($ap.riotId -and $p.riotId -and ($p.riotId -ieq $ap.riotId)) { $me = $p; break }
    if ($ap.riotIdGameName -and $p.riotIdGameName -and ($p.riotIdGameName -ieq $ap.riotIdGameName) -and ($p.riotIdTagLine -ieq $ap.riotIdTagLine)) { $me = $p; break }
    if ($ap.summonerName -and $p.summonerName -and ($p.summonerName -ieq $ap.summonerName)) { $me = $p; break }
}

Write-Output ("GAME {0} {1}" -f (Fmt-Time $g.gameTime), $g.gameMode)

if ($me) {
    $items = @()
    foreach ($it in $me.items) { $s = Short-Item "$($it.displayName)"; if ($s) { $items += $s } }
    $items = ($items | Select-Object -Unique) -join ' '
    $abil = $ap.abilities
    $q = 0; $w = 0; $e2 = 0; $r = 0
    if ($abil) {
        if ($abil.PSObject.Properties.Name -contains 'Q') {
            $q = [int]$abil.Q.abilityLevel; $w = [int]$abil.W.abilityLevel; $e2 = [int]$abil.E.abilityLevel; $r = [int]$abil.R.abilityLevel
        } else {
            foreach ($x in $abil) {
                switch ("$($x.id)") {
                    'Q' { $q = [int]$x.abilityLevel }
                    'W' { $w = [int]$x.abilityLevel }
                    'E' { $e2 = [int]$x.abilityLevel }
                    'R' { $r = [int]$x.abilityLevel }
                }
            }
        }
    }
    $ks = ''
    $fr = $ap.fullRunes
    if ($fr) { $ks = "$($fr.keystone.displayName)" }
    $cs = $ap.championStats
    $myGold = 0
    if ($null -ne $ap.currentGold) { $myGold = [math]::Round([double]$ap.currentGold) }
    Write-Output ("ME {0} {1} L{2} {3}/{4}/{5} CS{6} {7}g | {8} | Q{9}W{10}E{11}R{12} | {13}" -f $me.championName, (Pos-Short "$($me.position)"), $me.level, (Get-PStat $me 'kills'), (Get-PStat $me 'deaths'), (Get-PStat $me 'assists'), (Get-PStat $me 'creepScore'), $myGold, $items, $q, $w, $e2, $r, $ks)
    if ($cs) {
        Write-Output ("STATS HP {0:0}/{1:0} AD {2:0} AP {3:0} Armor {4:0} MR {5:0} MS {6:0}" -f [double]$cs.currentHealth, [double]$cs.maxHealth, [double]$cs.attackDamage, [double]$cs.abilityPower, [double]$cs.armor, [double]$cs.magicResist, [double]$cs.moveSpeed)
    }
}

$teamTotals = @{}
foreach ($tg in ($d.allPlayers | Group-Object team)) {
    $tot = 0
    $lines = @()
    foreach ($p in $tg.Group) {
        $items = @()
        $val = 0
        foreach ($it in $p.items) {
            $id = [int]$it.itemID
            if ($itemGold.ContainsKey($id)) { $val += $itemGold[$id] }
            $s = Short-Item "$($it.displayName)"
            if ($s) { $items += $s }
        }
        $items = ($items | Select-Object -Unique) -join ' '
        $tot += $val
        $pre = 'O'
        if ($tg.Name -ne 'ORDER') { $pre = 'C' }
        $lines += ("{0} {1} {2} L{3} {4}/{5}/{6} {7} {8}g | {9}" -f $pre, $p.championName, (Pos-Short "$($p.position)"), $p.level, (Get-PStat $p 'kills'), (Get-PStat $p 'deaths'), (Get-PStat $p 'assists'), (Get-PStat $p 'creepScore'), $val, $items)
    }
    $teamTotals[$tg.Name] = $tot
    foreach ($l in $lines) { Write-Output $l }
}
if ($teamTotals.ContainsKey('ORDER') -and $teamTotals.ContainsKey('CHAOS')) {
    $diff = [int]$teamTotals['ORDER'] - [int]$teamTotals['CHAOS']
    $sign = ''
    if ($diff -ge 0) { $sign = '+' }
    Write-Output ("GOLD O {0} C {1} {2}{3}" -f $teamTotals['ORDER'], $teamTotals['CHAOS'], $sign, $diff)
}

$tNow = [double]$g.gameTime
$lastDragon = -1.0
$dragonCount = 0
$lastDragonType = ''
$lastBaron = -1.0
$events = @()
foreach ($e in $d.events.Events) {
    $et = [double]$e.EventTime
    $name = "$($e.EventName)"
    if ($name -eq 'DragonKill') {
        $dragonCount++
        if ($et -gt $lastDragon) { $lastDragon = $et; $lastDragonType = "$($e.DragonType)" }
        $events += ("{0} Drg {1} {2}" -f (Fmt-Time $et), $e.DragonType, $e.KillerName)
    } elseif ($name -eq 'BaronKill') {
        if ($et -gt $lastBaron) { $lastBaron = $et }
        $events += ("{0} Baron {1}" -f (Fmt-Time $et), $e.KillerName)
    } elseif ($name -eq 'HeraldKill') {
        $events += ("{0} Herald {1}" -f (Fmt-Time $et), $e.KillerName)
    } elseif ($name -eq 'HordeKill') {
        $events += ("{0} Grubs {1}" -f (Fmt-Time $et), $e.KillerName)
    } elseif ($name -eq 'ChampionKill') {
        $events += ("{0} K {1}>{2}" -f (Fmt-Time $et), $e.KillerName, $e.VictimName)
    } elseif ($name -eq 'TurretKilled') {
        $side = '?'
        $tk = "$($e.TurretKilled)"
        if ($tk -like '*TOrder*') { $side = 'O' }
        elseif ($tk -like '*TChaos*') { $side = 'C' }
        $events += ("{0} Turret {1} {2}" -f (Fmt-Time $et), $side, $e.KillerName)
    } elseif ($name -eq 'InhibKilled') {
        $side = '?'
        $ik = "$($e.InhibKilled)"
        if ($ik -like '*TOrder*') { $side = 'O' }
        elseif ($ik -like '*TChaos*') { $side = 'C' }
        $events += ("{0} Inhib {1} {2}" -f (Fmt-Time $et), $side, $e.KillerName)
    } elseif ($name -eq 'FirstBlood') {
        $events += ("{0} FirstBlood {1}" -f (Fmt-Time $et), $e.Recipient)
    }
}

if ($lastDragon -ge 0) {
    if ($lastDragonType -eq 'Elder') { $dSpawn = $lastDragon + 360; $dName = 'Elder' }
    elseif ($dragonCount -ge 4) { $dSpawn = $lastDragon + 360; $dName = 'Elder' }
    else { $dSpawn = $lastDragon + 300; $dName = 'Dragon' }
} else {
    $dSpawn = 300
    $dName = 'Dragon'
}
$dLeft = $dSpawn - $tNow
if ($dLeft -le 0) { $dVal = "UP(" + (Fmt-Time $dSpawn) + ")" } else { $dVal = ("in {0}({1})" -f (Fmt-Time $dLeft), (Fmt-Time $dSpawn)) }

if ($lastBaron -ge 0) { $bSpawn = $lastBaron + 360 } else { $bSpawn = 1200 }
$bLeft = $bSpawn - $tNow
if ($bLeft -le 0) { $bVal = "UP(" + (Fmt-Time $bSpawn) + ")" } else { $bVal = ("in {0}({1})" -f (Fmt-Time $bLeft), (Fmt-Time $bSpawn)) }

Write-Output ("TIMERS {0} {1} | dragons {2} | Baron {3}" -f $dName, $dVal, $dragonCount, $bVal)

$tail = $events | Select-Object -Last 8
if ($tail) { Write-Output ("EV " + (($tail) -join ' | ')) }
