[CmdletBinding()]
param(
    [string]$CurrentVersionFile = '',
    [string]$CurrentVersion = '',
    [string]$ManifestUrl = 'https://github.com/Steven-BillyPrint/lingxing-erp-automation/releases/latest/download/latest.json',
    [string]$ManifestFile = '',
    [string]$PackageFile = '',
    [int]$GraceHours = 24,
    [string]$StateRoot = (Join-Path $env:LOCALAPPDATA 'LingxingERP'),
    [string]$InstanceName = $env:USERNAME,
    [string]$DesktopDirectory = '',
    [switch]$CheckOnly,
    [switch]$AssumeYes,
    [switch]$OutputJson
)

$ErrorActionPreference = 'Stop'
if ($OutputJson) {
    [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
}

function Get-Sha256Hex {
    param([Parameter(Mandatory = $true)][string]$LiteralPath)

    $stream = [IO.File]::OpenRead($LiteralPath)
    try {
        $algorithm = [Security.Cryptography.SHA256]::Create()
        try {
            $digest = $algorithm.ComputeHash($stream)
        } finally {
            $algorithm.Dispose()
        }
    } finally {
        $stream.Dispose()
    }
    return ([BitConverter]::ToString($digest)).Replace('-', '').ToLowerInvariant()
}
$repository = 'Steven-BillyPrint/lingxing-erp-automation'
$expectedAssetName = 'ERP-Automation-Client.zip'
$statePath = Join-Path $StateRoot 'update-state.json'
$updatesRoot = Join-Path $StateRoot 'updates'

function ConvertTo-VersionParts([string]$Value) {
    $normalized = ([string]$Value).Trim()
    if ($normalized -notmatch '^\d{4}\.\d{2}\.\d{2}\.\d+$') {
        throw "客户端版本号无效：$normalized"
    }
    return @($normalized.Split('.') | ForEach-Object { [int64]$_ })
}

function Compare-ClientVersion([string]$Left, [string]$Right) {
    $leftParts = ConvertTo-VersionParts $Left
    $rightParts = ConvertTo-VersionParts $Right
    for ($index = 0; $index -lt 4; $index++) {
        if ($leftParts[$index] -lt $rightParts[$index]) { return -1 }
        if ($leftParts[$index] -gt $rightParts[$index]) { return 1 }
    }
    return 0
}

function Read-UpdateState {
    if (-not (Test-Path -LiteralPath $statePath -PathType Leaf)) {
        return $null
    }
    try {
        $state = Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json
        if ([int]$state.schema_version -ne 1) {
            return $null
        }
        ConvertTo-VersionParts ([string]$state.latest_version) | Out-Null
        [DateTimeOffset]::Parse([string]$state.last_successful_check_utc) | Out-Null
        return $state
    } catch {
        return $null
    }
}

function Write-UpdateState([string]$LatestVersion, [string]$Sha256) {
    [IO.Directory]::CreateDirectory($StateRoot) | Out-Null
    $payload = [ordered]@{
        schema_version = 1
        last_successful_check_utc = [DateTime]::UtcNow.ToString('o')
        latest_version = $LatestVersion
        package_sha256 = $Sha256.ToLowerInvariant()
        manifest_url = $ManifestUrl
    } | ConvertTo-Json -Depth 3
    $temporary = Join-Path $StateRoot ('.update-state-' + [Guid]::NewGuid().ToString('N') + '.tmp')
    try {
        [IO.File]::WriteAllText(
            $temporary,
            $payload + [Environment]::NewLine,
            [Text.UTF8Encoding]::new($false)
        )
        Move-Item -LiteralPath $temporary -Destination $statePath -Force
    } finally {
        if (Test-Path -LiteralPath $temporary) {
            Remove-Item -LiteralPath $temporary -Force
        }
    }
}

function Get-ReleaseManifest {
    if ($ManifestFile) {
        return Get-Content -LiteralPath (Resolve-Path -LiteralPath $ManifestFile) -Raw |
            ConvertFrom-Json
    }
    return Invoke-RestMethod `
        -Uri $ManifestUrl `
        -Method Get `
        -TimeoutSec 15 `
        -Headers @{ 'Cache-Control' = 'no-cache' }
}

function Assert-ReleaseManifest($Manifest) {
    if ([int]$Manifest.schema_version -ne 1) {
        throw '更新清单版本不受支持。'
    }
    $version = [string]$Manifest.version
    ConvertTo-VersionParts $version | Out-Null
    if ($Manifest.mandatory -ne $true) {
        throw '正式客户端更新必须标记为 mandatory。'
    }
    $package = $Manifest.package
    if ($null -eq $package -or [string]$package.name -ne $expectedAssetName) {
        throw '更新清单中的客户端资产名称无效。'
    }
    $sha256 = ([string]$package.sha256).Trim().ToLowerInvariant()
    if ($sha256 -notmatch '^[a-f0-9]{64}$') {
        throw '更新清单中的 SHA256 无效。'
    }
    if ([int64]$package.size -le 0) {
        throw '更新清单中的客户端文件大小无效。'
    }
    $packageUri = [Uri]([string]$package.url)
    $expectedPath = "/$repository/releases/download/v$version/$expectedAssetName"
    if (
        $packageUri.Scheme -ne 'https' -or
        $packageUri.Host -ne 'github.com' -or
        $packageUri.AbsolutePath -ne $expectedPath -or
        $packageUri.Query -or
        $packageUri.Fragment
    ) {
        throw '更新清单中的下载地址不属于预期的不可变 GitHub Release。'
    }
}

function Show-UpdateConfirmation([string]$Current, [string]$Latest) {
    if ($AssumeYes) {
        return $true
    }
    Add-Type -AssemblyName System.Windows.Forms
    $message = @"
发现 ERP 自动化客户端新版本。

当前版本：$Current
最新版本：$Latest

本次更新为必需更新。点击“确定”立即更新；点击“取消”退出程序。
"@
    $choice = [System.Windows.Forms.MessageBox]::Show(
        $message,
        'ERP 自动化客户端更新',
        [System.Windows.Forms.MessageBoxButtons]::OKCancel,
        [System.Windows.Forms.MessageBoxIcon]::Information
    )
    return $choice -eq [System.Windows.Forms.DialogResult]::OK
}

function New-DownloadWindow([string]$Version) {
    if ($AssumeYes) {
        return $null
    }
    Add-Type -AssemblyName System.Windows.Forms
    Add-Type -AssemblyName System.Drawing
    $form = New-Object System.Windows.Forms.Form
    $form.Text = 'ERP 自动化客户端更新'
    $form.StartPosition = 'CenterScreen'
    $form.FormBorderStyle = 'FixedDialog'
    $form.ControlBox = $false
    $form.ShowInTaskbar = $true
    $form.TopMost = $true
    $form.ClientSize = New-Object System.Drawing.Size(450, 128)

    $label = New-Object System.Windows.Forms.Label
    $label.Text = "正在下载版本 $Version…"
    $label.AutoSize = $false
    $label.Size = New-Object System.Drawing.Size(406, 32)
    $label.Location = New-Object System.Drawing.Point(22, 22)
    $label.Font = New-Object System.Drawing.Font('Microsoft YaHei UI', 9)
    $form.Controls.Add($label)

    $progress = New-Object System.Windows.Forms.ProgressBar
    $progress.Minimum = 0
    $progress.Maximum = 1000
    $progress.Value = 0
    $progress.Size = New-Object System.Drawing.Size(406, 18)
    $progress.Location = New-Object System.Drawing.Point(22, 67)
    $form.Controls.Add($progress)

    $form.Tag = [pscustomobject]@{ Label = $label; Progress = $progress }
    $form.Show()
    [System.Windows.Forms.Application]::DoEvents()
    return $form
}

function Set-DownloadProgress($Window, [int64]$Downloaded, [int64]$Total) {
    if ($null -eq $Window) {
        return
    }
    if ($Total -gt 0) {
        $fraction = [Math]::Min(1.0, [double]$Downloaded / [double]$Total)
        $Window.Tag.Progress.Value = [int]([Math]::Floor($fraction * 1000))
        $Window.Tag.Label.Text = '正在下载更新… {0:N1} / {1:N1} MB' -f (
            $Downloaded / 1MB
        ), ($Total / 1MB)
    } else {
        $Window.Tag.Progress.Style = 'Marquee'
    }
    [System.Windows.Forms.Application]::DoEvents()
}

function Copy-ReleasePackage(
    [string]$SourceUrl,
    [string]$Destination,
    [int64]$ExpectedSize,
    [string]$Version
) {
    $window = New-DownloadWindow $Version
    try {
        if ($PackageFile) {
            Copy-Item -LiteralPath (Resolve-Path -LiteralPath $PackageFile) `
                -Destination $Destination -Force
            Set-DownloadProgress $window (Get-Item -LiteralPath $Destination).Length $ExpectedSize
            return
        }
        Add-Type -AssemblyName System.Net.Http
        $handler = [System.Net.Http.HttpClientHandler]::new()
        $client = [System.Net.Http.HttpClient]::new($handler)
        $client.Timeout = [TimeSpan]::FromMinutes(10)
        try {
            $response = $client.GetAsync(
                $SourceUrl,
                [System.Net.Http.HttpCompletionOption]::ResponseHeadersRead
            ).GetAwaiter().GetResult()
            $response.EnsureSuccessStatusCode() | Out-Null
            $total = $response.Content.Headers.ContentLength
            if ($null -eq $total) { $total = $ExpectedSize }
            $inputStream = $response.Content.ReadAsStreamAsync().GetAwaiter().GetResult()
            $outputStream = [IO.File]::Open(
                $Destination,
                [IO.FileMode]::CreateNew,
                [IO.FileAccess]::Write,
                [IO.FileShare]::None
            )
            try {
                $buffer = New-Object byte[] (1024 * 1024)
                $downloaded = [int64]0
                while (($read = $inputStream.Read($buffer, 0, $buffer.Length)) -gt 0) {
                    $outputStream.Write($buffer, 0, $read)
                    $downloaded += $read
                    Set-DownloadProgress $window $downloaded ([int64]$total)
                }
            } finally {
                $outputStream.Dispose()
                $inputStream.Dispose()
            }
        } finally {
            $client.Dispose()
            $handler.Dispose()
        }
    } finally {
        if ($null -ne $window) {
            $window.Close()
            $window.Dispose()
        }
    }
}

function Assert-SafeZip([string]$ZipPath, [string]$DestinationRoot) {
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $root = [IO.Path]::GetFullPath($DestinationRoot)
    $prefix = $root.TrimEnd('\', '/') + [IO.Path]::DirectorySeparatorChar
    $archive = [IO.Compression.ZipFile]::OpenRead($ZipPath)
    try {
        if ($archive.Entries.Count -le 0 -or $archive.Entries.Count -gt 20000) {
            throw '客户端 ZIP 文件数量无效。'
        }
        foreach ($entry in $archive.Entries) {
            $relative = $entry.FullName.Replace('\', '/')
            if (
                -not $relative -or
                $relative.StartsWith('/') -or
                $relative -match '^[A-Za-z]:' -or
                ($relative.Split('/') -contains '..')
            ) {
                throw "客户端 ZIP 包含不安全路径：$relative"
            }
            $target = [IO.Path]::GetFullPath((Join-Path $root $relative))
            if (
                $target -ne $root -and
                -not $target.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)
            ) {
                throw "客户端 ZIP 路径越界：$relative"
            }
            $unixMode = ($entry.ExternalAttributes -shr 16) -band 0xF000
            if ($unixMode -eq 0xA000) {
                throw "客户端 ZIP 不允许符号链接：$relative"
            }
        }
    } finally {
        $archive.Dispose()
    }
}

function New-UpdateResult(
    [string]$Status,
    [string]$CurrentVersion,
    [string]$LatestVersion,
    [string]$LauncherPath = '',
    [string]$ApplicationPath = ''
) {
    return [pscustomobject]@{
        status = $Status
        current_version = $CurrentVersion
        latest_version = $LatestVersion
        launcher_path = $LauncherPath
        application_path = $ApplicationPath
    }
}

if ($GraceHours -lt 1 -or $GraceHours -gt 168) {
    throw '更新检查宽限时间必须介于 1 到 168 小时。'
}
$currentVersion = ([string]$CurrentVersion).Trim()
if (-not $currentVersion) {
    $versionFile = ([string]$CurrentVersionFile).Trim()
    if (-not $versionFile) {
        $versionFile = Join-Path (
            Split-Path -Parent $PSScriptRoot
        ) 'VERSION.txt'
    }
    $resolvedVersionFile = (Resolve-Path -LiteralPath $versionFile).Path
    $currentVersion = (Get-Content -LiteralPath $resolvedVersionFile -Raw).Trim()
}
ConvertTo-VersionParts $currentVersion | Out-Null

$manifest = $null
$networkFailure = $null
try {
    $manifest = Get-ReleaseManifest
    Assert-ReleaseManifest $manifest
} catch {
    $networkFailure = $_
}

if ($null -eq $manifest) {
    $cached = Read-UpdateState
    if ($null -eq $cached) {
        throw "无法检查客户端更新，且没有可用缓存：$($networkFailure.Exception.Message)"
    }
    $checkedAt = [DateTimeOffset]::Parse([string]$cached.last_successful_check_utc)
    $age = [DateTimeOffset]::UtcNow - $checkedAt.ToUniversalTime()
    if ($age.TotalHours -gt $GraceHours) {
        throw "客户端更新检查缓存已超过 $GraceHours 小时，必须联网确认最新版。"
    }
    if ((Compare-ClientVersion $currentVersion ([string]$cached.latest_version)) -lt 0) {
        throw '缓存已确认存在必需更新，当前版本不能继续启动。'
    }
    $result = New-UpdateResult 'current_cached' $currentVersion ([string]$cached.latest_version)
    if ($OutputJson) { $result | ConvertTo-Json -Compress } else { $result }
    return
}

$latestVersion = [string]$manifest.version
$comparison = Compare-ClientVersion $currentVersion $latestVersion
if ($comparison -gt 0) {
    throw (
        "当前 EXE 内置版本 $currentVersion 高于正式发布版本 $latestVersion，" +
        '拒绝继续启动。请重新安装正式客户端。'
    )
}
Write-UpdateState $latestVersion ([string]$manifest.package.sha256)

if ($comparison -eq 0) {
    $result = New-UpdateResult 'current' $currentVersion $latestVersion
    if ($OutputJson) { $result | ConvertTo-Json -Compress } else { $result }
    return
}
if ($CheckOnly) {
    $result = New-UpdateResult 'update_required' $currentVersion $latestVersion
    if ($OutputJson) { $result | ConvertTo-Json -Compress } else { $result }
    return
}
if (-not (Show-UpdateConfirmation $currentVersion $latestVersion)) {
    $result = New-UpdateResult 'user_exit' $currentVersion $latestVersion
    if ($OutputJson) { $result | ConvertTo-Json -Compress } else { $result }
    return
}

[IO.Directory]::CreateDirectory($updatesRoot) | Out-Null
$mutex = [Threading.Mutex]::new($false, 'Local\LingxingERPClientUpdate')
$mutexAcquired = $false
$attemptRoot = Join-Path $updatesRoot (
    $latestVersion + '-' + [Guid]::NewGuid().ToString('N')
)
$attemptRoot = [IO.Path]::GetFullPath($attemptRoot)
$updatesPrefix = [IO.Path]::GetFullPath($updatesRoot).TrimEnd('\', '/') +
    [IO.Path]::DirectorySeparatorChar
if (-not $attemptRoot.StartsWith($updatesPrefix, [StringComparison]::OrdinalIgnoreCase)) {
    throw '更新暂存目录越界。'
}
try {
    $mutexAcquired = $mutex.WaitOne([TimeSpan]::FromMinutes(3))
    if (-not $mutexAcquired) {
        throw '另一项客户端更新长时间未完成，请稍后重试。'
    }
    [IO.Directory]::CreateDirectory($attemptRoot) | Out-Null
    $zipPath = Join-Path $attemptRoot $expectedAssetName
    Copy-ReleasePackage `
        ([string]$manifest.package.url) `
        $zipPath `
        ([int64]$manifest.package.size) `
        $latestVersion
    $downloaded = Get-Item -LiteralPath $zipPath
    if ($downloaded.Length -ne [int64]$manifest.package.size) {
        throw '客户端下载大小与更新清单不一致。'
    }
    $actualHash = Get-Sha256Hex -LiteralPath $zipPath
    if ($actualHash -ne [string]$manifest.package.sha256) {
        throw '客户端更新包 SHA256 校验失败。'
    }
    $extractRoot = Join-Path $attemptRoot 'package'
    [IO.Directory]::CreateDirectory($extractRoot) | Out-Null
    Assert-SafeZip $zipPath $extractRoot
    Expand-Archive -LiteralPath $zipPath -DestinationPath $extractRoot

    $packageVersion = (Get-Content -LiteralPath (Join-Path $extractRoot 'VERSION.txt') -Raw).Trim()
    if ($packageVersion -ne $latestVersion) {
        throw '客户端包版本与更新清单不一致。'
    }
    $installer = Join-Path $extractRoot 'scripts\install_shared_client.ps1'
    $application = Join-Path $extractRoot 'dist\ERP自动化\ERP自动化.exe'
    if (
        -not (Test-Path -LiteralPath $installer -PathType Leaf) -or
        -not (Test-Path -LiteralPath $application -PathType Leaf)
    ) {
        throw '客户端更新包结构不完整。'
    }
    $installerArguments = @{
        PackageRoot = $extractRoot
        Silent = $true
    }
    if ($DesktopDirectory) {
        $installerArguments.DesktopDirectory = $DesktopDirectory
    }
    & $installer @installerArguments | Out-Null

    $installedRoot = Join-Path $env:LOCALAPPDATA "Programs\LingxingERP\$latestVersion"
    $launcherPath = Join-Path $installedRoot 'scripts\start_shared_desktop.ps1'
    $applicationPath = Join-Path $installedRoot 'dist\ERP自动化\ERP自动化.exe'
    foreach ($required in @($launcherPath, $applicationPath)) {
        if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
            throw "更新完成后缺少程序文件：$required"
        }
    }
    Write-UpdateState $latestVersion ([string]$manifest.package.sha256)
    $result = New-UpdateResult `
        'updated' `
        $currentVersion `
        $latestVersion `
        $launcherPath `
        $applicationPath
    if ($OutputJson) { $result | ConvertTo-Json -Compress } else { $result }
} finally {
    if ($mutexAcquired) {
        $mutex.ReleaseMutex()
    }
    $mutex.Dispose()
    if (Test-Path -LiteralPath $attemptRoot) {
        Remove-Item -LiteralPath $attemptRoot -Recurse -Force
    }
}
