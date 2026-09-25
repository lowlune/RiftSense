param(
    [int]$TickSeconds = 60,
    [int]$IdleSeconds = 20,
    [int]$PollSeconds = 5,
    [int]$TickTimeoutSeconds = 180,
    [int]$DeathTimeoutSeconds = 90,
    [int]$MaxInferencesPerGame = 40,
    [int]$MinSecondsBetweenInferences = 15,
    [int]$MaxSourceAgeSeconds = 90,
    [int]$NoGameEndSeconds = 300,
    [int]$MaxCommandLineChars = 28000,
    [int]$MaxGameStartAttempts = 50,
    [string]$TimelineUrl = 'http://127.0.0.1:7777/api/events'
)

if ($PollSeconds -lt 1) { $PollSeconds = 5 }
if ($IdleSeconds -lt 1) { $IdleSeconds = 20 }
if ($TickSeconds -lt 1) { $TickSeconds = 60 }
if ($TickTimeoutSeconds -lt 5) { $TickTimeoutSeconds = 180 }
if ($DeathTimeoutSeconds -lt 5) { $DeathTimeoutSeconds = 90 }
if ($MaxInferencesPerGame -lt 1) { $MaxInferencesPerGame = 40 }
if ($MinSecondsBetweenInferences -lt 0) { $MinSecondsBetweenInferences = 15 }
if ($MaxSourceAgeSeconds -lt 10) { $MaxSourceAgeSeconds = 90 }
if ($NoGameEndSeconds -lt 30) { $NoGameEndSeconds = 300 }
if ($MaxCommandLineChars -lt 2000) { $MaxCommandLineChars = 28000 }
if ($MaxGameStartAttempts -lt 1) { $MaxGameStartAttempts = 50 }

$ErrorActionPreference = 'Continue'
$script:dir = $PSScriptRoot
$script:RunId = ([guid]::NewGuid().ToString('N')).Substring(0, 8)
$script:liveTmp = Join-Path $env:TEMP ("lol_autocoach_" + $script:RunId + "_live.json")
$script:scriptOut = Join-Path $env:TEMP ("lol_autocoach_" + $script:RunId + "_livetext.txt")
$script:tickOut = Join-Path $env:TEMP ("lol_autocoach_" + $script:RunId + "_tick.txt")
$script:tickErr = Join-Path $env:TEMP ("lol_autocoach_" + $script:RunId + "_tick.err")
$script:deathOut = Join-Path $env:TEMP ("lol_autocoach_" + $script:RunId + "_death.txt")
$script:deathErr = Join-Path $env:TEMP ("lol_autocoach_" + $script:RunId + "_death.err")
$script:coachFile = Join-Path $script:dir 'coach_latest.txt'
$script:coachMeta = Join-Path $script:dir 'coach_latest.json'
$script:deathFile = Join-Path $script:dir 'death_latest.txt'
$script:deathMeta = Join-Path $script:dir 'death_latest.json'
$script:apiBase = ($TimelineUrl -replace '/api/events/?$', '')
if ($script:apiBase -match '^(https?://[^/]+)') { $script:apiBase = $matches[1] }
if (-not $script:apiBase) { $script:apiBase = 'http://127.0.0.1:7777' }
$script:gameUrl = $script:apiBase + '/api/game'
$script:clock = [System.Diagnostics.Stopwatch]::StartNew()

$ownsMutex = $false
$script:InstanceMutex = $null
try {
    $script:InstanceMutex = New-Object -TypeName System.Threading.Mutex -ArgumentList @($false, 'Local\RiftSenseAutoCoach')
    try { $ownsMutex = $script:InstanceMutex.WaitOne(0, $false) }
    catch [System.Threading.AbandonedMutexException] { $ownsMutex = $true }
} catch {
    $script:InstanceMutex = $null
    $ownsMutex = $true
}
if (-not $ownsMutex) {
    Write-Host 'Another RiftSense AutoCoach instance is already running. Aborting.' -ForegroundColor Red
    exit 1
}

$ocExe = (Get-Command opencode -ErrorAction SilentlyContinue).Source

. (Join-Path $script:dir 'common.ps1')

$script:sessionActive = $false
$script:sessionId = ''
$script:sessionIdSource = 'generated'
$script:sessionChamp = ''
$script:sessionPlayer = ''
$script:sessionMode = ''
$script:sessionMap = ''
$script:lastGameTime = 0.0
$script:processedDeathKeys = @{}
$script:lastDeathT = -1.0
$script:seq = 0
$script:activeDeath = $null
$script:deathRetryAtSec = 0.0
$script:deathState = 'idle'
$script:tickProc = $null
$script:tickWatch = $null
$script:tickSnapshot = $null
$script:deathProc = $null
$script:deathWatch = $null
$script:tickFails = 0
$script:nextTickAtSec = 0.0
$script:coachSessionId = ''
$script:apiFails = 0
$script:apiDownSinceSec = -1.0
$script:disconnected = $false
$script:coverageOpen = $false
$script:coverageStartSec = 0.0
$script:lastGoodPollAtSec = $null
$script:gameStartAcked = $false
$script:gameStartAttempts = 0
$script:gameStartNextTrySec = 0.0
$script:nextSessionProbeSec = 0.0
$script:ts = ''
$script:myName = ''
$script:myGameName = ''
$script:myTagLine = ''
$script:myPosition = ''
$script:snapHistory = New-Object 'System.Collections.Generic.List[object]'
$script:eventHistory = New-Object 'System.Collections.Generic.List[object]'
$script:eventKeys = @{}
$script:SnapHistoryLimit = 12
$script:EventHistoryLimit = 24
$script:PreDeathWindowSeconds = 90
$script:PreDeathEventLimit = 6
$script:inferencesThisGame = 0
$script:lastInferenceAtSec = -100000.0
$script:tickRequestId = ''
$script:deathRequestId = ''
$script:lastItemSignature = ''
$script:lastObjectiveKey = ''
$script:forceTick = $false
$script:budgetExhausted = $false

function Get-ElapsedSeconds {
    return $script:clock.Elapsed.TotalSeconds
}

function Stop-ProcessTree {
    param($Process)
    if (-not $Process) { return }
    $procId = 0
    try { $procId = [int]$Process.Id } catch { return }
    if ($procId -le 0) { return }
    try {
        if (-not $Process.HasExited) {
            $null = & taskkill.exe /PID $procId /T /F 2>$null
        }
    } catch { }
    try {
        if (-not $Process.HasExited) { Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue }
    } catch { }
}

function Publish-Text {
    param([string]$Path, [string]$Text)
    $tmp = "$Path.tmp." + $script:RunId
    for ($i = 0; $i -lt 3; $i++) {
        try {
            Set-Content -LiteralPath $tmp -Value $Text -Encoding UTF8 -ErrorAction Stop
            Move-Item -LiteralPath $tmp -Destination $Path -Force -ErrorAction Stop
            return
        } catch {
            Start-Sleep -Milliseconds 100
        }
    }
    Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
    Write-Host "Failed to publish $Path" -ForegroundColor Red
}

function Read-FileText {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return '' }
    try {
        $t = Get-Content -LiteralPath $Path -Raw -Encoding UTF8
        if ($null -eq $t) { return '' }
        return "$t"
    } catch { return '' }
}

function Publish-Record {
    param(
        [string]$JsonPath,
        [string]$TextPath,
        [string]$Kind,
        [string]$Status,
        [string]$Text,
        [string]$ErrorText,
        $ObservedGameTime,
        $Seq,
        $Extra,
        $SourceAgeSec
    )
    $age = $SourceAgeSec
    if ($null -eq $age) {
        if ($null -ne $script:lastGoodPollAtSec) {
            $age = [math]::Round([math]::Max(0.0, (Get-ElapsedSeconds) - $script:lastGoodPollAtSec), 3)
        }
    }
    $obj = [ordered]@{
        schema = 'riftsense.v1'
        kind = $Kind
        session = $script:sessionId
        sessionId = $script:sessionId
        sessionSource = $script:sessionIdSource
        seq = $Seq
        observedGameTime = $ObservedGameTime
        completedAt = (Get-Date).ToUniversalTime().ToString('o')
        status = $Status
        error = $ErrorText
        sourceAgeSec = $age
        text = $Text
    }
    if ($Extra) { foreach ($k in $Extra.Keys) { $obj[$k] = $Extra[$k] } }
    Publish-Text -Path $JsonPath -Text ($obj | ConvertTo-Json -Compress -Depth 6)
    if ($TextPath) { Publish-Text -Path $TextPath -Text $Text }
}

function Post-TimelineBody {
    param([hashtable]$Body)
    try {
        $json = $Body | ConvertTo-Json -Depth 6 -Compress
        $res = Invoke-RestMethod -Uri $TimelineUrl -Method Post -ContentType 'application/json' -Body $json -TimeoutSec 3 -ErrorAction Stop
        if ($res -and ($res.PSObject.Properties.Name -contains 'ok') -and (-not $res.ok)) { return $false }
        return $true
    } catch {
        return $false
    }
}

