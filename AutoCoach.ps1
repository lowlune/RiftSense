param(
    [int]$TickSeconds = 60,
    [int]$IdleSeconds = 20,
    [int]$PollSeconds = 5
)

[System.Net.ServicePointManager]::ServerCertificateValidationCallback = { $true }
$ErrorActionPreference = 'Continue'
$dir = $PSScriptRoot
$liveTmp = Join-Path $env:TEMP 'lol_autocoach_live.json'
$scriptOut = Join-Path $env:TEMP 'lol_autocoach_livetext.txt'
$tickOut = Join-Path $env:TEMP 'lol_autocoach_tick.txt'
$tickErr = Join-Path $env:TEMP 'lol_autocoach_tick.err'
$deathOut = Join-Path $env:TEMP 'lol_autocoach_death.txt'
$deathErr = Join-Path $env:TEMP 'lol_autocoach_death.err'

$ocExe = (Get-Command opencode -ErrorAction SilentlyContinue).Source
if (-not $ocExe) { Write-Host 'opencode is not on PATH. Aborting.' -ForegroundColor Red; exit 1 }

$lastDeathT = -1.0
$nextTickAt = [datetime]::MinValue
$tickProc = $null
$tickStart = $null
$deathProc = $null
$tickFails = 0

Write-Host 'LoL AutoCoach - ticks every ' -NoNewline -ForegroundColor Green
Write-Host "$TickSeconds s" -NoNewline -ForegroundColor Yellow
Write-Host ', instant death reports. Ctrl+C or close this window to stop.' -ForegroundColor Green

function Get-LiveText {
    param([string]$SourceFile)
    if (Test-Path -LiteralPath $scriptOut) { Remove-Item -LiteralPath $scriptOut -Force -ErrorAction SilentlyContinue }
    $scriptArgs = '-NoProfile -ExecutionPolicy Bypass -File "' + (Join-Path $dir 'Get-LiveGame.ps1') + '"'
    if ($SourceFile) { $scriptArgs += ' -Source "' + $SourceFile + '"' }
    Start-Process -FilePath 'powershell' -ArgumentList $scriptArgs -RedirectStandardOutput $scriptOut -RedirectStandardError ($scriptOut + '.err') -NoNewWindow -Wait
    $t = ''
    if (Test-Path -LiteralPath $scriptOut) { $t = Get-Content -LiteralPath $scriptOut -Raw -Encoding UTF8 }
    return $t
}

function Start-CoachRun {
    param([string]$Prompt, [string]$OutFile, [string]$ErrFile, [string]$Champ)
    $data = Get-LiveText -SourceFile $liveTmp
    $intent = ''
    $intentFile = Join-Path $dir 'build_intent.txt'
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
    $coachFile = Join-Path $dir 'coach_latest.txt'
    if (Test-Path -LiteralPath $coachFile) { $prev = (Get-Content -LiteralPath $coachFile -Raw -Encoding UTF8).Trim() }
    $full = $Prompt + "`r`n`r`n=== GAME DATA (live) ===`r`n" + $data + "`r`n=== BUILD INTENT (current champion only) ===`r`n" + $intent + "`r`n=== PREVIOUS READOUT ===`r`n" + $prev
    $safe = $full.Replace([char]34, [char]39)
    $quoted = ([char]34) + $safe + ([char]34)
    $argLine = 'run --dir "' + $dir + '" --agent lol-coach --variant low --title "LoL AutoCoach" ' + $quoted
    if (Test-Path -LiteralPath $OutFile) { Remove-Item -LiteralPath $OutFile -Force -ErrorAction SilentlyContinue }
    return Start-Process -FilePath $ocExe -ArgumentList $argLine -RedirectStandardOutput $OutFile -RedirectStandardError $ErrFile -NoNewWindow -PassThru
}

function Read-RunText {
    param([string]$OutFile)
    $t = ''
    if (Test-Path -LiteralPath $OutFile) { $t = Get-Content -LiteralPath $OutFile -Raw -Encoding UTF8 }
    return $t
}

