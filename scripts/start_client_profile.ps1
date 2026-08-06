[CmdletBinding()]
param(
    [ValidateSet('Select', 'Stable', 'Candidate')]
    [string]$ClientProfile = 'Select',
    [string]$ApplicationArguments = '',
    [switch]$Silent
)

$ErrorActionPreference = 'Stop'

function Show-ProfileLaunchError([string]$Message) {
    if ($Silent) {
        return
    }
    try {
        Add-Type -AssemblyName System.Windows.Forms
        [void][System.Windows.Forms.MessageBox]::Show(
            $Message,
            'ERP 自动化启动失败',
            [System.Windows.Forms.MessageBoxButtons]::OK,
            [System.Windows.Forms.MessageBoxIcon]::Warning
        )
    } catch {
        # stderr remains available when the graphical dialog cannot load.
    }
}

function Get-ProfileStateRoot([string]$Profile) {
    if ($Profile -eq 'Candidate') {
        return Join-Path $env:LOCALAPPDATA 'LingxingERP-Candidate'
    }
    return Join-Path $env:LOCALAPPDATA 'LingxingERP'
}

function Get-ProfileRegistrationPath([string]$Profile) {
    $launcherRoot = Join-Path (
        (Join-Path $env:LOCALAPPDATA 'Programs\LingxingERP')
    ) 'launcher'
    return Join-Path $launcherRoot (
        $Profile.ToLowerInvariant() + '.json'
    )
}

