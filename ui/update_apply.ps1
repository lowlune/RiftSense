# RiftSense detached update helper.
#
# Applies a staged (already downloaded and verified) update to an install
# directory. Designed to be copied to %TEMP% and launched detached by the UI
# server, because a running install cannot safely replace its own files.
#
# PowerShell 5.1 compatible. ASCII only. No administrator rights required.
# Never touches user/runtime state listed in Get-PreservedUserState.
#
# Exit codes: 0 = success, 1 = failure (rolled back), 2 = validation error.

param(
    [string]$Staged,
    [string]$InstallDir,
    [string]$Version,
    [string]$BackupDir,
    [string]$LogDir,
    [int[]]$WaitPid = @(),
    [switch]$Restart,
    [switch]$WhatIf
)

function Get-AppFilePatterns {
    # Relative paths/patterns owned by the app; these are replaced on update.
    return @(
        'AutoCoach.ps1',
        'Get-LiveGame.ps1',
        'Get-DraftData.ps1',
        'Get-MyProfile.ps1',
        'common.ps1',
        'Start-AutoCoach.cmd',
        'Start-Ui.cmd',
        'VERSION',
        'install.ps1',
        'ui/*.py',
        'ui/*.html',
        'agent/lol-coach.md',
        'knowledge/packs/*.json',
        'LICENSE',
        'README.md'
    )
}

function Get-PreservedUserState {
    # User/runtime state that must never be overwritten by an update.
    return @(
        'build_intent.txt',
        'build_intent.txt.bak',
        'coach_latest.txt',
        'coach_latest.json',
        'death_latest.txt',
        'death_latest.json',
        'game_epoch.json',
        'dashboard_token.txt',
        'update_state.json',
        'ui/data',
        'champion.json',
        'items.json',
        'knowledge/packs/custom',
        '.git',
        '.update_in_progress'
    )
}

function ConvertTo-NormalizedPath {
    param([string]$Path)
    if (-not $Path) { return '' }
    $value = "$Path".Replace([char]47, [System.IO.Path]::DirectorySeparatorChar)
    $value = $value.Replace([char]92, [System.IO.Path]::DirectorySeparatorChar)
    return $value.TrimEnd([char[]]@([char]92, [char]47))
}

function Test-PathInsideRoot {
    # True when Path resolves to Root itself or to a descendant of Root.
    param([string]$Path, [string]$Root)
    if (-not $Path -or -not $Root) { return $false }
    $pathFull = ''
    $rootFull = ''
    try { $pathFull = [System.IO.Path]::GetFullPath($Path) } catch { return $false }
    try { $rootFull = [System.IO.Path]::GetFullPath($Root) } catch { return $false }
    $pathTrim = $pathFull.TrimEnd([char[]]@([char]92, [char]47))
    $rootTrim = $rootFull.TrimEnd([char[]]@([char]92, [char]47))
    if ($pathTrim.Length -eq 0 -or $rootTrim.Length -eq 0) { return $false }
    if ($pathTrim.Equals($rootTrim, [System.StringComparison]::OrdinalIgnoreCase)) { return $true }
    $sep = [string][System.IO.Path]::DirectorySeparatorChar
    $altSep = [string][System.IO.Path]::AltDirectorySeparatorChar
    if ($pathTrim.StartsWith($rootTrim + $sep, [System.StringComparison]::OrdinalIgnoreCase)) { return $true }
    if ($altSep -ne $sep -and $pathTrim.StartsWith($rootTrim + $altSep, [System.StringComparison]::OrdinalIgnoreCase)) { return $true }
    return $false
}

function Get-RelativePath {
    param([string]$BasePath, [string]$FullPath)
    if (-not $BasePath -or -not $FullPath) { return '' }
    $base = ''
    $full = ''
    try { $base = [System.IO.Path]::GetFullPath($BasePath) } catch { return '' }
    try { $full = [System.IO.Path]::GetFullPath($FullPath) } catch { return '' }
    $base = $base.TrimEnd([char[]]@([char]92, [char]47))
    if (-not $base) { return '' }
    $baseWithSep = $base + [string][System.IO.Path]::DirectorySeparatorChar
    if ($full.StartsWith($baseWithSep, [System.StringComparison]::OrdinalIgnoreCase)) {
        return $full.Substring($baseWithSep.Length)
    }
    return ''
}

