param(
    [ValidateSet('Install', 'Remove', 'Status')]
    [string]$Action = 'Install'
)

$ErrorActionPreference = 'Stop'
$identity = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $identity.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'This command requires an elevated administrator token.'
}

$controlKey = 'SYSTEM\CurrentControlSet\Control\Windows'
$controlPath = 'HKLM:\' + $controlKey
$setupPath = 'HKLM:\SOFTWARE\PanasonicPostureManager\Setup'
$valueName = 'LaptopSlateExclusiveAccess'

function Read-RawValue([string]$Path, [string]$Name) {
    $key = [Microsoft.Win32.Registry]::LocalMachine.OpenSubKey($Path, $false)
    if ($null -eq $key) { return $null }
    try {
        if ($key.GetValueNames() -notcontains $Name) {
            return [pscustomobject]@{ Present = $false; Value = $null; Kind = $null }
        }
        return [pscustomobject]@{
            Present = $true
            Value = $key.GetValue($Name, $null, [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
            Kind = $key.GetValueKind($Name)
        }
    } finally {
        $key.Dispose()
    }
}

if ($Action -eq 'Status') {
    $current = Read-RawValue $controlKey $valueName
    $setup = Get-ItemProperty -LiteralPath $setupPath -ErrorAction SilentlyContinue
    [ordered]@{
        configured = [bool]($setup.SlateExclusiveConfigured -eq 1)
        current_present = $current.Present
        current_value = $current.Value
        original_present = [bool]($setup.SlateExclusiveOriginalPresent -eq 1)
        original_value = $setup.SlateExclusiveOriginalValue
        reboot_required = [bool]($setup.SlateExclusiveConfigured -eq 1 -and $current.Value -ne 0)
    } | ConvertTo-Json
    exit 0
}

if ($Action -eq 'Install') {
    New-Item -Path $setupPath -Force | Out-Null
    $setup = Get-ItemProperty -LiteralPath $setupPath -ErrorAction SilentlyContinue
    if ($setup.SlateExclusiveConfigured -ne 1) {
        $original = Read-RawValue $controlKey $valueName
        New-ItemProperty -LiteralPath $setupPath -Name 'SlateExclusiveOriginalPresent' `
            -PropertyType DWord -Value ([int]$original.Present) -Force | Out-Null
        if ($original.Present) {
            if ($original.Kind -ne [Microsoft.Win32.RegistryValueKind]::DWord) {
                throw 'LaptopSlateExclusiveAccess has an unsupported registry type.'
            }
            New-ItemProperty -LiteralPath $setupPath -Name 'SlateExclusiveOriginalValue' `
                -PropertyType DWord -Value ([int]$original.Value) -Force | Out-Null
        }
        New-ItemProperty -LiteralPath $setupPath -Name 'SlateExclusiveConfigured' `
            -PropertyType DWord -Value 1 -Force | Out-Null
    }
    New-ItemProperty -LiteralPath $controlPath -Name $valueName `
        -PropertyType DWord -Value 0 -Force | Out-Null
    Write-Output 'Convertible-slate non-exclusive access configured. Restart Windows before Hardware.cmd can use it.'
    exit 0
}

$setup = Get-ItemProperty -LiteralPath $setupPath -ErrorAction SilentlyContinue
if ($null -ne $setup -and $setup.SlateExclusiveConfigured -eq 1) {
    if ($setup.SlateExclusiveOriginalPresent -eq 1) {
        New-ItemProperty -LiteralPath $controlPath -Name $valueName `
            -PropertyType DWord -Value ([int]$setup.SlateExclusiveOriginalValue) -Force | Out-Null
    } else {
        Remove-ItemProperty -LiteralPath $controlPath -Name $valueName -ErrorAction SilentlyContinue
    }
    Remove-ItemProperty -LiteralPath $setupPath -Name SlateExclusiveConfigured -ErrorAction SilentlyContinue
    Remove-ItemProperty -LiteralPath $setupPath -Name SlateExclusiveOriginalPresent -ErrorAction SilentlyContinue
    Remove-ItemProperty -LiteralPath $setupPath -Name SlateExclusiveOriginalValue -ErrorAction SilentlyContinue
}
Write-Output 'Convertible-slate configuration removed. Restart Windows to restore exclusive device access.'
