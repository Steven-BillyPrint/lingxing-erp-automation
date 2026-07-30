[CmdletBinding()]
param(
    [string]$ServerHost = '8.133.172.100',
    [string]$ServerUser = 'admin',
    [int]$LocalPort = 18765,
    [int]$RemotePort = 18765,
    [string]$SshKeyPath = (Join-Path $env:LOCALAPPDATA 'LingxingERP\server-tunnel-ed25519'),
    [string]$KnownHostsPath = (Join-Path $env:LOCALAPPDATA 'LingxingERP\known_hosts'),
    [string]$TokenFile = (Join-Path $env:LOCALAPPDATA 'LingxingERP\coordination-token'),
    [string]$InstanceName = $env:USERNAME,
    [string]$PythonPath = '',
    [string]$ApplicationPath = '',
    [string]$UpdateManifestUrl = 'https://github.com/Steven-BillyPrint/lingxing-erp-automation/releases/latest/download/latest.json'
)

$ErrorActionPreference = 'Stop'
$workspace = Split-Path -Parent $PSScriptRoot
$tunnel = $null
$browserTunnel = $null
$startupWindow = $null
$startupLabel = $null
$startupProgressTimer = $null
$launcherLogPath = Join-Path $env:LOCALAPPDATA 'LingxingERP\launcher.log'
$appTitleText = [Text.Encoding]::UTF8.GetString(
    [Convert]::FromBase64String('RVJQIOiHquWKqOWMluaOp+WItuWPsA==')
)

function Write-LauncherLog([string]$Message) {
    try {
        $logDirectory = Split-Path -Parent $launcherLogPath
        [IO.Directory]::CreateDirectory($logDirectory) | Out-Null
        $timestamp = [DateTime]::Now.ToString('yyyy-MM-dd HH:mm:ss.fff')
        [IO.File]::AppendAllText(
            $launcherLogPath,
            "$timestamp $Message$([Environment]::NewLine)",
            [Text.UTF8Encoding]::new($false)
        )
    } catch {
        # Startup diagnostics must never prevent the application from opening.
    }
}