function ConvertTo-RiftSenseVersion {
    # Parses v?MAJOR.MINOR.PATCH[-prerelease][+build]. Returns $null when invalid.
    param([string]$Text)
    if ($null -eq $Text) { return $null }
    $value = "$Text".Trim()
    if (-not $value) { return $null }
    if ($value -match '^[vV]') { $value = $value.Substring(1) }
    if ($value -notmatch '^(?<major>\d+)\.(?<minor>\d+)\.(?<patch>\d+)(?:-(?<pre>[0-9A-Za-z\.\-]+))?(?:\+(?<build>[0-9A-Za-z\.\-]+))?$') { return $null }
    $major = [int]$matches['major']
    $minor = [int]$matches['minor']
    $patch = [int]$matches['patch']
    $pre = ''
    if ($matches.ContainsKey('pre') -and $matches['pre']) { $pre = "$($matches['pre'])" }
    $normalized = ("{0}.{1}.{2}" -f $major, $minor, $patch)
    if ($pre) { $normalized = $normalized + '-' + $pre }
    return [pscustomobject]@{
        Major = $major
        Minor = $minor
        Patch = $patch
        Prerelease = $pre
        Normalized = $normalized
    }
}

function Compare-RiftSensePrerelease {
    param([string]$A, [string]$B)
    if ((-not $A) -and (-not $B)) { return 0 }
    if (-not $A) { return 1 }
    if (-not $B) { return -1 }
    $pa = "$A".Split('.')
    $pb = "$B".Split('.')
    $count = [math]::Max($pa.Count, $pb.Count)
    for ($i = 0; $i -lt $count; $i++) {
        if ($i -ge $pa.Count) { return -1 }
        if ($i -ge $pb.Count) { return 1 }
        $x = "$($pa[$i])"
        $y = "$($pb[$i])"
        $xn = 0
        $yn = 0
        $xIsNum = [int]::TryParse($x, [ref]$xn)
        $yIsNum = [int]::TryParse($y, [ref]$yn)
        if ($xIsNum -and $yIsNum) {
            if ($xn -gt $yn) { return 1 }
            if ($xn -lt $yn) { return -1 }
        } elseif ($xIsNum) {
            return -1
        } elseif ($yIsNum) {
            return 1
        } else {
            $cmp = [string]::Compare($x, $y, [System.StringComparison]::OrdinalIgnoreCase)
            if ($cmp -gt 0) { return 1 }
            if ($cmp -lt 0) { return -1 }
        }
    }
    return 0
}

function Compare-RiftSenseVersion {
    # -1 when A < B, 0 when equal, 1 when A > B, $null when either is invalid.
    param([string]$A, [string]$B)
    $va = ConvertTo-RiftSenseVersion $A
    $vb = ConvertTo-RiftSenseVersion $B
    if (-not $va -or -not $vb) { return $null }
    if ($va.Major -gt $vb.Major) { return 1 }
    if ($va.Major -lt $vb.Major) { return -1 }
    if ($va.Minor -gt $vb.Minor) { return 1 }
    if ($va.Minor -lt $vb.Minor) { return -1 }
    if ($va.Patch -gt $vb.Patch) { return 1 }
    if ($va.Patch -lt $vb.Patch) { return -1 }
    return (Compare-RiftSensePrerelease -A $va.Prerelease -B $vb.Prerelease)
}

