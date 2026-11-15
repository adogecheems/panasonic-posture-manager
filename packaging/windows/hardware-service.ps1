param(
    [ValidateSet('Configure', 'Install', 'Start', 'Stop', 'Status', 'Remove')]
    [string]$Action = 'Install',
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
$ServiceName = 'PanasonicPostureManagerHardware'
$simplifiedChinese = [Globalization.CultureInfo]::CurrentUICulture.Name -match '^zh-(CN|SG|Hans)(-|$)'
$DisplayName = if ($simplifiedChinese) {
    '松下二合一平板电脑姿态管理工具 硬件服务'
} else {
    'Panasonic 2-in-1 Tablet Posture Management Tool Hardware Service'
}
$Description = if ($simplifiedChinese) {
    '为松下二合一平板电脑姿态管理工具提供经过身份验证的 LocalSystem 硬件端点。'
} else {
    'Provides the authenticated LocalSystem hardware endpoint for Panasonic 2-in-1 Tablet Posture Management Tool.'
}

$identity = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $identity.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    $argumentLine = '-NoProfile -ExecutionPolicy Bypass -File "{0}" -Action {1}' -f $PSCommandPath, $Action
    if ($Force) { $argumentLine += ' -Force' }
    $elevated = Start-Process powershell.exe -Verb RunAs -Wait -PassThru -ArgumentList $argumentLine
    exit $elevated.ExitCode
}

$Root = [IO.Path]::GetFullPath((Split-Path -Parent $PSCommandPath))
$Python = Join-Path $Root 'runtime\python-hardware.exe'
$HardwarePth = Join-Path $Root 'runtime\python-hardware._pth'
$ServiceModule = Join-Path $Root 'app\panasonic_posture\windows\service\daemon.py'

function Get-ServiceRecord {
    Get-CimInstance Win32_Service -Filter "Name='$ServiceName'" -ErrorAction SilentlyContinue
}

function Wait-ServiceState([string]$State, [int]$Seconds = 30) {
    $deadline = [DateTime]::UtcNow.AddSeconds($Seconds)
    while ([DateTime]::UtcNow -lt $deadline) {
        $service = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
        if ($null -ne $service -and $service.Status.ToString() -eq $State) { return $service }
        Start-Sleep -Milliseconds 250
    }
    throw "Hardware service did not reach $State within $Seconds seconds."
}

function Stop-InstalledService {
    $service = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
    if ($null -eq $service -or $service.Status -eq 'Stopped') { return }
    Stop-Service -Name $ServiceName -Force
    Wait-ServiceState 'Stopped' 30 | Out-Null
}

function Start-InstalledService {
    $service = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
    if ($null -eq $service) { throw 'Hardware service is not installed.' }
    if ($service.Status -eq 'Running') { return $service }
    try {
        $service.Start()
    } catch {
        throw "Cannot request hardware service start: $($_.Exception.Message)"
    }
    return (Wait-ServiceState 'Running' 30)
}

function Configure-ServiceRecovery {
    & sc.exe config $ServiceName start= auto obj= LocalSystem | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Cannot configure the hardware service startup.' }
    & sc.exe failure $ServiceName reset= 86400 `
        actions= restart/5000/restart/15000/restart/60000 | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Cannot configure hardware service recovery.' }
}

function Protect-ServiceLogs {
    $stateRoot = Join-Path $env:ProgramData 'PanasonicPostureManager\State'
    New-Item -ItemType Directory -Path $stateRoot -Force | Out-Null
    & icacls.exe $stateRoot /inheritance:r `
        /grant:r '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Cannot protect the hardware service state directory.' }
    $logRoot = Join-Path $env:ProgramData 'PanasonicPostureManager\Logs'
    New-Item -ItemType Directory -Path $logRoot -Force | Out-Null
    # English SID names keep the ACL independent of Windows display language.
    & icacls.exe $logRoot /inheritance:r `
        /grant:r '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' `
        '*S-1-5-32-545:(OI)(CI)RX' | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Cannot protect the hardware service log directory.' }
}

function Test-ProtectedRuntime {
    & cmd.exe /d /c "`"$Python`" -B -I -m panasonic_posture.windows.service.daemon --verify >nul 2>&1"
    return $LASTEXITCODE -eq 0
}

if ($Action -eq 'Status') {
    $record = Get-ServiceRecord
    if ($null -eq $record) {
        [ordered]@{ installed = $false; service = $ServiceName } | ConvertTo-Json
        exit 0
    }
    [ordered]@{
        installed = $true
        service = $record.Name
        display_name = $record.DisplayName
        state = $record.State
        start_mode = $record.StartMode
        account = $record.StartName
        image_path = $record.PathName
    } | ConvertTo-Json
    exit 0
}

if ($Action -eq 'Configure') {
    if ($null -eq (Get-ServiceRecord)) {
        throw 'Hardware service is not installed.'
    }
    & sc.exe config $ServiceName start= auto obj= LocalSystem | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Cannot configure the hardware service startup.' }
    & sc.exe failure $ServiceName reset= 86400 `
        actions= restart/5000/restart/15000/restart/60000 | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Cannot configure hardware service recovery.' }
    Protect-ServiceLogs
    Start-InstalledService | Out-Null
    Write-Output 'Hardware service configuration updated.'
    exit 0
}

