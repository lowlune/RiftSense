# Pester-style tests for ui/update_apply.ps1.
#
# Static and isolated checks only: no network, no administrator rights, and no
# running RiftSense processes are required. Conservative PowerShell 5.1 syntax
# and Pester 4.4+ / Pester 5 compatible assertions (no Pester 5-only features).

Describe 'RiftSense update_apply helper' {
    BeforeAll {
        $scriptPath = Join-Path $PSScriptRoot 'update_apply.ps1'
        . $scriptPath
    }

    Context 'static contract' {
        It 'exists and dot-sources without executing the apply' {
            (Test-Path -LiteralPath $scriptPath -PathType Leaf) | Should -BeTrue
            ([string]::IsNullOrEmpty($script:UpdateApplyLogFile)) | Should -BeTrue
        }

        It 'is ASCII only' {
            $bytes = [System.IO.File]::ReadAllBytes($scriptPath)
            @($bytes | Where-Object { $_ -gt 127 }).Count | Should -Be 0
        }

        It 'declares the documented parameters' {
            $command = Get-Command -Name $scriptPath
            foreach ($name in @('Staged', 'InstallDir', 'Version', 'BackupDir', 'LogDir', 'WaitPid', 'Restart', 'WhatIf')) {
                $command.Parameters.ContainsKey($name) | Should -BeTrue
            }
        }

        It 'does not implement package-manager updates' {
            (Get-Content -LiteralPath $scriptPath -Raw) -notmatch '(?i)\bwinget\b|\bscoop\b' | Should -BeTrue
        }
    }

    Context 'app file and user state lists' {
        It 'lists the app files that are replaced on update' {
            $patterns = @(Get-AppFilePatterns)
            foreach ($expected in @('AutoCoach.ps1', 'Get-LiveGame.ps1', 'common.ps1', 'Start-AutoCoach.cmd', 'Start-Ui.cmd', 'VERSION', 'install.ps1', 'ui/*.py', 'ui/*.html', 'agent/lol-coach.md', 'knowledge/packs/*.json', 'LICENSE', 'README.md')) {
                $patterns | Should -Contain $expected
            }
        }

        It 'lists user state that is never replaced' {
            $preserved = @(Get-PreservedUserState)
            foreach ($expected in @('build_intent.txt', 'build_intent.txt.bak', 'coach_latest.txt', 'coach_latest.json', 'death_latest.txt', 'death_latest.json', 'game_epoch.json', 'dashboard_token.txt', 'update_state.json', 'ui/data', 'champion.json', 'items.json', 'knowledge/packs/custom', '.git', '.update_in_progress')) {
                $preserved | Should -Contain $expected
            }
        }

        It 'has no wildcards in the user state list' {
            foreach ($entry in @(Get-PreservedUserState)) {
                ($entry.Contains('*')) | Should -BeFalse
            }
        }

        It 'classifies user state paths correctly' {
            (Test-IsUserStatePath -RelativePath 'build_intent.txt') | Should -BeTrue
            (Test-IsUserStatePath -RelativePath 'build_intent.txt.bak') | Should -BeTrue
            (Test-IsUserStatePath -RelativePath 'coach_latest.json') | Should -BeTrue
            (Test-IsUserStatePath -RelativePath 'game_epoch.json') | Should -BeTrue
            (Test-IsUserStatePath -RelativePath 'dashboard_token.txt') | Should -BeTrue
            (Test-IsUserStatePath -RelativePath 'update_state.json') | Should -BeTrue
            (Test-IsUserStatePath -RelativePath 'champion.json') | Should -BeTrue
            (Test-IsUserStatePath -RelativePath 'items.json') | Should -BeTrue
            (Test-IsUserStatePath -RelativePath 'ui/data/timeline.db') | Should -BeTrue
            (Test-IsUserStatePath -RelativePath 'ui\data\timeline.db') | Should -BeTrue
            (Test-IsUserStatePath -RelativePath 'knowledge/packs/custom/my-pack.json') | Should -BeTrue
            (Test-IsUserStatePath -RelativePath '.git/HEAD') | Should -BeTrue
            (Test-IsUserStatePath -RelativePath 'AutoCoach.ps1') | Should -BeFalse
            (Test-IsUserStatePath -RelativePath 'VERSION') | Should -BeFalse
            (Test-IsUserStatePath -RelativePath 'ui/server.py') | Should -BeFalse
            (Test-IsUserStatePath -RelativePath 'knowledge/packs/garen.json') | Should -BeFalse
        }

        It 'never expands an app-file pattern into user state' {
            $install = Join-Path $TestDrive 'install-state'
            $staged = Join-Path $TestDrive 'staged-state'
            New-Item -ItemType Directory -Path $install -Force | Out-Null
            New-Item -ItemType Directory -Path $staged -Force | Out-Null
            New-Item -ItemType Directory -Path (Join-Path $install (Join-Path 'knowledge' (Join-Path 'packs' 'custom'))) -Force | Out-Null
            Set-Content -LiteralPath (Join-Path $install 'build_intent.txt') -Value 'plan' -Encoding Ascii
            Set-Content -LiteralPath (Join-Path $install 'coach_latest.json') -Value '{}' -Encoding Ascii
            Set-Content -LiteralPath (Join-Path $install (Join-Path 'knowledge' (Join-Path 'packs' 'builtin.json'))) -Value '{}' -Encoding Ascii
            Set-Content -LiteralPath (Join-Path $install (Join-Path 'knowledge' (Join-Path 'packs' (Join-Path 'custom' 'mine.json')))) -Value '{}' -Encoding Ascii

            $rels = @(Get-UpdateFileRelativePaths -Staged $staged -InstallDir $install)
            foreach ($rel in $rels) {
                (Test-IsUserStatePath -RelativePath $rel) | Should -BeFalse
            }
            @($rels | Where-Object { $_ -like '*builtin.json' }).Count | Should -Be 1
            @($rels | Where-Object { $_ -like '*custom*' }).Count | Should -Be 0
        }
    }

    Context 'path-inside-root guard' {
        It 'accepts the root itself and descendants' {
            $root = Join-Path $TestDrive 'guard-root'
            $inside = Join-Path (Join-Path $root 'ui') 'server.py'
            (Test-PathInsideRoot -Path $inside -Root $root) | Should -BeTrue
            (Test-PathInsideRoot -Path $root -Root $root) | Should -BeTrue
            (Test-PathInsideRoot -Path ($root + [System.IO.Path]::DirectorySeparatorChar) -Root $root) | Should -BeTrue
        }

        It 'rejects siblings and prefix lookalikes' {
            $root = Join-Path $TestDrive 'guard-root'
            $sibling = Join-Path $TestDrive 'guard-root2'
            $other = Join-Path $TestDrive 'somewhere-else'
            (Test-PathInsideRoot -Path $sibling -Root $root) | Should -BeFalse
            (Test-PathInsideRoot -Path (Join-Path $sibling 'server.py') -Root $root) | Should -BeFalse
            (Test-PathInsideRoot -Path $other -Root $root) | Should -BeFalse
        }

        It 'handles empty input' {
            (Test-PathInsideRoot -Path '' -Root (Join-Path $TestDrive 'guard-root')) | Should -BeFalse
            (Test-PathInsideRoot -Path (Join-Path $TestDrive 'guard-root') -Root '') | Should -BeFalse
        }

        It 'computes relative paths inside the root' {
            $root = Join-Path $TestDrive 'guard-root'
            $file = Join-Path (Join-Path $root 'ui') 'server.py'
            (Get-RelativePath -BasePath $root -FullPath $file) | Should -Be (Join-Path 'ui' 'server.py')
            (Get-RelativePath -BasePath $root -FullPath (Join-Path $TestDrive 'elsewhere')) | Should -Be ''
        }
    }

    Context 'version comparison' {
        It 'parses valid and rejects invalid versions' {
            (ConvertTo-RiftSenseVersion '1.2.3') | Should -Not -BeNullOrEmpty
            (ConvertTo-RiftSenseVersion '1.2.3').Normalized | Should -Be '1.2.3'
            (ConvertTo-RiftSenseVersion 'v1.2.3-beta.1').Prerelease | Should -Be 'beta.1'
            (ConvertTo-RiftSenseVersion '1.2') | Should -BeNullOrEmpty
            (ConvertTo-RiftSenseVersion 'nope') | Should -BeNullOrEmpty
            (ConvertTo-RiftSenseVersion '') | Should -BeNullOrEmpty
        }

        It 'compares numeric parts' {
            (Compare-RiftSenseVersion -A '1.2.3' -B '1.2.4') | Should -Be -1
            (Compare-RiftSenseVersion -A '1.2.3' -B '1.2.3') | Should -Be 0
            (Compare-RiftSenseVersion -A '2.0.0' -B '1.9.9') | Should -Be 1
            (Compare-RiftSenseVersion -A 'v1.2.3' -B '1.2.3') | Should -Be 0
            (Compare-RiftSenseVersion -A 'bad' -B '1.0.0') | Should -BeNullOrEmpty
        }

        It 'treats a release as newer than its prerelease' {
            (Compare-RiftSenseVersion -A '1.2.3' -B '1.2.3-beta.1') | Should -Be 1
            (Compare-RiftSenseVersion -A '1.2.3-beta.1' -B '1.2.3') | Should -Be -1
        }

        It 'orders prerelease identifiers per semver' {
            (Compare-RiftSenseVersion -A '1.2.3-beta.2' -B '1.2.3-beta.1') | Should -Be 1
            (Compare-RiftSenseVersion -A '1.2.3-beta.10' -B '1.2.3-beta.9') | Should -Be 1
            (Compare-RiftSenseVersion -A '1.2.3-beta.1' -B '1.2.3-beta.1.1') | Should -Be -1
        }
    }

    Context 'apply and rollback' {
        It 'applies an update, preserves user state, and deletes staging' {
            $install = Join-Path $TestDrive 'install-ok'
            $staged = Join-Path $TestDrive 'staged-ok'
            $backup = Join-Path $TestDrive 'backup-ok'
            $logs = Join-Path $TestDrive 'logs-ok'
            New-Item -ItemType Directory -Path $install -Force | Out-Null
            New-Item -ItemType Directory -Path $staged -Force | Out-Null
            New-Item -ItemType Directory -Path (Join-Path $install 'ui') -Force | Out-Null
            New-Item -ItemType Directory -Path (Join-Path $staged 'ui') -Force | Out-Null
            Set-Content -LiteralPath (Join-Path $install 'VERSION') -Value '1.0.0' -Encoding Ascii
            Set-Content -LiteralPath (Join-Path $install 'AutoCoach.ps1') -Value 'old-coach' -Encoding Ascii
            Set-Content -LiteralPath (Join-Path $install 'build_intent.txt') -Value 'MY PLAN' -Encoding Ascii
            Set-Content -LiteralPath (Join-Path $install (Join-Path 'ui' 'server.py')) -Value 'old-server' -Encoding Ascii
            Set-Content -LiteralPath (Join-Path $staged 'VERSION') -Value '2.0.0' -Encoding Ascii
            Set-Content -LiteralPath (Join-Path $staged 'AutoCoach.ps1') -Value 'new-coach' -Encoding Ascii
            Set-Content -LiteralPath (Join-Path $staged (Join-Path 'ui' 'server.py')) -Value 'new-server' -Encoding Ascii

            $code = Invoke-UpdateApply -Staged $staged -InstallDir $install -Version '2.0.0' -BackupDir $backup -LogDir $logs -SkipProcessStop
            $code | Should -Be 0
            (Get-Content -LiteralPath (Join-Path $install 'AutoCoach.ps1') -Raw).Trim() | Should -Be 'new-coach'
            (Get-Content -LiteralPath (Join-Path $install (Join-Path 'ui' 'server.py')) -Raw).Trim() | Should -Be 'new-server'
            (Get-Content -LiteralPath (Join-Path $install 'VERSION') -Raw).Trim() | Should -Be '2.0.0'
            (Get-Content -LiteralPath (Join-Path $install 'build_intent.txt') -Raw).Trim() | Should -Be 'MY PLAN'
            (Test-Path -LiteralPath (Join-Path $install '.update_in_progress')) | Should -BeFalse
            (Test-Path -LiteralPath $staged) | Should -BeFalse
            (Test-Path -LiteralPath (Join-Path $backup 'AutoCoach.ps1')) | Should -BeTrue
            (Get-Content -LiteralPath (Join-Path $backup 'AutoCoach.ps1') -Raw).Trim() | Should -Be 'old-coach'
        }

        It 'rolls back everything when hash verification fails' {
            $install = Join-Path $TestDrive 'install-bad'
            $staged = Join-Path $TestDrive 'staged-bad'
            $backup = Join-Path $TestDrive 'backup-bad'
            $logs = Join-Path $TestDrive 'logs-bad'
            New-Item -ItemType Directory -Path $install -Force | Out-Null
            New-Item -ItemType Directory -Path $staged -Force | Out-Null
            Set-Content -LiteralPath (Join-Path $install 'VERSION') -Value '1.0.0' -Encoding Ascii
            Set-Content -LiteralPath (Join-Path $install 'AutoCoach.ps1') -Value 'old-coach' -Encoding Ascii
            Set-Content -LiteralPath (Join-Path $install 'build_intent.txt') -Value 'MY PLAN' -Encoding Ascii
            Set-Content -LiteralPath (Join-Path $staged 'VERSION') -Value '2.0.0' -Encoding Ascii
            Set-Content -LiteralPath (Join-Path $staged 'AutoCoach.ps1') -Value 'new-coach' -Encoding Ascii
            Set-Content -LiteralPath (Join-Path $staged 'SHA256SUMS.txt') -Value '0000000000000000000000000000000000000000000000000000000000000000  AutoCoach.ps1' -Encoding Ascii

            $code = Invoke-UpdateApply -Staged $staged -InstallDir $install -Version '2.0.0' -BackupDir $backup -LogDir $logs -SkipProcessStop
            $code | Should -Be 1
            (Get-Content -LiteralPath (Join-Path $install 'AutoCoach.ps1') -Raw).Trim() | Should -Be 'old-coach'
            (Get-Content -LiteralPath (Join-Path $install 'VERSION') -Raw).Trim() | Should -Be '1.0.0'
            (Get-Content -LiteralPath (Join-Path $install 'build_intent.txt') -Raw).Trim() | Should -Be 'MY PLAN'
            (Test-Path -LiteralPath (Join-Path $install '.update_in_progress')) | Should -BeFalse
            (Test-Path -LiteralPath $staged) | Should -BeTrue
        }

        It 'accepts an asset-level SHA256SUMS.txt that covers no replaced file' {
            $install = Join-Path $TestDrive 'install-sums'
            $staged = Join-Path $TestDrive 'staged-sums'
            $backup = Join-Path $TestDrive 'backup-sums'
            New-Item -ItemType Directory -Path $install -Force | Out-Null
            New-Item -ItemType Directory -Path $staged -Force | Out-Null
            Set-Content -LiteralPath (Join-Path $install 'VERSION') -Value '1.0.0' -Encoding Ascii
            Set-Content -LiteralPath (Join-Path $install 'AutoCoach.ps1') -Value 'old-coach' -Encoding Ascii
            Set-Content -LiteralPath (Join-Path $staged 'VERSION') -Value '2.0.0' -Encoding Ascii
            Set-Content -LiteralPath (Join-Path $staged 'AutoCoach.ps1') -Value 'new-coach' -Encoding Ascii
            Set-Content -LiteralPath (Join-Path $staged 'SHA256SUMS.txt') -Value '1111111111111111111111111111111111111111111111111111111111111111  RiftSense-win-x64-v2.0.0.zip' -Encoding Ascii

            $code = Invoke-UpdateApply -Staged $staged -InstallDir $install -Version '2.0.0' -BackupDir $backup -LogDir (Join-Path $TestDrive 'logs-sums') -SkipProcessStop
            $code | Should -Be 0
            (Get-Content -LiteralPath (Join-Path $install 'AutoCoach.ps1') -Raw).Trim() | Should -Be 'new-coach'
            (Get-Content -LiteralPath (Join-Path $install 'VERSION') -Raw).Trim() | Should -Be '2.0.0'
            (Test-Path -LiteralPath $staged) | Should -BeFalse
        }

        It 'refuses to apply an older or equal version' {
            $install = Join-Path $TestDrive 'install-old'
            $staged = Join-Path $TestDrive 'staged-old'
            New-Item -ItemType Directory -Path $install -Force | Out-Null
            New-Item -ItemType Directory -Path $staged -Force | Out-Null
            Set-Content -LiteralPath (Join-Path $install 'VERSION') -Value '3.0.0' -Encoding Ascii
            Set-Content -LiteralPath (Join-Path $install 'AutoCoach.ps1') -Value 'old-coach' -Encoding Ascii
            Set-Content -LiteralPath (Join-Path $staged 'VERSION') -Value '2.0.0' -Encoding Ascii
            Set-Content -LiteralPath (Join-Path $staged 'AutoCoach.ps1') -Value 'new-coach' -Encoding Ascii

            $code = Invoke-UpdateApply -Staged $staged -InstallDir $install -Version '2.0.0' -BackupDir (Join-Path $TestDrive 'backup-old') -LogDir (Join-Path $TestDrive 'logs-old') -SkipProcessStop
            $code | Should -Be 2
            (Get-Content -LiteralPath (Join-Path $install 'AutoCoach.ps1') -Raw).Trim() | Should -Be 'old-coach'
            (Test-Path -LiteralPath (Join-Path $install '.update_in_progress')) | Should -BeFalse
            (Test-Path -LiteralPath $staged) | Should -BeTrue
        }

        It 'previews with -WhatIf without changing anything' {
            $install = Join-Path $TestDrive 'install-preview'
            $staged = Join-Path $TestDrive 'staged-preview'
            $backup = Join-Path $TestDrive 'backup-preview'
            New-Item -ItemType Directory -Path $install -Force | Out-Null
            New-Item -ItemType Directory -Path $staged -Force | Out-Null
            Set-Content -LiteralPath (Join-Path $install 'VERSION') -Value '1.0.0' -Encoding Ascii
            Set-Content -LiteralPath (Join-Path $install 'AutoCoach.ps1') -Value 'old-coach' -Encoding Ascii
            Set-Content -LiteralPath (Join-Path $staged 'VERSION') -Value '2.0.0' -Encoding Ascii
            Set-Content -LiteralPath (Join-Path $staged 'AutoCoach.ps1') -Value 'new-coach' -Encoding Ascii

            $code = Invoke-UpdateApply -Staged $staged -InstallDir $install -Version '2.0.0' -BackupDir $backup -LogDir (Join-Path $TestDrive 'logs-preview') -SkipProcessStop -WhatIf
            $code | Should -Be 0
            (Get-Content -LiteralPath (Join-Path $install 'AutoCoach.ps1') -Raw).Trim() | Should -Be 'old-coach'
            (Get-Content -LiteralPath (Join-Path $install 'VERSION') -Raw).Trim() | Should -Be '1.0.0'
            (Test-Path -LiteralPath (Join-Path $install '.update_in_progress')) | Should -BeFalse
            (Test-Path -LiteralPath $staged) | Should -BeTrue
            (Test-Path -LiteralPath $backup) | Should -BeFalse
        }
    }
}