function Test-IsUserStatePath {
    param([string]$RelativePath)
    if (-not $RelativePath) { return $false }
    $rel = "$RelativePath".Replace('\', '/').TrimStart('/').TrimEnd('/')
    if (-not $rel) { return $false }
    foreach ($entry in @(Get-PreservedUserState)) {
        $preserved = "$entry".TrimStart('/').TrimEnd('/')
        if (-not $preserved) { continue }
        if ($rel.Equals($preserved, [System.StringComparison]::OrdinalIgnoreCase)) { return $true }
        if ($rel.StartsWith($preserved + '/', [System.StringComparison]::OrdinalIgnoreCase)) { return $true }
    }
    return $false
}

function Get-AppFileRelativePaths {
    # Expands the app-file patterns against a root directory. Only existing files.
    param([string]$Root)
    $found = New-Object 'System.Collections.Generic.List[string]'
    if (-not $Root -or -not (Test-Path -LiteralPath $Root -PathType Container)) { return @() }
    foreach ($pattern in @(Get-AppFilePatterns)) {
        $osPattern = $pattern.Replace([char]47, [System.IO.Path]::DirectorySeparatorChar)
        $full = Join-Path $Root $osPattern
        if ($osPattern.Contains('*')) {
            foreach ($item in @(Get-ChildItem -Path $full -File -ErrorAction SilentlyContinue)) {
                $rel = Get-RelativePath -BasePath $Root -FullPath $item.FullName
                if ($rel) { [void]$found.Add($rel) }
            }
        } elseif (Test-Path -LiteralPath $full -PathType Leaf) {
            [void]$found.Add($osPattern)
        }
    }
    return @($found | Sort-Object -Unique)
}

function Get-UpdateFileRelativePaths {
    # Union of app files present in the install dir and in staging.
    param([string]$Staged, [string]$InstallDir)
    $set = @{}
    foreach ($root in @($InstallDir, $Staged)) {
        foreach ($rel in @(Get-AppFileRelativePaths -Root $root)) {
            if (Test-IsUserStatePath -RelativePath $rel) {
                Write-UpdateLog ("Ignoring app-file pattern that matches user state: {0}" -f $rel) 'WARN'
                continue
            }
            $set[$rel] = $true
        }
    }
    return @($set.Keys | Sort-Object)
}

function Resolve-DefaultBackupDir {
    param([string]$VersionText)
    $base = $env:LOCALAPPDATA
    if (-not $base) { $base = $env:TEMP }
    if (-not $base) { $base = [System.IO.Path]::GetTempPath() }
    $ver = "$VersionText"
    if (-not $ver) { $ver = 'unknown' }
    $ver = $ver -replace '[^0-9A-Za-z\.\-]', '_'
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    return (Join-Path $base ("RiftSense\backup\previous-" + $ver + '-' + $stamp))
}

function Resolve-DefaultLogDir {
    $base = $env:LOCALAPPDATA
    if (-not $base) { $base = $env:TEMP }
    if (-not $base) { $base = [System.IO.Path]::GetTempPath() }
    return (Join-Path $base 'RiftSense\logs')
}

function Write-UpdateLog {
    param([string]$Message, [string]$Level = 'INFO')
    $stamp = (Get-Date).ToString('yyyy-MM-dd HH:mm:ss')
    $line = "[$stamp] [$Level] $Message"
    if ($script:UpdateApplyLogFile) {
        try { Add-Content -LiteralPath $script:UpdateApplyLogFile -Value $line -Encoding Ascii -ErrorAction SilentlyContinue } catch { }
    }
    Write-Host $line
}

function Get-ProcessCommandLine {
    param([int]$ProcessId)
    try {
        $info = Get-CimInstance Win32_Process -Filter ("ProcessId = {0}" -f $ProcessId) -ErrorAction Stop
        if ($info -and $info.CommandLine) { return "$($info.CommandLine)" }
    } catch { }
    return ''
}

function Test-ProcessInInstallDir {
    # A process belongs to the install only when its executable or command line
    # resolves inside the install directory.
    param([int]$ProcessId, [string]$InstallDir)
    if ($ProcessId -le 0 -or -not $InstallDir) { return $false }
    try {
        $proc = Get-Process -Id $ProcessId -ErrorAction Stop
        $exePath = "$($proc.Path)"
        if ($exePath -and (Test-PathInsideRoot -Path $exePath -Root $InstallDir)) { return $true }
    } catch { }
    $cmdLine = Get-ProcessCommandLine -ProcessId $ProcessId
    if ($cmdLine) {
        $needle = ConvertTo-NormalizedPath $InstallDir
        $hay = ConvertTo-NormalizedPath $cmdLine
        if ($needle -and $hay -and $hay.ToLowerInvariant().Contains($needle.ToLowerInvariant())) { return $true }
    }
    return $false
}

function Stop-InstallProcesses {
    param(
        [string]$InstallDir,
        [int[]]$WaitPid = @(),
        [int]$TimeoutSeconds = 30
    )
    $timeout = $TimeoutSeconds
    if ($timeout -lt 1) { $timeout = 30 }
    $selfId = $PID
    $parentId = 0
    try {
        $info = Get-CimInstance Win32_Process -Filter ("ProcessId = {0}" -f $PID) -ErrorAction Stop
        if ($info) { $parentId = [int]$info.ParentProcessId }
    } catch { $parentId = 0 }

    $waitList = @()
    if ($WaitPid) {
        foreach ($procId in @($WaitPid)) {
            $id = 0
            try { $id = [int]$procId } catch { $id = 0 }
            if ($id -gt 0 -and $id -ne $selfId -and $id -ne $parentId) { $waitList += $id }
        }
        $waitList = @($waitList | Sort-Object -Unique)
    }

    if ($waitList.Count -gt 0) {
        $deadline = (Get-Date).AddSeconds($timeout)
        foreach ($procId in $waitList) {
            $alive = $true
            while ($alive -and (Get-Date) -lt $deadline) {
                $alive = $false
                try {
                    $proc = Get-Process -Id $procId -ErrorAction Stop
                    if (-not $proc.HasExited) { $alive = $true }
                } catch { $alive = $false }
                if ($alive) { Start-Sleep -Milliseconds 250 }
            }
            if ($alive) {
                Write-UpdateLog ("PID {0} did not exit within {1}s" -f $procId, $timeout) 'WARN'
            } else {
                Write-UpdateLog ("PID {0} exited" -f $procId)
            }
        }
    }

    $targets = New-Object 'System.Collections.Generic.List[int]'
    foreach ($procId in $waitList) {
        $alive = $false
        try {
            $proc = Get-Process -Id $procId -ErrorAction Stop
            if (-not $proc.HasExited) { $alive = $true }
        } catch { $alive = $false }
        if (-not $alive) { continue }
        if (Test-ProcessInInstallDir -ProcessId $procId -InstallDir $InstallDir) {
            [void]$targets.Add($procId)
        } else {
            Write-UpdateLog ("Not stopping PID {0}: executable and command line are outside the install dir" -f $procId) 'WARN'
        }
    }

    try {
        foreach ($proc in @(Get-Process -ErrorAction SilentlyContinue)) {
            $procId = 0
            try { $procId = [int]$proc.Id } catch { continue }
            if ($procId -le 0 -or $procId -eq $selfId -or $procId -eq $parentId) { continue }
            if ($targets.Contains($procId)) { continue }
            $exePath = ''
            try { $exePath = "$($proc.Path)" } catch { $exePath = '' }
            if ($exePath -and (Test-PathInsideRoot -Path $exePath -Root $InstallDir)) {
                [void]$targets.Add($procId)
            }
        }
    } catch { }

    if ($targets.Count -eq 0) {
        Write-UpdateLog 'No running RiftSense processes found inside the install dir.'
        return
    }
    foreach ($procId in @($targets)) {
        try {
            Stop-Process -Id $procId -Force -ErrorAction Stop
            Write-UpdateLog ("Stopped process {0}" -f $procId)
        } catch {
            Write-UpdateLog ("Could not stop process {0}: {1}" -f $procId, $_.Exception.Message) 'WARN'
        }
    }
}

function Restore-UpdateBackup {
    param([string]$BackupDir, [string]$InstallDir, [string]$Staged)
    if (-not $BackupDir -or -not (Test-Path -LiteralPath $BackupDir -PathType Container)) {
        Write-UpdateLog 'No backup available to restore.' 'ERROR'
        return
    }
    Write-UpdateLog ("Rolling back from {0}" -f $BackupDir) 'WARN'
    $restored = 0
    $backupRels = @{}
    foreach ($item in @(Get-ChildItem -LiteralPath $BackupDir -Recurse -File -ErrorAction SilentlyContinue)) {
        $rel = Get-RelativePath -BasePath $BackupDir -FullPath $item.FullName
        if (-not $rel) { continue }
        $backupRels[("$rel".Replace('\', '/'))] = $true
        $dest = Join-Path $InstallDir $rel
        try {
            $parent = Split-Path -Parent $dest
            if ($parent -and -not (Test-Path -LiteralPath $parent)) { New-Item -ItemType Directory -Path $parent -Force | Out-Null }
            Copy-Item -LiteralPath $item.FullName -Destination $dest -Force -ErrorAction Stop
            $restored++
        } catch {
            Write-UpdateLog ("Rollback failed for {0}: {1}" -f $rel, $_.Exception.Message) 'ERROR'
        }
    }
    $removed = 0
    foreach ($rel in @(Get-UpdateFileRelativePaths -Staged $Staged -InstallDir $InstallDir)) {
        $key = "$rel".Replace('\', '/')
        if ($backupRels.ContainsKey($key)) { continue }
        $target = Join-Path $InstallDir $rel
        if (Test-Path -LiteralPath $target -PathType Leaf) {
            try {
                Remove-Item -LiteralPath $target -Force -ErrorAction Stop
                $removed++
            } catch {
                Write-UpdateLog ("Rollback could not remove {0}: {1}" -f $rel, $_.Exception.Message) 'ERROR'
            }
        }
    }
    if ($removed -gt 0) {
        Write-UpdateLog ("Rollback removed {0} file(s) that were added by the update." -f $removed) 'WARN'
    }
    Write-UpdateLog ("Rollback restored {0} file(s)." -f $restored) 'WARN'
}

function Remove-UpdateStaging {
    param([string]$Staged, [string]$InstallDir)
    if (-not $Staged) { return }
    if (-not (Test-Path -LiteralPath $Staged -PathType Container)) { return }
    $stagedFull = ConvertTo-NormalizedPath ([System.IO.Path]::GetFullPath($Staged))
    $installFull = ConvertTo-NormalizedPath ([System.IO.Path]::GetFullPath($InstallDir))
    if ($stagedFull -ieq $installFull) {
        Write-UpdateLog 'Refusing to delete staging directory equal to the install dir.' 'WARN'
        return
    }
    $sep = [string][System.IO.Path]::DirectorySeparatorChar
    if ($installFull.StartsWith($stagedFull + $sep, [System.StringComparison]::OrdinalIgnoreCase)) {
        Write-UpdateLog 'Refusing to delete a staging directory that contains the install dir.' 'WARN'
        return
    }
    try {
        Remove-Item -LiteralPath $Staged -Recurse -Force -ErrorAction Stop
        Write-UpdateLog ("Deleted staging directory {0}" -f $Staged)
    } catch {
        Write-UpdateLog ("Could not delete staging directory: {0}" -f $_.Exception.Message) 'WARN'
    }
}

function Start-RiftSenseApps {
    param([string]$InstallDir)
    foreach ($name in @('Start-AutoCoach.cmd', 'Start-Ui.cmd')) {
        $exe = Join-Path $InstallDir $name
        if (-not (Test-Path -LiteralPath $exe -PathType Leaf)) {
            Write-UpdateLog ("Relaunch skipped (missing): {0}" -f $name) 'WARN'
            continue
        }
        try {
            Start-Process -FilePath $exe -WorkingDirectory $InstallDir -ErrorAction Stop | Out-Null
            Write-UpdateLog ("Relaunched {0}" -f $name)
        } catch {
            Write-UpdateLog ("Relaunch failed for {0}: {1}" -f $name, $_.Exception.Message) 'ERROR'
        }
    }
}

function Get-SumsMap {
    param([string]$SumsPath)
    $map = @{}
    foreach ($line in @(Get-Content -LiteralPath $SumsPath -ErrorAction SilentlyContinue)) {
        if ("$line" -match '^\s*([0-9a-fA-F]{64})\s+\*?(.+?)\s*$') {
            $hash = "$($matches[1])".ToLowerInvariant()
            $name = "$($matches[2])".Trim().Replace('\', '/')
            $name = $name.TrimStart('.', '/')
            if ($name) { $map[$name] = $hash }
        }
    }
    return $map
}

function Invoke-UpdateApply {
    param(
        [string]$Staged,
        [string]$InstallDir,
        [string]$Version,
        [string]$BackupDir,
        [string]$LogDir,
        [int[]]$WaitPid = @(),
        [switch]$Restart,
        [switch]$WhatIf,
        [switch]$SkipProcessStop,
        [int]$WaitTimeoutSeconds = 30
    )
    $ErrorActionPreference = 'Stop'
    $script:UpdateApplyLogFile = ''

    if (-not $LogDir) { $LogDir = Resolve-DefaultLogDir }
    try {
        if (-not (Test-Path -LiteralPath $LogDir -PathType Container)) {
            New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
        }
        $script:UpdateApplyLogFile = Join-Path $LogDir ('update-' + (Get-Date -Format 'yyyyMMdd') + '.log')
    } catch {
        $script:UpdateApplyLogFile = ''
    }

    Write-UpdateLog ("RiftSense update helper start (pid={0}, whatIf={1}, restart={2})" -f $PID, [bool]$WhatIf, [bool]$Restart)

    # ---- validation (exit 2) ----
    if (-not $Staged) {
        Write-UpdateLog 'Validation: -Staged is required.' 'ERROR'
        return 2
    }
    if (-not $InstallDir) {
        Write-UpdateLog 'Validation: -InstallDir is required.' 'ERROR'
        return 2
    }
    if (-not (Test-Path -LiteralPath $Staged -PathType Container)) {
        Write-UpdateLog ("Validation: staging directory not found: {0}" -f $Staged) 'ERROR'
        return 2
    }
    if (-not (Test-Path -LiteralPath $InstallDir -PathType Container)) {
        Write-UpdateLog ("Validation: install directory not found: {0}" -f $InstallDir) 'ERROR'
        return 2
    }
    $stagedFull = ConvertTo-NormalizedPath ([System.IO.Path]::GetFullPath($Staged))
    $installFull = ConvertTo-NormalizedPath ([System.IO.Path]::GetFullPath($InstallDir))
    if ($stagedFull -ieq $installFull) {
        Write-UpdateLog 'Validation: staging and install directories must differ.' 'ERROR'
        return 2
    }

    $stagedVersionFile = Join-Path $Staged 'VERSION'
    if (-not (Test-Path -LiteralPath $stagedVersionFile -PathType Leaf)) {
        Write-UpdateLog 'Validation: staged VERSION file is missing.' 'ERROR'
        return 2
    }
    $stagedVersionText = ''
    try {
        $stagedVersionText = "$(Get-Content -LiteralPath $stagedVersionFile -Raw -Encoding UTF8)".Trim()
    } catch {
        Write-UpdateLog ("Validation: could not read staged VERSION: {0}" -f $_.Exception.Message) 'ERROR'
        return 2
    }
    $parsedStaged = ConvertTo-RiftSenseVersion $stagedVersionText
    if (-not $parsedStaged) {
        Write-UpdateLog ("Validation: staged VERSION is not valid semver: {0}" -f $stagedVersionText) 'ERROR'
        return 2
    }
    if ($Version) {
        $parsedGiven = ConvertTo-RiftSenseVersion $Version
        if (-not $parsedGiven) {
            Write-UpdateLog ("Validation: -Version is not valid semver: {0}" -f $Version) 'ERROR'
            return 2
        }
        if ($parsedGiven.Normalized -ne $parsedStaged.Normalized) {
            Write-UpdateLog ("Validation: -Version {0} does not match staged VERSION {1}" -f $parsedGiven.Normalized, $parsedStaged.Normalized) 'ERROR'
            return 2
        }
    }

    $installedVersionText = ''
    $installedVersionFile = Join-Path $InstallDir 'VERSION'
    if (Test-Path -LiteralPath $installedVersionFile -PathType Leaf) {
        try { $installedVersionText = "$(Get-Content -LiteralPath $installedVersionFile -Raw -Encoding UTF8)".Trim() } catch { $installedVersionText = '' }
    }
    if ($installedVersionText) {
        $cmp = Compare-RiftSenseVersion -A $parsedStaged.Normalized -B $installedVersionText
        if ($null -eq $cmp) {
            Write-UpdateLog ("Validation: installed VERSION is not valid semver: {0}" -f $installedVersionText) 'ERROR'
            return 2
        }
        if ($cmp -le 0) {
            Write-UpdateLog ("Validation: staged version {0} is not newer than installed {1}; refusing downgrade" -f $parsedStaged.Normalized, $installedVersionText) 'ERROR'
            return 2
        }
    } else {
        Write-UpdateLog 'Installed VERSION is missing; treating the install as 0.0.0 (pre-version install).' 'WARN'
    }

    if (-not $BackupDir) { $BackupDir = Resolve-DefaultBackupDir -VersionText $parsedStaged.Normalized }
    $backupFull = ConvertTo-NormalizedPath ([System.IO.Path]::GetFullPath($BackupDir))

    $relPaths = @(Get-UpdateFileRelativePaths -Staged $Staged -InstallDir $InstallDir)
    Write-UpdateLog ("App files considered: {0}" -f (($relPaths) -join ', '))
    Write-UpdateLog ("User state preserved: {0}" -f ((Get-PreservedUserState) -join ', '))

    if ($WhatIf) {
        Write-UpdateLog 'WHATIF preview: no files will be changed.'
        foreach ($rel in $relPaths) {
            $src = Join-Path $Staged $rel
            $dst = Join-Path $InstallDir $rel
            $hasSrc = Test-Path -LiteralPath $src -PathType Leaf
            $hasDst = Test-Path -LiteralPath $dst -PathType Leaf
            if ($hasDst) { Write-UpdateLog ("WHATIF would back up {0}" -f $rel) }
            if ($hasSrc) { Write-UpdateLog ("WHATIF would replace {0}" -f $rel) }
        }
        Write-UpdateLog ("WHATIF backup dir would be {0}" -f $backupFull)
        Write-UpdateLog ("WHATIF would delete staging on success: {0}" -f $Staged)
        if ($Restart) { Write-UpdateLog 'WHATIF would relaunch Start-AutoCoach.cmd and Start-Ui.cmd.' }
        return 0
    }

    $markerPath = Join-Path $InstallDir '.update_in_progress'
    try {
        if (-not $SkipProcessStop) {
            Write-UpdateLog 'Waiting for and stopping RiftSense processes (install-scoped only).'
            Stop-InstallProcesses -InstallDir $InstallDir -WaitPid $WaitPid -TimeoutSeconds $WaitTimeoutSeconds
        } else {
            Write-UpdateLog 'Process handling skipped by request.' 'WARN'
        }

        if (Test-Path -LiteralPath $markerPath -PathType Leaf) {
            Write-UpdateLog 'An existing .update_in_progress marker was found; overwriting.' 'WARN'
        }
        $markerData = [ordered]@{
            helper = 'update_apply.ps1'
            pid = $PID
            version = $parsedStaged.Normalized
            staged = $stagedFull
            installDir = $installFull
            backupDir = $backupFull
            startedAt = (Get-Date).ToUniversalTime().ToString('o')
        }
        Set-Content -LiteralPath $markerPath -Value ($markerData | ConvertTo-Json -Compress) -Encoding Ascii -Force
        Write-UpdateLog ("Created marker {0}" -f $markerPath)

        $backedUp = New-Object 'System.Collections.Generic.List[string]'
        foreach ($rel in $relPaths) {
            $src = Join-Path $InstallDir $rel
            if (-not (Test-Path -LiteralPath $src -PathType Leaf)) { continue }
            $dst = Join-Path $BackupDir $rel
            $parent = Split-Path -Parent $dst
            if ($parent -and -not (Test-Path -LiteralPath $parent)) { New-Item -ItemType Directory -Path $parent -Force | Out-Null }
            Copy-Item -LiteralPath $src -Destination $dst -Force
            [void]$backedUp.Add($rel)
        }
        Write-UpdateLog ("Backed up {0} file(s) to {1}" -f $backedUp.Count, $backupFull)

        $copied = New-Object 'System.Collections.Generic.List[string]'
        foreach ($rel in $relPaths) {
            $src = Join-Path $Staged $rel
            if (-not (Test-Path -LiteralPath $src -PathType Leaf)) {
                Write-UpdateLog ("Staged file missing; keeping current file: {0}" -f $rel) 'WARN'
                continue
            }
            $dst = Join-Path $InstallDir $rel
            $parent = Split-Path -Parent $dst
            if ($parent -and -not (Test-Path -LiteralPath $parent)) { New-Item -ItemType Directory -Path $parent -Force | Out-Null }
            Copy-Item -LiteralPath $src -Destination $dst -Force
            [void]$copied.Add($rel)
        }
        Write-UpdateLog ("Replaced {0} file(s) from staging" -f $copied.Count)
        if ($copied.Count -eq 0) { throw 'no files were copied from staging' }

        $sumsPath = Join-Path $Staged 'SHA256SUMS.txt'
        if (Test-Path -LiteralPath $sumsPath -PathType Leaf) {
            $sums = Get-SumsMap -SumsPath $sumsPath
            $verified = 0
            $uncovered = New-Object 'System.Collections.Generic.List[string]'
            foreach ($rel in $copied) {
                $key = "$rel".Replace('\', '/')
                if (-not $sums.ContainsKey($key)) {
                    [void]$uncovered.Add($rel)
                    continue
                }
                $destPath = Join-Path $InstallDir $rel
                $actual = "$((Get-FileHash -LiteralPath $destPath -Algorithm SHA256 -ErrorAction Stop).Hash)".ToLowerInvariant()
                if ($actual -ne "$($sums[$key])") {
                    throw ("SHA256 mismatch for {0}" -f $rel)
                }
                $verified++
            }
            if ($verified -eq 0) {
                # Release SHA256SUMS.txt may list only the downloaded archive, not
                # its contents; the archive itself was verified before extraction.
                Write-UpdateLog 'SHA256SUMS.txt does not cover any replaced file; per-file verification skipped.' 'WARN'
            } else {
                if ($uncovered.Count -gt 0) {
                    Write-UpdateLog ("{0} replaced file(s) were not covered by SHA256SUMS.txt" -f $uncovered.Count) 'WARN'
                }
                Write-UpdateLog ("Verified {0} file(s) against SHA256SUMS.txt" -f $verified)
            }
        } else {
            Write-UpdateLog 'SHA256SUMS.txt not present in staging; hash verification skipped.' 'WARN'
        }

        $installedNowRaw = "$(Get-Content -LiteralPath (Join-Path $InstallDir 'VERSION') -Raw -Encoding UTF8)".Trim()
        $installedNow = ConvertTo-RiftSenseVersion $installedNowRaw
        if (-not $installedNow -or ($installedNow.Normalized -ne $parsedStaged.Normalized)) {
            throw ("installed VERSION is {0} after copy, expected {1}" -f $installedNowRaw, $parsedStaged.Normalized)
        }

        Remove-Item -LiteralPath $markerPath -Force -ErrorAction SilentlyContinue
        Write-UpdateLog ("Update to {0} applied successfully" -f $parsedStaged.Normalized)
        Remove-UpdateStaging -Staged $stagedFull -InstallDir $installFull
        if ($Restart) { Start-RiftSenseApps -InstallDir $InstallDir }
        return 0
    } catch {
        Write-UpdateLog ("Update failed: {0}" -f $_.Exception.Message) 'ERROR'
        try { Restore-UpdateBackup -BackupDir $BackupDir -InstallDir $InstallDir -Staged $Staged } catch {
            Write-UpdateLog ("Rollback error: {0}" -f $_.Exception.Message) 'ERROR'
        }
        Remove-Item -LiteralPath $markerPath -Force -ErrorAction SilentlyContinue
        if ($Restart) { Start-RiftSenseApps -InstallDir $InstallDir }
        return 1
    }
}

# Main: run only when executed, not when dot-sourced (tests dot-source this file).
if ($MyInvocation.InvocationName -ne '.') {
    $applyExitCode = Invoke-UpdateApply -Staged $Staged -InstallDir $InstallDir -Version $Version -BackupDir $BackupDir -LogDir $LogDir -WaitPid $WaitPid -Restart:$Restart -WhatIf:$WhatIf
    exit $applyExitCode
}
