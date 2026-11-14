# Explicit, authorized SYSTEM lifecycle fixture. No driver or boot changes.
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][ValidateSet('Run', 'RunStaged', 'Cleanup')][string]$Action,
    [Parameter(Mandatory=$true)][ValidatePattern('^PostureManager[.]Validation[.][a-f0-9]{12}$')][string]$TaskName,
    [string]$Manifest,
    [ValidatePattern('^[a-f0-9]{64}$')][string]$ManifestHash
)
$ErrorActionPreference = 'Stop'
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Explicit administrator consent is required for this isolated fixture.'
}
$programs = [Environment]::GetFolderPath('ProgramFiles')
$target = [IO.Path]::GetFullPath((Join-Path $programs $TaskName))
if ([IO.Path]::GetDirectoryName($target) -ne $programs -or [IO.Path]::GetFileName($target) -ne $TaskName) {
    throw 'Target is outside the exact isolated Program Files directory.'
}
$marker = Join-Path $target 'validation-owner.json'
$utf8 = New-Object Text.UTF8Encoding($false)

function Assert-OrdinaryPath([string]$Path) {
    $item = Get-Item -LiteralPath $Path -Force
    while ($null -ne $item) {
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Reparse point rejected.' }
        if ($item -is [IO.FileInfo]) { $item = $item.Directory } else { $item = $item.Parent }
    }
}
function Write-Status($Value) {
    [IO.File]::WriteAllText((Join-Path $target 'validation/admin.json'), ($Value | ConvertTo-Json -Depth 6), $utf8)
}