function Send-GameStart {
    if (-not $script:sessionActive) { return $false }
    if ($script:gameStartAcked) { return $true }
    if ($script:gameStartAttempts -ge $MaxGameStartAttempts) { return $false }
    $nowSec = Get-ElapsedSeconds
    if ($nowSec -lt $script:gameStartNextTrySec) { return $false }
    $script:gameStartAttempts++
    $body = @{
        kind = 'game_start'
        label = ("{0} {1}" -f $script:sessionChamp, $script:sessionMode)
        sessionId = $script:sessionId
        champ = $script:sessionChamp
        mode = $script:sessionMode
        gameTime = $script:lastGameTime
        data = @{ champ = $script:sessionChamp; mode = $script:sessionMode; source = $script:sessionIdSource }
    }
    if ($script:sessionMap) { try { $body.map = [int]$script:sessionMap } catch { } }
    if (Post-TimelineBody -Body $body) {
        $script:gameStartAcked = $true
        $script:gameStartAttempts = 0
        $script:gameStartNextTrySec = 0.0
        return $true
    }
    $wait = [math]::Min(30.0, [math]::Pow(2.0, [math]::Min(6, $script:gameStartAttempts)))
    $script:gameStartNextTrySec = $nowSec + $wait
    return $false
}

function Send-TimelineEvent {
    param(
        [string]$Kind,
        [string]$Label = '',
        [hashtable]$Data = $null,
        [string]$RequestId = '',
        [string]$Status = '',
        [double]$GameTime = -1,
        [string]$Text = '',
        [string]$AdviceKind = '',
        $TokensIn = $null,
        $TokensOut = $null,
        $Cost = $null
    )
    if ($script:sessionActive -and (-not $script:gameStartAcked) -and ($Kind -ne 'game_start')) {
        $null = Send-GameStart
    }
    $gt = $script:lastGameTime
    if ($GameTime -ge 0) { $gt = $GameTime }
    $body = @{
        kind = $Kind
        label = $Label
        sessionId = $script:sessionId
        champ = $script:sessionChamp
        mode = $script:sessionMode
        gameTime = $gt
    }
    if ($script:sessionMap) { try { $body.map = [int]$script:sessionMap } catch { } }
    if ($Data) { $body.data = $Data }
    if ($RequestId) { $body.requestId = $RequestId }
    if ($Status) { $body.status = $Status }
    if ($Text) { $body.text = $Text }
    if ($AdviceKind) {
        $body.adviceKind = $AdviceKind
        if ($Data) { $body.meta = $Data }
    }
    if ($null -ne $TokensIn) { $body.tokensIn = $TokensIn }
    if ($null -ne $TokensOut) { $body.tokensOut = $TokensOut }
    if ($null -ne $Cost) { $body.cost = $Cost }
    return (Post-TimelineBody -Body $body)
}

function Send-Coverage {
    param([string]$Action, [string]$Reason = '')
    if ($Action -eq 'start') {
        if ($script:coverageOpen) { return }
        $script:coverageOpen = $true
        $script:coverageStartSec = Get-ElapsedSeconds
        $null = Send-TimelineEvent -Kind 'coverage' -Label ("coverage_start gameTime={0} reason={1}" -f ([int]$script:lastGameTime), $Reason) -Data @{ action = 'start'; reason = $Reason; gameTime = [double]$script:lastGameTime }
    } else {
        if (-not $script:coverageOpen) { return }
        $script:coverageOpen = $false
        $secs = [int][math]::Max(0.0, (Get-ElapsedSeconds) - $script:coverageStartSec)
        $null = Send-TimelineEvent -Kind 'coverage' -Label ("coverage_stop gameTime={0} seconds={1} reason={2}" -f ([int]$script:lastGameTime), $secs, $Reason) -Data @{ action = 'stop'; reason = $Reason; gameTime = [double]$script:lastGameTime; seconds = $secs }
    }
}

function Get-ServerSessionId {
    try {
        $res = Invoke-RestMethod -Uri $script:gameUrl -Method Get -TimeoutSec 3 -ErrorAction Stop
        if (-not $res) { return '' }
        if (($res.PSObject.Properties.Name -contains 'inGame') -and (-not $res.inGame)) { return '' }
        if ($res.PSObject.Properties.Name -contains 'sessionId') {
            $sid = "$($res.sessionId)"
            if ($sid) { return $sid }
        }
    } catch { }
    return ''
}

function Get-InferenceBudget {
    $remaining = [math]::Max(0, $MaxInferencesPerGame - $script:inferencesThisGame)
    return [pscustomobject]@{ Used = $script:inferencesThisGame; Limit = $MaxInferencesPerGame; Remaining = $remaining }
}

function Request-InferenceSlot {
    param([string]$Kind, [switch]$IgnoreCooldown)
    $budget = Get-InferenceBudget
    if ($budget.Remaining -le 0) {
        if (-not $script:budgetExhausted) {
            $script:budgetExhausted = $true
            Write-Host "[$script:ts] Inference budget exhausted ($MaxInferencesPerGame this game) - coaching paused, detection continues." -ForegroundColor Yellow
            $null = Send-TimelineEvent -Kind 'status' -Label ("budget exhausted ({0}/{1})" -f $budget.Used, $budget.Limit) -Data @{ used = $budget.Used; limit = $budget.Limit }
        }
        return $null
    }
    if (-not $IgnoreCooldown) {
        if (((Get-ElapsedSeconds) - $script:lastInferenceAtSec) -lt $MinSecondsBetweenInferences) { return $null }
    }
    $script:inferencesThisGame++
    $script:lastInferenceAtSec = Get-ElapsedSeconds
    $rid = [guid]::NewGuid().ToString('N')
    $null = Send-TimelineEvent -Kind 'inference' -RequestId $rid -Status 'started' -Label $Kind
    return $rid
}

function Complete-Inference {
    param([string]$RequestId, [string]$Status, [hashtable]$Data = $null, $TokensIn = $null, $TokensOut = $null, $Cost = $null)
    if (-not $RequestId) { return }
    $null = Send-TimelineEvent -Kind 'inference' -RequestId $RequestId -Status $Status -Data $Data -TokensIn $TokensIn -TokensOut $TokensOut -Cost $Cost
}

function Get-DragonObjectiveState {
    param($Data)
    $teamByName = @{}
    if ($Data.allPlayers) {
        foreach ($p in $Data.allPlayers) {
            foreach ($n in @((Get-PlayerLabel $p), "$($p.riotIdGameName)", "$($p.summonerName)")) {
                $n = "$n"
                if ($n) {
                    $key = $n.ToLowerInvariant()
                    if (-not $teamByName.ContainsKey($key)) { $teamByName[$key] = "$($p.team)" }
                }
            }
        }
    }
    $orderElems = 0
    $chaosElems = 0
    $unknownElems = 0
    $lastDragon = -1.0
    $lastDragonType = ''
    $lastBaron = -1.0
    if ($Data.events -and $Data.events.Events) {
        foreach ($e in $Data.events.Events) {
            $n = "$($e.EventName)"
            $t = [double]$e.EventTime
            if ($n -eq 'DragonKill') {
                $dt = "$($e.DragonType)"
                if ($dt -ne 'Elder') {
                    $k = "$($e.KillerName)".ToLowerInvariant()
                    $kt = ''
                    if ($teamByName.ContainsKey($k)) { $kt = $teamByName[$k] }
                    if ($kt -eq 'ORDER') { $orderElems++ }
                    elseif ($kt -eq 'CHAOS') { $chaosElems++ }
                    else { $unknownElems++ }
                }
                if ($t -gt $lastDragon) { $lastDragon = $t; $lastDragonType = $dt }
            } elseif ($n -eq 'BaronKill') {
                if ($t -gt $lastBaron) { $lastBaron = $t }
            }
        }
    }
    $soulTeam = ''
    if ($orderElems -ge 4) { $soulTeam = 'ORDER' }
    elseif ($chaosElems -ge 4) { $soulTeam = 'CHAOS' }
    $soulState = 'none'
    if ($soulTeam) {
        $soulState = $soulTeam
    } elseif ($unknownElems -gt 0) {
        if ((($orderElems + $unknownElems) -ge 4) -or (($chaosElems + $unknownElems) -ge 4)) { $soulState = 'unknown' }
    }
    $spawn = 300.0
    $kind = 'dragon'
    if ($lastDragon -ge 0) {
        if (($lastDragonType -eq 'Elder') -or $soulTeam) {
            $spawn = $lastDragon + 360.0
            $kind = 'elder'
        } else {
            $spawn = $lastDragon + 300.0
        }
    }
    $baronSpawn = 1200.0
    if ($lastBaron -ge 0) { $baronSpawn = $lastBaron + 360.0 }
    return [pscustomobject]@{
        DragonSpawn = $spawn
        DragonKind = $kind
        Soul = $soulState
        OrderElems = $orderElems
        ChaosElems = $chaosElems
        UnknownElems = $unknownElems
        BaronSpawn = $baronSpawn
    }
}

