param(
    [int]$TickSeconds = 60,
    [int]$IdleSeconds = 20,
    [int]$PollSeconds = 5,
    [int]$TickTimeoutSeconds = 180,
    [int]$DeathTimeoutSeconds = 90
)

if ($PollSeconds -lt 1) { $PollSeconds = 5 }
if ($IdleSeconds -lt 1) { $IdleSeconds = 20 }
if ($TickSeconds -lt 1) { $TickSeconds = 60 }
if ($TickTimeoutSeconds -lt 5) { $TickTimeoutSeconds = 180 }
if ($DeathTimeoutSeconds -lt 5) { $DeathTimeoutSeconds = 90 }

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
if (-not $ocExe) { Write-Host 'opencode is not on PATH. Aborting.' -ForegroundColor Red; exit 1 }

. (Join-Path $script:dir 'common.ps1')

$script:sessionActive = $false
$script:sessionId = ''
$script:sessionChamp = ''
$script:sessionPlayer = ''
$script:sessionMode = ''
$script:sessionMap = ''
$script:lastGameTime = 0.0
$script:processedDeathKeys = @{}
$script:lastDeathT = -1.0
$script:seq = 0
$script:activeDeath = $null
$script:deathRetryAt = [datetime]::MinValue
$script:deathState = 'idle'
$script:tickProc = $null
$script:tickWatch = $null
$script:tickSnapshot = $null
$script:deathProc = $null
$script:deathWatch = $null
$script:tickFails = 0
$script:nextTickAt = [datetime]::MinValue
$script:coachSessionId = ''
$script:apiFails = 0
$script:ts = ''
$script:myName = ''
$script:myGameName = ''
$script:myTagLine = ''

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

