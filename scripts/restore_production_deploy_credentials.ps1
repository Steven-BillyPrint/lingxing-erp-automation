[CmdletBinding()]
param(
    [switch]$ConfirmCredentialRestore,
    [string]$EncryptedBackupPath = 'Z:\同事个人\颜奕超\ERP自动化部署专用\erp-production-deploy-ed25519.dpapi',
    [string]$PublicKeyBackupPath = 'Z:\同事个人\颜奕超\ERP自动化部署专用\erp-production-deploy-ed25519.pub',
    [string]$KnownHostsBackupPath = 'Z:\同事个人\颜奕超\ERP自动化部署专用\known_hosts',
    [string]$TargetDirectory = (Join-Path $env:LOCALAPPDATA 'Codex\credentials')
)

$ErrorActionPreference = 'Stop'

if (-not $ConfirmCredentialRestore) {
    throw '恢复部署凭据必须显式传入 -ConfirmCredentialRestore。'
}

foreach ($requiredPath in @(
    $EncryptedBackupPath,
    $PublicKeyBackupPath,
    $KnownHostsBackupPath
)) {
    if (-not (Test-Path -LiteralPath $requiredPath -PathType Leaf)) {
        throw "部署凭据备份不存在：$requiredPath"
    }
}

$targetKeyPath = Join-Path $TargetDirectory 'erp-production-deploy-ed25519'
$targetPublicKeyPath = "$targetKeyPath.pub"
$targetKnownHostsPath = Join-Path `
    $TargetDirectory `
    'erp-production-known_hosts'
foreach ($targetPath in @(
    $targetKeyPath,
    $targetPublicKeyPath,
    $targetKnownHostsPath
)) {
    if (Test-Path -LiteralPath $targetPath) {
        throw "目标部署凭据已存在，拒绝覆盖：$targetPath"
    }
}

Add-Type -AssemblyName System.Security
New-Item -ItemType Directory -Path $TargetDirectory -Force | Out-Null

$currentSid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
$systemSid = New-Object System.Security.Principal.SecurityIdentifier(
    'S-1-5-18'
)
$administratorsSid = New-Object System.Security.Principal.SecurityIdentifier(
    'S-1-5-32-544'
)
$allowedPrincipals = @($currentSid, $systemSid, $administratorsSid)
$allow = [System.Security.AccessControl.AccessControlType]::Allow
$inheritance = [System.Security.AccessControl.InheritanceFlags](
    [System.Security.AccessControl.InheritanceFlags]::ContainerInherit -bor
    [System.Security.AccessControl.InheritanceFlags]::ObjectInherit
)
$directoryAcl = New-Object System.Security.AccessControl.DirectorySecurity
$directoryAcl.SetAccessRuleProtection($true, $false)
$directoryAcl.SetOwner($currentSid)
foreach ($principal in $allowedPrincipals) {
    $directoryAcl.AddAccessRule((
        New-Object System.Security.AccessControl.FileSystemAccessRule(
            $principal,
            [System.Security.AccessControl.FileSystemRights]::FullControl,
            $inheritance,
            [System.Security.AccessControl.PropagationFlags]::None,
            $allow
        )
    ))
}
Set-Acl -LiteralPath $TargetDirectory -AclObject $directoryAcl

$temporaryKeyPath = Join-Path `
    $TargetDirectory `
    ('.erp-production-deploy-ed25519.' + [guid]::NewGuid().ToString('N') + '.tmp')
$plainBytes = $null
try {
    $entropy = [System.Text.Encoding]::UTF8.GetBytes(
        'LingxingERP-Codex-Deploy-Key-v1'
    )
    $protectedBytes = [System.IO.File]::ReadAllBytes($EncryptedBackupPath)
    $plainBytes = [System.Security.Cryptography.ProtectedData]::Unprotect(
        $protectedBytes,
        $entropy,
        [System.Security.Cryptography.DataProtectionScope]::CurrentUser
    )
    [System.IO.File]::WriteAllBytes($temporaryKeyPath, $plainBytes)

    $fileAcl = New-Object System.Security.AccessControl.FileSecurity
    $fileAcl.SetAccessRuleProtection($true, $false)
    $fileAcl.SetOwner($currentSid)
    foreach ($principal in $allowedPrincipals) {
        $fileAcl.AddAccessRule((
            New-Object System.Security.AccessControl.FileSystemAccessRule(
                $principal,
                [System.Security.AccessControl.FileSystemRights]::FullControl,
                $allow
            )
        ))
    }
    Set-Acl -LiteralPath $temporaryKeyPath -AclObject $fileAcl

    $sshKeygen = Join-Path $env:WINDIR 'System32\OpenSSH\ssh-keygen.exe'
    $derivedPublicKey = @(& $sshKeygen -y -f $temporaryKeyPath 2>$null)
    if ($LASTEXITCODE -ne 0 -or $derivedPublicKey.Count -ne 1) {
        throw '解密后的部署私钥格式无效。'
    }
    $expectedPublicKey = @(
        [System.IO.File]::ReadAllText($PublicKeyBackupPath).Trim() -split '\s+'
    )
    $actualPublicKey = @($derivedPublicKey[0].Trim() -split '\s+')
    if (
        $expectedPublicKey.Count -lt 2 -or
        $actualPublicKey.Count -lt 2 -or
        $expectedPublicKey[0] -ne $actualPublicKey[0] -or
        $expectedPublicKey[1] -ne $actualPublicKey[1]
    ) {
        throw 'DPAPI 私钥备份与公钥备份不匹配。'
    }

    Move-Item -LiteralPath $temporaryKeyPath -Destination $targetKeyPath
    Copy-Item -LiteralPath $PublicKeyBackupPath -Destination $targetPublicKeyPath
    Copy-Item -LiteralPath $KnownHostsBackupPath -Destination $targetKnownHostsPath
    foreach ($path in @($targetPublicKeyPath, $targetKnownHostsPath)) {
        Set-Acl -LiteralPath $path -AclObject $fileAcl
    }
} finally {
    if ($plainBytes) {
        [Array]::Clear($plainBytes, 0, $plainBytes.Length)
    }
    if (Test-Path -LiteralPath $temporaryKeyPath) {
        Remove-Item -LiteralPath $temporaryKeyPath -Force
    }
}

Write-Host "部署凭据已恢复到本机受保护目录：$TargetDirectory"