function Get-NextObjective {
    param($Data, [double]$GameTime)
    $ds = Get-DragonObjectiveState -Data $Data
    $best = $null
    $candidates = @(
        @{ Key = ("dragon:{0}:{1}" -f $ds.DragonKind, [int]$ds.DragonSpawn); Label = 'dragon up soon'; Spawn = $ds.DragonSpawn; Kind = $ds.DragonKind; Soul = $ds.Soul },
        @{ Key = ("baron:{0}" -f [int]$ds.BaronSpawn); Label = 'baron up soon'; Spawn = $ds.BaronSpawn; Kind = 'baron'; Soul = $ds.Soul }
    )
    foreach ($c in $candidates) {
        $left = [double]$c.Spawn - $GameTime
        if (($left -ge -30) -and ($left -le 30)) {
            if (($null -eq $best) -or ($left -lt $best.SecondsLeft)) {
                $best = [pscustomobject]@{ Key = $c.Key; Label = $c.Label; SecondsLeft = $left; Kind = $c.Kind; Soul = $c.Soul }
            }
        }
    }
    return $best
}

function Test-SamePlayer {
    param($A, $B)
    if (-not $A -or -not $B) { return $false }
    $aFull = "$($A.riotId)"; $bFull = "$($B.riotId)"
    if ($aFull -and $bFull) { return ($aFull -ieq $bFull) }
    if ($A.riotIdGameName -and $B.riotIdGameName) {
        if ($A.riotIdTagLine -and $B.riotIdTagLine) {
            return (($A.riotIdGameName -ieq $B.riotIdGameName) -and ($A.riotIdTagLine -ieq $B.riotIdTagLine))
        }
        return ($A.riotIdGameName -ieq $B.riotIdGameName)
    }
    $aS = "$($A.summonerName)"; $bS = "$($B.summonerName)"
    if ($aS -and $bS) { return ($aS -ieq $bS) }
    return $false
}

function Test-IsVictimMe {
    param([string]$Victim)
    if (-not $Victim) { return $false }
    if ($script:myName -and ($Victim -ieq $script:myName)) { return $true }
    if ($script:myGameName -and ($Victim -ieq $script:myGameName)) { return $true }
    if ($script:myGameName -and $script:myTagLine -and ($Victim -ieq ("{0}#{1}" -f $script:myGameName, $script:myTagLine))) { return $true }
    if ($script:myGameName -and ($Victim -like ($script:myGameName + '#*'))) { return $true }
    if ($script:myName -and ($script:myName -notlike '*#*') -and ($Victim -like ($script:myName + '#*'))) { return $true }
    return $false
}

function Get-EventKey {
    param($E)
    if ($null -ne $E.EventID -and "$($E.EventID)" -ne '') { return "id:$($E.EventID)" }
    return ("t:{0}|k:{1}|v:{2}" -f $E.EventTime, $E.KillerName, $E.VictimName)
}

function Format-GameClock {
    param([double]$Time)
    $total = [int][math]::Floor($Time)
    if ($total -lt 0) { $total = 0 }
    return ('{0}:{1:00}' -f [int][math]::Floor($total / 60), ($total % 60))
}

function Get-PlayerLabel {
    param($P)
    $full = "$($P.riotId)"
    if (-not $full) {
        if ($P.riotIdGameName -and $P.riotIdTagLine) { $full = "$($P.riotIdGameName)#$($P.riotIdTagLine)" }
        elseif ($P.riotIdGameName) { $full = "$($P.riotIdGameName)" }
        elseif ($P.summonerName) { $full = "$($P.summonerName)" }
    }
    return $full
}

function Test-KillerIsChampion {
    param($Data, [string]$Killer)
    if (-not $Killer) { return $false }
    if (-not $Data -or -not $Data.allPlayers) { return $false }
    foreach ($p in $Data.allPlayers) {
        foreach ($n in @((Get-PlayerLabel $p), "$($p.riotIdGameName)", "$($p.summonerName)")) {
            $n = "$n"
            if ($n -and ($n -ieq $Killer)) { return $true }
        }
    }
    return $false
}

function Get-EventText {
    param($Event)
    if (-not $Event) { return '' }
    $t = Format-GameClock ([double]$Event.EventTime)
    switch ("$($Event.EventName)") {
        'ChampionKill' { return ("{0} K {1}>{2}" -f $t, $Event.KillerName, $Event.VictimName) }
        'DragonKill' { return ("{0} Drg {1} {2}" -f $t, $Event.DragonType, $Event.KillerName) }
        'BaronKill' { return ("{0} Baron {1}" -f $t, $Event.KillerName) }
        'HeraldKill' { return ("{0} Herald {1}" -f $t, $Event.KillerName) }
        'HordeKill' { return ("{0} Grubs {1}" -f $t, $Event.KillerName) }
        'FirstBlood' { return ("{0} FirstBlood {1}" -f $t, $Event.Recipient) }
        'TurretKilled' {
            $side = '?'
            $tk = "$($Event.TurretKilled)"
            if ($tk -like '*TOrder*') { $side = 'O' } elseif ($tk -like '*TChaos*') { $side = 'C' }
            return ("{0} Turret {1} {2}" -f $t, $side, $Event.KillerName)
        }
        'InhibKilled' {
            $side = '?'
            $ik = "$($Event.InhibKilled)"
            if ($ik -like '*TOrder*') { $side = 'O' } elseif ($ik -like '*TChaos*') { $side = 'C' }
            return ("{0} Inhib {1} {2}" -f $t, $side, $Event.KillerName)
        }
        default { return '' }
    }
}

function Add-GameEvent {
    param($Event)
    if (-not $Event) { return }
    if (-not "$($Event.EventName)") { return }
    $key = Get-EventKey $Event
    if ($script:eventKeys.ContainsKey($key)) { return }
    $script:eventKeys[$key] = $true
    $text = Get-EventText $Event
    if (-not $text) { return }
    $script:eventHistory.Add([pscustomobject]@{ Key = $key; Time = [double]$Event.EventTime; Text = $text })
    while ($script:eventHistory.Count -gt $script:EventHistoryLimit) { $script:eventHistory.RemoveAt(0) }
}

function Add-GameEvents {
    param($Data)
    if (-not $Data.events -or -not $Data.events.Events) { return }
    foreach ($e in $Data.events.Events) { Add-GameEvent -Event $e }
}

function Get-ItemCounts {
    param($Ids)
    $counts = @{}
    foreach ($id in @($Ids)) {
        $k = "$id"
        if ($k -eq '' -or $k -eq '0') { continue }
        if ($counts.ContainsKey($k)) { $counts[$k] = [int]$counts[$k] + 1 } else { $counts[$k] = 1 }
    }
    return $counts
}

function Format-SnapshotLine {
    param($S)
    $level = '?'
    if ($null -ne $S.Level) { $level = "L$($S.Level)" }
    $k = '?'; if ($null -ne $S.K) { $k = "$($S.K)" }
    $d = '?'; if ($null -ne $S.D) { $d = "$($S.D)" }
    $a = '?'; if ($null -ne $S.A) { $a = "$($S.A)" }
    $cs = '?'; if ($null -ne $S.CS) { $cs = "CS$($S.CS)" }
    $gold = '?g'; if ($null -ne $S.Gold) { $gold = "$($S.Gold)g" }
    $items = '-'
    $counts = Get-ItemCounts $S.Items
    if ($counts.Count) {
        $items = (($counts.Keys | Sort-Object { [int]$_ } | ForEach-Object { "$($_)x$($counts[$_])" }) -join ',')
    }
    return ("t={0} {1} {2}/{3}/{4} {5} {6} items:{7}" -f (Format-GameClock ([double]$S.GameTime)), $level, $k, $d, $a, $cs, $gold, $items)
}

function Add-GameSnapshot {
    param($Data, [double]$GameTime, [int]$Seq)
    $ap = $Data.activePlayer
    $me = $null
    foreach ($p in $Data.allPlayers) { if (Test-SamePlayer $p $ap) { $me = $p; break } }
    $gold = $null
    if ($ap -and ($null -ne $ap.currentGold)) {
        try { $gold = [int][math]::Round([double]$ap.currentGold) } catch { $gold = $null }
    }
    $level = $null
    if ($me -and ($null -ne $me.level)) { try { $level = [int]$me.level } catch { $level = $null } }
    if (($null -eq $level) -and $ap -and ($null -ne $ap.level)) { try { $level = [int]$ap.level } catch { } }
    $k = Get-PStat $me 'kills'
    $d = Get-PStat $me 'deaths'
    $a = Get-PStat $me 'assists'
    $cs = Get-PStat $me 'creepScore'
    $itemIds = @()
    if ($me -and $me.items) {
        foreach ($it in $me.items) {
            $id = 0
            if ($null -ne $it.itemID) { try { $id = [int]$it.itemID } catch { $id = 0 } }
            if ($id -gt 0) { $itemIds += $id }
        }
    }
    $snap = [pscustomobject]@{
        GameTime = [double]$GameTime
        Seq = [int]$Seq
        Gold = $gold
        Level = $level
        K = $k
        D = $d
        A = $a
        CS = $cs
        Items = @($itemIds)
    }
    $script:snapHistory.Add($snap)
    while ($script:snapHistory.Count -gt $script:SnapHistoryLimit) { $script:snapHistory.RemoveAt(0) }
}