function Publish-Envelope {
    param(
        [string]$Path,
        [string]$Kind,
        [string]$Status,
        [string]$Text,
        [string]$ErrorText,
        $ObservedGameTime,
        $Seq,
        $Extra
    )
    $obj = [ordered]@{
        schema = 'riftsense.v1'
        kind = $Kind
        session = $script:sessionId
        seq = $Seq
        observedGameTime = $ObservedGameTime
        completedAt = (Get-Date).ToUniversalTime().ToString('o')
        status = $Status
        error = $ErrorText
        text = $Text
    }
    if ($Extra) { foreach ($k in $Extra.Keys) { $obj[$k] = $Extra[$k] } }
    Publish-Text -Path $Path -Text ($obj | ConvertTo-Json -Compress -Depth 5)
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

function Stop-Workers {
    if ($script:tickProc) {
        Stop-ProcessTree $script:tickProc
        $script:tickProc = $null
        $script:tickWatch = $null
        $script:tickSnapshot = $null
    }
    if ($script:deathProc) {
        Stop-ProcessTree $script:deathProc
        $script:deathProc = $null
        $script:deathWatch = $null
    }
}

function Start-Session {
    param($Data, [string]$Champ)
    Stop-Workers
    $g = $Data.gameData
    $script:sessionActive = $true
    $script:sessionId = ('{0}|{1}|{2}|{3}' -f $g.gameMode, $g.mapNumber, $Champ, (Get-Date -Format 'yyyyMMddHHmmss'))
    $script:sessionChamp = $Champ
    $script:sessionPlayer = $script:myName
    $script:sessionMode = "$($g.gameMode)"
    $script:sessionMap = "$($g.mapNumber)"
    $script:lastGameTime = [double]$g.gameTime
    $script:processedDeathKeys = @{}
    $script:lastDeathT = -1.0
    $script:activeDeath = $null
    $script:deathRetryAt = [datetime]::MinValue
    $script:deathState = 'idle'
    $script:tickFails = 0
    $script:nextTickAt = [datetime]::MinValue
    $script:coachSessionId = ''
    $script:seq = 0
    if ($Data.events -and $Data.events.Events) {
        foreach ($e in $Data.events.Events) {
            if ("$($e.EventName)" -ne 'ChampionKill') { continue }
            $script:processedDeathKeys[(Get-EventKey $e)] = $true
            if (Test-IsVictimMe "$($e.VictimName)") {
                $et = [double]$e.EventTime
                if ($et -gt $script:lastDeathT) { $script:lastDeathT = $et }
            }
        }
    }
    Publish-Text -Path $script:coachFile -Text ''
    Publish-Envelope -Path $script:coachMeta -Kind 'coach' -Status 'starting' -Text '' -ErrorText '' -ObservedGameTime $g.gameTime -Seq 0
    Publish-Text -Path $script:deathFile -Text ''
    Publish-Envelope -Path $script:deathMeta -Kind 'death' -Status 'idle' -Text '' -ErrorText '' -ObservedGameTime $g.gameTime -Seq 0
}

function Close-Session {
    Stop-Workers
    $script:activeDeath = $null
    $script:deathState = 'idle'
    $script:sessionActive = $false
    Publish-Text -Path $script:coachFile -Text 'OUT OF GAME'
    Publish-Envelope -Path $script:coachMeta -Kind 'coach' -Status 'no_game' -Text 'OUT OF GAME' -ErrorText '' -ObservedGameTime $null -Seq $script:seq
    Publish-Text -Path $script:deathFile -Text ''
    Publish-Envelope -Path $script:deathMeta -Kind 'death' -Status 'no_game' -Text '' -ErrorText '' -ObservedGameTime $null -Seq $script:seq
}

function New-DeathRecord {
    param([string]$Key, [double]$Time, [string]$Killer)
    $total = [int][math]::Floor($Time)
    if ($total -lt 0) { $total = 0 }
    $clock = '{0}:{1:00}' -f [int][math]::Floor($total / 60), ($total % 60)
    $script:activeDeath = [pscustomobject]@{
        Key = $Key
        Time = $Time
        Killer = $Killer
        Clock = $clock
        Attempts = 0
        State = 'detected'
        Session = $script:sessionId
        Seq = $script:seq
        GameTime = $script:lastGameTime
    }
    $script:deathState = 'detected'
    $script:deathRetryAt = [datetime]::MinValue
}

function Handle-DeathFailure {
    param([string]$Reason)
    $script:deathProc = $null
    $script:deathWatch = $null
    $script:deathState = 'failed'
    $ad = $script:activeDeath
    if (-not $ad) { return }
    $ad.State = 'failed'
    $failText = ("DIED: {0} to {1} - report unavailable ({2})" -f $ad.Clock, $ad.Killer, $Reason)
    Publish-Text -Path $script:deathFile -Text $failText
    Publish-Envelope -Path $script:deathMeta -Kind 'death' -Status 'failed' -Text $failText -ErrorText $Reason -ObservedGameTime $ad.GameTime -Seq $ad.Seq -Extra @{ clock = $ad.Clock; killer = $ad.Killer; eventTime = $ad.Time }
    Write-Host "[$script:ts] Death report failed ($Reason)." -ForegroundColor Red
    if ([int]$ad.Attempts -ge 3) {
        $script:processedDeathKeys[$ad.Key] = $true
        $script:lastDeathT = [math]::Max([double]$script:lastDeathT, [double]$ad.Time)
        $script:activeDeath = $null
    } else {
        $script:deathRetryAt = [datetime]::Now.AddSeconds(30)
    }
}

function Start-DeathWorker {
    $ad = $script:activeDeath
    if (-not $ad) { return }
    $ad.Attempts = [int]$ad.Attempts + 1
    $ad.State = 'queued'
    $script:deathState = 'queued'
    $script:deathRetryAt = [datetime]::MinValue
    $pending = ("PENDING|" + $ad.Clock + "|" + $ad.Killer)
    Publish-Text -Path $script:deathFile -Text $pending
    Publish-Envelope -Path $script:deathMeta -Kind 'death' -Status 'pending' -Text $pending -ErrorText '' -ObservedGameTime $ad.GameTime -Seq $ad.Seq -Extra @{ clock = $ad.Clock; killer = $ad.Killer; eventTime = $ad.Time }
    Write-Host "[$script:ts] Death detected at $($ad.Clock) (to $($ad.Killer)) - generating report... (attempt $($ad.Attempts))" -ForegroundColor Red
    $deathPrompt = "DEATH REPORT. You just died at $($ad.Clock) to $($ad.Killer). The live game data and your build intent are included below - answer directly from them. Output up to 4 hidden reasoning lines first (>>), then the ===DEATH=== block exactly per your Death reports section (DIED/WHY/NOW/NEXT/DO NOW), under 8 lines total."
    $p = $null
    try { $p = Start-CoachRun -Prompt $deathPrompt -OutFile $script:deathOut -ErrFile $script:deathErr -Champ $script:sessionChamp } catch { $p = $null }
    if (-not $p) {
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
    $lastGood = Read-FileText $script:coachFile
    if (-not $lastGood.Trim()) {
        $lastGood = ("(coach tick failed: {0})" -f $Reason)
        Publish-Text -Path $script:coachFile -Text $lastGood
    }
    Publish-Envelope -Path $script:coachMeta -Kind 'coach' -Status 'failed' -Text $lastGood -ErrorText $Reason -ObservedGameTime $script:lastGameTime -Seq $script:seq
    Write-Host "[$script:ts] Coach run failed ($Reason)." -ForegroundColor Red
    $script:tickFails++
    if ($script:tickFails -le 2) {
        Write-Host "[$script:ts] Retrying immediately ($($script:tickFails)/2)." -ForegroundColor Red
        $script:nextTickAt = [datetime]::MinValue
    } else {
        Write-Host "[$script:ts] Coach run failed repeatedly - waiting 60s." -ForegroundColor Red
        $script:nextTickAt = [datetime]::Now.AddSeconds(60)
    }
    $script:tickProc = $null
    $script:tickWatch = $null
    $script:tickSnapshot = $null
}

function Start-TickWorker {
    $script:tickSnapshot = [pscustomobject]@{ Session = $script:sessionId; Seq = $script:seq; GameTime = $script:lastGameTime }
    Write-Host "[$script:ts] Tick started..." -ForegroundColor Cyan
    $tickPrompt = 'Live tick. The live game data and your build intent are included below - answer directly from them. Output up to 4 hidden reasoning lines first (>>), then the ===COACH=== readout exactly per your format.'
    $p = $null
    try { $p = Start-CoachRun -Prompt $tickPrompt -OutFile $script:tickOut -ErrFile $script:tickErr -Champ $script:sessionChamp } catch { $p = $null }
    if (-not $p) {
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
            if ($valid -and $sameSession) {
                Publish-Text -Path $script:coachFile -Text $body
                Publish-Envelope -Path $script:coachMeta -Kind 'coach' -Status 'ok' -Text $body -ErrorText '' -ObservedGameTime $snap.GameTime -Seq $snap.Seq
                $script:coachSessionId = $script:sessionId
                Write-Host $body
                Write-Host ''
                $script:tickFails = 0
                $script:nextTickAt = [datetime]::Now.AddSeconds($TickSeconds)
                $script:tickProc = $null
                $script:tickWatch = $null
                $script:tickSnapshot = $null
            } else {
                $reason = 'invalid or empty output'
                if ($code -ne 0) { $reason = "exit code $code" }
                elseif (-not $body) { $reason = 'missing or empty ===COACH=== block' }
                if (-not $sameSession) { $reason = 'stale result from a previous session' }
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
            $script:deathProc = $null
            $script:deathWatch = $null
            if ($valid -and $sameSession) {
                Publish-Text -Path $script:deathFile -Text $body
                Publish-Envelope -Path $script:deathMeta -Kind 'death' -Status 'ok' -Text $body -ErrorText '' -ObservedGameTime $ad.GameTime -Seq $ad.Seq -Extra @{ clock = $ad.Clock; killer = $ad.Killer; eventTime = $ad.Time }
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
                if (-not $sameSession) { $reason = 'stale result from a previous session' }
                Handle-DeathFailure -Reason $reason
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

function Start-CoachRun {
    param([string]$Prompt, [string]$OutFile, [string]$ErrFile, [string]$Champ)
    $data = Get-LiveText -SourceFile $script:liveTmp
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
    $full = $Prompt + "`r`n`r`n=== GAME DATA (live) ===`r`n" + $data + "`r`n=== BUILD INTENT (current champion only) ===`r`n" + $intent + "`r`n=== PREVIOUS READOUT ===`r`n" + $prev
    $safe = $full.Replace([char]34, [char]39)
    $quoted = ([char]34) + $safe + ([char]34)
    $argLine = 'run --dir "' + $script:dir + '" --agent lol-coach --variant low --title "LoL AutoCoach" ' + $quoted
    if (Test-Path -LiteralPath $OutFile) { Remove-Item -LiteralPath $OutFile -Force -ErrorAction SilentlyContinue }
    return Start-Process -FilePath $ocExe -ArgumentList $argLine -RedirectStandardOutput $OutFile -RedirectStandardError $ErrFile -NoNewWindow -PassThru
}

try {
    Write-Host 'Checking Data Dragon assets...' -ForegroundColor DarkGray
    try { Update-DdragonAsset -Names @('item', 'champion') } catch { }
    Close-Session

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
            if ($script:sessionActive -and $script:apiFails -ge 3) {
                Write-Host "[$script:ts] Game ended or Live API unavailable - closing session." -ForegroundColor DarkGray
                Close-Session
            } elseif (-not $script:sessionActive) {
                Write-Host "[$script:ts] Waiting for a game..." -ForegroundColor DarkGray
            } else {
                Write-Host "[$script:ts] Live API temporarily unavailable ($($script:apiFails)/3) - keeping session state." -ForegroundColor DarkGray
            }
            Update-Workers
            Start-Sleep -Seconds $IdleSeconds
            continue
        }
        $script:apiFails = 0

        $g = $d.gameData
        $ap = $d.activePlayer
        $me = $null
        foreach ($p in $d.allPlayers) {
            if (Test-SamePlayer $p $ap) { $me = $p; break }
        }
        $script:myName = ''
        $script:myGameName = ''
        $script:myTagLine = ''
        if ($me) {
            $script:myName = "$($me.riotId)"
            if (-not $script:myName) { $script:myName = "$($me.riotIdGameName)" }
            if (-not $script:myName) { $script:myName = "$($me.summonerName)" }
            $script:myGameName = "$($me.riotIdGameName)"
            $script:myTagLine = "$($me.riotIdTagLine)"
        }
        if (-not $script:myName) {
            $script:myName = "$($ap.riotId)"
            if (-not $script:myName) { $script:myName = "$($ap.riotIdGameName)" }
            if (-not $script:myName) { $script:myName = "$($ap.summonerName)" }
            $script:myGameName = "$($ap.riotIdGameName)"
            $script:myTagLine = "$($ap.riotIdTagLine)"
        }
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
            Start-Session -Data $d -Champ $newChamp
        } elseif (-not $script:sessionActive) {
            Write-Host "[$script:ts] In game as $newChamp - starting session." -ForegroundColor Green
            Start-Session -Data $d -Champ $newChamp
        }
        $script:lastGameTime = $gt
        $script:seq++

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
                    Stop-ProcessTree $script:deathProc
                    $script:deathProc = $null
                    $script:deathWatch = $null
                }
                New-DeathRecord -Key $bestKey -Time $bestT -Killer $bestKiller
            }
        }
        if ($script:activeDeath -and -not $script:deathProc -and ([datetime]::Now -ge $script:deathRetryAt)) {
            if (($script:activeDeath.State -eq 'detected') -or ($script:activeDeath.State -eq 'queued') -or ($script:activeDeath.State -eq 'failed')) {
                Start-DeathWorker
            }
        }

        if (-not $script:tickProc -and ([datetime]::Now -ge $script:nextTickAt)) {
            Start-TickWorker
        }

        Start-Sleep -Seconds $PollSeconds
    }
} finally {
    Stop-Workers
    foreach ($f in @($script:liveTmp, $script:scriptOut, ($script:scriptOut + '.err'), $script:tickOut, $script:tickErr, $script:deathOut, $script:deathErr)) {
        Remove-Item -LiteralPath $f -Force -ErrorAction SilentlyContinue
    }
    if ($script:InstanceMutex -and $ownsMutex) {
        try { $script:InstanceMutex.ReleaseMutex() } catch { }
        try { $script:InstanceMutex.Dispose() } catch { }
    }
}
