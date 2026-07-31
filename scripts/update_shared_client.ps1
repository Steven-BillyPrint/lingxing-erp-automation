[CmdletBinding()]
param(
    [string]$CurrentVersionFile = '',
    [string]$CurrentVersion = '',
    [string]$CurrentPackageRoot = '',
    [int]$CurrentProcessId = 0,
    [string]$ManifestUrl = 'https://github.com/Steven-BillyPrint/lingxing-erp-automation/releases/latest/download/latest.json',
    [string]$ManifestFile = '',
    [string]$PackageFile = '',
    [int]$GraceHours = 24,
    [string]$StateRoot = (Join-Path $env:LOCALAPPDATA 'LingxingERP'),
    [string]$InstanceName = $env:USERNAME,
    [string]$DesktopDirectory = '',
    [switch]$CheckOnly,
    [switch]$AssumeYes,
    [switch]$SkipApplicationSmokeTest,
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

function Get-DirectoryContentInfo([string]$Root) {
    $resolvedRoot = (Resolve-Path -LiteralPath $Root).Path
    $paths = [string[]]@(
        Get-ChildItem -LiteralPath $resolvedRoot -Recurse -File |
            Where-Object {
                $_.Name -ne 'install-receipt.json' -and
                $_.Name -notlike '.install-receipt-*.tmp'
            } |
            ForEach-Object {
                $_.FullName.Substring($resolvedRoot.Length).
                    TrimStart('\').Replace('\', '/')
            }
    )
    [Array]::Sort($paths, [StringComparer]::Ordinal)
    if (@($paths | Select-Object -Unique).Count -ne $paths.Count) {
        throw '客户端安装目录包含重复文件路径。'
    }
    $canonical = [Text.StringBuilder]::new()
    foreach ($relativePath in $paths) {
        if (
            -not $relativePath -or
            $relativePath.Contains([char]0) -or
            $relativePath.Contains("`n")
        ) {
            throw '客户端安装目录包含无法校验的文件路径。'
        }
        $literalPath = Join-Path (
            $resolvedRoot
        ) $relativePath.Replace('/', [IO.Path]::DirectorySeparatorChar)
        [void]$canonical.Append($relativePath)
        [void]$canonical.Append([char]0)
        [void]$canonical.Append((Get-Sha256Hex -LiteralPath $literalPath))
        [void]$canonical.Append("`n")
    }
    $bytes = [Text.Encoding]::UTF8.GetBytes($canonical.ToString())
    $algorithm = [Security.Cryptography.SHA256]::Create()
    try {
        $digest = $algorithm.ComputeHash($bytes)
    } finally {
        $algorithm.Dispose()
    }
    return [pscustomobject]@{
        Sha256 = ([BitConverter]::ToString($digest)).Replace(
            '-',
            ''
        ).ToLowerInvariant()
        FileCount = $paths.Count
    }
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
    $contentSha256 = (
        [string]$package.content_sha256
    ).Trim().ToLowerInvariant()
    if ($contentSha256 -notmatch '^[a-f0-9]{64}$') {
        throw '更新清单中的客户端内容 SHA256 无效。'
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

function Get-InstalledClientInfo([string]$Version) {
    $programBase = Join-Path $env:LOCALAPPDATA 'Programs\LingxingERP'
    $root = [IO.Path]::GetFullPath((Join-Path $programBase $Version))
    $base = [IO.Path]::GetFullPath($programBase)
    $prefix = $base.TrimEnd('\', '/') + [IO.Path]::DirectorySeparatorChar
    if (-not $root.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
        return $null
    }
    $versionFile = Join-Path $root 'VERSION.txt'
    $application = Join-Path $root 'dist\ERP自动化\ERP自动化.exe'
    $launcher = Join-Path $root 'scripts\start_shared_desktop.ps1'
    $updater = Join-Path $root 'scripts\update_shared_client.ps1'
    $installer = Join-Path $root 'scripts\install_shared_client.ps1'
    $promoter = Join-Path $root 'scripts\promote_portable_client.ps1'
    foreach ($required in @(
        $versionFile,
        $application,
        $launcher,
        $updater,
        $installer,
        $promoter
    )) {
        if (
            -not (Test-Path -LiteralPath $required -PathType Leaf) -or
            (Get-Item -LiteralPath $required).Length -le 0
        ) {
            return $null
        }
    }
    if ((Get-Content -LiteralPath $versionFile -Raw).Trim() -ne $Version) {
        return $null
    }
    return [pscustomobject]@{
        Root = $root
        VersionFile = $versionFile
        Application = $application
        Launcher = $launcher
        Updater = $updater
        Installer = $installer
        Promoter = $promoter
        Receipt = (Join-Path $root 'install-receipt.json')
    }
}

function Write-InstallReceipt(
    $Installed,
    [string]$Version,
    [string]$PackageSha256,
    [string]$ContentSha256
) {
    $content = Get-DirectoryContentInfo -Root $Installed.Root
    $expectedContent = $ContentSha256.ToLowerInvariant()
    if ($content.Sha256 -ne $expectedContent) {
        throw '安装后的客户端内容与正式发布清单不一致。'
    }
    $payload = [ordered]@{
        schema_version = 1
        version = $Version
        package_sha256 = $PackageSha256.ToLowerInvariant()
        content_sha256 = $expectedContent
        file_count = [int]$content.FileCount
    } | ConvertTo-Json -Depth 5
    $temporary = Join-Path $Installed.Root (
        '.install-receipt-' + [Guid]::NewGuid().ToString('N') + '.tmp'
    )
    try {
        [IO.File]::WriteAllText(
            $temporary,
            $payload + [Environment]::NewLine,
            [Text.UTF8Encoding]::new($false)
        )
        Move-Item -LiteralPath $temporary -Destination $Installed.Receipt -Force
    } finally {
        if (Test-Path -LiteralPath $temporary -PathType Leaf) {
            Remove-Item -LiteralPath $temporary -Force
        }
    }
}

function Test-InstallReceipt(
    $Installed,
    [string]$Version,
    [string]$PackageSha256,
    [string]$ContentSha256
) {
    if (-not (Test-Path -LiteralPath $Installed.Receipt -PathType Leaf)) {
        return $false
    }
    try {
        $receipt = [IO.File]::ReadAllText(
            $Installed.Receipt,
            [Text.Encoding]::UTF8
        ) | ConvertFrom-Json
        if (
            [int]$receipt.schema_version -ne 1 -or
            [string]$receipt.version -ne $Version -or
            [string]$receipt.package_sha256 -ne
                $PackageSha256.ToLowerInvariant() -or
            [string]$receipt.content_sha256 -ne
                $ContentSha256.ToLowerInvariant() -or
            [int]$receipt.file_count -le 0
        ) {
            return $false
        }
        $content = Get-DirectoryContentInfo -Root $Installed.Root
        return (
            $content.Sha256 -eq $ContentSha256.ToLowerInvariant() -and
            [int]$content.FileCount -eq [int]$receipt.file_count
        )
    } catch {
        return $false
    }
}

function Get-ReusableInstalledClient($Manifest) {
    $version = [string]$Manifest.version
    $packageSha256 = ([string]$Manifest.package.sha256).ToLowerInvariant()
    $contentSha256 = (
        [string]$Manifest.package.content_sha256
    ).ToLowerInvariant()
    $installed = Get-InstalledClientInfo $version
    if ($null -eq $installed) {
        return $null
    }
    if (Test-InstallReceipt `
        $installed `
        $version `
        $packageSha256 `
        $contentSha256
    ) {
        return $installed
    }
    return $null
}

function Quote-ProcessArgument([string]$Value) {
    if ($Value.Contains('"')) {
        throw '更新辅助程序参数不能包含双引号。'
    }
    return '"' + $Value + '"'
}

function Start-PortableClientPromotion(
    $Installed,
    [string]$Current,
    [string]$Latest
) {
    $candidate = ([string]$CurrentPackageRoot).Trim()
    if (-not $candidate -or $CurrentProcessId -le 0) {
        return
    }
    if (-not (Test-Path -LiteralPath $candidate -PathType Container)) {
        return
    }
    $targetRoot = (Resolve-Path -LiteralPath $candidate).Path
    $sourceRoot = [IO.Path]::GetFullPath([string]$Installed.Root)
    if ($targetRoot.Equals($sourceRoot, [StringComparison]::OrdinalIgnoreCase)) {
        return
    }
    $programBase = [IO.Path]::GetFullPath(
        (Join-Path $env:LOCALAPPDATA 'Programs\LingxingERP')
    )
    $programPrefix = $programBase.TrimEnd('\', '/') +
        [IO.Path]::DirectorySeparatorChar
    if ($targetRoot.StartsWith(
        $programPrefix,
        [StringComparison]::OrdinalIgnoreCase
    )) {
        return
    }
    if ($targetRoot -eq [IO.Path]::GetPathRoot($targetRoot)) {
        return
    }
    $targetApplication = Join-Path (
        $targetRoot
    ) 'dist\ERP自动化\ERP自动化.exe'
    $targetUpdater = Join-Path $targetRoot 'scripts\update_shared_client.ps1'
    foreach ($required in @(
        $targetApplication,
        $targetUpdater
    )) {
        if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
            return
        }
    }
    $helper = Join-Path $PSScriptRoot 'promote_portable_client.ps1'
    if (-not (Test-Path -LiteralPath $helper -PathType Leaf)) {
        return
    }
    $argumentList = @(
        '-NoProfile',
        '-ExecutionPolicy', 'Bypass',
        '-WindowStyle', 'Hidden',
        '-File', (Quote-ProcessArgument $helper),
        '-SourcePackageRoot', (Quote-ProcessArgument $sourceRoot),
        '-TargetPackageRoot', (Quote-ProcessArgument $targetRoot),
        '-ExpectedCurrentVersion', (Quote-ProcessArgument $Current),
        '-ExpectedVersion', (Quote-ProcessArgument $Latest),
        '-ExpectedTargetSha256', (
            Quote-ProcessArgument (Get-Sha256Hex -LiteralPath $targetApplication)
        ),
        '-WaitProcessId', [string]$CurrentProcessId
    ) -join ' '
    Start-Process `
        -FilePath "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe" `
        -ArgumentList $argumentList `
        -WindowStyle Hidden | Out-Null
}

function Show-UpdateConfirmation([string]$Current, [string]$Latest) {
    if ($AssumeYes) {
        return $true
    }
    Add-Type -AssemblyName System.Windows.Forms
    Add-Type -AssemblyName System.Drawing
    [System.Windows.Forms.Application]::EnableVisualStyles()

    $form = New-Object System.Windows.Forms.Form
    $form.Text = 'ERP 自动化客户端更新'
    $form.StartPosition = 'CenterScreen'
    $form.FormBorderStyle = 'FixedDialog'
    $form.MaximizeBox = $false
    $form.MinimizeBox = $false
    $form.ShowInTaskbar = $true
    $form.TopMost = $true
    $form.AutoScaleMode = 'Dpi'
    $form.ClientSize = New-Object System.Drawing.Size(540, 336)
    $form.BackColor = [Drawing.Color]::FromArgb(244, 247, 252)

    $card = New-Object System.Windows.Forms.Panel
    $card.Location = New-Object System.Drawing.Point(20, 20)
    $card.Size = New-Object System.Drawing.Size(500, 296)
    $card.BackColor = [Drawing.Color]::White
    $card.BorderStyle = 'FixedSingle'
    $form.Controls.Add($card)

    $badge = New-Object System.Windows.Forms.Label
    $badge.Text = 'UP'
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
    $title.Text = '发现新版本'
    $title.Font = New-Object System.Drawing.Font(
        'Microsoft YaHei UI',
        14,
        [Drawing.FontStyle]::Bold
    )
    $title.ForeColor = [Drawing.Color]::FromArgb(16, 33, 58)
    $title.AutoSize = $false
    $title.Size = New-Object System.Drawing.Size(380, 29)
    $title.Location = New-Object System.Drawing.Point(84, 18)
    $card.Controls.Add($title)

    $subtitle = New-Object System.Windows.Forms.Label
    $subtitle.Text = '更新后程序会自动重新打开，无需手工安装。'
    $subtitle.Font = New-Object System.Drawing.Font('Microsoft YaHei UI', 9)
    $subtitle.ForeColor = [Drawing.Color]::FromArgb(102, 117, 140)
    $subtitle.AutoSize = $false
    $subtitle.Size = New-Object System.Drawing.Size(380, 23)
    $subtitle.Location = New-Object System.Drawing.Point(84, 47)
    $card.Controls.Add($subtitle)

    $versionPanel = New-Object System.Windows.Forms.Panel
    $versionPanel.Location = New-Object System.Drawing.Point(22, 86)
    $versionPanel.Size = New-Object System.Drawing.Size(454, 70)
    $versionPanel.BackColor = [Drawing.Color]::FromArgb(247, 249, 253)
    $versionPanel.BorderStyle = 'FixedSingle'
    $card.Controls.Add($versionPanel)

    $currentCaption = New-Object System.Windows.Forms.Label
    $currentCaption.Text = '当前版本'
    $currentCaption.Font = New-Object System.Drawing.Font('Microsoft YaHei UI', 8)
    $currentCaption.ForeColor = [Drawing.Color]::FromArgb(123, 135, 154)
    $currentCaption.AutoSize = $true
    $currentCaption.Location = New-Object System.Drawing.Point(16, 11)
    $versionPanel.Controls.Add($currentCaption)

    $currentValue = New-Object System.Windows.Forms.Label
    $currentValue.Text = $Current
    $currentValue.Font = New-Object System.Drawing.Font(
        'Microsoft YaHei UI',
        10,
        [Drawing.FontStyle]::Bold
    )
    $currentValue.ForeColor = [Drawing.Color]::FromArgb(64, 81, 107)
    $currentValue.AutoSize = $true
    $currentValue.Location = New-Object System.Drawing.Point(16, 34)
    $versionPanel.Controls.Add($currentValue)

    $arrow = New-Object System.Windows.Forms.Label
    $arrow.Text = '→'
    $arrow.Font = New-Object System.Drawing.Font(
        'Microsoft YaHei UI',
        13,
        [Drawing.FontStyle]::Bold
    )
    $arrow.ForeColor = [Drawing.Color]::FromArgb(47, 111, 237)
    $arrow.TextAlign = 'MiddleCenter'
    $arrow.Size = New-Object System.Drawing.Size(50, 30)
    $arrow.Location = New-Object System.Drawing.Point(202, 25)
    $versionPanel.Controls.Add($arrow)

    $latestCaption = New-Object System.Windows.Forms.Label
    $latestCaption.Text = '最新版本'
    $latestCaption.Font = New-Object System.Drawing.Font('Microsoft YaHei UI', 8)
    $latestCaption.ForeColor = [Drawing.Color]::FromArgb(47, 111, 237)
    $latestCaption.AutoSize = $true
    $latestCaption.Location = New-Object System.Drawing.Point(286, 11)
    $versionPanel.Controls.Add($latestCaption)

    $latestValue = New-Object System.Windows.Forms.Label
    $latestValue.Text = $Latest
    $latestValue.Font = New-Object System.Drawing.Font(
        'Microsoft YaHei UI',
        10,
        [Drawing.FontStyle]::Bold
    )
    $latestValue.ForeColor = [Drawing.Color]::FromArgb(36, 95, 206)
    $latestValue.AutoSize = $true
    $latestValue.Location = New-Object System.Drawing.Point(286, 34)
    $versionPanel.Controls.Add($latestValue)

    $requiredPanel = New-Object System.Windows.Forms.Panel
    $requiredPanel.Location = New-Object System.Drawing.Point(22, 170)
    $requiredPanel.Size = New-Object System.Drawing.Size(454, 48)
    $requiredPanel.BackColor = [Drawing.Color]::FromArgb(255, 248, 229)
    $requiredPanel.BorderStyle = 'FixedSingle'
    $card.Controls.Add($requiredPanel)

    $requiredLabel = New-Object System.Windows.Forms.Label
    $requiredLabel.Text = '为保证数据与服务器兼容，本次更新为必需更新。'
    $requiredLabel.Font = New-Object System.Drawing.Font('Microsoft YaHei UI', 9)
    $requiredLabel.ForeColor = [Drawing.Color]::FromArgb(133, 91, 0)
    $requiredLabel.TextAlign = 'MiddleLeft'
    $requiredLabel.AutoSize = $false
    $requiredLabel.Size = New-Object System.Drawing.Size(424, 46)
    $requiredLabel.Location = New-Object System.Drawing.Point(14, 0)
    $requiredPanel.Controls.Add($requiredLabel)

    $exitButton = New-Object System.Windows.Forms.Button
    $exitButton.Text = '退出程序'
    $exitButton.DialogResult = [System.Windows.Forms.DialogResult]::Cancel
    $exitButton.FlatStyle = 'Flat'
    $exitButton.FlatAppearance.BorderColor = [Drawing.Color]::FromArgb(207, 217, 232)
    $exitButton.BackColor = [Drawing.Color]::White
    $exitButton.ForeColor = [Drawing.Color]::FromArgb(52, 68, 94)
    $exitButton.Font = New-Object System.Drawing.Font(
        'Microsoft YaHei UI',
        9,
        [Drawing.FontStyle]::Bold
    )
    $exitButton.Size = New-Object System.Drawing.Size(112, 38)
    $exitButton.Location = New-Object System.Drawing.Point(242, 236)
    $card.Controls.Add($exitButton)

    $updateButton = New-Object System.Windows.Forms.Button
    $updateButton.Text = '立即更新'
    $updateButton.DialogResult = [System.Windows.Forms.DialogResult]::OK
    $updateButton.FlatStyle = 'Flat'
    $updateButton.FlatAppearance.BorderColor = [Drawing.Color]::FromArgb(47, 111, 237)
    $updateButton.BackColor = [Drawing.Color]::FromArgb(47, 111, 237)
    $updateButton.ForeColor = [Drawing.Color]::White
    $updateButton.Font = New-Object System.Drawing.Font(
        'Microsoft YaHei UI',
        9,
        [Drawing.FontStyle]::Bold
    )
    $updateButton.Size = New-Object System.Drawing.Size(112, 38)
    $updateButton.Location = New-Object System.Drawing.Point(364, 236)
    $card.Controls.Add($updateButton)

    $form.AcceptButton = $updateButton
    $form.CancelButton = $exitButton
    try {
        $choice = $form.ShowDialog()
        return $choice -eq [System.Windows.Forms.DialogResult]::OK
    } finally {
        $form.Dispose()
    }
}

function New-DownloadWindow([string]$Version) {
    if ($AssumeYes) {
        return $null
    }
    Add-Type -AssemblyName System.Windows.Forms
    Add-Type -AssemblyName System.Drawing
    [System.Windows.Forms.Application]::EnableVisualStyles()

    $form = New-Object System.Windows.Forms.Form
    $form.Text = 'ERP 自动化客户端更新'
    $form.StartPosition = 'CenterScreen'
    $form.FormBorderStyle = 'FixedDialog'
    $form.ControlBox = $false
    $form.MaximizeBox = $false
    $form.MinimizeBox = $false
    $form.ShowInTaskbar = $true
    $form.TopMost = $true
    $form.AutoScaleMode = 'Dpi'
    $form.ClientSize = New-Object System.Drawing.Size(520, 250)
    $form.BackColor = [Drawing.Color]::FromArgb(244, 247, 252)

    $card = New-Object System.Windows.Forms.Panel
    $card.Location = New-Object System.Drawing.Point(20, 20)
    $card.Size = New-Object System.Drawing.Size(480, 210)
    $card.BackColor = [Drawing.Color]::White
    $card.BorderStyle = 'FixedSingle'
    $form.Controls.Add($card)

    $badge = New-Object System.Windows.Forms.Label
    $badge.Text = 'UP'
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
    $title.Text = '正在更新 ERP 自动化'
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

    $versionLabel = New-Object System.Windows.Forms.Label
    $versionLabel.Text = "正在获取版本 $Version"
    $versionLabel.Font = New-Object System.Drawing.Font('Microsoft YaHei UI', 9)
    $versionLabel.ForeColor = [Drawing.Color]::FromArgb(102, 117, 140)
    $versionLabel.AutoSize = $false
    $versionLabel.Size = New-Object System.Drawing.Size(370, 23)
    $versionLabel.Location = New-Object System.Drawing.Point(84, 47)
    $card.Controls.Add($versionLabel)

    $label = New-Object System.Windows.Forms.Label
    $label.Text = '正在连接安全下载源…'
    $label.AutoSize = $false
    $label.Size = New-Object System.Drawing.Size(434, 28)
    $label.Location = New-Object System.Drawing.Point(22, 91)
    $label.Font = New-Object System.Drawing.Font('Microsoft YaHei UI', 9)
    $label.ForeColor = [Drawing.Color]::FromArgb(38, 58, 87)
    $card.Controls.Add($label)

    $progressTrack = New-Object System.Windows.Forms.Panel
    $progressTrack.Size = New-Object System.Drawing.Size(434, 10)
    $progressTrack.Location = New-Object System.Drawing.Point(22, 127)
    $progressTrack.BackColor = [Drawing.Color]::FromArgb(231, 237, 247)
    $card.Controls.Add($progressTrack)

    $progressFill = New-Object System.Windows.Forms.Panel
    $progressFill.Size = New-Object System.Drawing.Size(0, 10)
    $progressFill.Location = New-Object System.Drawing.Point(0, 0)
    $progressFill.BackColor = [Drawing.Color]::FromArgb(47, 111, 237)
    $progressTrack.Controls.Add($progressFill)

    $hint = New-Object System.Windows.Forms.Label
    $hint.Text = '下载与完整性校验完成后，程序会自动重新打开。'
    $hint.Font = New-Object System.Drawing.Font('Microsoft YaHei UI', 8)
    $hint.ForeColor = [Drawing.Color]::FromArgb(123, 135, 154)
    $hint.AutoSize = $false
    $hint.Size = New-Object System.Drawing.Size(434, 24)
    $hint.Location = New-Object System.Drawing.Point(22, 151)
    $card.Controls.Add($hint)

    $form.Tag = [pscustomobject]@{
        Label = $label
        ProgressTrack = $progressTrack
        ProgressFill = $progressFill
        VersionLabel = $versionLabel
    }
    $form.Show()
    $form.Activate()
    [System.Windows.Forms.Application]::DoEvents()
    return $form
}

function Set-DownloadProgress($Window, [int64]$Downloaded, [int64]$Total) {
    if ($null -eq $Window) {
        return
    }
    if ($Total -gt 0) {
        $fraction = [Math]::Min(1.0, [double]$Downloaded / [double]$Total)
        $Window.Tag.ProgressFill.Width = [int](
            [Math]::Floor($fraction * $Window.Tag.ProgressTrack.Width)
        )
        $Window.Tag.Label.Text = '正在下载更新… {0:N1} / {1:N1} MB' -f (
            $Downloaded / 1MB
        ), ($Total / 1MB)
    } else {
        $Window.Tag.ProgressFill.Width = 64
        $Window.Tag.Label.Text = '正在下载更新…'
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

function Copy-VerifiedReleasePackage(
    [string]$SourceUrl,
    [string]$Destination,
    [int64]$ExpectedSize,
    [string]$ExpectedSha256,
    [string]$Version
) {
    $maximumAttempts = if ($PackageFile) { 1 } else { 3 }
    for ($attempt = 1; $attempt -le $maximumAttempts; $attempt++) {
        if (Test-Path -LiteralPath $Destination -PathType Leaf) {
            Remove-Item -LiteralPath $Destination -Force
        }
        try {
            Copy-ReleasePackage `
                $SourceUrl `
                $Destination `
                $ExpectedSize `
                $Version
            $downloaded = Get-Item -LiteralPath $Destination
            if ($downloaded.Length -ne $ExpectedSize) {
                throw '客户端下载大小与更新清单不一致。'
            }
            $actualHash = Get-Sha256Hex -LiteralPath $Destination
            if ($actualHash -ne $ExpectedSha256.ToLowerInvariant()) {
                throw '客户端更新包 SHA256 校验失败。'
            }
            return
        } catch {
            if ($attempt -ge $maximumAttempts) {
                throw
            }
            Start-Sleep -Seconds ([Math]::Pow(2, $attempt - 1))
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
        $totalExpandedSize = [int64]0
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
            if ([int64]$entry.Length -gt 2GB) {
                throw "客户端 ZIP 单个文件过大：$relative"
            }
            $totalExpandedSize += [int64]$entry.Length
            if ($totalExpandedSize -gt 8GB) {
                throw '客户端 ZIP 解压后总大小超过安全限制。'
            }
        }
    } finally {
        $archive.Dispose()
    }
}

function Assert-SufficientUpdateSpace(
    [string]$DestinationRoot,
    [int64]$PackageSize
) {
    $fullPath = [IO.Path]::GetFullPath($DestinationRoot)
    $driveRoot = [IO.Path]::GetPathRoot($fullPath)
    $drive = [IO.DriveInfo]::new($driveRoot)
    $required = [Math]::Max(
        [int64](512MB),
        [int64]($PackageSize * 4)
    )
    if ($drive.AvailableFreeSpace -lt $required) {
        throw (
            '磁盘可用空间不足，至少需要 {0:N0} MB。' -f
            ($required / 1MB)
        )
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

function Invoke-InstalledApplicationSmokeTest($Installed) {
    if ($SkipApplicationSmokeTest) {
        return
    }
    $smokeRoot = Join-Path $StateRoot (
        'smoke-test-' + [Guid]::NewGuid().ToString('N')
    )
    $previousHome = $env:ERP_AUTOMATION_HOME
    try {
        [IO.Directory]::CreateDirectory($smokeRoot) | Out-Null
        $env:ERP_AUTOMATION_HOME = $smokeRoot
        $process = Start-Process `
            -FilePath $Installed.Application `
            -ArgumentList '--release-smoke-test' `
            -WorkingDirectory $Installed.Root `
            -WindowStyle Hidden `
            -Wait `
            -PassThru
        if ($process.ExitCode -ne 0) {
            throw (
                '新版本客户端启动自检失败，已保留原快捷方式。' +
                "退出代码：$($process.ExitCode)"
            )
        }
    } finally {
        $env:ERP_AUTOMATION_HOME = $previousHome
        if (Test-Path -LiteralPath $smokeRoot -PathType Container) {
            Remove-Item -LiteralPath $smokeRoot -Recurse -Force
        }
    }
}

function Set-InstalledClientActive($Installed, $Manifest) {
    Invoke-InstalledApplicationSmokeTest $Installed
    if (-not (Test-InstallReceipt `
        $Installed `
        ([string]$Manifest.version) `
        ([string]$Manifest.package.sha256) `
        ([string]$Manifest.package.content_sha256)
    )) {
        throw '新版本客户端自检后的内容复核失败，已保留原快捷方式。'
    }
    $activationArguments = @{
        PackageRoot = $Installed.Root
        ActivateOnly = $true
        Silent = $true
    }
    if ($DesktopDirectory) {
        $activationArguments.DesktopDirectory = $DesktopDirectory
    }
    & $Installed.Installer @activationArguments | Out-Null
}

function Remove-StaleClientArtifacts(
    [string]$ActiveVersion,
    [int]$KeepVersionCount = 2
) {
    $programBase = Join-Path $env:LOCALAPPDATA 'Programs\LingxingERP'
    if (-not (Test-Path -LiteralPath $programBase -PathType Container)) {
        return
    }
    $runningPaths = @(
        Get-Process -ErrorAction SilentlyContinue |
            ForEach-Object {
                try { [string]$_.Path } catch { '' }
            } |
            Where-Object { $_ }
    )
    $versions = @(
        Get-ChildItem -LiteralPath $programBase -Directory -Force |
            Where-Object { $_.Name -match '^\d{4}\.\d{2}\.\d{2}\.\d+$' } |
            Sort-Object {
                $parts = ConvertTo-VersionParts $_.Name
                '{0:D6}.{1:D3}.{2:D3}.{3:D10}' -f $parts
            } -Descending
    )
    $keep = [Collections.Generic.HashSet[string]]::new(
        [StringComparer]::OrdinalIgnoreCase
    )
    [void]$keep.Add($ActiveVersion)
    foreach ($directory in @($versions | Select-Object -First $KeepVersionCount)) {
        [void]$keep.Add($directory.Name)
    }
    foreach ($directory in $versions) {
        if ($keep.Contains($directory.Name)) {
            continue
        }
        $prefix = $directory.FullName.TrimEnd('\', '/') +
            [IO.Path]::DirectorySeparatorChar
        if (@($runningPaths | Where-Object {
            $_.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)
        }).Count -gt 0) {
            continue
        }
        try {
            Remove-Item -LiteralPath $directory.FullName -Recurse -Force
        } catch {
            # Cleanup is best-effort and must never invalidate a good update.
        }
    }
    $cutoff = [DateTime]::UtcNow.AddHours(-24)
    foreach ($artifact in @(
        Get-ChildItem -LiteralPath $programBase -Directory -Force |
            Where-Object {
                (
                    $_.Name -match
                        '^\.\d{4}\.\d{2}\.\d{2}\.\d+\.(install|replace)-'
                ) -and
                $_.LastWriteTimeUtc -lt $cutoff
            }
    )) {
        try {
            Remove-Item -LiteralPath $artifact.FullName -Recurse -Force
        } catch {
            # A current installer may still own the path.
        }
    }
    if (Test-Path -LiteralPath $updatesRoot -PathType Container) {
        foreach ($attempt in @(
            Get-ChildItem -LiteralPath $updatesRoot -Directory -Force |
                Where-Object { $_.LastWriteTimeUtc -lt $cutoff }
        )) {
            try {
                Remove-Item -LiteralPath $attempt.FullName -Recurse -Force
            } catch {
                # A current updater may still own the path.
            }
        }
    }
}

function Repair-CurrentInstalledClientReceipt(
    [string]$Version,
    $Manifest
) {
    $candidate = ([string]$CurrentPackageRoot).Trim()
    if (-not $candidate -or -not (Test-Path -LiteralPath $candidate -PathType Container)) {
        return $null
    }
    $programBase = [IO.Path]::GetFullPath(
        (Join-Path $env:LOCALAPPDATA 'Programs\LingxingERP')
    )
    $expectedRoot = [IO.Path]::GetFullPath(
        (Join-Path $programBase $Version)
    )
    $currentRoot = (Resolve-Path -LiteralPath $candidate).Path
    if (-not $currentRoot.Equals(
        $expectedRoot,
        [StringComparison]::OrdinalIgnoreCase
    )) {
        return $null
    }

    # VERSION.txt is package metadata, not the running version authority.
    # Repairing only this marker is safe and prevents a harmless stale file
    # from forcing a full reinstall.
    $versionFile = Join-Path $currentRoot 'VERSION.txt'
    $storedVersion = if (Test-Path -LiteralPath $versionFile -PathType Leaf) {
        (Get-Content -LiteralPath $versionFile -Raw).Trim()
    } else {
        ''
    }
    if ($storedVersion -ne $Version) {
        $temporaryVersion = Join-Path $currentRoot (
            '.VERSION-repair-' + [Guid]::NewGuid().ToString('N') + '.tmp'
        )
        try {
            [IO.File]::WriteAllText(
                $temporaryVersion,
                $Version + [Environment]::NewLine,
                [Text.UTF8Encoding]::new($false)
            )
            Move-Item -LiteralPath $temporaryVersion `
                -Destination $versionFile -Force
        } finally {
            if (Test-Path -LiteralPath $temporaryVersion -PathType Leaf) {
                Remove-Item -LiteralPath $temporaryVersion -Force
            }
        }
    }

    $installed = Get-InstalledClientInfo $Version
    if ($null -eq $installed) {
        throw '当前正式客户端安装结构损坏，请重新运行安装包修复。'
    }
    $packageSha256 = ([string]$Manifest.package.sha256).ToLowerInvariant()
    $contentSha256 = (
        [string]$Manifest.package.content_sha256
    ).ToLowerInvariant()
    if (-not (Test-InstallReceipt `
        $installed `
        $Version `
        $packageSha256 `
        $contentSha256
    )) {
        $content = Get-DirectoryContentInfo -Root $installed.Root
        if ($content.Sha256 -ne $contentSha256) {
            throw (
                '当前正式客户端文件与发布内容不一致，请重新运行安装包修复。'
            )
        }
        Write-InstallReceipt `
            $installed `
            $Version `
            $packageSha256 `
            $contentSha256
    }
    if (-not (Test-InstallReceipt `
        $installed `
        $Version `
        $packageSha256 `
        $contentSha256
    )) {
        throw '当前正式客户端安装收据复核失败。'
    }
    return $installed
}

if ($GraceHours -lt 1 -or $GraceHours -gt 168) {
    throw '更新检查宽限时间必须介于 1 到 168 小时。'
}
if (
    $SkipApplicationSmokeTest -and
    (-not $ManifestFile -or -not $PackageFile)
) {
    throw '正式网络更新不允许跳过新版本 EXE 启动自检。'
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
$previousState = Read-UpdateState
if (
    $null -ne $previousState -and
    (Compare-ClientVersion `
        $latestVersion `
        ([string]$previousState.latest_version)
    ) -lt 0
) {
    throw (
        '正式更新清单低于本机已经确认过的版本，已拒绝可能的版本回退。'
    )
}
$comparison = Compare-ClientVersion $currentVersion $latestVersion
if ($comparison -gt 0) {
    throw (
        "当前 EXE 内置版本 $currentVersion 高于正式发布版本 $latestVersion，" +
        '拒绝继续启动。请重新安装正式客户端。'
    )
}

if ($comparison -eq 0) {
    [void](Repair-CurrentInstalledClientReceipt $latestVersion $manifest)
    Write-UpdateState $latestVersion ([string]$manifest.package.sha256)
    Remove-StaleClientArtifacts $latestVersion
    $result = New-UpdateResult 'current' $currentVersion $latestVersion
    if ($OutputJson) { $result | ConvertTo-Json -Compress } else { $result }
    return
}
Write-UpdateState $latestVersion ([string]$manifest.package.sha256)
if ($CheckOnly) {
    $result = New-UpdateResult 'update_required' $currentVersion $latestVersion
    if ($OutputJson) { $result | ConvertTo-Json -Compress } else { $result }
    return
}
$reusable = Get-ReusableInstalledClient $manifest
if ($null -ne $reusable) {
    Set-InstalledClientActive $reusable $manifest
    Start-PortableClientPromotion $reusable $currentVersion $latestVersion
    Remove-StaleClientArtifacts $latestVersion
    $result = New-UpdateResult `
        'updated' `
        $currentVersion `
        $latestVersion `
        $reusable.Launcher `
        $reusable.Application
    if ($OutputJson) { $result | ConvertTo-Json -Compress } else { $result }
    return
}
if (-not (Show-UpdateConfirmation $currentVersion $latestVersion)) {
    $result = New-UpdateResult 'user_exit' $currentVersion $latestVersion
    if ($OutputJson) { $result | ConvertTo-Json -Compress } else { $result }
    return
}

[IO.Directory]::CreateDirectory($updatesRoot) | Out-Null
Assert-SufficientUpdateSpace `
    $updatesRoot `
    ([int64]$manifest.package.size)
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
    # Another client may have completed the same update while this process
    # waited for the mutex.  Re-check before downloading or touching disk.
    $reusableAfterWait = Get-ReusableInstalledClient $manifest
    if ($null -ne $reusableAfterWait) {
        Set-InstalledClientActive $reusableAfterWait $manifest
        Start-PortableClientPromotion `
            $reusableAfterWait $currentVersion $latestVersion
        Remove-StaleClientArtifacts $latestVersion
        $result = New-UpdateResult `
            'updated' `
            $currentVersion `
            $latestVersion `
            $reusableAfterWait.Launcher `
            $reusableAfterWait.Application
        if ($OutputJson) { $result | ConvertTo-Json -Compress } else { $result }
        return
    }
    [IO.Directory]::CreateDirectory($attemptRoot) | Out-Null
    $zipPath = Join-Path $attemptRoot $expectedAssetName
    Copy-VerifiedReleasePackage `
        ([string]$manifest.package.url) `
        $zipPath `
        ([int64]$manifest.package.size) `
        ([string]$manifest.package.sha256) `
        $latestVersion
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
        SkipLegacyPortablePromotion = $true
        SkipShortcut = $true
        SkipApplicationSmokeTest = $true
    }
    if ($DesktopDirectory) {
        $installerArguments.DesktopDirectory = $DesktopDirectory
    }
    & $installer @installerArguments | Out-Null

    $installed = Get-InstalledClientInfo $latestVersion
    if ($null -eq $installed) {
        throw '更新完成后客户端安装目录不完整。'
    }
    Write-InstallReceipt `
        $installed `
        $latestVersion `
        ([string]$manifest.package.sha256) `
        ([string]$manifest.package.content_sha256)
    if (-not (Test-InstallReceipt `
        $installed `
        $latestVersion `
        ([string]$manifest.package.sha256) `
        ([string]$manifest.package.content_sha256)
    )) {
        throw '更新完成后客户端安装校验失败。'
    }
    Set-InstalledClientActive $installed $manifest
    Write-UpdateState $latestVersion ([string]$manifest.package.sha256)
    Start-PortableClientPromotion $installed $currentVersion $latestVersion
    Remove-StaleClientArtifacts $latestVersion
    $result = New-UpdateResult `
        'updated' `
        $currentVersion `
        $latestVersion `
        $installed.Launcher `
        $installed.Application
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