function Get-PreDeathWindow {
    param([double]$Time)
    $start = $Time - $script:PreDeathWindowSeconds
    $snaps = @($script:snapHistory | Where-Object { ([double]$_.GameTime -ge $start) -and ([double]$_.GameTime -le ($Time + 0.001)) })
    if (-not $snaps.Count) { $snaps = @($script:snapHistory | Select-Object -Last 1) }
    if (-not $snaps.Count) { return '' }
    $lines = @()
    foreach ($s in $snaps) { $lines += (Format-SnapshotLine $s) }
    if ($snaps.Count -ge 2) {
        $first = $snaps[0]
        $last = $snaps[$snaps.Count - 1]
        $delta = @()
        if (($null -ne $first.Level) -and ($null -ne $last.Level)) { $delta += ("level {0:+0;-0;0}" -f ([int]$last.Level - [int]$first.Level)) }
        if (($null -ne $first.Gold) -and ($null -ne $last.Gold)) { $delta += ("gold {0:+0;-0;0}" -f ([int]$last.Gold - [int]$first.Gold)) }
        if (($null -ne $first.CS) -and ($null -ne $last.CS)) { $delta += ("CS {0:+0;-0;0}" -f ([int]$last.CS - [int]$first.CS)) }
        $fk = 0; if ($null -ne $first.K) { $fk = [int]$first.K }
        $fd = 0; if ($null -ne $first.D) { $fd = [int]$first.D }
        $fa = 0; if ($null -ne $first.A) { $fa = [int]$first.A }
        $lk = 0; if ($null -ne $last.K) { $lk = [int]$last.K }
        $ld = 0; if ($null -ne $last.D) { $ld = [int]$last.D }
        $la = 0; if ($null -ne $last.A) { $la = [int]$last.A }
        $delta += ("KDA {0:+0;-0;0}/{1:+0;-0;0}/{2:+0;-0;0}" -f ($lk - $fk), ($ld - $fd), ($la - $fa))
        $firstCounts = Get-ItemCounts $first.Items
        $lastCounts = Get-ItemCounts $last.Items
        $itemKeys = @{}
        foreach ($key in $firstCounts.Keys) { $itemKeys[$key] = $true }
        foreach ($key in $lastCounts.Keys) { $itemKeys[$key] = $true }
        $itemDelta = @()
        foreach ($key in ($itemKeys.Keys | Sort-Object { [int]$_ })) {
            $was = 0; if ($firstCounts.ContainsKey($key)) { $was = [int]$firstCounts[$key] }
            $now = 0; if ($lastCounts.ContainsKey($key)) { $now = [int]$lastCounts[$key] }
            $diff = $now - $was
            if ($diff -gt 0) { $itemDelta += ("$key+$diff") } elseif ($diff -lt 0) { $itemDelta += ("$key$diff") }
        }
        if ($itemDelta.Count) { $delta += ("items " + ($itemDelta -join ',')) }
        $lines += ("DELTA(" + (Format-GameClock ([double]$first.GameTime)) + "->" + (Format-GameClock ([double]$last.GameTime)) + "): " + ($delta -join ', '))
    }
    $evLines = @()
    foreach ($ev in ($script:eventHistory | Where-Object { [double]$_.Time -le ($Time + 0.001) } | Select-Object -Last $script:PreDeathEventLimit)) {
        $evLines += $ev.Text
    }
    if ($evLines.Count) { $lines += ("EVENTS: " + ($evLines -join ' | ')) }
    return (($lines -join "`r`n"))
}

function ConvertFrom-DeathReport {
    param([string]$Text)
    $facts = New-Object 'System.Collections.Generic.List[string]'
    $hypotheses = New-Object 'System.Collections.Generic.List[string]'
    $now = ''
    $next = ''
    $doNow = ''
    $died = ''
    $bucket = ''
    foreach ($rawLine in ($Text -split "`r?`n")) {
        $l = "$rawLine".Trim()
        if (-not $l) { continue }
        if ($l -match '^OBSERVED\s*:\s*(.*)$') {
            $v = "$($matches[1])".Trim()
            if ($v) { $facts.Add($v) }
            $bucket = 'facts'
        } elseif ($l -match '^HYPOTHES(?:IS|ES)\s*:\s*(.*)$') {
            $v = "$($matches[1])".Trim()
            if ($v) { $hypotheses.Add($v) }
            $bucket = 'hypotheses'
        } elseif ($l -match '^WHY\s*:\s*(.*)$') {
            $v = "$($matches[1])".Trim()
            if ($v) { $hypotheses.Add($v) }
            $bucket = 'hypotheses'
        } elseif ($l -match '^NOW\s*:\s*(.*)$') {
            $now = "$($matches[1])".Trim()
            $bucket = 'now'
        } elseif ($l -match '^NEXT\s*:\s*(.*)$') {
            $next = "$($matches[1])".Trim()
            $bucket = 'next'
        } elseif ($l -match '^DO NOW\s*:\s*(.*)$') {
            $doNow = "$($matches[1])".Trim()
            $bucket = 'doNow'
        } elseif ($l -match '^DIED\s*:\s*(.*)$') {
            $died = "$($matches[1])".Trim()
            $bucket = ''
        } else {
            switch ($bucket) {
                'facts' { if ($facts.Count) { $facts[$facts.Count - 1] = $facts[$facts.Count - 1] + ' ' + $l } else { $facts.Add($l) } }
                'hypotheses' { if ($hypotheses.Count) { $hypotheses[$hypotheses.Count - 1] = $hypotheses[$hypotheses.Count - 1] + ' ' + $l } else { $hypotheses.Add($l) } }
                'now' { if ($now) { $now = $now + ' ' + $l } else { $now = $l } }
                'next' { if ($next) { $next = $next + ' ' + $l } else { $next = $l } }
                'doNow' { if ($doNow) { $doNow = $doNow + ' ' + $l } else { $doNow = $l } }
            }
        }
    }
    return [pscustomobject]@{
        Facts = @($facts)
        Hypotheses = @($hypotheses)
        Now = $now
        Next = $next
        DoNow = $doNow
        Died = $died
    }
}

function Stop-Workers {
    param([string]$DeadReason = 'cancelled')
    if ($script:tickProc) {
        Complete-Inference -RequestId $script:tickRequestId -Status $DeadReason -Data @{ reason = $DeadReason }
        $script:tickRequestId = ''
        Stop-ProcessTree $script:tickProc
        $script:tickProc = $null
        $script:tickWatch = $null
        $script:tickSnapshot = $null
    }
    if ($script:deathProc) {
        Complete-Inference -RequestId $script:deathRequestId -Status $DeadReason -Data @{ reason = $DeadReason }
        $script:deathRequestId = ''
        Stop-ProcessTree $script:deathProc
        $script:deathProc = $null
        $script:deathWatch = $null
    }
}

function Start-Session {
    param($Data, [string]$Champ, [string]$Reason = 'new_game')
    if ($script:sessionActive) { Close-Session -Reason $Reason }
    Stop-Workers -DeadReason 'superseded'
    $g = $Data.gameData
    $script:sessionActive = $true
    $script:sessionChamp = $Champ
    $script:sessionPlayer = $script:myName
    $script:sessionMode = "$($g.gameMode)"
    $script:sessionMap = "$($g.mapNumber)"
    $script:lastGameTime = [double]$g.gameTime
    $sid = Get-ServerSessionId
    if ($sid) {
        $script:sessionId = $sid
        $script:sessionIdSource = 'server'
    } else {
        $script:sessionId = ('ps|{0}|{1}|{2}' -f $g.gameMode, $g.mapNumber, [guid]::NewGuid().ToString('N'))
        $script:sessionIdSource = 'generated'
    }
    $script:gameStartAcked = $false
    $script:gameStartAttempts = 0
    $script:gameStartNextTrySec = 0.0
    $script:nextSessionProbeSec = (Get-ElapsedSeconds) + 10
    $script:processedDeathKeys = @{}
    $script:lastDeathT = -1.0
    $script:activeDeath = $null
    $script:deathRetryAtSec = 0.0
    $script:deathState = 'idle'
    $script:tickFails = 0
    $script:nextTickAtSec = 0.0
    $script:coachSessionId = ''
    $script:seq = 0
    $script:snapHistory = New-Object 'System.Collections.Generic.List[object]'
    $script:eventHistory = New-Object 'System.Collections.Generic.List[object]'
    $script:eventKeys = @{}
    $script:inferencesThisGame = 0
    $script:lastInferenceAtSec = -100000.0
    $script:tickRequestId = ''
    $script:deathRequestId = ''
    $script:lastItemSignature = ''
    $script:lastObjectiveKey = ''
    $script:forceTick = $false
    $script:budgetExhausted = $false
    $script:lastGoodPollAtSec = Get-ElapsedSeconds
    if ($Data.events -and $Data.events.Events) {
        foreach ($e in $Data.events.Events) {
            Add-GameEvent -Event $e
            if ("$($e.EventName)" -ne 'ChampionKill') { continue }
            $script:processedDeathKeys[(Get-EventKey $e)] = $true
            if (Test-IsVictimMe "$($e.VictimName)") {
                $et = [double]$e.EventTime
                if ($et -gt $script:lastDeathT) { $script:lastDeathT = $et }
            }
        }
    }
    Publish-Record -JsonPath $script:coachMeta -TextPath $script:coachFile -Kind 'coach' -Status 'starting' -Text '' -ErrorText '' -ObservedGameTime $g.gameTime -Seq 0
    Publish-Record -JsonPath $script:deathMeta -TextPath $script:deathFile -Kind 'death' -Status 'idle' -Text '' -ErrorText '' -ObservedGameTime $g.gameTime -Seq 0
    Send-Coverage -Action 'start' -Reason $Reason
    $null = Send-GameStart
}

