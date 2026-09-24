. (Join-Path $PSScriptRoot 'common.ps1')

$lock = Get-LcuLockfile
if (-not $lock) { Write-Output 'League client is not running (lockfile not found).'; exit 1 }

$champs = Get-ChampMap
$me = $null
try { $me = Invoke-LcuApi -Lock $lock -ApiPath '/lol-summoner/v1/current-summoner' } catch { }
if (-not $me) { Write-Output 'Could not read summoner info from the client.'; exit 0 }

$name = $me.displayName
if ($me.gameName) { $name = "$($me.gameName)#$($me.tagLine)" }
Write-Output ("SUMMONER: {0} | level {1}" -f $name, $me.summonerLevel)

$myPuuid = $me.puuid
$mySummonerId = $me.summonerId
$myAccountId = $me.accountId

try {
    $r = Invoke-LcuApi -Lock $lock -ApiPath '/lol-ranked/v1/current-ranked-stats'
    foreach ($q in $r.queueMap.PSObject.Properties) {
        $v = $q.Value
        if ($v.tier) { Write-Output ("RANKED {0}: {1} {2} {3} LP | {4}W-{5}L" -f $q.Name, $v.tier, $v.division, $v.leaguePoints, $v.wins, $v.losses) }
    }
} catch { }

try {
    $m = Invoke-LcuApi -Lock $lock -ApiPath '/lol-champion-mastery/v1/local-player/champion-mastery'
    Write-Output 'CHAMPION POOL (top 15 by mastery points):'
    $m | Sort-Object championPoints -Descending | Select-Object -First 15 | ForEach-Object {
        Write-Output ('  {0} - mastery {1} ({2} pts)' -f (Get-ChampName $champs $_.championId), $_.championLevel, $_.championPoints)
    }
} catch { }

$games = $null
foreach ($ep in @("/lol-match-history/v1/products/lol/$myPuuid/matches?begIndex=0&endIndex=10", "/lol-match-history/v1/products/lol/current-summoner/matches?begIndex=0&endIndex=10")) {
    try {
        $res = Invoke-LcuApi -Lock $lock -ApiPath $ep
        if ($res.games -and $res.games.games) { $games = $res.games.games; break }
        if ($res.games -and ($res.games -is [array])) { $games = $res.games; break }
    } catch { }
}

if ($games) {
    Write-Output ("RECENT GAMES (last {0}):" -f $games.Count)
    foreach ($g in $games) {
        $dur = $g.gameDuration
        if ($dur -gt 100000) { $dur = [int]($dur / 1000) }
        $myPartId = $null
        if ($g.participantIdentities) {
            foreach ($ident in $g.participantIdentities) {
                if ($myPuuid -and $ident.player.puuid -and ($ident.player.puuid -eq $myPuuid)) { $myPartId = $ident.participantId; break }
                if ($mySummonerId -and $ident.player.summonerId -and ($ident.player.summonerId -eq $mySummonerId)) { $myPartId = $ident.participantId; break }
            }
            if (-not $myPartId -and $myAccountId) {
                foreach ($ident in $g.participantIdentities) {
                    if ($ident.player.accountId -eq $myAccountId) { $myPartId = $ident.participantId; break }
                }
            }
        }
        $part = $null
        if ($myPartId) { $part = $g.participants | Where-Object { $_.participantId -eq $myPartId } | Select-Object -First 1 }
        $champId = 0
        $stats = $null
        $role = ''
        if ($part) {
            $champId = $part.championId
            $stats = $part.stats
            if ($part.timeline) { $role = "$($part.timeline.lane)/$($part.timeline.role)" }
        }
        $win = '?'
        if ($stats) { if ($stats.win) { $win = 'W' } else { $win = 'L' } }
        $when = [datetimeoffset]::FromUnixTimeMilliseconds([long]$g.gameCreation).LocalDateTime.ToString('MM-dd HH:mm')
        Write-Output ("  {0} {1} {2} {3}/{4}/{5} CS{6} {7} {8}m" -f $when, (Get-ChampName $champs $champId), $win, $stats.kills, $stats.deaths, $stats.assists, $stats.totalMinionsKilled, (Get-QueueName $g.queueId), [int]($dur / 60))
    }
} else {
    Write-Output 'Match history unavailable.'
}