if ($Action -eq 'Remove') {
    Stop-InstalledService
    if ($null -ne (Get-ServiceRecord)) {
        & sc.exe delete $ServiceName | Out-Null
        if ($LASTEXITCODE -ne 0) { throw 'Cannot delete the hardware service.' }
    }
    Write-Output 'Hardware service removed. The protected installation and logs were kept.'
    exit 0
}

foreach ($path in @($Python, $HardwarePth, $ServiceModule)) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "Hardware runtime is incomplete: $path"
    }
}

if ($Action -eq 'Install' -and -not (Test-ProtectedRuntime)) {
    $protectedRoot = [IO.Path]::GetFullPath((Join-Path $env:ProgramFiles 'PanasonicPostureManager'))
    $programFiles = [IO.Path]::GetFullPath($env:ProgramFiles).TrimEnd('\') + '\'
    if (-not $protectedRoot.StartsWith($programFiles, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Refusing an unprotected Program Files installation target.'
    }
    if ($Root.StartsWith($protectedRoot + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Cannot bootstrap a protected installation from inside its target directory.'
    }
    $record = Get-ServiceRecord
    if ($null -ne $record -and $record.State -ne 'Stopped') {
        Stop-InstalledService
    }
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $backup = Join-Path $env:ProgramFiles "PanasonicPostureManager.previous-$stamp"
    if (Test-Path -LiteralPath $backup) { throw "Protected backup path already exists: $backup" }
    $movedOld = $false
    if (Test-Path -LiteralPath $protectedRoot) {
        Move-Item -LiteralPath $protectedRoot -Destination $backup
        $movedOld = $true
    }
    try {
        New-Item -ItemType Directory -Path $protectedRoot | Out-Null
        Copy-Item -Path (Join-Path $Root '*') -Destination $protectedRoot -Recurse -Force
        $protectedInstaller = Join-Path $protectedRoot 'hardware-service.ps1'
        $arguments = '-NoProfile -ExecutionPolicy Bypass -File "{0}" -Action Install -Force' -f `
            $protectedInstaller
        $child = Start-Process powershell.exe -ArgumentList $arguments -Wait -PassThru
        if ($child.ExitCode -ne 0) { throw "Protected installer failed with exit code $($child.ExitCode)." }
        Write-Output "Installed protected bundle at $protectedRoot."
        if ($movedOld) { Write-Output "Previous protected bundle: $backup" }
        exit 0
    } catch {
        # Preserve the previous working tree if copying or installation fails.
        if (Test-Path -LiteralPath $protectedRoot) {
            $failed = Join-Path $env:ProgramFiles "PanasonicPostureManager.failed-$stamp"
            Move-Item -LiteralPath $protectedRoot -Destination $failed
        }
        if ($movedOld -and -not (Test-Path -LiteralPath $protectedRoot)) {
            Move-Item -LiteralPath $backup -Destination $protectedRoot
            $service = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
            if ($null -ne $service) {
                try {
                    Start-InstalledService | Out-Null
                } catch {
                    Write-Warning "Could not restart the previous hardware service: $($_.Exception.Message)"
                }
            }
        }
        throw
    }
}

if ($Action -eq 'Start') {
    $service = Start-InstalledService
    Write-Output "Hardware service is $($service.Status)."
    exit 0
}

if ($Action -eq 'Stop') {
    Stop-InstalledService
    Write-Output 'Hardware service is Stopped.'
    exit 0
}

if (-not (Test-ProtectedRuntime)) {
    throw 'The bundle is not in a protected runtime path or failed its security preflight.'
}
$imagePath = (& $Python -B -I -m panasonic_posture.windows.service.daemon --image-path).Trim()
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($imagePath)) {
    throw 'Cannot construct the hardware service command.'
}

$existing = Get-ServiceRecord
if ($null -ne $existing) {
    if ($existing.PathName -ne $imagePath) {
        if (-not $Force) {
            throw 'A different hardware service is already installed. Use -Force to replace it.'
        }
        Stop-InstalledService
        & sc.exe delete $ServiceName | Out-Null
        if ($LASTEXITCODE -ne 0) { throw 'Cannot replace the existing hardware service.' }
    } else {
        Protect-ServiceLogs
        Configure-ServiceRecovery
        Stop-InstalledService
        $service = Start-InstalledService
        Write-Output "Hardware service updated in place and is $($service.Status)."
        exit 0
    }
}

Protect-ServiceLogs
New-Service -Name $ServiceName -BinaryPathName $imagePath -DisplayName $DisplayName `
    -StartupType Automatic | Out-Null
& sc.exe description $ServiceName $Description | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Cannot describe the hardware service.' }
Configure-ServiceRecovery
$service = Start-InstalledService
Write-Output "Hardware service installed and $($service.Status)."