function Close-Session {
    param([string]$Reason = 'collector_stopped')
    $wasActive = $script:sessionActive
    Stop-Workers -DeadReason 'cancelled'
    if ($wasActive) {
        $null = Send-TimelineEvent -Kind 'game_end' -Label ("game end ({0})" -f $Reason) -Data @{ reason = $Reason; lastGameTime = [double]$script:lastGameTime } -GameTime $script:lastGameTime
        Send-Coverage -Action 'stop' -Reason $Reason
    }
    $script:activeDeath = $null
    $script:deathState = 'idle'
    $script:sessionActive = $false
    $script:disconnected = $false
    $script:coverageOpen = $false
    Publish-Record -JsonPath $script:coachMeta -TextPath $script:coachFile -Kind 'coach' -Status 'no_game' -Text 'OUT OF GAME' -ErrorText '' -ObservedGameTime $null -Seq $script:seq
    Publish-Record -JsonPath $script:deathMeta -TextPath $script:deathFile -Kind 'death' -Status 'no_game' -Text '' -ErrorText '' -ObservedGameTime $null -Seq $script:seq
}

function New-DeathRecord {
    param([string]$Key, [double]$Time, [string]$Killer, [bool]$KilledByChampion)
    $clock = Format-GameClock $Time
    $script:activeDeath = [pscustomobject]@{
        Key = $Key
        Time = $Time
        Killer = $Killer
        Clock = $clock
        KilledByChampion = $KilledByChampion
        Window = (Get-PreDeathWindow -Time $Time)
        Attempts = 0
        State = 'detected'
        Session = $script:sessionId
        Seq = $script:seq
        GameTime = $script:lastGameTime
        DetectedAtSec = Get-ElapsedSeconds
        DetectedAtUtc = (Get-Date).ToUniversalTime().ToString('o')
    }
    $script:deathState = 'detected'
    $script:deathRetryAtSec = 0.0
    $null = Send-TimelineEvent -Kind 'death' -Label ("died {0} to {1}" -f $clock, $Killer) -Data @{ clock = $clock; killer = $Killer; killedByChampion = [bool]$KilledByChampion; sessionSource = $script:sessionIdSource } -GameTime $Time
}

function Handle-DeathFailure {
    param([string]$Reason, [switch]$Permanent)
    Complete-Inference -RequestId $script:deathRequestId -Status 'failed' -Data @{ reason = $Reason }
    $script:deathRequestId = ''
    $script:deathProc = $null
    $script:deathWatch = $null
    $script:deathState = 'failed'
    $ad = $script:activeDeath
    if (-not $ad) { return }
    $ad.State = 'failed'
    $failText = ("DIED: {0} to {1} - report unavailable ({2})" -f $ad.Clock, $ad.Killer, $Reason)
    Publish-Record -JsonPath $script:deathMeta -TextPath $script:deathFile -Kind 'death' -Status 'failed' -Text $failText -ErrorText $Reason -ObservedGameTime $ad.GameTime -Seq $ad.Seq -SourceAgeSec ((Get-ElapsedSeconds) - [double]$ad.DetectedAtSec) -Extra @{ eventId = $ad.Key; clock = $ad.Clock; killer = $ad.Killer; eventTime = $ad.Time; killedByChampion = [bool]$ad.KilledByChampion; role = $script:myPosition }
    Write-Host "[$script:ts] Death report failed ($Reason)." -ForegroundColor Red
    if ($Permanent -or ([int]$ad.Attempts -ge 3)) {
        $script:processedDeathKeys[$ad.Key] = $true
        $script:lastDeathT = [math]::Max([double]$script:lastDeathT, [double]$ad.Time)
        $script:activeDeath = $null
    } else {
        $script:deathRetryAtSec = (Get-ElapsedSeconds) + 30
    }
}

function Start-DeathWorker {
    $ad = $script:activeDeath
    if (-not $ad) { return }
    $age = (Get-ElapsedSeconds) - [double]$ad.DetectedAtSec
    if ($age -gt $MaxSourceAgeSeconds) {
        Handle-DeathFailure -Reason ("source snapshot too old ({0:0}s)" -f $age) -Permanent
        return
    }
    $windowBlock = "PRE-DEATH WINDOW (observed snapshots and events from your own game data; use these for OBSERVED lines):`r`n" + "$($ad.Window)"
    if (-not ("$($ad.Window)").Trim()) { $windowBlock = 'PRE-DEATH WINDOW: unavailable.' }
    $deathPrompt = "DEATH REPORT. You just died at $($ad.Clock) to $($ad.Killer). The live game data, pre-death window, and your build intent are included below - answer directly from them.`r`n$windowBlock`r`nOutput up to 4 hidden reasoning lines first (>>), then the ===DEATH=== block exactly per your Death reports section: DIED, OBSERVED: lines (facts only, from the supplied data), HYPOTHESIS: lines (possible explanations), NOW, NEXT, DO NOW. Say insufficient evidence if the data cannot support a cause. Max 12 lines total."
    $prep = New-CoachPrompt -BasePrompt $deathPrompt -Champ $script:sessionChamp -Role $script:myPosition
    if (-not $prep.Ok) {
        if ("$($prep.Error)" -like 'prompt too large*') {
            Handle-DeathFailure -Reason $prep.Error -Permanent
        } else {
            Handle-DeathFailure -Reason ("live snapshot unusable: {0}" -f $prep.Error)
        }
        return
    }
    $rid = Request-InferenceSlot -Kind 'death' -IgnoreCooldown
    if (-not $rid) {
        Write-Host "[$script:ts] Inference budget exhausted - death report for $($ad.Clock) unavailable." -ForegroundColor Yellow
        $script:deathState = 'failed'
        $ad.State = 'failed'
        $failText = ("DIED: {0} to {1} - report unavailable (inference budget exhausted)" -f $ad.Clock, $ad.Killer)
        Publish-Record -JsonPath $script:deathMeta -TextPath $script:deathFile -Kind 'death' -Status 'failed' -Text $failText -ErrorText 'budget exhausted' -ObservedGameTime $ad.GameTime -Seq $ad.Seq -SourceAgeSec $age -Extra @{ eventId = $ad.Key; clock = $ad.Clock; killer = $ad.Killer; eventTime = $ad.Time; killedByChampion = [bool]$ad.KilledByChampion; role = $script:myPosition }
        $script:processedDeathKeys[$ad.Key] = $true
        $script:lastDeathT = [math]::Max([double]$script:lastDeathT, [double]$ad.Time)
        $script:activeDeath = $null
        return
    }
    $script:deathRequestId = $rid
    $ad.Attempts = [int]$ad.Attempts + 1
    $ad.State = 'queued'
    $script:deathState = 'queued'
    $script:deathRetryAtSec = 0.0
    $pending = ("PENDING|" + $ad.Clock + "|" + $ad.Killer)
    $pendingUntil = (Get-Date).ToUniversalTime().AddSeconds($DeathTimeoutSeconds).ToString('o')
    Publish-Record -JsonPath $script:deathMeta -TextPath $script:deathFile -Kind 'death' -Status 'pending' -Text $pending -ErrorText '' -ObservedGameTime $ad.GameTime -Seq $ad.Seq -SourceAgeSec $age -Extra @{ requestId = $rid; eventId = $ad.Key; clock = $ad.Clock; killer = $ad.Killer; eventTime = $ad.Time; killedByChampion = [bool]$ad.KilledByChampion; pendingUntil = $pendingUntil; role = $script:myPosition }
    Write-Host "[$script:ts] Death detected at $($ad.Clock) (to $($ad.Killer)) - generating report... (attempt $($ad.Attempts))" -ForegroundColor Red
    $p = $null
    try { $p = Start-CoachRun -ArgLine $prep.ArgLine -OutFile $script:deathOut -ErrFile $script:deathErr } catch { $p = $null }
    if (-not $p) {
        $script:inferencesThisGame = [math]::Max(0, $script:inferencesThisGame - 1)
        Handle-DeathFailure -Reason 'could not start inference worker'
        return
    }
    $script:deathProc = $p
    $script:deathWatch = [System.Diagnostics.Stopwatch]::StartNew()
    $ad.State = 'running'
    $script:deathState = 'running'
}

