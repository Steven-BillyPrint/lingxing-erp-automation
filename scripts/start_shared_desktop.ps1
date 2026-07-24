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
    [string]$ApplicationPath = ''
)

$ErrorActionPreference = 'Stop'
$workspace = Split-Path -Parent $PSScriptRoot

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

function Quote-NativeArgument([string]$Value) {
    if ($Value.Contains('"')) {
        throw 'Native command arguments may not contain a double quote.'
    }
    return '"' + $Value + '"'
}

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

try {
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

    $env:ERP_AUTOMATION_SERVER_URL = "http://127.0.0.1:${LocalPort}"
    $env:ERP_AUTOMATION_SERVER_TOKEN = $token
    $env:ERP_AUTOMATION_INSTANCE_NAME = $InstanceName
    if ($isPackagedApplication) {
        $applicationProcess = Start-Process `
            -FilePath $ApplicationPath `
            -PassThru `
            -Wait
        exit $applicationProcess.ExitCode
    } else {
        & $PythonPath $ApplicationPath
        exit $LASTEXITCODE
    }
} finally {
    $env:ERP_AUTOMATION_SERVER_TOKEN = $null
    if (-not $tunnel.HasExited) {
        Stop-Process -Id $tunnel.Id
    }
}