if ($Action -eq 'Cleanup') {
    Assert-OrdinaryPath $target
    if ((Get-Content -LiteralPath $marker -Raw | ConvertFrom-Json).task -ne $TaskName) {
        throw 'Fixture ownership marker does not match.'
    }
    $tasks = @(Get-ScheduledTask -TaskPath '\' | Where-Object { $_.TaskName -eq $TaskName })
    if ($tasks.Count -ne 0) { throw 'Refuse deletion while a task is still registered.' }
    foreach ($item in Get-ChildItem -LiteralPath $target -Force -Recurse) {
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Reparse descendant rejected.' }
    }
    # The resolved absolute target, parent, marker and every descendant have
    # been checked. Never remove a computed parent or follow a junction.
    Remove-Item -LiteralPath $target -Recurse -Force
    exit
}

if ($Action -eq 'Run') {
if (-not $Manifest -or -not $ManifestHash) { throw 'Pinned source manifest is required.' }
Assert-OrdinaryPath $Manifest
$manifestBytes = [IO.File]::ReadAllBytes($Manifest)
$sha = [Security.Cryptography.SHA256]::Create()
try { $digest = ([BitConverter]::ToString($sha.ComputeHash($manifestBytes))).Replace('-', '').ToLowerInvariant() }
finally { $sha.Dispose() }
if ($digest -ne $ManifestHash) { throw 'Source manifest changed before elevation.' }
$entries = [Text.Encoding]::UTF8.GetString($manifestBytes) | ConvertFrom-Json
if ($entries.Count -lt 10 -or $entries.Count -gt 20000) { throw 'Unexpected source manifest size.' }
if (Test-Path -LiteralPath $target) { throw 'Refuse to reuse an existing fixture directory.' }
Assert-OrdinaryPath $programs
New-Item -ItemType Directory -Path $target | Out-Null
$acl = New-Object Security.AccessControl.DirectorySecurity
$acl.SetAccessRuleProtection($true, $false)
$admins = New-Object Security.Principal.SecurityIdentifier('S-1-5-32-544')
$acl.SetOwner($admins)
foreach ($entry in @(@('S-1-5-18', 'FullControl'), @('S-1-5-32-544', 'FullControl'), @('S-1-5-32-545', 'ReadAndExecute'))) {
    $sid = New-Object Security.Principal.SecurityIdentifier($entry[0])
    $rule = New-Object Security.AccessControl.FileSystemAccessRule($sid, $entry[1], 'ContainerInherit,ObjectInherit', 'None', 'Allow')
    $acl.AddAccessRule($rule)
}
Set-Acl -LiteralPath $target -AclObject $acl
New-Item -ItemType Directory -Path (Join-Path $target 'validation') | Out-Null
[IO.File]::WriteAllText($marker, (@{task=$TaskName} | ConvertTo-Json), $utf8)
} else {
    Assert-OrdinaryPath $target
    if ((Get-Content -LiteralPath $marker -Raw | ConvertFrom-Json).task -ne $TaskName) {
        throw 'Fixture ownership marker does not match.'
    }
    foreach ($name in @('ready.json', 'result.json')) {
        $output = Join-Path $target ('validation/' + $name)
        if (Test-Path -LiteralPath $output) { Remove-Item -LiteralPath $output }
    }
}
$registered = $false
try {
    if ($Action -eq 'Run') {
    $seen = @{}
    foreach ($entry in $entries) {
        if ($entry.relative -match '(^[\\/]|:|(^|[\\/])[.][.]([\\/]|$))' -or
            $entry.hash -notmatch '^[a-f0-9]{64}$') { throw 'Invalid source entry.' }
        $destination = [IO.Path]::GetFullPath((Join-Path $target $entry.relative))
        if (-not $destination.StartsWith($target + '\', [StringComparison]::OrdinalIgnoreCase) -or $seen.ContainsKey($destination)) {
            throw 'Invalid or duplicate destination.'
        }
        $seen[$destination] = $true
        Assert-OrdinaryPath $entry.source
        New-Item -ItemType Directory -Path ([IO.Path]::GetDirectoryName($destination)) -Force | Out-Null
        Copy-Item -LiteralPath $entry.source -Destination $destination
        if ((Get-FileHash -LiteralPath $destination -Algorithm SHA256).Hash.ToLowerInvariant() -ne $entry.hash) {
            throw 'Staged source changed or copy was incomplete.'
        }
    }
    Copy-Item -LiteralPath (Join-Path $target 'runtime/python.exe') -Destination (Join-Path $target 'runtime/python-hardware.exe')
    foreach ($name in @('python._pth', 'pythonw._pth')) {
        [IO.File]::WriteAllText((Join-Path $target ('runtime/' + $name)), "Lib`nDLLs`n../app`n", $utf8)
    }
    [IO.File]::WriteAllText((Join-Path $target 'runtime/python-hardware._pth'), "Lib`nDLLs`n../app`n", $utf8)
    & "$env:SystemRoot\System32\icacls.exe" $target /setowner '*S-1-5-32-544' /T /C /Q | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Cannot set trusted ownership for the entire fixture.' }
    }
    $python = Join-Path $target 'runtime/python-hardware.exe'
    # Validate the actual staged ACLs and isolated interpreter before SYSTEM
    # executes anything. This command only pins and reads the protected tree.
    & $python -B -I -c 'from panasonic_posture.windows.service.trusted_runtime import TrustedRuntime; r=TrustedRuntime.current(); r.close()' 2> (Join-Path $target 'validation/runtime-check.log')
    if ($LASTEXITCODE -ne 0) { throw 'Protected runtime verification failed.' }
    $arguments = '-B -I "' + (Join-Path $target 'fixture.py') + '" --system --root "' + $target + '"'
    $operation = New-ScheduledTaskAction -Execute $python -Argument $arguments -WorkingDirectory $target
    $account = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
    $settings = New-ScheduledTaskSettingsSet -Hidden -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 2)
    Register-ScheduledTask -TaskName $TaskName -Action $operation -Principal $account -Settings $settings | Out-Null
    $registered = $true
    Write-Status @{phase='starting'; task=$TaskName}
    Start-ScheduledTask -TaskName $TaskName
    $deadline = [DateTime]::UtcNow.AddSeconds(125)
    do {
        Start-Sleep -Milliseconds 200
        $task = Get-ScheduledTask -TaskName $TaskName
        $info = Get-ScheduledTaskInfo -TaskName $TaskName
        if ((Test-Path -LiteralPath (Join-Path $target 'validation/result.json')) -and $task.State -ne 'Running') { break }
        if ($task.State -ne 'Running' -and $info.LastRunTime.Year -gt 2000 -and $info.LastTaskResult -ne 267009) { break }
    } while ([DateTime]::UtcNow -lt $deadline)
    if ($task.State -eq 'Running') { throw 'SYSTEM fixture exceeded its bounded execution window.' }
    Write-Status @{phase='finished'; task=$TaskName; result=$info.LastTaskResult}
}
catch {
    Write-Status @{phase='failed'; task=$TaskName; error=$_.Exception.Message}
    throw
}
finally {
    if ($registered) {
        $task = Get-ScheduledTask -TaskName $TaskName
        if ($task.State -eq 'Running') { Stop-ScheduledTask -TaskName $TaskName }
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    }
}