function Handle-TickFailure {
    param([string]$Reason)
    Complete-Inference -RequestId $script:tickRequestId -Status 'failed' -Data @{ reason = $Reason }
    $script:tickRequestId = ''
    $lastGood = Read-FileText $script:coachFile
    if (-not $lastGood.Trim()) {
        $lastGood = ("(coach tick failed: {0})" -f $Reason)
    }
    Publish-Record -JsonPath $script:coachMeta -TextPath $script:coachFile -Kind 'coach' -Status 'failed' -Text $lastGood -ErrorText $Reason -ObservedGameTime $script:lastGameTime -Seq $script:seq -Extra @{ role = $script:myPosition }
    Write-Host "[$script:ts] Coach run failed ($Reason)." -ForegroundColor Red
    $script:tickFails++
    if ($script:tickFails -le 2) {
        Write-Host "[$script:ts] Retrying immediately ($($script:tickFails)/2)." -ForegroundColor Red
        $script:nextTickAtSec = 0.0
    } else {
        Write-Host "[$script:ts] Coach run failed repeatedly - waiting 60s." -ForegroundColor Red
        $script:nextTickAtSec = (Get-ElapsedSeconds) + 60
    }
    $script:tickProc = $null
    $script:tickWatch = $null
    $script:tickSnapshot = $null
}

function Start-TickWorker {
    $tickPrompt = 'Live tick. The live game data and your build intent are included below - answer directly from them. Output up to 4 hidden reasoning lines first (>>), then the ===COACH=== readout exactly per your format.'
    $prep = New-CoachPrompt -BasePrompt $tickPrompt -Champ $script:sessionChamp -Role $script:myPosition
    if (-not $prep.Ok) {
        Handle-TickFailure -Reason ("live snapshot unusable: {0}" -f $prep.Error)
        if ("$($prep.Error)" -like 'prompt too large*') {
            $script:nextTickAtSec = (Get-ElapsedSeconds) + 300
        }
        return
    }
    $rid = Request-InferenceSlot -Kind 'tick'
    if (-not $rid) {
        if ((Get-InferenceBudget).Remaining -le 0) {
            $script:nextTickAtSec = (Get-ElapsedSeconds) + 3600
        } else {
            $script:nextTickAtSec = (Get-ElapsedSeconds) + [math]::Max(5, $MinSecondsBetweenInferences)
        }
        return
    }
    $script:tickRequestId = $rid
    $script:tickSnapshot = [pscustomobject]@{ Session = $script:sessionId; Seq = $script:seq; GameTime = $script:lastGameTime; CapturedAtSec = Get-ElapsedSeconds }
    Write-Host "[$script:ts] Tick started..." -ForegroundColor Cyan
    $p = $null
    try { $p = Start-CoachRun -ArgLine $prep.ArgLine -OutFile $script:tickOut -ErrFile $script:tickErr } catch { $p = $null }
    if (-not $p) {
        $script:inferencesThisGame = [math]::Max(0, $script:inferencesThisGame - 1)
        Handle-TickFailure -Reason 'could not start inference worker'
        return
    }
    $script:tickProc = $p
    $script:tickWatch = [System.Diagnostics.Stopwatch]::StartNew()
}

function Update-Workers {
    if ($script:tickProc) {
        $exited = $false
        try { $exited = $script:tickProc.HasExited } catch { $exited = $true }
        if (-not $exited) {
            if ($script:tickWatch -and ($script:tickWatch.Elapsed.TotalSeconds -gt $TickTimeoutSeconds)) {
                Stop-ProcessTree $script:tickProc
                $script:tickProc = $null
                $script:tickWatch = $null
                $script:tickSnapshot = $null
                Complete-Inference -RequestId $script:tickRequestId -Status 'timeout'
                $script:tickRequestId = ''
                Handle-TickFailure -Reason ("timed out after {0}s" -f $TickTimeoutSeconds)
            }
        } else {
            $code = -1
            try { $code = [int]$script:tickProc.ExitCode } catch { $code = -1 }
            $out = Read-FileText $script:tickOut
            $body = ''
            $valid = $false
            if ($code -eq 0) {
                if ($out -match '(?s)===COACH===\s*(.*)$') {
                    $body = "$($matches[1])".Trim()
                    if ($body) { $valid = $true }
                }
            }
            $snap = $script:tickSnapshot
            $sameSession = ($snap -and ("$($snap.Session)" -eq $script:sessionId))
            $snapAge = $null
            if ($snap -and ($null -ne $snap.CapturedAtSec)) { $snapAge = (Get-ElapsedSeconds) - [double]$snap.CapturedAtSec }
            if ($valid -and $sameSession -and ($null -ne $snapAge) -and ($snapAge -le $MaxSourceAgeSeconds)) {
                $rid = $script:tickRequestId
                Complete-Inference -RequestId $rid -Status 'ok'
                $script:tickRequestId = ''
                $null = Send-TimelineEvent -Kind 'advice' -AdviceKind 'coach' -Text $body -RequestId $rid -GameTime ([double]$snap.GameTime) -Data @{ seq = [int]$snap.Seq; sourceAgeSec = [math]::Round($snapAge, 3); role = $script:myPosition }
                Publish-Record -JsonPath $script:coachMeta -TextPath $script:coachFile -Kind 'coach' -Status 'ok' -Text $body -ErrorText '' -ObservedGameTime $snap.GameTime -Seq $snap.Seq -SourceAgeSec $snapAge -Extra @{ requestId = $rid; role = $script:myPosition }
                $script:coachSessionId = $script:sessionId
                Write-Host $body
                Write-Host ''
                $script:tickFails = 0
                $script:nextTickAtSec = (Get-ElapsedSeconds) + $TickSeconds
                $script:tickProc = $null
                $script:tickWatch = $null
                $script:tickSnapshot = $null
            } else {
                $reason = 'invalid or empty output'
                if ($code -ne 0) { $reason = "exit code $code" }
                elseif (-not $body) { $reason = 'missing or empty ===COACH=== block' }
                elseif (-not $sameSession) { $reason = 'stale result from a previous session' }
                elseif (($null -ne $snapAge) -and ($snapAge -gt $MaxSourceAgeSeconds)) { $reason = ("source snapshot too old ({0:0}s)" -f $snapAge) }
                Complete-Inference -RequestId $script:tickRequestId -Status 'failed' -Data @{ reason = $reason }
                $script:tickRequestId = ''
                Handle-TickFailure -Reason $reason
            }
        }
    }

    if ($script:deathProc) {
        $exited = $false
        try { $exited = $script:deathProc.HasExited } catch { $exited = $true }
        if (-not $exited) {
            if ($script:deathWatch -and ($script:deathWatch.Elapsed.TotalSeconds -gt $DeathTimeoutSeconds)) {
                Stop-ProcessTree $script:deathProc
                $script:deathProc = $null
                $script:deathWatch = $null
                Complete-Inference -RequestId $script:deathRequestId -Status 'timeout'
                $script:deathRequestId = ''
                Handle-DeathFailure -Reason ("timed out after {0}s" -f $DeathTimeoutSeconds)
            }
        } else {
            $code = -1
            try { $code = [int]$script:deathProc.ExitCode } catch { $code = -1 }
            $out = Read-FileText $script:deathOut
            $body = ''
            $valid = $false
            if ($code -eq 0) {
                if ($out -match '(?s)===DEATH===\s*(.*)$') {
                    $body = "$($matches[1])".Trim()
                    if ($body) { $valid = $true }
                }
            }
            $ad = $script:activeDeath
            $sameSession = ($ad -and ("$($ad.Session)" -eq $script:sessionId))
            $deathAge = $null
            if ($ad) { $deathAge = (Get-ElapsedSeconds) - [double]$ad.DetectedAtSec }
            $script:deathProc = $null
            $script:deathWatch = $null
            if ($valid -and $sameSession -and ($null -ne $deathAge) -and ($deathAge -le $MaxSourceAgeSeconds)) {
                $rid = $script:deathRequestId
                Complete-Inference -RequestId $rid -Status 'ok'
                $script:deathRequestId = ''
                $parsed = ConvertFrom-DeathReport -Text $body
                $null = Send-TimelineEvent -Kind 'advice' -AdviceKind 'death' -Text $body -RequestId $rid -GameTime ([double]$ad.GameTime) -Data @{ clock = $ad.Clock; eventId = $ad.Key; sourceAgeSec = [math]::Round($deathAge, 3) }
                $observedAt = (Get-Date).ToUniversalTime().ToString('o')
                Publish-Record -JsonPath $script:deathMeta -TextPath $script:deathFile -Kind 'death' -Status 'ok' -Text $body -ErrorText '' -ObservedGameTime $ad.GameTime -Seq $ad.Seq -SourceAgeSec $deathAge -Extra @{
                    requestId = $rid
                    eventId = $ad.Key
                    clock = $ad.Clock
                    gameClock = $ad.Clock
                    killer = $ad.Killer
                    killedByChampion = [bool]$ad.KilledByChampion
                    eventTime = $ad.Time
                    detectedAt = $ad.DetectedAtUtc
                    observedAt = $observedAt
                    died = $parsed.Died
                    facts = @($parsed.Facts)
                    hypotheses = @($parsed.Hypotheses)
                    now = $parsed.Now
                    next = $parsed.Next
                    doNow = $parsed.DoNow
                    role = $script:myPosition
                    raw = $out
                }
                $script:processedDeathKeys[$ad.Key] = $true
                $script:lastDeathT = [math]::Max([double]$script:lastDeathT, [double]$ad.Time)
                $script:deathState = 'completed'
                $script:activeDeath = $null
                Write-Host $body -ForegroundColor Red
                Write-Host ''
            } else {
                $reason = 'invalid or empty output'
                if ($code -ne 0) { $reason = "exit code $code" }
                elseif (-not $body) { $reason = 'missing or empty ===DEATH=== block' }
                elseif (-not $sameSession) { $reason = 'stale result from a previous session' }
                elseif (($null -ne $deathAge) -and ($deathAge -gt $MaxSourceAgeSeconds)) { $reason = ("source snapshot too old ({0:0}s)" -f $deathAge) }
                Complete-Inference -RequestId $script:deathRequestId -Status 'failed' -Data @{ reason = $reason }
                $script:deathRequestId = ''
                if (($null -ne $deathAge) -and ($deathAge -gt $MaxSourceAgeSeconds)) {
                    Handle-DeathFailure -Reason $reason -Permanent
                } else {
                    Handle-DeathFailure -Reason $reason
                }
            }
        }
    }
}