function Initialize-StartupWindow {
    Add-Type -AssemblyName System.Windows.Forms
    Add-Type -AssemblyName System.Drawing
    [System.Windows.Forms.Application]::EnableVisualStyles()

    $script:startupWindow = New-Object System.Windows.Forms.Form
    $script:startupWindow.Text = $appTitleText
    $script:startupWindow.StartPosition = 'CenterScreen'
    $script:startupWindow.FormBorderStyle = 'FixedDialog'
    $script:startupWindow.ControlBox = $false
    $script:startupWindow.MaximizeBox = $false
    $script:startupWindow.MinimizeBox = $false
    $script:startupWindow.ShowInTaskbar = $true
    $script:startupWindow.TopMost = $true
    $script:startupWindow.AutoScaleMode = 'Dpi'
    $script:startupWindow.ClientSize = New-Object System.Drawing.Size(520, 252)
    $script:startupWindow.BackColor = [Drawing.Color]::FromArgb(244, 247, 252)

    $card = New-Object System.Windows.Forms.Panel
    $card.Location = New-Object System.Drawing.Point(20, 20)
    $card.Size = New-Object System.Drawing.Size(480, 212)
    $card.BackColor = [Drawing.Color]::White
    $card.BorderStyle = 'FixedSingle'
    $script:startupWindow.Controls.Add($card)

    $badge = New-Object System.Windows.Forms.Label
    $badge.Text = 'ERP'
    $badge.Font = New-Object System.Drawing.Font(
        'Microsoft YaHei UI',
        10,
        [Drawing.FontStyle]::Bold
    )
    $badge.ForeColor = [Drawing.Color]::FromArgb(36, 95, 206)
    $badge.BackColor = [Drawing.Color]::FromArgb(232, 240, 255)
    $badge.TextAlign = 'MiddleCenter'
    $badge.Size = New-Object System.Drawing.Size(48, 48)
    $badge.Location = New-Object System.Drawing.Point(22, 20)
    $card.Controls.Add($badge)

    $title = New-Object System.Windows.Forms.Label
    $title.Text = [Text.Encoding]::UTF8.GetString(
        [Convert]::FromBase64String('5q2j5Zyo5YeG5aSHIEVSUCDoh6rliqjljJY=')
    )
    $title.Font = New-Object System.Drawing.Font(
        'Microsoft YaHei UI',
        14,
        [Drawing.FontStyle]::Bold
    )
    $title.ForeColor = [Drawing.Color]::FromArgb(16, 33, 58)
    $title.AutoSize = $false
    $title.Size = New-Object System.Drawing.Size(370, 29)
    $title.Location = New-Object System.Drawing.Point(84, 18)
    $card.Controls.Add($title)

    $subtitle = New-Object System.Windows.Forms.Label
    $subtitle.Text = [Text.Encoding]::UTF8.GetString(
        [Convert]::FromBase64String('5q2j5Zyo5a6J5YWo6L+e5o6l5Yiw5YWs5Y+45pyN5Yqh')
    )
    $subtitle.Font = New-Object System.Drawing.Font('Microsoft YaHei UI', 9)
    $subtitle.ForeColor = [Drawing.Color]::FromArgb(102, 117, 140)
    $subtitle.AutoSize = $false
    $subtitle.Size = New-Object System.Drawing.Size(370, 23)
    $subtitle.Location = New-Object System.Drawing.Point(84, 47)
    $card.Controls.Add($subtitle)

    $statusPanel = New-Object System.Windows.Forms.Panel
    $statusPanel.Location = New-Object System.Drawing.Point(22, 84)
    $statusPanel.Size = New-Object System.Drawing.Size(434, 62)
    $statusPanel.BackColor = [Drawing.Color]::FromArgb(247, 249, 253)
    $statusPanel.BorderStyle = 'FixedSingle'
    $card.Controls.Add($statusPanel)

    $statusTitle = New-Object System.Windows.Forms.Label
    $statusTitle.Text = [Text.Encoding]::UTF8.GetString(
        [Convert]::FromBase64String('5ZCv5Yqo6L+b5bqm')
    )
    $statusTitle.Font = New-Object System.Drawing.Font(
        'Microsoft YaHei UI',
        8,
        [Drawing.FontStyle]::Bold
    )
    $statusTitle.ForeColor = [Drawing.Color]::FromArgb(47, 111, 237)
    $statusTitle.AutoSize = $true
    $statusTitle.Location = New-Object System.Drawing.Point(12, 8)
    $statusPanel.Controls.Add($statusTitle)

    $script:startupLabel = New-Object System.Windows.Forms.Label
    $script:startupLabel.Text = [Text.Encoding]::UTF8.GetString(
        [Convert]::FromBase64String('5q2j5Zyo5YeG5aSH5ZCv5Yqo4oCm')
    )
    $script:startupLabel.Font = New-Object System.Drawing.Font('Microsoft YaHei UI', 9)
    $script:startupLabel.ForeColor = [Drawing.Color]::FromArgb(38, 58, 87)
    $script:startupLabel.AutoSize = $false
    $script:startupLabel.Size = New-Object System.Drawing.Size(408, 23)
    $script:startupLabel.Location = New-Object System.Drawing.Point(12, 29)
    $statusPanel.Controls.Add($script:startupLabel)

    $progressTrack = New-Object System.Windows.Forms.Panel
    $progressTrack.Size = New-Object System.Drawing.Size(434, 8)
    $progressTrack.Location = New-Object System.Drawing.Point(22, 158)
    $progressTrack.BackColor = [Drawing.Color]::FromArgb(231, 237, 247)
    $card.Controls.Add($progressTrack)

    $progressFill = New-Object System.Windows.Forms.Panel
    $progressFill.Size = New-Object System.Drawing.Size(86, 8)
    $progressFill.Location = New-Object System.Drawing.Point(-86, 0)
    $progressFill.BackColor = [Drawing.Color]::FromArgb(47, 111, 237)
    $progressTrack.Controls.Add($progressFill)

    $script:startupProgressTimer = New-Object System.Windows.Forms.Timer
    $script:startupProgressTimer.Interval = 24
    $progressTick = {
        $nextLeft = $progressFill.Left + 8
        if ($nextLeft -gt $progressTrack.Width) {
            $nextLeft = -$progressFill.Width
        }
        $progressFill.Left = $nextLeft
    }.GetNewClosure()
    $script:startupProgressTimer.Add_Tick($progressTick)
    $script:startupProgressTimer.Start()

    $hint = New-Object System.Windows.Forms.Label
    $hint.Text = [Text.Encoding]::UTF8.GetString(
        [Convert]::FromBase64String(
            '6K+356iN5YCZ77yM56iL5bqP5bCx57uq5ZCO5q2k56qX5Y+j5Lya6Ieq5Yqo5YWz6Zet44CC'
        )
    )
    $hint.Font = New-Object System.Drawing.Font('Microsoft YaHei UI', 8)
    $hint.ForeColor = [Drawing.Color]::FromArgb(123, 135, 154)
    $hint.TextAlign = 'MiddleCenter'
    $hint.AutoSize = $false
    $hint.Size = New-Object System.Drawing.Size(434, 24)
    $hint.Location = New-Object System.Drawing.Point(22, 174)
    $card.Controls.Add($hint)

    $script:startupWindow.Show()
    $script:startupWindow.Activate()
    [System.Windows.Forms.Application]::DoEvents()
}