function Assert-ManagedApplication([string]$Path) {
    $resolved = (Resolve-Path -LiteralPath $Path).Path
    $programBase = [IO.Path]::GetFullPath(
        (Join-Path $env:LOCALAPPDATA 'Programs\LingxingERP')
    )
    $programPrefix = $programBase.TrimEnd('\', '/') +
        [IO.Path]::DirectorySeparatorChar
    if (-not $resolved.StartsWith(
        $programPrefix,
        [StringComparison]::OrdinalIgnoreCase
    )) {
        throw '客户端入口不在受控安装目录中。'
    }
    if (-not $resolved.EndsWith(
        '\dist\ERP自动化\ERP自动化.exe',
        [StringComparison]::OrdinalIgnoreCase
    )) {
        throw '客户端入口结构无效。'
    }
    return $resolved
}

function Read-ProfileRegistration(
    [ValidateSet('Stable', 'Candidate')]
    [string]$Profile,
    [switch]$Required
) {
    $registrationPath = Get-ProfileRegistrationPath $Profile
    if (-not (Test-Path -LiteralPath $registrationPath -PathType Leaf)) {
        if ($Required) {
            throw "尚未安装 ERP 自动化$Profile 客户端。"
        }
        return $null
    }
    try {
        $registration = Get-Content -LiteralPath $registrationPath -Raw `
            -Encoding UTF8 |
            ConvertFrom-Json
        if (
            [int]$registration.schema_version -ne 1 -or
            ([string]$registration.profile).Trim().ToLowerInvariant() -ne
                $Profile.ToLowerInvariant()
        ) {
            throw 'registration schema mismatch'
        }
        $application = Assert-ManagedApplication (
            [string]$registration.application_path
        )
        $versionRoot = Split-Path -Parent (
            Split-Path -Parent (
                Split-Path -Parent $application
            )
        )
        $version = Split-Path -Leaf $versionRoot
        if (
            $version -notmatch '^\d{4}\.\d{2}\.\d{2}\.\d+$' -or
            $version -ne ([string]$registration.version).Trim()
        ) {
            throw 'registration version mismatch'
        }
        $releaseChannel = (
            [string]$registration.release_channel
        ).Trim().ToLowerInvariant()
        if ($releaseChannel -and $releaseChannel -ne $Profile.ToLowerInvariant()) {
            throw 'registration channel mismatch'
        }
        $displayVersion = ([string]$registration.display_version).Trim()
        if (-not $displayVersion) {
            $displayVersion = $version
        }
        $expiresAt = $null
        if ($Profile -eq 'Candidate') {
            try {
                $expiresAt = [DateTimeOffset]::Parse(
                    ([string]$registration.expires_at).Trim(),
                    [Globalization.CultureInfo]::InvariantCulture,
                    [Globalization.DateTimeStyles]::RoundtripKind
                )
            } catch {
                throw 'candidate registration expiry invalid'
            }
            if ($expiresAt -le [DateTimeOffset]::UtcNow) {
                throw 'candidate registration expired'
            }
        }
        return [pscustomobject]@{
            Profile = $Profile
            Application = $application
            Version = $version
            DisplayVersion = $displayVersion
            ExpiresAt = $expiresAt
        }
    } catch {
        if ($Required) {
            if (
                $Profile -eq 'Candidate' -and
                $_.Exception.Message -eq 'candidate registration expired'
            ) {
                throw 'ERP 自动化候选版已过期，请安装新的候选版本。'
            }
            throw "ERP 自动化$Profile 客户端登记无效，请重新安装该版本。"
        }
        return $null
    }
}

function Show-ProfileSelection($Stable, $Candidate) {
    Add-Type -AssemblyName System.Windows.Forms
    Add-Type -AssemblyName System.Drawing
    $form = [System.Windows.Forms.Form]::new()
    $form.Text = '选择 ERP 自动化版本'
    $form.StartPosition = 'CenterScreen'
    $form.FormBorderStyle = 'FixedDialog'
    $form.MaximizeBox = $false
    $form.MinimizeBox = $false
    $form.TopMost = $true
    $form.ClientSize = [Drawing.Size]::new(430, 205)
    $form.Tag = ''

    $label = [System.Windows.Forms.Label]::new()
    $label.AutoSize = $false
    $label.TextAlign = 'MiddleCenter'
    $label.Location = [Drawing.Point]::new(20, 15)
    $label.Size = [Drawing.Size]::new(390, 40)
    $label.Text = '本机已安装候选版，请选择本次要启动的版本。'
    $form.Controls.Add($label)

    $expiryLabel = [System.Windows.Forms.Label]::new()
    $expiryLabel.AutoSize = $false
    $expiryLabel.TextAlign = 'MiddleCenter'
    $expiryLabel.ForeColor = [Drawing.Color]::FromArgb(180, 83, 9)
    $expiryLabel.Location = [Drawing.Point]::new(20, 52)
    $expiryLabel.Size = [Drawing.Size]::new(390, 25)
    $expiryLabel.Text = (
        '候选版有效至 ' +
        $Candidate.ExpiresAt.ToLocalTime().ToString('yyyy-MM-dd HH:mm')
    )
    $form.Controls.Add($expiryLabel)

    $stableButton = [System.Windows.Forms.Button]::new()
    $stableButton.Text = "正式版  $($Stable.DisplayVersion)"
    $stableButton.Location = [Drawing.Point]::new(25, 92)
    $stableButton.Size = [Drawing.Size]::new(170, 42)
    $stableButton.Add_Click({
        $form.Tag = 'Stable'
        $form.Close()
    })
    $form.Controls.Add($stableButton)

    $candidateButton = [System.Windows.Forms.Button]::new()
    $candidateButton.Text = "候选版  $($Candidate.DisplayVersion)"
    $candidateButton.Location = [Drawing.Point]::new(235, 92)
    $candidateButton.Size = [Drawing.Size]::new(170, 42)
    $candidateButton.Add_Click({
        $form.Tag = 'Candidate'
        $form.Close()
    })
    $form.Controls.Add($candidateButton)

    $cancelButton = [System.Windows.Forms.Button]::new()
    $cancelButton.Text = '取消'
    $cancelButton.Location = [Drawing.Point]::new(165, 157)
    $cancelButton.Size = [Drawing.Size]::new(100, 28)
    $cancelButton.Add_Click({ $form.Close() })
    $form.CancelButton = $cancelButton
    $form.Controls.Add($cancelButton)
    try {
        [void]$form.ShowDialog()
        return [string]$form.Tag
    } finally {
        $form.Dispose()
    }
}

function Read-RestartRequest(
    [string]$Path,
    [DateTime]$Deadline
) {
    while (Test-Path -LiteralPath $Path -PathType Leaf) {
        try {
            $request = Get-Content -LiteralPath $Path -Raw -Encoding UTF8 |
                ConvertFrom-Json
        } catch {
            if ([DateTime]::UtcNow -lt $Deadline) {
                Start-Sleep -Milliseconds 100
                continue
            }
            throw '客户端重启请求无效。'
        }
        if ([int]$request.schema_version -ne 1) {
            throw '客户端重启请求版本不受支持。'
        }
        $status = ([string]$request.status).Trim().ToLowerInvariant()
        if ($status -eq 'pending') {
            if ([DateTime]::UtcNow -ge $Deadline) {
                throw '等待客户端退出后修复超时。'
            }
            Start-Sleep -Milliseconds 200
            continue
        }
        if ($status -eq 'failed') {
            $detail = ([string]$request.message).Trim()
            if (-not $detail) {
                $detail = '客户端退出后修复失败。'
            }
            throw $detail
        }
        if ($status -ne 'ready') {
            throw '客户端重启请求状态无效。'
        }
        return Assert-ManagedApplication ([string]$request.application_path)
    }
    return ''
}

$previousEnvironment = @{
    ERP_AUTOMATION_CLIENT_PROFILE = $env:ERP_AUTOMATION_CLIENT_PROFILE
    ERP_AUTOMATION_CLIENT_DISPLAY_VERSION = (
        $env:ERP_AUTOMATION_CLIENT_DISPLAY_VERSION
    )
    ERP_AUTOMATION_CLIENT_STATE_ROOT = $env:ERP_AUTOMATION_CLIENT_STATE_ROOT
    ERP_AUTOMATION_HOME = $env:ERP_AUTOMATION_HOME
    ERP_AUTOMATION_PROFILE_RESTART_REQUEST = (
        $env:ERP_AUTOMATION_PROFILE_RESTART_REQUEST
    )
}
$mutexScope = [IO.Path]::GetFullPath($env:LOCALAPPDATA).
    TrimEnd('\', '/').ToLowerInvariant()
$mutexHashAlgorithm = [Security.Cryptography.SHA256]::Create()
try {
    $mutexHash = $mutexHashAlgorithm.ComputeHash(
        [Text.Encoding]::UTF8.GetBytes($mutexScope)
    )
} finally {
    $mutexHashAlgorithm.Dispose()
}
$mutexSuffix = ([BitConverter]::ToString($mutexHash)).Replace('-', '').
    Substring(0, 16)
$runMutex = [Threading.Mutex]::new(
    $false,
    ('Local\LingxingERPClientExclusiveRun-' + $mutexSuffix)
)
$runMutexAcquired = $false
$exitCode = 0
$restartRequestPath = ''
try {
    try {
        $runMutexAcquired = $runMutex.WaitOne(0)
    } catch [Threading.AbandonedMutexException] {
        $runMutexAcquired = $true
    }
    if (-not $runMutexAcquired) {
        $message = (
            'ERP 自动化的正式版或候选版已经在运行。' +
            '请先关闭当前版本，再打开另一个快捷方式。'
        )
        Show-ProfileLaunchError $message
        [Console]::Error.WriteLine($message)
        $exitCode = 2
    } else {
        $selectedProfile = $ClientProfile
        if ($selectedProfile -eq 'Select') {
            $stableRegistration = Read-ProfileRegistration 'Stable'
            $candidateRegistration = Read-ProfileRegistration 'Candidate'
            if ($null -ne $stableRegistration -and $null -ne $candidateRegistration) {
                if ($Silent) {
                    $selectedProfile = 'Stable'
                } else {
                    $selectedProfile = Show-ProfileSelection `
                        $stableRegistration `
                        $candidateRegistration
                }
            } elseif ($null -ne $stableRegistration) {
                $selectedProfile = 'Stable'
            } elseif ($null -ne $candidateRegistration) {
                $selectedProfile = 'Candidate'
            } else {
                throw '本机尚未安装可启动的 ERP 自动化客户端。'
            }
            if (-not $selectedProfile) {
                $exitCode = 0
                return
            }
        }
        $registration = Read-ProfileRegistration $selectedProfile -Required
        $profile = $selectedProfile.ToLowerInvariant()
        $StateRoot = [IO.Path]::GetFullPath(
            (Get-ProfileStateRoot $selectedProfile)
        )
        [IO.Directory]::CreateDirectory($StateRoot) | Out-Null
        $runtimeRoot = Join-Path $StateRoot 'runtime'
        [IO.Directory]::CreateDirectory($runtimeRoot) | Out-Null
        $application = $registration.Application
        $restartRequestPath = Join-Path $StateRoot (
            '.profile-restart-' + [Guid]::NewGuid().ToString('N') + '.json'
        )
        $env:ERP_AUTOMATION_CLIENT_PROFILE = $profile
        $env:ERP_AUTOMATION_CLIENT_DISPLAY_VERSION = (
            $registration.DisplayVersion
        )
        $env:ERP_AUTOMATION_CLIENT_STATE_ROOT = $StateRoot
        $env:ERP_AUTOMATION_HOME = $runtimeRoot
        $env:ERP_AUTOMATION_PROFILE_RESTART_REQUEST = $restartRequestPath

        $nextApplication = $application
        $nextArguments = $ApplicationArguments
        while ($nextApplication) {
            if (Test-Path -LiteralPath $restartRequestPath -PathType Leaf) {
                Remove-Item -LiteralPath $restartRequestPath -Force
            }
            $processArguments = @{
                FilePath = $nextApplication
                WorkingDirectory = (
                    Split-Path -Parent (
                        Split-Path -Parent (
                            Split-Path -Parent $nextApplication
                        )
                    )
                )
                PassThru = $true
            }
            if ($nextArguments) {
                $processArguments.ArgumentList = $nextArguments
            }
            $process = Start-Process @processArguments
            $process.WaitForExit()
            $process.Refresh()
            $exitCode = [int]$process.ExitCode
            $nextApplication = Read-RestartRequest `
                $restartRequestPath `
                ([DateTime]::UtcNow.AddMinutes(5))
            $nextArguments = ''
        }
    }
} catch {
    $exitCode = 1
    Show-ProfileLaunchError $_.Exception.Message
    [Console]::Error.WriteLine($_.Exception.Message)
} finally {
    if (
        $restartRequestPath -and
        (Test-Path -LiteralPath $restartRequestPath -PathType Leaf)
    ) {
        Remove-Item -LiteralPath $restartRequestPath -Force
    }
    foreach ($name in $previousEnvironment.Keys) {
        if ($null -eq $previousEnvironment[$name]) {
            Remove-Item -Path "Env:$name" -ErrorAction SilentlyContinue
        } else {
            Set-Item -Path "Env:$name" -Value $previousEnvironment[$name]
        }
    }
    if ($runMutexAcquired) {
        $runMutex.ReleaseMutex()
    }
    $runMutex.Dispose()
}
exit $exitCode