function Get-LiveText {
    param([string]$SourceFile)
    if (Test-Path -LiteralPath $script:scriptOut) { Remove-Item -LiteralPath $script:scriptOut -Force -ErrorAction SilentlyContinue }
    $scriptArgs = '-NoProfile -ExecutionPolicy Bypass -File "' + (Join-Path $script:dir 'Get-LiveGame.ps1') + '"'
    if ($SourceFile) { $scriptArgs += ' -Source "' + $SourceFile + '"' }
    $p = $null
    try {
        $p = Start-Process -FilePath 'powershell' -ArgumentList $scriptArgs -RedirectStandardOutput $script:scriptOut -RedirectStandardError ($script:scriptOut + '.err') -NoNewWindow -PassThru
    } catch { return '' }
    $done = $false
    try { $done = $p.WaitForExit(20000) } catch { $done = $false }
    if (-not $done) {
        Stop-ProcessTree $p
        Write-Host "[$script:ts] Game-state formatter timed out - snapshot unavailable." -ForegroundColor Red
        return ''
    }
    $t = ''
    if (Test-Path -LiteralPath $script:scriptOut) { $t = Get-Content -LiteralPath $script:scriptOut -Raw -Encoding UTF8 }
    return $t
}

function Test-LiveDataAvailable {
    param([string]$Text)
    if (-not $Text) { return $false }
    $t = "$Text".Trim()
    if (-not $t) { return $false }
    if ($t -match '^Not in a live game') { return $false }
    if ($t -notmatch '(?m)^GAME ') { return $false }
    return $true
}

function New-CoachPrompt {
    param([string]$BasePrompt, [string]$Champ, [string]$Role = '')
    $data = Get-LiveText -SourceFile $script:liveTmp
    if (-not (Test-LiveDataAvailable -Text $data)) {
        $why = 'empty formatter output'
        if ($data -and ("$data".Trim() -match '^Not in a live game')) { $why = 'not in a live game' }
        return [pscustomobject]@{ Ok = $false; Error = $why; Prompt = ''; ArgLine = '' }
    }
    $intent = ''
    $intentFile = Join-Path $script:dir 'build_intent.txt'
    if (Test-Path -LiteralPath $intentFile) {
        $lines = (Get-Content -LiteralPath $intentFile -Raw -Encoding UTF8) -split "`r?`n"
        $planLine = ''
        $defaultLine = ''
        foreach ($l in $lines) {
            if ($l -match '^PLAN\[([^\]]+)\]\s*:') {
                $name = $matches[1].Trim()
                if ($Champ -and ($name -ieq $Champ)) { $planLine = $l.Trim() }
                elseif ($name -ieq 'default') { $defaultLine = $l.Trim() }
            }
        }
        if (-not $planLine) { $planLine = $defaultLine }
        $notes = ($lines | Where-Object { $_ -notmatch '^PLAN\[' -and $_.Trim() -ne '' }) -join "`r`n"
        $intent = "$planLine`r`n$notes"
    }
    $prev = ''
    if ($script:coachSessionId -and ($script:coachSessionId -eq $script:sessionId)) {
        $prev = (Read-FileText $script:coachFile).Trim()
    }
    $packBlock = Get-ChampionPackBlock -Champ $Champ -Role $Role
    $full = $BasePrompt + "`r`n`r`n=== GAME DATA (live) ===`r`n" + $data + "`r`n=== BUILD INTENT (current champion only) ===`r`n" + $intent + $packBlock + "`r`n=== PREVIOUS READOUT ===`r`n" + $prev
    $safe = $full.Replace([char]34, [char]39)
    $quoted = ([char]34) + $safe + ([char]34)
    $argLine = 'run --dir "' + $script:dir + '" --agent lol-coach --variant low --title "LoL AutoCoach" ' + $quoted
    if ($argLine.Length -gt $MaxCommandLineChars) {
        return [pscustomobject]@{ Ok = $false; Error = ("prompt too large: {0} chars (limit {1})" -f $argLine.Length, $MaxCommandLineChars); Prompt = ''; ArgLine = '' }
    }
    return [pscustomobject]@{ Ok = $true; Error = ''; Prompt = $full; ArgLine = $argLine }
}

function Start-CoachRun {
    param([string]$ArgLine, [string]$OutFile, [string]$ErrFile)
    if (-not $ArgLine) { return $null }
    if (Test-Path -LiteralPath $OutFile) { Remove-Item -LiteralPath $OutFile -Force -ErrorAction SilentlyContinue }
    return Start-Process -FilePath $ocExe -ArgumentList $ArgLine -RedirectStandardOutput $OutFile -RedirectStandardError $ErrFile -NoNewWindow -PassThru
}

function Get-ChampionPackBlock {
    param([string]$Champ, [string]$Role = '', [int]$MaxChars = 1800)
    if (-not $Champ) { return '' }
    $packScript = Join-Path $script:dir 'ui\packs.py'
    if (-not (Test-Path -LiteralPath $packScript)) { return '' }
    $py = $null
    foreach ($cand in @('python', 'py')) {
        $cmd = Get-Command $cand -ErrorAction SilentlyContinue
        if ($cmd) { $py = $cmd.Source; break }
    }
    if (-not $py) { return '' }
    $packText = ''
    $packArgs = @($packScript, '--prompt', '--champ', "$Champ", '--max-chars', $MaxChars)
    if ($Role) { $packArgs += @('--role', "$Role") }
    try {
        $out = & $py @packArgs 2>$null
        if ($LASTEXITCODE -ne 0) { return '' }
        $packText = (($out | Out-String).Trim())
    } catch {
        return ''
    }
    if (-not $packText) { return '' }
    return ("`r`n" + $packText)
}