function Set-StartupStatus([string]$Message) {
    Write-LauncherLog $Message
    if ($null -ne $script:startupWindow -and -not $script:startupWindow.IsDisposed) {
        $script:startupLabel.Text = $Message
        [System.Windows.Forms.Application]::DoEvents()
    }
}

function Close-StartupWindow {
    if ($null -ne $script:startupProgressTimer) {
        $script:startupProgressTimer.Stop()
        $script:startupProgressTimer.Dispose()
        $script:startupProgressTimer = $null
    }
    if ($null -ne $script:startupWindow -and -not $script:startupWindow.IsDisposed) {
        $script:startupWindow.Close()
        $script:startupWindow.Dispose()
    }
    $script:startupWindow = $null
    $script:startupLabel = $null
}

function Quote-NativeArgument([string]$Value) {
    if ($Value.Contains('"')) {
        throw 'Native command arguments may not contain a double quote.'
    }
    return '"' + $Value + '"'
}

function Remove-StaleClientVersions([string]$CurrentVersion) {
    if (-not $CurrentVersion) {
        return
    }
    $programBase = Join-Path $env:LOCALAPPDATA 'Programs\LingxingERP'
    if (-not (Test-Path -LiteralPath $programBase -PathType Container)) {
        return
    }
    $resolvedBase = (Resolve-Path -LiteralPath $programBase).Path
    $currentRoot = [IO.Path]::GetFullPath((Join-Path $resolvedBase $CurrentVersion))
    $runningPaths = @(
        Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
            ForEach-Object { [string]$_.ExecutablePath } |
            Where-Object { $_ }
    )
    $otherVersions = @(
        Get-ChildItem -LiteralPath $resolvedBase -Directory |
            Where-Object {
                $_.Name -match '^\d{4}\.\d{2}\.\d{2}\.\d+$' -and
                $_.FullName -ne $currentRoot
            } |
            Sort-Object LastWriteTimeUtc -Descending
    )
    $keepOther = $otherVersions | Select-Object -First 1
    foreach ($candidate in ($otherVersions | Select-Object -Skip 1)) {
        $resolvedCandidate = [IO.Path]::GetFullPath($candidate.FullName)
        $prefix = $resolvedBase.TrimEnd('\', '/') + [IO.Path]::DirectorySeparatorChar
        if (
            -not $resolvedCandidate.StartsWith(
                $prefix,
                [StringComparison]::OrdinalIgnoreCase
            ) -or
            ($candidate.Attributes -band [IO.FileAttributes]::ReparsePoint)
        ) {
            continue
        }
        $candidatePrefix = $resolvedCandidate.TrimEnd('\', '/') +
            [IO.Path]::DirectorySeparatorChar
        if ($runningPaths | Where-Object {
            $_.StartsWith($candidatePrefix, [StringComparison]::OrdinalIgnoreCase)
        }) {
            continue
        }
        try {
            Remove-Item -LiteralPath $resolvedCandidate -Recurse -Force
            Write-LauncherLog "Removed stale client version $($candidate.Name)."
        } catch {
            Write-LauncherLog "Could not remove stale client version $($candidate.Name)."
        }
    }
}

try {
    $versionFile = Join-Path $workspace 'VERSION.txt'
    $updater = Join-Path $workspace 'scripts\update_shared_client.ps1'
    $clientVersion = ''
    if (
        (Test-Path -LiteralPath $versionFile -PathType Leaf) -and
        (Test-Path -LiteralPath $updater -PathType Leaf)
    ) {
        $clientVersion = (Get-Content -LiteralPath $versionFile -Raw).Trim()
        $updateResult = & $updater `
            -CurrentVersionFile $versionFile `
            -ManifestUrl $UpdateManifestUrl `
            -StateRoot (Join-Path $env:LOCALAPPDATA 'LingxingERP') `
            -InstanceName $InstanceName
        if ($updateResult.status -eq 'user_exit') {
            exit 0
        }
        if ($updateResult.status -eq 'updated') {
            $restartArguments = @(
                '-NoProfile',
                '-ExecutionPolicy', 'Bypass',
                '-WindowStyle', 'Hidden',
                '-File', (Quote-NativeArgument ([string]$updateResult.launcher_path)),
                '-ServerHost', (Quote-NativeArgument $ServerHost),
                '-ServerUser', (Quote-NativeArgument $ServerUser),
                '-LocalPort', [string]$LocalPort,
                '-RemotePort', [string]$RemotePort,
                '-SshKeyPath', (Quote-NativeArgument $SshKeyPath),
                '-KnownHostsPath', (Quote-NativeArgument $KnownHostsPath),
                '-TokenFile', (Quote-NativeArgument $TokenFile),
                '-InstanceName', (Quote-NativeArgument $InstanceName),
                '-ApplicationPath', (
                    Quote-NativeArgument ([string]$updateResult.application_path)
                ),
                '-UpdateManifestUrl', (Quote-NativeArgument $UpdateManifestUrl)
            ) -join ' '
            Start-Process `
                -FilePath "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe" `
                -ArgumentList $restartArguments `
                -WindowStyle Hidden | Out-Null
            exit 0
        }
        Remove-StaleClientVersions $clientVersion
    }

    Initialize-StartupWindow
    Write-LauncherLog 'Launcher started.'
    Set-StartupStatus (
        [Text.Encoding]::UTF8.GetString(
            [Convert]::FromBase64String('5q2j5Zyo5qOA5p+l5pys5py66YWN572u4oCm')
        )
    )

    foreach ($requiredPath in @($SshKeyPath, $KnownHostsPath, $TokenFile)) {
        if (-not (Test-Path -LiteralPath $requiredPath -PathType Leaf)) {
            throw "Required client file does not exist: $requiredPath"
        }
    }

    $token = (Get-Content -LiteralPath $TokenFile -Raw).Trim()
    if ($token.Length -lt 32) {
        throw 'The coordination token file is empty or invalid.'
    }

    if (-not $ApplicationPath) {
        $applicationName = Split-Path -Leaf $workspace
        $packagedApplication = Join-Path $workspace (
            Join-Path 'dist' (
                Join-Path $applicationName ($applicationName + '.exe')
            )
        )
        if (Test-Path -LiteralPath $packagedApplication -PathType Leaf) {
            $ApplicationPath = $packagedApplication
        } else {
            $ApplicationPath = Join-Path $workspace 'desktop_main.py'
        }
    }
    if (-not (Test-Path -LiteralPath $ApplicationPath -PathType Leaf)) {
        throw "Desktop entry point does not exist: $ApplicationPath"
    }
    $isPackagedApplication = (
        [IO.Path]::GetExtension($ApplicationPath).Equals(
            '.exe',
            [StringComparison]::OrdinalIgnoreCase
        )
    )
    if (-not $isPackagedApplication) {
        if (-not $PythonPath) {
            $PythonPath = Join-Path $workspace '.venv\Scripts\python.exe'
        }
        if (-not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) {
            throw "Python executable does not exist: $PythonPath"
        }
    }

    Set-StartupStatus (
        [Text.Encoding]::UTF8.GetString(
            [Convert]::FromBase64String('5q2j5Zyo6L+e5o6l6Zi/6YeM5LqR5YWx5Lqr5pyN5Yqh4oCm')
        )
    )
    $sshArguments = @(
        '-N',
        '-o', 'BatchMode=yes',
        '-o', 'ExitOnForwardFailure=yes',
        '-o', 'ServerAliveInterval=15',
        '-o', 'ServerAliveCountMax=3',
        '-o', 'StrictHostKeyChecking=yes',
        '-o', ('UserKnownHostsFile=' + (Quote-NativeArgument $KnownHostsPath)),
        '-i', (Quote-NativeArgument $SshKeyPath),
        '-L', "127.0.0.1:${LocalPort}:127.0.0.1:${RemotePort}",
        "${ServerUser}@${ServerHost}"
    ) -join ' '

    $tunnel = Start-Process -FilePath 'ssh.exe' `
        -ArgumentList $sshArguments `
        -PassThru `
        -WindowStyle Hidden

    $deadline = [DateTime]::UtcNow.AddSeconds(15)
    do {
        if ($tunnel.HasExited) {
            throw "SSH tunnel stopped with exit code $($tunnel.ExitCode)."
        }
        try {
            $response = Invoke-RestMethod `
                -Uri "http://127.0.0.1:${LocalPort}/health" `
                -Method Get `
                -TimeoutSec 2
            if ($response.ok -eq $true) {
                break
            }
        } catch {
            Start-Sleep -Milliseconds 250
        }
    } while ([DateTime]::UtcNow -lt $deadline)

    if ([DateTime]::UtcNow -ge $deadline) {
        throw 'The shared ERP server did not become healthy through the SSH tunnel.'
    }

    $instanceId = [Guid]::NewGuid().ToString('N')
    $headers = @{ Authorization = "Bearer $token" }
    $allocationBody = @{
        instance_id = $instanceId
        display_name = $InstanceName
        client_version = $clientVersion
    } | ConvertTo-Json -Compress
    $allocation = Invoke-RestMethod `
        -Uri "http://127.0.0.1:${LocalPort}/v1/instances/browser-endpoint" `
        -Method Post `
        -Headers $headers `
        -ContentType 'application/json' `
        -Body $allocationBody `
        -TimeoutSec 5
    if ($allocation.ok -ne $true -or [int]$allocation.browser_port -le 0) {
        throw 'The server did not allocate a desktop browser tunnel.'
    }
    $browserPort = [int]$allocation.browser_port
    $listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, $browserPort)
    try {
        $listener.Start()
    } finally {
        $listener.Stop()
    }
    $browserSshArguments = @(
        '-N',
        '-o', 'BatchMode=yes',
        '-o', 'ExitOnForwardFailure=yes',
        '-o', 'ServerAliveInterval=15',
        '-o', 'ServerAliveCountMax=3',
        '-o', 'StrictHostKeyChecking=yes',
        '-o', ('UserKnownHostsFile=' + (Quote-NativeArgument $KnownHostsPath)),
        '-i', (Quote-NativeArgument $SshKeyPath),
        '-R', "127.0.0.1:${browserPort}:127.0.0.1:${browserPort}",
        "${ServerUser}@${ServerHost}"
    ) -join ' '
    $browserTunnel = Start-Process -FilePath 'ssh.exe' `
        -ArgumentList $browserSshArguments `
        -PassThru `
        -WindowStyle Hidden
    Start-Sleep -Milliseconds 500
    if ($browserTunnel.HasExited) {
        throw "Desktop browser tunnel stopped with exit code $($browserTunnel.ExitCode)."
    }

    $env:ERP_AUTOMATION_SERVER_URL = "http://127.0.0.1:${LocalPort}"
    $env:ERP_AUTOMATION_SERVER_TOKEN = $token
    $env:ERP_AUTOMATION_INSTANCE_NAME = $InstanceName
    $env:ERP_AUTOMATION_INSTANCE_ID = $instanceId
    $env:ERP_AUTOMATION_CLIENT_VERSION = $clientVersion
    $env:ERP_AUTOMATION_BROWSER_ENDPOINT = [string]$allocation.browser_endpoint
    $env:ERP_AUTOMATION_BROWSER_LOCAL_PORT = [string]$browserPort
    $env:ERP_AUTOMATION_BROWSER_PROFILE = Join-Path $env:LOCALAPPDATA 'LingxingERP\browser-profile'
    Set-StartupStatus (
        [Text.Encoding]::UTF8.GetString(
            [Convert]::FromBase64String('6L+e5o6l5oiQ5Yqf77yM5q2j5Zyo5ZCv5YqoIEVSUCDmjqfliLblj7DigKY=')
        )
    )
    if ($isPackagedApplication) {
        $applicationProcess = Start-Process `
            -FilePath $ApplicationPath `
            -PassThru
    } else {
        $applicationProcess = Start-Process `
            -FilePath $PythonPath `
            -ArgumentList (Quote-NativeArgument $ApplicationPath) `
            -PassThru `
            -WindowStyle Hidden
    }

    Set-StartupStatus (
        [Text.Encoding]::UTF8.GetString(
            [Convert]::FromBase64String('5bqU55So5bey5ZCv5Yqo77yM5q2j5Zyo5Yqg6L2955WM6Z2i4oCm')
        )
    )
    $windowDeadline = [DateTime]::UtcNow.AddSeconds(20)
    do {
        $applicationProcess.Refresh()
        if ($applicationProcess.HasExited -or $applicationProcess.MainWindowHandle -ne 0) {
            break
        }
        [System.Windows.Forms.Application]::DoEvents()
        Start-Sleep -Milliseconds 100
    } while ([DateTime]::UtcNow -lt $windowDeadline)

    if ($applicationProcess.HasExited) {
        throw "ERP application stopped during startup with exit code $($applicationProcess.ExitCode)."
    }

    Close-StartupWindow
    Write-LauncherLog 'Application window is ready.'
    Wait-Process -Id $applicationProcess.Id
    $applicationProcess.Refresh()
    exit $applicationProcess.ExitCode
} catch {
    $failureTitle = [Text.Encoding]::UTF8.GetString(
        [Convert]::FromBase64String('RVJQIOiHquWKqOWMluaOp+WItuWPsOWQr+WKqOWksei0peOAgg==')
    )
    $diagnosticLogLabel = [Text.Encoding]::UTF8.GetString(
        [Convert]::FromBase64String('6K+K5pat5pel5b+X77ya')
    )
    $failureMessage = "$failureTitle`n`n$($_.Exception.Message)`n`n$diagnosticLogLabel$launcherLogPath"
    Write-LauncherLog ("Startup failed: " + $_.Exception.GetType().Name + '.')
    Close-StartupWindow
    Add-Type -AssemblyName System.Windows.Forms
    [System.Windows.Forms.MessageBox]::Show(
        $failureMessage,
        $appTitleText,
        [System.Windows.Forms.MessageBoxButtons]::OK,
        [System.Windows.Forms.MessageBoxIcon]::Error
    ) | Out-Null
    exit 1
} finally {
    $env:ERP_AUTOMATION_SERVER_TOKEN = $null
    $env:ERP_AUTOMATION_INSTANCE_ID = $null
    $env:ERP_AUTOMATION_CLIENT_VERSION = $null
    $env:ERP_AUTOMATION_BROWSER_ENDPOINT = $null
    $env:ERP_AUTOMATION_BROWSER_LOCAL_PORT = $null
    $env:ERP_AUTOMATION_BROWSER_PROFILE = $null
    Close-StartupWindow
    if ($null -ne $browserTunnel -and -not $browserTunnel.HasExited) {
        Stop-Process -Id $browserTunnel.Id
    }
    if ($null -ne $tunnel -and -not $tunnel.HasExited) {
        Stop-Process -Id $tunnel.Id
    }
}
