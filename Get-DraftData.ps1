. (Join-Path $PSScriptRoot 'common.ps1')

$lock = Get-LcuLockfile
if (-not $lock) { Write-Output 'League client is not running (lockfile not found).'; exit 1 }

try {
    $session = Invoke-LcuApi -Lock $lock -ApiPath '/lol-champ-select/v1/session'
} catch {
    if ($_.Exception.Message -match ' 404 ') {
        Write-Output 'League is running, but you are not in champ select.'
        exit 0
    }
    Write-Output "LCU error: $($_.Exception.Message)"
    exit 1
}

$champs = Get-ChampMap
$meId = $null
try { $meId = (Invoke-LcuApi -Lock $lock -ApiPath '/lol-summoner/v1/current-summoner').summonerId } catch { }

function Show-Team {
    param($Title, $Members, $LocalCellId)
    Write-Output "--- $Title ---"
    foreach ($m in $Members) {
        $champ = Get-ChampName $champs $m.championId
        $hover = ''
        if ((-not $m.championId -or [int]$m.championId -eq 0) -and $m.championPickIntent) {
            $hover = ' | hover: ' + (Get-ChampName $champs $m.championPickIntent)
        }
        $pos = '-'
        if ($m.assignedPosition) { $pos = $m.assignedPosition }
        $isMe = $false
        if (($null -ne $m.cellId) -and ($null -ne $LocalCellId) -and ([int]$m.cellId -eq [int]$LocalCellId)) { $isMe = $true }
        elseif ($meId -and $m.summonerId -and ($m.summonerId -eq $meId)) { $isMe = $true }
        $tag = ''
        if ($isMe) { $tag = ' (ME)' }
        $sp = ''
        if ($m.spell1Id -or $m.spell2Id) { $sp = ' | ' + (Get-SpellName $m.spell1Id) + '/' + (Get-SpellName $m.spell2Id) }
        Write-Output ('  cell {0} [{1}]{2}: {3}{4}{5}' -f $m.cellId, $pos, $tag, $champ, $hover, $sp)
    }
}

$phase = ''
$left = '?'
if ($session.timer) {
    if ($session.timer.phase) { $phase = "$($session.timer.phase)" }
    if ($null -ne $session.timer.adjustedTimeLeftInPhase) {
        try {
            $left = [int][math]::Floor([double]$session.timer.adjustedTimeLeftInPhase / 1000.0)
            if ($left -lt 0) { $left = 0 }
        } catch { $left = '?' }
    }
}
Write-Output "PHASE: $phase | time left: ${left}s"

$mine = $null
foreach ($m in $session.myTeam) { if ($m.cellId -eq $session.localPlayerCellId) { $mine = $m } }
if ($mine) {
    Write-Output ('YOU: cell {0} | position {1} | champ {2}' -f $mine.cellId, $mine.assignedPosition, (Get-ChampName $champs $mine.championId))
}

Show-Team 'MY TEAM' $session.myTeam $session.localPlayerCellId
Show-Team 'ENEMY TEAM' $session.theirTeam $session.localPlayerCellId

if ($session.bans) {
    $mb = (($session.bans.myTeamBans | Where-Object { $_ -gt 0 } | ForEach-Object { Get-ChampName $champs $_ }) -join ', ')
    $tb = (($session.bans.theirTeamBans | Where-Object { $_ -gt 0 } | ForEach-Object { Get-ChampName $champs $_ }) -join ', ')
    Write-Output "BANS my team: $mb"
    Write-Output "BANS enemy:  $tb"
}

Write-Output '--- PENDING ACTIONS ---'
foreach ($group in $session.actions) {
    foreach ($a in $group) {
        if ((-not $a.completed) -or $a.isInProgress) {
            $who = "cell $($a.actorCellId)"
            if ($a.actorCellId -eq $session.localPlayerCellId) { $who = 'YOU' }
            $state = 'pending'
            if ($a.isInProgress) { $state = 'IN PROGRESS' }
            $kind = '?'
            if ($a.type) { $kind = $a.type }
            Write-Output ('  {0} [{1}] {2} -> {3}' -f $kind, $state, $who, (Get-ChampName $champs $a.championId))
        }
    }
}