try {
    Write-Host 'Checking Data Dragon assets...' -ForegroundColor DarkGray
    try { Update-DdragonAsset -Names @('item', 'champion') } catch { }
    Close-Session -Reason 'startup'
    if (-not $ocExe) {
        Write-Host 'opencode is not on PATH. Cleared stale coach output; aborting.' -ForegroundColor Red
        exit 1
    }

    Write-Host 'LoL AutoCoach - ticks every ' -NoNewline -ForegroundColor Green
    Write-Host "$TickSeconds s" -NoNewline -ForegroundColor Yellow
    Write-Host ', instant death reports. Ctrl+C or close this window to stop.' -ForegroundColor Green

    while ($true) {
        $script:ts = Get-Date -Format 'HH:mm:ss'

        if (Test-Path -LiteralPath $script:liveTmp) { Remove-Item -LiteralPath $script:liveTmp -Force -ErrorAction SilentlyContinue }
        $code = '000'
        $code = curl.exe -k -s -o $script:liveTmp -w '%{http_code}' --max-time 5 'https://127.0.0.1:2999/liveclientdata/allgamedata'
        $curlExit = $LASTEXITCODE
        $d = $null
        if ($curlExit -eq 0 -and $code -eq '200' -and (Test-Path -LiteralPath $script:liveTmp)) {
            try { $d = ((Get-Content -LiteralPath $script:liveTmp -Raw -Encoding UTF8) | ConvertFrom-Json) } catch { $d = $null }
        }

        if (-not $d -or -not $d.gameData) {
            $script:apiFails++
            if ($script:sessionActive) {
                if (-not $script:disconnected) {
                    $script:disconnected = $true
                    $script:apiDownSinceSec = Get-ElapsedSeconds
                    Send-Coverage -Action 'stop' -Reason 'disconnected'
                    $null = Send-TimelineEvent -Kind 'status' -Label ("disconnected after {0} failed polls" -f $script:apiFails) -Data @{ reason = 'live_client_unavailable'; lastGameTime = [double]$script:lastGameTime } -GameTime $script:lastGameTime
                    Publish-Record -JsonPath $script:coachMeta -TextPath $script:coachFile -Kind 'coach' -Status 'disconnected' -Text (Read-FileText $script:coachFile) -ErrorText 'live client unavailable' -ObservedGameTime $script:lastGameTime -Seq $script:seq -Extra @{ role = $script:myPosition }
                    Write-Host "[$script:ts] Live API unavailable - session kept, waiting for it to return." -ForegroundColor DarkGray
                } elseif (((Get-ElapsedSeconds) - $script:apiDownSinceSec) -ge $NoGameEndSeconds) {
                    Write-Host "[$script:ts] No live game for $([int]$NoGameEndSeconds)s - closing session (no_game_timeout)." -ForegroundColor DarkGray
                    Close-Session -Reason 'no_game_timeout'
                }
            } else {
                Write-Host "[$script:ts] Waiting for a game..." -ForegroundColor DarkGray
            }
            Update-Workers
            Start-Sleep -Seconds $IdleSeconds
            continue
        }
        $script:apiFails = 0
        $script:lastGoodPollAtSec = Get-ElapsedSeconds
        if ($script:disconnected) {
            $script:disconnected = $false
            Send-Coverage -Action 'start' -Reason 'reconnected'
            Write-Host "[$script:ts] Live API is back - resuming coverage." -ForegroundColor DarkGray
        }

        $g = $d.gameData
        $ap = $d.activePlayer
        $me = $null
        foreach ($p in $d.allPlayers) {
            if (Test-SamePlayer $p $ap) { $me = $p; break }
        }
        $script:myName = ''
        $script:myGameName = ''
        $script:myTagLine = ''
        $script:myPosition = ''
        if ($me) {
            $script:myName = "$($me.riotId)"
            if (-not $script:myName) { $script:myName = "$($me.riotIdGameName)" }
            if (-not $script:myName) { $script:myName = "$($me.summonerName)" }
            $script:myGameName = "$($me.riotIdGameName)"
            $script:myTagLine = "$($me.riotIdTagLine)"
            $script:myPosition = "$($me.position)"
        }
        if (-not $script:myName) {
            $script:myName = "$($ap.riotId)"
            if (-not $script:myName) { $script:myName = "$($ap.riotIdGameName)" }
            if (-not $script:myName) { $script:myName = "$($ap.summonerName)" }
            $script:myGameName = "$($ap.riotIdGameName)"
            $script:myTagLine = "$($ap.riotIdTagLine)"
            $script:myPosition = "$($ap.position)"
        }
        if ($script:myPosition -ieq 'NONE') { $script:myPosition = '' }
        $newChamp = ''
        if ($me) { $newChamp = "$($me.championName)" }

        $gt = [double]$g.gameTime
        $rollback = ($script:sessionActive -and ($gt -lt ($script:lastGameTime - 5)))
        $champChanged = ($script:sessionActive -and $script:sessionChamp -and $newChamp -and ($newChamp -ine $script:sessionChamp))
        $playerChanged = ($script:sessionActive -and $script:sessionPlayer -and $script:myName -and ($script:myName -ine $script:sessionPlayer))
        $modeChanged = ($script:sessionActive -and $script:sessionMode -and ("$($g.gameMode)" -ne $script:sessionMode))
        $mapChanged = ($script:sessionActive -and $script:sessionMap -and ("$($g.mapNumber)" -ne $script:sessionMap))
        if ($script:sessionActive -and ($rollback -or $champChanged -or $playerChanged -or $modeChanged -or $mapChanged)) {
            Write-Host "[$script:ts] New game detected (rollback=$rollback champ=$champChanged player=$playerChanged) - resetting session state." -ForegroundColor Yellow
            Start-Session -Data $d -Champ $newChamp -Reason 'new_game'
        } elseif (-not $script:sessionActive) {
            Write-Host "[$script:ts] In game as $newChamp - starting session." -ForegroundColor Green
            Start-Session -Data $d -Champ $newChamp -Reason 'game_detected'
        }
        if ($script:sessionActive -and (-not $script:gameStartAcked)) {
            if ((Get-ElapsedSeconds) -ge $script:nextSessionProbeSec) {
                $script:nextSessionProbeSec = (Get-ElapsedSeconds) + 10
                $sid = Get-ServerSessionId
                if ($sid -and ($sid -ne $script:sessionId)) {
                    $script:sessionId = $sid
                    $script:sessionIdSource = 'server'
                    $script:gameStartAttempts = 0
                    $script:gameStartNextTrySec = 0.0
                    Write-Host "[$script:ts] Adopted server session id." -ForegroundColor DarkGray
                }
            }
            $null = Send-GameStart
        }
        $script:lastGameTime = $gt
        $script:seq++
        Add-GameSnapshot -Data $d -GameTime $gt -Seq $script:seq
        Add-GameEvents -Data $d

        $sigParts = @()
        if ($me -and $me.items) {
            foreach ($it in $me.items) {
                $cnt = 1
                if ($null -ne $it.count) { try { $cnt = [int]$it.count } catch { $cnt = 1 } }
                $sigParts += ("{0}x{1}" -f $it.itemID, $cnt)
            }
        }
        $sig = (($sigParts | Sort-Object) -join ',')
        if ($script:lastItemSignature -and ($sig -ne $script:lastItemSignature)) {
            $null = Send-TimelineEvent -Kind 'item' -Label ("inventory changed: {0}" -f $sig)
            $script:forceTick = $true
        }
        $script:lastItemSignature = $sig

        $nextObj = Get-NextObjective -Data $d -GameTime $gt
        if ($nextObj -and ($nextObj.Key -ne $script:lastObjectiveKey)) {
            $script:lastObjectiveKey = $nextObj.Key
            $null = Send-TimelineEvent -Kind 'objective' -Label $nextObj.Label -Data @{ secondsLeft = [int]$nextObj.SecondsLeft; kind = $nextObj.Kind; soul = $nextObj.Soul }
            $script:forceTick = $true
        }

        Update-Workers

        $bestKey = ''
        $bestT = -1.0
        $bestKiller = ''
        if ($d.events -and $d.events.Events) {
            foreach ($e in $d.events.Events) {
                if ("$($e.EventName)" -ne 'ChampionKill') { continue }
                if (-not (Test-IsVictimMe "$($e.VictimName)")) { continue }
                $et = [double]$e.EventTime
                if ($et -le $script:lastDeathT) { continue }
                $key = Get-EventKey $e
                if ($script:processedDeathKeys.ContainsKey($key)) { continue }
                if ($et -gt $bestT) { $bestT = $et; $bestKey = $key; $bestKiller = "$($e.KillerName)" }
            }
        }
        if ($bestKey) {
            $replace = $false
            if (-not $script:activeDeath) { $replace = $true }
            elseif (($script:activeDeath.Key -ne $bestKey) -and ($bestT -gt ([double]$script:activeDeath.Time + 0.001))) { $replace = $true }
            if ($replace) {
                if ($script:deathProc) {
                    Complete-Inference -RequestId $script:deathRequestId -Status 'superseded' -Data @{ reason = 'newer death detected' }
                    $script:deathRequestId = ''
                    Stop-ProcessTree $script:deathProc
                    $script:deathProc = $null
                    $script:deathWatch = $null
                }
                New-DeathRecord -Key $bestKey -Time $bestT -Killer $bestKiller -KilledByChampion (Test-KillerIsChampion -Data $d -Killer $bestKiller)
            }
        }
        if ($script:activeDeath -and -not $script:deathProc -and ((Get-ElapsedSeconds) -ge $script:deathRetryAtSec)) {
            if (($script:activeDeath.State -eq 'detected') -or ($script:activeDeath.State -eq 'queued') -or ($script:activeDeath.State -eq 'failed')) {
                Start-DeathWorker
            }
        }

        if (-not $script:tickProc -and (((Get-ElapsedSeconds) -ge $script:nextTickAtSec) -or $script:forceTick)) {
            $script:forceTick = $false
            Start-TickWorker
        }

        Start-Sleep -Seconds $PollSeconds
    }
} finally {
    try { Close-Session -Reason 'collector_stopped' } catch { }
    Stop-Workers -DeadReason 'cancelled'
    foreach ($f in @($script:liveTmp, $script:scriptOut, ($script:scriptOut + '.err'), $script:tickOut, $script:tickErr, $script:deathOut, $script:deathErr)) {
        Remove-Item -LiteralPath $f -Force -ErrorAction SilentlyContinue
    }
    if ($script:InstanceMutex -and $ownsMutex) {
        try { $script:InstanceMutex.ReleaseMutex() } catch { }
        try { $script:InstanceMutex.Dispose() } catch { }
    }
}
