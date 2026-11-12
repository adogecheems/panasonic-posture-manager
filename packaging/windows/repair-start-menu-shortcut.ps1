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
$RuntimeFolder = Join-Path (Split-Path -Parent $PSCommandPath) 'runtime'
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

$target = Join-Path $RuntimeFolder 'pythonw.exe'
$launcher = Join-Path $InstallFolder 'launch.py'
$icon = Join-Path $InstallFolder 'data\assets\work.mmoe.panasonic.PostureManager.ico'
$appUserModelId = 'work.mmoe.panasonic.PostureManager'
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

# WScript.Shell cannot persist System.AppUserModel.ID. Without this property
# Windows cannot associate the explicit process AUMID with the installed
# display name and notification cards fall back to pythonw.exe's "Python"
# identity. Keep the shortcut properties and the process identity in lockstep.
if (-not ('PanasonicPosture.Windows.ShortcutIdentity' -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;

namespace PanasonicPosture.Windows
{
    public static class ShortcutIdentity
    {
        [StructLayout(LayoutKind.Sequential, Pack = 4)]
        private struct PropertyKey
        {
            public Guid FormatId;
            public int PropertyId;

            public PropertyKey(Guid formatId, int propertyId)
            {
                FormatId = formatId;
                PropertyId = propertyId;
            }
        }

        [StructLayout(LayoutKind.Explicit, Size = 24)]
        private struct PropVariant
        {
            [FieldOffset(0)] public ushort VariantType;
            [FieldOffset(8)] public IntPtr PointerValue;
        }

        [ComImport]
        [Guid("0000010b-0000-0000-C000-000000000046")]
        [InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
        private interface IPersistFile
        {
            [PreserveSig] int GetClassID(out Guid classId);
            [PreserveSig] int IsDirty();
            [PreserveSig] int Load(
                [MarshalAs(UnmanagedType.LPWStr)] string fileName,
                int mode);
            [PreserveSig] int Save(
                [MarshalAs(UnmanagedType.LPWStr)] string fileName,
                [MarshalAs(UnmanagedType.Bool)] bool remember);
            [PreserveSig] int SaveCompleted(
                [MarshalAs(UnmanagedType.LPWStr)] string fileName);
            [PreserveSig] int GetCurFile(out IntPtr fileName);
        }

        [ComImport]
        [Guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99")]
        [InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
        private interface IPropertyStore
        {
            [PreserveSig] int GetCount(out uint propertyCount);
            [PreserveSig] int GetAt(uint propertyIndex, out PropertyKey key);
            [PreserveSig] int GetValue(
                ref PropertyKey key,
                out PropVariant value);
            [PreserveSig] int SetValue(
                ref PropertyKey key,
                ref PropVariant value);
            [PreserveSig] int Commit();
        }

        [DllImport("ole32.dll")]
        private static extern int PropVariantClear(ref PropVariant value);

        private static readonly Guid ShellLinkClassId =
            new Guid("00021401-0000-0000-C000-000000000046");
        private static readonly PropertyKey AppUserModelIdKey =
            new PropertyKey(
                new Guid("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3"),
                5);

        private static void Check(int result)
        {
            if (result < 0)
            {
                Marshal.ThrowExceptionForHR(result);
            }
        }

        public static void Set(string shortcutPath, string appUserModelId)
        {
            object link = Activator.CreateInstance(
                Type.GetTypeFromCLSID(ShellLinkClassId));
            try
            {
                var file = (IPersistFile)link;
                Check(file.Load(shortcutPath, 2));
                var store = (IPropertyStore)link;
                var value = new PropVariant();
                value.VariantType = 31; // VT_LPWSTR
                value.PointerValue =
                    Marshal.StringToCoTaskMemUni(appUserModelId);
                var key = AppUserModelIdKey;
                try
                {
                    Check(store.SetValue(ref key, ref value));
                }
                finally
                {
                    PropVariantClear(ref value);
                }
                Check(store.Commit());
                Check(file.Save(shortcutPath, true));
            }
            finally
            {
                Marshal.ReleaseComObject(link);
            }
        }

        public static string Get(string shortcutPath)
        {
            object link = Activator.CreateInstance(
                Type.GetTypeFromCLSID(ShellLinkClassId));
            try
            {
                Check(((IPersistFile)link).Load(shortcutPath, 0));
                var store = (IPropertyStore)link;
                var key = AppUserModelIdKey;
                PropVariant value;
                Check(store.GetValue(ref key, out value));
                try
                {
                    return value.VariantType == 31
                        ? Marshal.PtrToStringUni(value.PointerValue)
                        : null;
                }
                finally
                {
                    PropVariantClear(ref value);
                }
            }
            finally
            {
                Marshal.ReleaseComObject(link);
            }
        }
    }
}
'@
}

[PanasonicPosture.Windows.ShortcutIdentity]::Set(
    $destinationLink,
    $appUserModelId
)
$actualAppUserModelId = [PanasonicPosture.Windows.ShortcutIdentity]::Get(
    $destinationLink
)
if ($actualAppUserModelId -ne $appUserModelId) {
    throw "Start menu shortcut AppUserModelID verification failed"
}
