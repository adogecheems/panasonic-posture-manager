param(
    [Parameter(Mandatory = $true)]
    [ValidateRange(0, 65535)]
    [int]$LanguageId,

    [Parameter(Mandatory = $true)]
    [string]$InstallFolder
)

$ErrorActionPreference = 'Stop'
$InstallFolder = [IO.Path]::GetFullPath(
    $InstallFolder.Trim('"').TrimEnd([char[]]@('\', '/'))
)
$displayName = if (($LanguageId -band 0x3ff) -eq 0x04) {
    '松下二合一平板电脑姿态管理工具'
} else {
    'Panasonic 2-in-1 Tablet Posture Management Tool'
}
$knownNames = @(
    'Panasonic 2-in-1 Tablet Posture Management Tool',
    '松下二合一平板电脑姿态管理工具',
    'Panasonic Posture Manager'
)
$programs = Join-Path $env:ProgramData 'Microsoft\Windows\Start Menu\Programs'
$destinationFolder = Join-Path $programs $displayName
$destinationLink = Join-Path $destinationFolder "$displayName.lnk"

if (-not (Test-Path -LiteralPath $destinationFolder -PathType Container)) {
    New-Item -ItemType Directory -Path $destinationFolder | Out-Null
}

foreach ($name in $knownNames) {
    $folder = Join-Path $programs $name
    if ($folder -eq $destinationFolder -or -not (Test-Path -LiteralPath $folder -PathType Container)) {
        continue
    }
    foreach ($candidateName in $knownNames) {
        $candidate = Join-Path $folder "$candidateName.lnk"
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            Remove-Item -LiteralPath $candidate -Force
        }
    }
    Remove-Item -LiteralPath $folder -Force -ErrorAction SilentlyContinue
}

$target = Join-Path $InstallFolder 'runtime\pythonw.exe'
$launcher = Join-Path $InstallFolder 'launch.py'
$icon = Join-Path $InstallFolder 'data\assets\work.mmoe.panasonic.PostureManager.ico'
if (Test-Path -LiteralPath $destinationLink -PathType Leaf) {
    Remove-Item -LiteralPath $destinationLink -Force
}
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($destinationLink)
$shortcut.TargetPath = $target
$shortcut.Arguments = "-I `"$launcher`""
$shortcut.WorkingDirectory = $InstallFolder
$shortcut.IconLocation = "$icon,0"
$shortcut.Description = "Open $displayName"
$shortcut.Save()