while ($true) {
    $ts = Get-Date -Format 'HH:mm:ss'
    $code = curl.exe -k -s -o $liveTmp -w '%{http_code}' --max-time 5 'https://127.0.0.1:2999/liveclientdata/allgamedata'
    $d = $null
    if ($code -eq '200' -and (Test-Path -LiteralPath $liveTmp)) {
        try { $d = ((Get-Content -LiteralPath $liveTmp -Raw -Encoding UTF8) | ConvertFrom-Json) } catch { $d = $null }
    }

    if (-not $d -or -not $d.gameData) {
        Write-Host "[$ts] Waiting for a game..." -ForegroundColor DarkGray
        if ($tickProc -and -not $tickProc.HasExited) { Stop-Process -Id $tickProc.Id -Force -ErrorAction SilentlyContinue }
        if ($deathProc -and -not $deathProc.HasExited) { Stop-Process -Id $deathProc.Id -Force -ErrorAction SilentlyContinue }
        $tickProc = $null
        $deathProc = $null
        $lastDeathT = -1.0
        $nextTickAt = [datetime]::MinValue
        Start-Sleep -Seconds $IdleSeconds
        continue
    }

    $ap = $d.activePlayer
    $myName = $ap.riotIdGameName
    if (-not $myName) { $myName = $ap.riotId }
    if (-not $myName) { $myName = $ap.summonerName }
    $myChamp = ''
    foreach ($p in $d.allPlayers) {
        $pn = $p.riotIdGameName
        if (-not $pn) { $pn = $p.riotId }
        if (-not $pn) { $pn = $p.summonerName }
        if ($pn -and $myName -and ($pn -ieq $myName)) { $myChamp = "$($p.championName)"; break }
    }

    $deathT = -1.0
    $deathKiller = ''
    foreach ($e in $d.events.Events) {
        if ($e.EventName -ne 'ChampionKill') { continue }
        $victim = "$($e.VictimName)"
        if (-not $victim) { continue }
        $isMe = $false
        if ($myName) {
            $base = $myName
            if ($base -like '*#*') { $base = ($base -split '#')[0] }
            if ($victim -ieq $myName -or $victim -ieq $base -or $victim -like "$base#*") { $isMe = $true }
        }
        if ($isMe) {
            $et = [double]$e.EventTime
            if ($et -gt $lastDeathT) { $lastDeathT = $et; $deathT = $et; $deathKiller = "$($e.KillerName)" }
        }
    }

    if ($deathProc -and $deathProc.HasExited) {
        $out = Read-RunText $deathOut
        $deathText = $out.Trim()
        if ($deathText -match '(?s)===DEATH===(.*)$') { $deathText = $matches[1].Trim() }
        if (-not $deathText) { $deathText = '(death report failed)' }
        Set-Content -LiteralPath (Join-Path $dir 'death_latest.txt') -Value $deathText -Encoding UTF8
        Write-Host $deathText -ForegroundColor Red
        Write-Host ''
        if ($deathProc.ExitCode -ne 0) { Write-Host "[$ts] Death report run failed (exit $($deathProc.ExitCode))." -ForegroundColor Red }
        $deathProc = $null
    }

    if ($deathT -ge 0) {
        $clock = '{0}:{1:00}' -f [int]($deathT / 60), [int]($deathT % 60)
        Write-Host "[$ts] Death detected at $clock (to $deathKiller) - generating report..." -ForegroundColor Red
        Set-Content -LiteralPath (Join-Path $dir 'death_latest.txt') -Value ("PENDING|" + $clock + "|" + $deathKiller) -Encoding UTF8
        if ($deathProc -and -not $deathProc.HasExited) { Stop-Process -Id $deathProc.Id -Force -ErrorAction SilentlyContinue }
        $deathPrompt = "DEATH REPORT. You just died at $clock to $deathKiller. The live game data and your build intent are included below - answer directly from them. Output up to 4 hidden reasoning lines first (>>), then the ===DEATH=== block exactly per your Death reports section (DIED/WHY/NOW/NEXT/DO NOW), under 8 lines total."
        $deathProc = Start-CoachRun -Prompt $deathPrompt -OutFile $deathOut -ErrFile $deathErr -Champ $myChamp
    }

    if ($tickProc -and $tickProc.HasExited) {
        $out = Read-RunText $tickOut
        $clean = $out.Trim()
        if ($clean -match '(?s)===COACH===(.*)$') { $clean = $matches[1].Trim() }
        $bad = ($tickProc.ExitCode -ne 0) -or (-not $clean)
        if (-not $clean) { $clean = '(no coach output this tick)' }
        Set-Content -LiteralPath (Join-Path $dir 'coach_latest.txt') -Value $clean -Encoding UTF8
        Write-Host $clean
        if ($bad) {
            $tickFails++
            if ($tickFails -le 2) {
                Write-Host "[$ts] Coach run failed (exit $($tickProc.ExitCode)) - retrying immediately ($tickFails/2)." -ForegroundColor Red
                $nextTickAt = [datetime]::MinValue
            } else {
                Write-Host "[$ts] Coach run failed repeatedly - waiting 60s." -ForegroundColor Red
                $nextTickAt = [datetime]::Now.AddSeconds(60)
            }
        } else {
            $tickFails = 0
            $nextTickAt = $tickStart.AddSeconds($TickSeconds)
        }
        $tickProc = $null
        Write-Host ''
    }

    if ([datetime]::Now -ge $nextTickAt -and -not ($tickProc -and -not $tickProc.HasExited)) {
        $tickStart = [datetime]::Now
        Write-Host "[$ts] Tick started..." -ForegroundColor Cyan
        $tickPrompt = 'Live tick. The live game data and your build intent are included below - answer directly from them. Output up to 4 hidden reasoning lines first (>>), then the ===COACH=== readout exactly per your format.'
        $tickProc = Start-CoachRun -Prompt $tickPrompt -OutFile $tickOut -ErrFile $tickErr -Champ $myChamp
    }

    Start-Sleep -Seconds $PollSeconds
}
